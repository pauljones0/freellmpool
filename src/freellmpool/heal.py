"""Demand-driven tool-bench heal (G31).

When the fresh tool bench drops below minimum, `verify --heal` (or an
AUTOHEAL-consented timer/`maintenance --refresh`) re-probes a bounded
set of verification targets through the exact `verify` path. `status`
only ever offers — it never probes.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .allowances import AllowanceDenied
from .config import finite_float

DEFAULT_MINIMUM = 3  # kept in sync with managed_cli.TOOLS_BENCH_MINIMUM by test
HEAL_SCHEMA = 1
HEAL_MAX_TARGETS = 4
HEAL_MAX_RUNS_PER_DAY = 3
HEAL_MAX_PROBES_PER_DAY = 36
HEAL_COOLDOWN_BASE_SECONDS = 3600.0
HEAL_COOLDOWN_MAX_SECONDS = 86400.0
HEAL_BUDGET_DEFAULT = 300.0
HEAL_BUDGET_MIN = 60.0
HEAL_BUDGET_MAX = 600.0
HEAL_FEATURES = ("chat", "tools", "streaming")
HEAL_HISTORY_KEPT = 10


class HealBusy(Exception):
    """Another heal run holds the lease."""


def default_heal_path(env: Any | None = None) -> Path:
    """State path for heal.json (env-overridable for tests)."""
    from .maintenance import state_directory

    source = os.environ if env is None else env
    override = source.get("FREELLMPOOL_HEAL_PATH", "")
    if override:
        return Path(override).expanduser()
    return state_directory(dict(source)) / "heal.json"


def autoheal_enabled(env: Any) -> bool:
    """True only for explicit truthy opt-in; "0"/unset/anything else disables."""
    return str(env.get("FREELLMPOOL_AUTOHEAL", "")).strip().lower() in {
        "1", "true", "yes", "on"}


def heal_budget_seconds(env: Any) -> float:
    """Wall-box for one heal run (typo→default, clamped 60–600)."""
    return finite_float(env.get("FREELLMPOOL_HEAL_BUDGET_SECONDS", "300"),
                        HEAL_BUDGET_DEFAULT, minimum=HEAL_BUDGET_MIN,
                        maximum=HEAL_BUDGET_MAX)


def _utc_day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class HealStore:
    """Tiny locked heal.json store (own file lock, immediate writes).

    The quota store is deliberately NOT reused: heal needs timestamps,
    and quota batching would make the daily cap soft.
    """

    def __init__(self, path: Path | None = None, *,
                 clock: Any = None, monotonic: Any = None) -> None:
        self.path = Path(path) if path is not None else default_heal_path()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._thread_lock = threading.Lock()

    def _blank(self) -> dict[str, Any]:
        return {"schema": HEAL_SCHEMA, "day": _utc_day(self._clock()),
                "probes_today": 0, "runs_today": 0, "last_heal": None,
                "cooldown_until": None, "consecutive_low_yield": 0, "history": []}

    @staticmethod
    def _num(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return number if number >= 0 else 0

    def load(self) -> dict[str, Any]:
        """Tolerant read; garbage degrades to a blank state (never raises)."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self._blank()
        if not isinstance(data, dict):
            return self._blank()
        blank = self._blank()
        history = data.get("history")
        return {
            "schema": HEAL_SCHEMA,
            "day": data.get("day") if isinstance(data.get("day"), str) else blank["day"],
            "probes_today": self._num(data.get("probes_today")),
            "runs_today": self._num(data.get("runs_today")),
            "last_heal": data.get("last_heal") if isinstance(data.get("last_heal"), dict) else None,
            "cooldown_until": (data.get("cooldown_until")
                               if _parse_ts(data.get("cooldown_until")) is not None else None),
            "consecutive_low_yield": self._num(data.get("consecutive_low_yield")),
            "history": [row for row in history if isinstance(row, dict)][:HEAL_HISTORY_KEPT]
            if isinstance(history, list) else [],
        }

    def view(self) -> dict[str, Any]:
        """Rollover-applied read for status surfaces (no writes)."""
        state = self.load()
        today = _utc_day(self._clock())
        if state["day"] != today:
            state["day"] = today
            state["probes_today"] = 0
            state["runs_today"] = 0
        return state

    @contextmanager
    def lease(self) -> Any:
        """Hold the run lease (threading outer + flock inner, non-blocking)."""
        if not self._thread_lock.acquire(blocking=False):
            raise HealBusy("heal already running")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise HealBusy("heal already running") from None
                yield self
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self._thread_lock.release()

    def save(self, state: dict[str, Any]) -> None:
        """Atomic write; callers hold the lease (may raise OSError)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, self.path)


def gate_open(view: dict[str, Any], now: datetime) -> str | None:
    """None when a heal may run, else the blocking reason (cooldown/budget)."""
    cooldown = _parse_ts(view.get("cooldown_until"))
    if cooldown is not None and cooldown > now:
        return "cooldown"
    if view.get("runs_today", 0) >= HEAL_MAX_RUNS_PER_DAY:
        return "budget"
    if view.get("probes_today", 0) >= HEAL_MAX_PROBES_PER_DAY:
        return "budget"
    return None


def run_heal(pool: Any, store: HealStore, *, trigger: str, limit: int = HEAL_MAX_TARGETS,
             features: tuple[str, ...] = HEAL_FEATURES, timeout: float = 20.0,
             call_fn: Any = None, stream_fn: Any = None, out: Any = None,
             minimum: int = DEFAULT_MINIMUM,
             budget_seconds: float | None = None) -> dict[str, Any]:
    """Run one bounded heal; see design v2 (/tmp/g31_autoheal_design_v2.md).

    Returns {"ran", "reason", "targets", "passes", "probes", "skipped"}.
    Probes count dispatched calls; skipped counts allowance-denied calls
    plus stale-target features. Never raises for heal-domain failures;
    OSError during state/evidence writes becomes reason "io-error".
    """

    emit = out or (lambda message: print(message, file=sys.stderr))
    zero = {"targets": [], "passes": 0, "probes": 0, "skipped": 0}
    ready = pool.managed_status().get("tools_ready", 0)
    if not isinstance(ready, int) or ready >= minimum:
        return {"ran": False, "reason": "healthy", **zero}
    try:
        with store.lease():
            return _run_locked(pool, store, trigger=trigger, limit=limit,
                               features=features, timeout=timeout, call_fn=call_fn,
                               stream_fn=stream_fn, emit=emit, minimum=minimum,
                               budget_seconds=budget_seconds)
    except HealBusy:
        emit("heal already running; skipping")
        return {"ran": False, "reason": "busy", **zero}
    except OSError as exc:
        emit(f"heal aborted: state unwritable ({exc.__class__.__name__})")
        return {"ran": True, "reason": "io-error", **zero}


class _WallBoxStop(BaseException):
    """Internal: the wall-box expired between probes.

    BaseException (like asyncio.CancelledError) so it propagates
    through run_target_canaries' per-feature `except Exception`:
    a wall-box stop must abort the matrix, not masquerade as a
    provider failure row in conformance evidence.
    """


def _worst_case_calls(features: tuple[str, ...]) -> int:
    # One call per feature plus the tools-followup call when present.
    return len(features) + (1 if "tools" in features else 0)


def _save_best_effort(store: HealStore, state: dict[str, Any], now: datetime,
                       trigger: str, probed: list[str], passes: int,
                       dispatched: int, skipped: int) -> None:
    """Record a partial history entry; callers already committed to io-error."""
    entry: dict[str, Any] = {"at": now.isoformat(), "trigger": trigger,
                             "targets": probed, "passes": passes,
                             "probes": dispatched, "skipped": skipped}
    state["day"] = _utc_day(now)
    state["last_heal"] = entry
    state["history"] = [entry, *state["history"]][:HEAL_HISTORY_KEPT]
    try:
        store.save(state)
    except OSError:
        pass


def _run_locked(pool: Any, store: HealStore, *, trigger: str, limit: int,
                features: tuple[str, ...], timeout: float, call_fn: Any,
                stream_fn: Any, emit: Any, minimum: int,
                budget_seconds: float | None) -> dict[str, Any]:
    from .conformance import run_target_canaries
    from .maintenance import select_verification_targets
    from .router import Target

    zero = {"targets": [], "passes": 0, "probes": 0, "skipped": 0}
    now = store._clock()
    state = store.view()
    blocked = gate_open(state, now)
    if blocked is not None:
        return {"ran": False, "reason": blocked, **zero}
    budget = budget_seconds if budget_seconds is not None else heal_budget_seconds(pool.env)
    # Review fix 1: clamp the selector input — a caller --limit must never
    # widen a heal run past the per-run target cap (floored at 0 so a
    # direct-API negative limit degrades to an empty run, not a ValueError).
    clamped = min(max(int(limit), 0), HEAL_MAX_TARGETS)
    routes = [r for r in pool.snapshot().routes
              if r.modality == "chat" and r.automatic]
    targets = [Target(r.provider, r.model, 0, r.metadata.get("context")) for r in routes]
    selected = select_verification_targets(targets, pool.conformance, clamped)
    if not selected:
        emit("heal: no verification targets; run freellmpool update")
        entry: dict[str, Any] = {"at": now.isoformat(), "trigger": trigger, "targets": [],
                                 "passes": 0, "probes": 0, "skipped": 0}
        state["day"] = _utc_day(now)
        state["last_heal"] = entry
        state["history"] = [entry, *state["history"]][:HEAL_HISTORY_KEPT]
        try:
            store.save(state)
        except OSError as exc:
            emit(f"heal aborted: state unwritable ({exc.__class__.__name__})")
            return {"ran": True, "reason": "io-error", **zero}
        return {"ran": True, "reason": "empty", **zero}
    remaining = HEAL_MAX_PROBES_PER_DAY - state["probes_today"]
    emit(f"healing bench: {len(selected)} target(s), "
         f"budget {min(HEAL_MAX_TARGETS * len(features), remaining)} probes")
    # Re-admit at probe time: snapshot generations go stale, so intersect
    # with a fresh snapshot instead of trusting selection-time admission.
    # Review fix 3: the SAME chat+automatic filter applies — a route that
    # flipped automatic→manual between selection and probe is not probed.
    fresh = {(r.provider.id, r.model) for r in pool.snapshot().routes
             if r.modality == "chat" and r.automatic}
    call_impl = call_fn or pool.probe_call
    stream_impl = stream_fn or pool.probe_stream
    dispatched = skipped = 0
    deadline = store._monotonic() + budget

    def _check_wallbox() -> None:
        # Review fix 9: the wall-box is checked between PROBES, not just
        # between targets, so a slow target cannot overrun the budget.
        if store._monotonic() >= deadline:
            raise _WallBoxStop

    def spy_call(provider: Any, model: str, messages: Any, **kwargs: Any) -> Any:
        nonlocal dispatched, skipped
        _check_wallbox()
        try:
            reply = call_impl(provider, model, messages, **kwargs)
        except AllowanceDenied:
            skipped += 1
            raise
        dispatched += 1
        return reply

    def spy_stream(provider: Any, model: str, messages: Any, **kwargs: Any) -> Any:
        nonlocal dispatched, skipped
        _check_wallbox()
        try:
            chunks = list(stream_impl(provider, model, messages, **kwargs))
        except AllowanceDenied:
            skipped += 1
            raise
        dispatched += 1
        return chunks

    run_cap = HEAL_MAX_TARGETS * len(features)
    worst_case = _worst_case_calls(features)
    probed: list[str] = []
    passes = 0
    rate_limited = 0
    recorded = 0
    wallboxed = False
    capped = False
    try:
        for target in selected:
            # Probes count dispatched CALLS (a tools feature may issue a
            # followup call); caps reserve a full worst-case target so the
            # run can never overshoot either budget mid-target (fixes 2, 4).
            if dispatched + skipped + worst_case > run_cap:
                capped = True
                break
            if dispatched + worst_case > remaining:
                capped = True
                break
            if store._monotonic() >= deadline:
                wallboxed = True
                break
            if (target.provider.id, target.model) not in fresh:
                emit(f"heal: {target.name} skipped (stale admission)")
                skipped += len(features)
                continue
            denied_before = skipped
            try:
                results = run_target_canaries(
                    target.provider, target.model, env=pool.env, features=features,
                    timeout=timeout, call_fn=spy_call, stream_fn=spy_stream)
            except _WallBoxStop:
                wallboxed = True
                break
            if skipped > denied_before:
                # Any denial skips the whole target: per-feature denial
                # attribution is unknowable from outside the matrix, and
                # under budget pressure re-probing later (cooldown-paced)
                # is correct. Spend stays counted; evidence stays clean.
                emit(f"heal: {target.name} skipped (allowance)")
                continue
            for feature, result in results.items():
                pool.conformance.record(target.provider, target.model, feature,
                                        status=result["status"],
                                        classification=result["classification"])
                recorded += 1
                if result["classification"] == "rate_limit":
                    rate_limited += 1
            probed.append(target.name)
            if all(value["status"] == "pass" for value in results.values()):
                passes += 1
    except OSError as exc:
        emit(f"heal aborted: evidence unwritable ({exc.__class__.__name__})")
        _save_best_effort(store, state, now, trigger, probed, passes, dispatched,
                           skipped)
        return {"ran": True, "reason": "io-error", "targets": probed,
                "passes": passes, "probes": dispatched, "skipped": skipped}
    restored = pool.managed_status().get("tools_ready", 0) >= minimum
    rate = (rate_limited / recorded) if recorded else 0.0
    low_yield = passes == 0 or (rate >= 0.5 and passes < minimum)
    consecutive = 0 if restored else (
        state["consecutive_low_yield"] + 1 if low_yield else state["consecutive_low_yield"])
    wait = min(HEAL_COOLDOWN_BASE_SECONDS * (2 ** consecutive), HEAL_COOLDOWN_MAX_SECONDS)
    entry = {"at": now.isoformat(), "trigger": trigger, "targets": probed,
             "passes": passes, "probes": dispatched, "skipped": skipped}
    state["day"] = _utc_day(now)
    state["runs_today"] += 1
    state["probes_today"] += dispatched
    state["consecutive_low_yield"] = consecutive
    state["cooldown_until"] = (now + timedelta(seconds=wait)).isoformat()
    state["last_heal"] = entry
    state["history"] = [entry, *state["history"]][:HEAL_HISTORY_KEPT]
    try:
        store.save(state)
    except OSError as exc:
        emit(f"heal aborted: state unwritable ({exc.__class__.__name__})")
        return {"ran": True, "reason": "io-error", "targets": probed,
                "passes": passes, "probes": dispatched, "skipped": skipped}
    try:
        pool.flush()
    except OSError as exc:
        # Evidence already wrote through per-record; the flush failure is
        # still surfaced loudly (review fix 6).
        emit(f"heal aborted: flush failed ({exc.__class__.__name__})")
        return {"ran": True, "reason": "io-error", "targets": probed,
                "passes": passes, "probes": dispatched, "skipped": skipped}
    suffix = f", {skipped} skipped" if skipped else ""
    emit(f"heal: {passes}/{len(probed)} re-verified, {dispatched} probes{suffix}")
    reason = "wall-box" if wallboxed else ("capped" if capped else "ok")
    return {"ran": True, "reason": reason,
            "targets": probed, "passes": passes, "probes": dispatched,
            "skipped": skipped}
