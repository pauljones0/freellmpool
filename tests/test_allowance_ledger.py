"""Reservations must enforce shared allowances before network dispatch."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from freellmpool.allowances import AllowanceDenied, AllowanceLedger, Limit


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def test_concurrent_instances_share_one_account_allowance(tmp_path):
    path = tmp_path / "limits.db"
    stores = [AllowanceLedger(path, clock=lambda: 1000) for _ in range(3)]
    limit = Limit("account:free", "requests", 5, seconds=60)

    def attempt(n):
        try:
            return stores[n % 3].reserve([limit], {"requests": 1})
        except AllowanceDenied:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        accepted = list(executor.map(attempt, range(30)))
    assert sum(item is not None for item in accepted) == 5
    assert stores[0].status([limit])[0]["remaining"] == 0
    assert path.stat().st_mode & 0o077 == 0


def test_multi_window_reservation_is_atomic(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    minute = Limit("account:rpm", "requests", 10, seconds=60)
    daily = Limit("account:rpd", "requests", 1, seconds=86400)
    store.reserve([daily], {"requests": 1})
    with pytest.raises(AllowanceDenied):
        store.reserve([minute, daily], {"requests": 1})
    assert store.status([minute])[0]["used"] == 0


def test_success_reconciles_all_token_windows_once(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limits = [Limit("account:tpm", "tokens", 100, seconds=60),
              Limit("account:tpd", "tokens", 200, seconds=86400)]
    reservation = store.reserve(limits, {"tokens": 80})
    store.settle(reservation, {"tokens": 30})
    store.settle(reservation, {"tokens": 0})
    assert [row["used"] for row in store.status(limits)] == [30, 30]
    store.reserve(limits, {"tokens": 70})
    with pytest.raises(AllowanceDenied):
        store.reserve(limits, {"tokens": 1})


def test_failure_and_unknown_usage_keep_reservation(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("account:tokens", "tokens", 80, seconds=60)
    reservation = store.reserve([limit], {"tokens": 80})
    store.settle(reservation)
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"tokens": 1})
    assert denied.value.retry_after == 60


def test_prior_window_uncertain_work_does_not_poison_every_future_observation(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tpm", "tokens", None, algorithm="observed")
    store.reserve([limit], {"tokens": 80}, ttl=5)
    store.observe_remaining(limit.key, 100, reset_at=200)
    assert store.status([limit])[0]["remaining"] == 20
    now[0] = 201
    store.observe_remaining(limit.key, 100, reset_at=300)
    assert store.status([limit])[0]["remaining"] == 100
    # A process restart and another header in the same new window must not
    # resurrect the already-aged-out debit from the older window.
    restarted = AllowanceLedger(store.path, clock=lambda: now[0])
    restarted.observe_remaining(limit.key, 99, reset_at=300)
    assert restarted.status([limit])[0]["remaining"] == 99


@pytest.mark.parametrize("ttl", [100, 150])
def test_pending_work_whose_lease_reaches_reset_stays_reserved(tmp_path, ttl):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tpm", "tokens", None, algorithm="observed")
    store.reserve([limit], {"tokens": 80}, ttl=ttl)
    store.observe_remaining(limit.key, 100, reset_at=200)
    now[0] = 201
    store.observe_remaining(limit.key, 100, reset_at=300)
    assert store.status([limit])[0]["remaining"] == 20


def test_observation_reset_never_clears_longer_local_window_charge(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tokens", "tokens", 100, seconds=86400)
    store.reserve([limit], {"tokens": 80}, ttl=5)
    store.observe_remaining(limit.key, 100, reset_at=200)
    now[0] = 201
    store.observe_remaining(limit.key, 100, reset_at=300)
    assert store.status([limit])[0]["used"] == 80
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 21})


@pytest.mark.parametrize("replacement", [
    Limit("org:quota", "tokens", 100, algorithm="token_bucket", refill_per_second=10),
    Limit("org:quota", "requests", 100, seconds=60),
    Limit("org:quota", "tokens", 100, seconds=5),
])
def test_changed_limit_semantics_cannot_mint_capacity_before_old_reset(tmp_path, replacement):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    original = Limit("org:quota", "tokens", 100, seconds=60)
    reservation = store.reserve([original], {"tokens": 100})
    store.settle(reservation)
    now[0] = 106
    with pytest.raises(AllowanceDenied, match="changed") as denied:
        store.reserve([replacement], {replacement.unit: 1})
    assert denied.value.retry_after == 54
    assert store.status([replacement])[0]["remaining"] == 0
    now[0] = 160
    store.reserve([replacement], {replacement.unit: 100})
    assert store.status([replacement])[0]["remaining"] == 0


def test_unknown_observed_limit_needs_reset_evidence_before_becoming_bucket(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    unknown = Limit("org:quota", "tokens", None, algorithm="observed")
    bucket = Limit("org:quota", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    reservation = store.reserve([unknown], {"tokens": 80}, ttl=5)
    store.settle(reservation)
    now[0] = 106
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([bucket], {"tokens": 100})
    assert denied.value.retry_after is None
    store.observe_remaining(unknown.key, 20, reset_at=200)
    now[0] = 200
    store.reserve([bucket], {"tokens": 100})


def test_rebaseline_preserves_history_but_does_not_reinterpret_old_units(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    tokens = Limit("org:quota", "tokens", 100, seconds=1)
    requests = Limit("org:quota", "requests", 100, seconds=86400)
    rid = store.reserve([tokens], {"tokens": 100}, ttl=.1)
    store.settle(rid)
    now[0] = 102
    store.reserve([requests], {"requests": 1})
    assert store.status([requests])[0]["remaining"] == 99
    assert store.summary()["reservations"] == 2


def test_legacy_mismatched_accounting_requires_review_without_window_definition(tmp_path):
    import sqlite3
    from contextlib import closing

    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("INSERT INTO reservations VALUES('historic',1,2,1)")
        db.execute("INSERT INTO charges VALUES('historic','org:quota','tokens',100,1,'rolling')")
        db.commit()
    replacement = Limit("org:quota", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    with pytest.raises(AllowanceDenied):
        store.reserve([replacement], {"tokens": 100})


def test_late_old_headers_and_pending_charges_do_not_pollute_rebased_unit(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    old = Limit("org:quota", "tokens", 100, seconds=1)
    new = Limit("org:quota", "requests", 100, seconds=60)
    historic = store.reserve([old], {"tokens": 80}, ttl=.1)
    now[0] = 102
    current = store.reserve([new], {"requests": 1})
    store.observe_remaining(new.key, 99, reset_at=200, reservation=current)
    assert store.status([new])[0]["remaining"] == 99
    store.observe_remaining(old.key, 0, reset_at=200, reservation=historic)
    assert store.status([new])[0]["remaining"] == 99


def test_unobserved_window_cannot_claim_retry_time_from_lease_expiry(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    unknown = Limit("org:quota", "tokens", None, algorithm="observed")
    store.reserve([unknown], {"tokens": 80}, ttl=5)
    bucket = Limit("org:quota", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([bucket], {"tokens": 100})
    assert denied.value.retry_after is None


def test_observed_rebaseline_needs_reset_after_uncertain_crossing_lease(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    unknown = Limit("org:quota", "tokens", None, algorithm="observed")
    bucket = Limit("org:quota", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    store.reserve([unknown], {"tokens": 80}, ttl=150)
    store.observe_remaining(unknown.key, 100, reset_at=200)
    now[0] = 251
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([bucket], {"tokens": 100})
    assert denied.value.retry_after is None
    store.observe_remaining(unknown.key, 100, reset_at=300)
    now[0] = 300
    store.reserve([bucket], {"tokens": 100})


def test_rolling_window_does_not_reset_at_arbitrary_midnight(tmp_path):
    now = [epoch("2026-09-05T23:59:59+00:00")]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:rpd", "requests", 1, seconds=86400)
    store.reserve([limit], {"requests": 1})
    now[0] += 2
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"requests": 1})
    now[0] += 86400
    store.reserve([limit], {"requests": 1})


@pytest.mark.parametrize("before,after", [
    ("2026-03-08T07:59:59+00:00", "2026-03-08T08:00:00+00:00"),
    ("2026-03-09T06:59:59+00:00", "2026-03-09T07:00:00+00:00"),
    ("2026-11-02T07:59:59+00:00", "2026-11-02T08:00:00+00:00"),
])
def test_pacific_calendar_day_observes_dst(tmp_path, before, after):
    now = [epoch(before)]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("project:rpd", "requests", 1, algorithm="day", timezone="America/Los_Angeles")
    store.reserve([limit], {"requests": 1})
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"requests": 1})
    assert denied.value.retry_after == 1
    now[0] = epoch(after)
    store.reserve([limit], {"requests": 1})


def test_anniversary_month_keeps_original_day_and_time(tmp_path):
    now = [epoch("2026-02-28T11:59:59+00:00")]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("signup:credits", "micro_usd", 100, algorithm="anniversary_month",
                  anchor="2026-01-31T12:00:00+00:00")
    store.reserve([limit], {"micro_usd": 100})
    now[0] += 1
    store.reserve([limit], {"micro_usd": 100})
    now[0] = epoch("2026-03-30T12:00:00+00:00")
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"micro_usd": 1})
    now[0] = epoch("2026-03-31T12:00:00+00:00")
    store.reserve([limit], {"micro_usd": 100})


def test_token_bucket_refill_and_capacity_are_enforced(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:bucket", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    store.reserve([limit], {"tokens": 100})
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"tokens": 20})
    assert denied.value.retry_after == 2
    now[0] += 2
    store.reserve([limit], {"tokens": 20})
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"tokens": 101})
    assert denied.value.retry_after is None


def test_concurrency_lease_releases_after_finish_or_crash_deadline(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("account:inflight", "requests", 1, algorithm="concurrency")
    one = store.reserve([limit], {"requests": 1}, ttl=10)
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"requests": 1})
    store.settle(one)
    store.reserve([limit], {"requests": 1}, ttl=10)
    now[0] += 11
    store.reserve([limit], {"requests": 1})


def test_observed_remaining_tightens_and_never_expands_local_limit(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tpm", "tokens", 100, seconds=60)
    store.observe_remaining(limit.key, 20, reset_at=160)
    store.observe_remaining(limit.key, 80, reset_at=160)
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 21})
    store.reserve([limit], {"tokens": 20})
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 1})
    now[0] = 161
    store.observe_remaining(limit.key, 1000, reset_at=200)
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 101})


def test_model_cooldown_does_not_block_sibling_but_account_does(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    a = Limit("account:a:rpm", "requests", 10, seconds=60)
    b = Limit("account:b:rpm", "requests", 10, seconds=60)
    store.block("model:a", 120)
    with pytest.raises(AllowanceDenied):
        store.reserve([a], {"requests": 1}, scopes=["account", "model:a"])
    store.reserve([b], {"requests": 1}, scopes=["account", "model:b"])
    store.block("account", 150)
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([b], {"requests": 1}, scopes=["account", "model:b"])
    assert denied.value.retry_after == 50


@pytest.mark.parametrize("kwargs", [
    {"capacity": -1}, {"capacity": float("nan")}, {"seconds": 0},
    {"algorithm": "unknown"}, {"algorithm": "anniversary_month"},
    {"timezone": "Mars/Test"}, {"algorithm": "token_bucket", "refill_per_second": 0},
])
def test_malformed_limit_fails_closed(kwargs):
    values = dict(key="x", unit="requests", capacity=1, seconds=60)
    values.update(kwargs)
    with pytest.raises(ValueError):
        Limit(**values)


def test_missing_cost_and_bad_usage_cannot_release_budget(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("account:neurons", "neurons", 10, seconds=60)
    with pytest.raises(ValueError):
        store.reserve([limit], {"requests": 1})
    reservation = store.reserve([limit], {"neurons": 10})
    with pytest.raises(ValueError):
        store.settle(reservation, {"neurons": -1})
    assert store.status([limit])[0]["remaining"] == 0


def test_zero_capacity_is_blocked_not_unknown_or_unlimited(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: datetime.now(UTC).timestamp())
    with pytest.raises(AllowanceDenied):
        store.reserve([Limit("blocked", "requests", 0)], {"requests": 1})


def test_late_token_bucket_refund_cannot_reissue_refilled_and_spent_capacity(tmp_path):
    now = [100.0]
    path = tmp_path / "limits.db"
    store = AllowanceLedger(path, clock=lambda: now[0])
    limit = Limit("org:bucket", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    original = store.reserve([limit], {"tokens": 100})
    now[0] = 110
    restarted = AllowanceLedger(path, clock=lambda: now[0])
    restarted.reserve([limit], {"tokens": 100})
    store.settle(original, {"tokens": 0})
    with pytest.raises(AllowanceDenied):
        restarted.reserve([limit], {"tokens": 1})


def test_clock_rollback_does_not_refill_same_elapsed_time_twice(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:bucket", "tokens", 100, algorithm="token_bucket", refill_per_second=1)
    store.reserve([limit], {"tokens": 50})
    now[0] = 90
    store.reserve([limit], {"tokens": 10})
    now[0] = 100
    assert store.status([limit])[0]["remaining"] == 40
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 41})


def test_remaining_header_deducts_other_pending_reservations(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("org:tokens", "tokens", 100)
    own = store.reserve([limit], {"tokens": 1})
    store.reserve([limit], {"tokens": 8})
    store.settle(own, {"tokens": 1})
    store.observe_remaining(limit.key, 10, reset_at=160)
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 3})
    store.reserve([limit], {"tokens": 2})


def test_stream_header_excludes_only_its_own_pending_reservation(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("org:tokens", "tokens", 100)
    own = store.reserve([limit], {"tokens": 10})
    store.reserve([limit], {"tokens": 8})
    store.observe_remaining(limit.key, 10, reset_at=160, reservation=own)
    assert store.status([limit])[0]["remaining"] == 2
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 3})


def test_crashed_unknown_usage_is_still_deducted_from_new_remaining_header(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tokens", "tokens", 100)
    store.reserve([limit], {"tokens": 8}, ttl=1)
    now[0] = 102
    store.observe_remaining(limit.key, 10, reset_at=160)
    assert store.status([limit])[0]["remaining"] == 2


def _process_allowance_attempt(path):
    """A fresh interpreter shares persisted admission, never an in-memory lock."""
    import os

    store = AllowanceLedger(path, clock=lambda: 100)
    limit = Limit("account:free", "requests", 7, seconds=60)
    accepted = 0
    for _ in range(8):
        try:
            store.reserve([limit], {"requests": 1})
        except AllowanceDenied:
            pass
        else:
            accepted += 1
    return os.getpid(), accepted


def test_separate_processes_and_restart_cannot_double_spend_or_release_unknown_usage(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    path = tmp_path / "shared.db"
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_process_allowance_attempt, [path] * 8))
    assert len({pid for pid, _ in results}) > 1
    assert sum(accepted for _, accepted in results) == 7
    restart = AllowanceLedger(path, clock=lambda: 101)
    limit = Limit("account:free", "requests", 7, seconds=60)
    assert restart.status([limit])[0]["remaining"] == 0
    with pytest.raises(AllowanceDenied):
        restart.reserve([limit], {"requests": 1})


def test_admission_timestamp_is_sampled_after_obtaining_transaction(tmp_path, monkeypatch):
    from contextlib import contextmanager

    now = [epoch("2026-09-05T23:59:59+00:00")]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("account:rpd", "requests", 1, algorithm="day")
    original_transaction = store._transaction

    @contextmanager
    def delayed_transaction():
        # Another process's writer can hold admission across the reset boundary.
        now[0] = epoch("2026-09-06T00:00:00+00:00")
        with original_transaction() as db:
            yield db

    monkeypatch.setattr(store, "_transaction", delayed_transaction)
    store.reserve([limit], {"requests": 1})
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"requests": 1})


def test_observed_limit_is_unknown_until_header_and_unknown_again_after_reset(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:tpm", "tokens", None, algorithm="observed")
    status = store.status([limit])[0]
    assert status["capacity"] is None
    assert status["used"] is None
    assert status["remaining"] is None
    own = store.reserve([limit], {"tokens": 80})
    store.settle(own, {"tokens": 20})
    store.observe_remaining(limit.key, 10, reset_at=160, reservation=own)
    assert store.status([limit])[0]["remaining"] == 10
    store.reserve([limit], {"tokens": 10})
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"tokens": 1})
    assert denied.value.retry_after == 60
    now[0] = 160
    assert store.status([limit])[0]["remaining"] is None
    store.reserve([limit], {"tokens": 1})


def test_observed_limit_accounts_for_pending_work_and_other_limits_atomically(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    unknown = Limit("org:tpm", "tokens", None, algorithm="observed")
    paced = Limit("org:pace", "requests", 1)
    store.reserve([unknown], {"tokens": 8})
    store.observe_remaining(unknown.key, 10, reset_at=160)
    with pytest.raises(AllowanceDenied):
        store.reserve([paced, unknown], {"requests": 1, "tokens": 3})
    assert store.status([paced])[0]["remaining"] == 1
    store.reserve([paced, unknown], {"requests": 1, "tokens": 2})
    assert store.status([unknown])[0]["remaining"] == 0


@pytest.mark.parametrize("algorithm", [
    "rolling", "day", "month", "anniversary_month", "token_bucket", "concurrency",
])
def test_unknown_capacity_requires_observed_algorithm(algorithm):
    with pytest.raises(ValueError):
        Limit("unknown", "tokens", None, algorithm=algorithm)


def test_bucket_final_usage_above_estimate_remains_debited_once(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("org:bucket", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    reservation = store.reserve([limit], {"tokens": 40})
    store.settle(reservation, {"tokens": 80})
    store.settle(reservation, {"tokens": 80})
    assert store.status([limit])[0]["remaining"] == 20


def test_bucket_retry_includes_clock_rollback_before_refill_can_resume(tmp_path):
    now = [100.0]
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: now[0])
    limit = Limit("org:bucket", "tokens", 100, algorithm="token_bucket", refill_per_second=10)
    store.reserve([limit], {"tokens": 100})
    now[0] = 90
    with pytest.raises(AllowanceDenied) as denied:
        store.reserve([limit], {"tokens": 10})
    assert denied.value.retry_after == 11
    now[0] += denied.value.retry_after
    store.reserve([limit], {"tokens": 10})


def test_usage_above_estimate_also_debits_observed_remaining_once(tmp_path):
    store = AllowanceLedger(tmp_path / "limits.db", clock=lambda: 100)
    limit = Limit("org:tpm", "tokens", None, algorithm="observed")
    store.observe_remaining(limit.key, 100, reset_at=160)
    reservation = store.reserve([limit], {"tokens": 80})
    store.settle(reservation, {"tokens": 90})
    store.settle(reservation, {"tokens": 90})
    assert store.status([limit])[0]["remaining"] == 10
    with pytest.raises(AllowanceDenied):
        store.reserve([limit], {"tokens": 11})
