"""Transactional, shared allowance reservations. No prompts or keys are stored.

Unlike usage statistics, admission happens before dispatch. A crash or unknown
upstream usage keeps the reservation charged. Local accounting never establishes
free billing eligibility; that is an independent policy decision.
"""

from __future__ import annotations

import calendar
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _quantity(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("allowance quantity must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("allowance quantity must be a finite nonnegative number")
    return float(value)


@dataclass(frozen=True)
class Limit:
    """One independently enforced budget; keys identify shared scope + window."""

    key: str
    unit: str
    capacity: float | None
    seconds: float = 60
    algorithm: str = "rolling"
    timezone: str = "UTC"
    anchor: str | None = None
    refill_per_second: float | None = None

    def __post_init__(self) -> None:
        if self.algorithm == "observed":
            if self.capacity is not None:
                raise ValueError("observed limits require an unknown capacity")
        else:
            _quantity(self.capacity)
        if not self.key or not self.unit:
            raise ValueError("limit scope and unit are required")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown reset timezone") from exc
        if self.algorithm not in {
            "rolling", "day", "month", "anniversary_month", "token_bucket", "concurrency", "observed"
        }:
            raise ValueError("unknown allowance reset algorithm")
        if self.algorithm == "rolling" and _quantity(self.seconds) <= 0:
            raise ValueError("rolling window must be positive")
        if self.algorithm == "token_bucket":
            if self.refill_per_second is None or _quantity(self.refill_per_second) <= 0:
                raise ValueError("token bucket requires a positive refill rate")
        if self.algorithm == "anniversary_month":
            try:
                anchor = datetime.fromisoformat(self.anchor or "")
                if anchor.tzinfo is None:
                    raise ValueError("anchor needs an explicit timezone")
            except ValueError as exc:
                raise ValueError("anniversary reset needs an evidenced timestamp") from exc

    def interval(self, now: float) -> tuple[float, float]:
        """Return the active interval; use a named timezone for DST boundaries."""
        if self.algorithm == "rolling":
            return now - self.seconds, now + self.seconds
        dt = datetime.fromtimestamp(now, ZoneInfo(self.timezone))
        if self.algorithm == "day":
            start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return start.timestamp(), (start + timedelta(days=1)).timestamp()
        if self.algorithm == "month":
            start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = _month_at(start, 1, start)
            return start.timestamp(), end.timestamp()
        if self.algorithm == "anniversary_month":
            anchor = datetime.fromisoformat(self.anchor or "").astimezone(ZoneInfo(self.timezone))
            start = _month_at(dt, 0, anchor)
            if start.timestamp() > now:
                start = _month_at(dt, -1, anchor)
            return start.timestamp(), _month_at(start, 1, anchor).timestamp()
        return 0, now


def _month_at(dt: datetime, offset: int, anchor: datetime) -> datetime:
    month_number = dt.year * 12 + dt.month - 1 + offset
    year, month = divmod(month_number, 12)
    return anchor.replace(
        year=year, month=month + 1,
        day=min(anchor.day, calendar.monthrange(year, month + 1)[1]),
    )


class AllowanceDenied(Exception):
    """No dispatch occurred. None means no truthful retry time is known."""

    def __init__(self, key: str, retry_after: float | None, reason: str = "allowance exhausted"):
        self.key = key
        self.retry_after = retry_after
        self.reason = reason
        super().__init__(f"{key}: {reason}")


def default_allowance_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("FREELLMPOOL_ALLOWANCE_FILE") or
                Path.home() / ".config/freellmpool/allowances.sqlite3").expanduser()


class AllowanceLedger:
    """Use short SQLite transactions shared by threads, processes and restarts."""

    def __init__(self, path: Path | str | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else default_allowance_path()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported allowance state version; preserve the file for migration")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
                    settled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS charges (
                    reservation TEXT NOT NULL, key TEXT NOT NULL, unit TEXT NOT NULL,
                    amount REAL NOT NULL, created REAL NOT NULL, algorithm TEXT NOT NULL,
                    PRIMARY KEY (reservation, key)
                );
                CREATE INDEX IF NOT EXISTS charges_window ON charges(key, created);
                CREATE TABLE IF NOT EXISTS buckets (
                    key TEXT PRIMARY KEY, balance REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    key TEXT PRIMARY KEY, remaining REAL NOT NULL, reset REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_watermarks (
                    key TEXT PRIMARY KEY, boundary REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS limit_definitions (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, since REAL
                );
                CREATE TABLE IF NOT EXISTS blocks (key TEXT PRIMARY KEY, until REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            yield db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _availability(
        self, db: sqlite3.Connection, limit: Limit, now: float, amount: float = 0,
    ) -> tuple[float | None, float | None]:
        if limit.algorithm == "observed":
            return None, None
        assert limit.capacity is not None
        if amount > limit.capacity:
            return float(limit.capacity), None
        if limit.algorithm == "token_bucket":
            assert limit.refill_per_second is not None
            row = db.execute("SELECT balance, updated FROM buckets WHERE key=?", (limit.key,)).fetchone()
            balance = limit.capacity if row is None else min(
                limit.capacity, row["balance"] + max(0, now - row["updated"]) * limit.refill_per_second
            )
            retry = max(0, amount - balance) / limit.refill_per_second
            if retry > 0 and row is not None:
                retry += max(0, row["updated"] - now)
            return float(balance), retry
        _, epoch = self._definition(db, limit.key)
        if limit.algorithm == "concurrency":
            rows = db.execute("""
                SELECT c.amount, r.expires AS boundary FROM charges c
                JOIN reservations r ON c.reservation=r.id
                WHERE c.key=? AND c.unit=? AND c.algorithm=?
                AND (? IS NULL OR c.created>=?) AND r.settled=0 AND r.expires>?
                ORDER BY r.expires
            """, (limit.key, limit.unit, limit.algorithm, epoch, epoch, now)).fetchall()
        else:
            start, end = limit.interval(now)
            # Calendar boundaries include their exact start; a rolling interval
            # excludes its expired boundary to admit at exactly Retry-After.
            operator = ">" if limit.algorithm == "rolling" else ">="
            rows = db.execute(
                f"SELECT amount, created AS boundary FROM charges WHERE key=? AND unit=? AND algorithm=? AND created {operator} ? AND (? IS NULL OR created>=?)",
                (limit.key, limit.unit, limit.algorithm, start, epoch, epoch),
            ).fetchall()
        used = sum(row["amount"] for row in rows)
        remaining = max(0, limit.capacity - used)
        if remaining >= amount:
            return remaining, 0
        if limit.algorithm in {"day", "month", "anniversary_month"}:
            return remaining, max(0, end - now)
        for row in sorted(rows, key=lambda value: value["boundary"]):
            used -= row["amount"]
            if used + amount <= limit.capacity:
                boundary = row["boundary"]
                if limit.algorithm == "rolling":
                    boundary += limit.seconds
                return remaining, max(0, boundary - now)
        return remaining, None

    @staticmethod
    def _definition(db: sqlite3.Connection, key: str) -> tuple[Limit | None, float | None]:
        row = db.execute("SELECT payload,since FROM limit_definitions WHERE key=?", (key,)).fetchone()
        if row is None:
            return None, None
        try:
            return Limit(**json.loads(row["payload"])), row["since"]
        except (ValueError, TypeError):
            raise ValueError("invalid stored allowance definition; preserve the state for review") from None

    @staticmethod
    def _identity(limit: Limit) -> tuple[object, ...]:
        # Capacity may legitimately tighten or expand without reinterpreting
        # usage. Units, reset boundaries, and refill rules cannot do so.
        return (limit.unit, limit.algorithm,
                limit.seconds if limit.algorithm == "rolling" else None,
                limit.timezone if limit.algorithm in {"day", "month", "anniversary_month"} else None,
                limit.anchor if limit.algorithm == "anniversary_month" else None,
                limit.refill_per_second if limit.algorithm == "token_bucket" else None)

    def _definition_conflict(
        self, db: sqlite3.Connection, limit: Limit, now: float,
    ) -> AllowanceDenied | None:
        previous, epoch = self._definition(db, limit.key)
        if previous is None:
            historical = db.execute("SELECT DISTINCT unit,algorithm FROM charges WHERE key=?", (limit.key,)).fetchall()
            if any(row["unit"] != limit.unit or row["algorithm"] != limit.algorithm for row in historical):
                return AllowanceDenied(limit.key, None, "allowance definition changed; legacy state needs a reset review")
            return None
        if self._identity(previous) == self._identity(limit):
            return None
        waits = []
        unknown = False
        active = db.execute("""
            SELECT MAX(r.expires) FROM charges c JOIN reservations r ON c.reservation=r.id
            WHERE c.key=? AND (? IS NULL OR c.created>=?) AND r.settled=0 AND r.expires>?
        """, (limit.key, epoch, epoch, now)).fetchone()[0]
        if active is not None:
            waits.append(active - now)
        observation = db.execute("SELECT reset FROM observations WHERE key=?", (limit.key,)).fetchone()
        if observation is not None and observation["reset"] > now:
            waits.append(observation["reset"] - now)
        if previous.algorithm == "observed":
            boundary = observation["reset"] if observation is not None else None
            unbounded = db.execute("""
                SELECT 1 FROM charges c JOIN reservations r ON c.reservation=r.id
                WHERE c.key=? AND (? IS NULL OR c.created>=?)
                AND (? IS NULL OR c.created>=? OR (r.settled=0 AND r.expires>=?)) LIMIT 1
            """, (limit.key, epoch, epoch, boundary, boundary, boundary)).fetchone()
            unknown = unbounded is not None
        else:
            assert previous.capacity is not None
            available, retry = self._availability(db, previous, now, previous.capacity)
            if available is not None and available < previous.capacity:
                if retry is None:
                    unknown = True
                else:
                    waits.append(retry)
        if unknown or waits:
            return AllowanceDenied(limit.key, None if unknown else max(waits),
                                   "allowance definition changed; previous accounting must reset before rebaseline")
        return None

    def _prepare_definitions(self, db: sqlite3.Connection, limits: Iterable[Limit], now: float) -> None:
        for limit in limits:
            conflict = self._definition_conflict(db, limit, now)
            if conflict is not None:
                raise conflict
            previous, epoch = self._definition(db, limit.key)
            if previous is not None and self._identity(previous) != self._identity(limit):
                # The old bound has expired. Keep historical charges, but give
                # the reviewed new unit/window its own accounting epoch.
                epoch = now
                for table in ("buckets", "observations", "observation_watermarks"):
                    db.execute(f"DELETE FROM {table} WHERE key=?", (limit.key,))
            db.execute("INSERT OR REPLACE INTO limit_definitions VALUES(?,?,?)",
                       (limit.key, json.dumps(asdict(limit), sort_keys=True), epoch))

    def reserve(
        self, limits: Iterable[Limit], amounts: Mapping[str, float], *,
        scopes: Iterable[str] = (), ttl: float = 90,
    ) -> str:
        """All limits pass in one transaction or no counter is changed."""
        limits = tuple(limits)
        if len({limit.key for limit in limits}) != len(limits):
            raise ValueError("duplicate allowance identities must be reconciled before reservation")
        costs = {}
        for limit in limits:
            if limit.unit not in amounts:
                raise ValueError(f"missing cost for {limit.unit}")
            costs[limit.key] = _quantity(amounts[limit.unit])
        ttl = _quantity(ttl)
        if ttl <= 0:
            raise ValueError("reservation lifetime must be positive")
        rid = uuid.uuid4().hex
        with self._transaction() as db:
            # A competing writer may hold the lock across a reset boundary.
            now = self.clock()
            for key in set(scopes) | {limit.key for limit in limits}:
                block = db.execute("SELECT until FROM blocks WHERE key=?", (key,)).fetchone()
                if block is not None and block[0] > now:
                    raise AllowanceDenied(key, block[0] - now, "upstream cooldown")
            self._prepare_definitions(db, limits, now)
            balances = {}
            for limit in limits:
                amount = costs[limit.key]
                remaining, retry = self._availability(db, limit, now, amount)
                if ((limit.capacity is not None and amount > limit.capacity)
                        or (remaining is not None and remaining < amount)):
                    raise AllowanceDenied(limit.key, retry)
                observation = db.execute("SELECT remaining,reset FROM observations WHERE key=?", (limit.key,)).fetchone()
                if observation is not None and observation["reset"] > now and observation["remaining"] < amount:
                    raise AllowanceDenied(limit.key, observation["reset"] - now, "upstream remaining allowance")
                balances[limit.key] = remaining
            db.execute("INSERT INTO reservations(id,created,expires) VALUES(?,?,?)", (rid, now, now + ttl))
            for limit in limits:
                amount = costs[limit.key]
                db.execute("INSERT INTO charges VALUES(?,?,?,?,?,?)",
                           (rid, limit.key, limit.unit, amount, now, limit.algorithm))
                if limit.algorithm == "token_bucket":
                    balance = balances[limit.key]
                    assert balance is not None
                    # Wall-clock rollback cannot move the refill baseline back.
                    db.execute("""
                        INSERT INTO buckets VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                        balance=excluded.balance, updated=MAX(updated,excluded.updated)
                    """, (limit.key, balance - amount, now))
                db.execute("UPDATE observations SET remaining=MAX(0,remaining-?) WHERE key=? AND reset>?",
                           (amount, limit.key, now))
        return rid

    def settle(self, reservation: str, actual: Mapping[str, float] | None = None) -> None:
        """Settle once; unknown usage and token-bucket estimates stay charged.

        Bucket debits can already have been replaced by refill and spent again,
        so refunding a lower final amount could mint capacity. Higher usage is
        still charged. Fixed-window estimates can safely be reconciled.
        """
        actual = {} if actual is None else {key: _quantity(value) for key, value in actual.items()}
        with self._transaction() as db:
            now = self.clock()
            row = db.execute("SELECT settled FROM reservations WHERE id=?", (reservation,)).fetchone()
            if row is None or row[0]:
                return
            for charge in db.execute("SELECT * FROM charges WHERE reservation=?", (reservation,)).fetchall():
                amount = actual.get(charge["unit"], charge["amount"])
                if charge["algorithm"] == "concurrency":
                    continue
                if charge["algorithm"] == "token_bucket":
                    amount = max(amount, charge["amount"])
                db.execute("UPDATE charges SET amount=? WHERE reservation=? AND key=?",
                           (amount, reservation, charge["key"]))
                current, epoch = self._definition(db, charge["key"])
                current_charge = ((epoch is None or charge["created"] >= epoch)
                                  and (current is None or (current.unit == charge["unit"] and current.algorithm == charge["algorithm"])))
                if charge["algorithm"] == "token_bucket" and current_charge:
                    db.execute("UPDATE buckets SET balance=balance+? WHERE key=?",
                               (charge["amount"] - amount, charge["key"]))
                # Observations are never refunded, but an underestimated cost
                # must still consume their remaining allowance exactly once.
                extra = max(0, amount - charge["amount"])
                if extra and current_charge:
                    db.execute("UPDATE observations SET remaining=MAX(0,remaining-?) WHERE key=? AND reset>?",
                               (extra, charge["key"], now))
            db.execute("UPDATE reservations SET settled=1 WHERE id=?", (reservation,))

    def observe_remaining(
        self, key: str, remaining: float, *, reset_at: float, reservation: str | None = None,
    ) -> None:
        """Tighten a window after deducting other pending work.

        A streaming response can identify its own still-pending reservation,
        which its header already includes. Other pending estimates stay charged
        even after their concurrency lease expires. After an observed reset,
        only leases that ended before that boundary age out of observations;
        their ordinary fixed/rolling charges remain intact. Crossing leases
        stay reserved. The boundary persists across restarts and later headers.
        Late/out-of-order responses cannot replenish an active observation.
        """
        remaining = _quantity(remaining)
        reset_at = _quantity(reset_at)
        with self._transaction() as db:
            now = self.clock()
            if reset_at <= now:
                return
            current, epoch = self._definition(db, key)
            if reservation is not None:
                observed_charge = db.execute("SELECT unit,algorithm,created FROM charges WHERE key=? AND reservation=?", (key, reservation)).fetchone()
                if observed_charge is None or (epoch is not None and observed_charge["created"] < epoch):
                    return
                if current is not None and (observed_charge["unit"] != current.unit or observed_charge["algorithm"] != current.algorithm):
                    return
            old = db.execute("SELECT remaining,reset FROM observations WHERE key=?", (key,)).fetchone()
            if old is not None and old["reset"] <= now:
                db.execute("""
                    INSERT INTO observation_watermarks VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET boundary=MAX(boundary,excluded.boundary)
                """, (key, old["reset"]))
            cutoff_row = db.execute("SELECT boundary FROM observation_watermarks WHERE key=?", (key,)).fetchone()
            cutoff = cutoff_row["boundary"] if cutoff_row is not None else None
            pending = db.execute("""
                SELECT COALESCE(SUM(c.amount),0) FROM charges c
                JOIN reservations r ON c.reservation=r.id
                WHERE c.key=? AND r.settled=0 AND (? IS NULL OR r.id!=?)
                AND (? IS NULL OR r.expires>=?)
                AND (? IS NULL OR c.created>=?)
            """, (key, reservation, reservation, cutoff, cutoff, epoch, epoch)).fetchone()[0]
            remaining = max(0, remaining - pending)
            if old is not None and old["reset"] > now:
                remaining = min(remaining, old["remaining"])
                reset_at = max(reset_at, old["reset"])
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?)", (key, remaining, reset_at))

    def block(self, key: str, until: float) -> None:
        until = _quantity(until)
        with self._transaction() as db:
            db.execute("INSERT INTO blocks VALUES(?,?) ON CONFLICT(key) DO UPDATE SET until=MAX(until,excluded.until)",
                       (key, until))

    def cooldown(self, scopes: Iterable[str]) -> float:
        now = self.clock()
        with self._connection() as db:
            ends = [db.execute("SELECT until FROM blocks WHERE key=?", (key,)).fetchone() for key in scopes]
        return max([0.0] + [float(row[0]) - now for row in ends if row is not None])

    def status(self, limits: Iterable[Limit]) -> list[dict[str, Any]]:
        now = self.clock()
        rows = []
        with self._connection() as db:
            for limit in limits:
                conflict = self._definition_conflict(db, limit, now)
                previous, _ = self._definition(db, limit.key)
                rebaseline = previous is not None and self._identity(previous) != self._identity(limit)
                remaining = (0.0 if conflict is not None else limit.capacity if rebaseline
                             else self._availability(db, limit, now)[0])
                observation = db.execute("SELECT remaining,reset FROM observations WHERE key=?", (limit.key,)).fetchone()
                upstream = observation["remaining"] if not rebaseline and observation is not None and observation["reset"] > now else None
                rows.append({
                    "key": limit.key, "unit": limit.unit, "capacity": limit.capacity,
                    "used": (max(0, limit.capacity - remaining)
                             if limit.capacity is not None and remaining is not None else None),
                    "remaining": (upstream if remaining is None else remaining
                                  if upstream is None else min(remaining, upstream)),
                    "upstream_remaining": upstream, "algorithm": limit.algorithm,
                    "scope": "local reservations; external usage may be unknown",
                    **({"definition_status": "changed", "reason": conflict.reason,
                        "retry_after": conflict.retry_after, "used": None} if conflict is not None else {}),
                })
        return rows

    def summary(self) -> dict[str, Any]:
        with self._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM reservations WHERE settled=0 AND expires>?", (self.clock(),)).fetchone()[0]
        return {"reservations": count, "in_flight": pending, "path": str(self.path)}

    def export(self) -> str:
        """Sanitized diagnostics contain counters only, never request data."""
        return json.dumps(self.summary(), sort_keys=True)
