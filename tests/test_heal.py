"""G31 demand-driven tool-bench heal: explicit verify --heal + status offer.

Covers the v2.1 test plan: triggers/consent, lease, budget, cooldown,
empty selector, OSError, ledger-denied, re-admit, status surface,
free-only, timer interplay. Deterministic and offline (make_pool +
fake probe fns + tmp stores + injected clocks).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_managed_runtime import make_pool

from freellmpool import heal as h
from freellmpool.allowances import AllowanceDenied
from freellmpool.conformance import ConformanceStore
from freellmpool.models import Reply


def _reply(text="OK", provider="a", model="free", message=None):
    return Reply(text=text, provider_id=provider, model=model, raw={},
                 message=message)


def _tool_call_reply(provider, model):
    return _reply("", provider.id, model, message={
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "record_number",
                                    "arguments": '{"number": 7}'}}]})


def passing_call(provider, model, messages, **kwargs):
    if kwargs.get("tools"):
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            return _reply("OK", provider.id, model)
        return _tool_call_reply(provider, model)
    return _reply("OK", provider.id, model)


def passing_stream(provider, model, messages, **kwargs):
    yield "OK"


def failing_call(provider, model, messages, **kwargs):
    from freellmpool.errors import ProviderHTTPError

    raise ProviderHTTPError(429, "slow down", retryable=True)


def failing_stream(provider, model, messages, **kwargs):
    from freellmpool.errors import ProviderHTTPError

    raise ProviderHTTPError(429, "slow down", retryable=True)
    yield "unreachable"


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def mono(self) -> float:
        return self.at.timestamp()

    def advance(self, **kwargs) -> None:
        self.at += timedelta(**kwargs)


def _pool(tmp_path, ids=("a", "b", "c", "d"), **kwargs):
    kwargs.setdefault("conformance", ConformanceStore(tmp_path / "c.json"))
    return make_pool(tmp_path, ids=ids, **kwargs)


def _store(tmp_path, clock, name="heal.json"):
    return h.HealStore(tmp_path / name, clock=clock, monotonic=clock.mono)


# --- store: defaults, rollover, lease ------------------------------------


def test_store_defaults_and_tolerant_parse(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    store = _store(tmp_path, clock)
    assert store.view() == {"schema": 1, "day": "2026-09-20", "probes_today": 0,
                            "runs_today": 0, "last_heal": None,
                            "cooldown_until": None, "consecutive_low_yield": 0,
                            "history": []}
    (tmp_path / "heal.json").write_text("{not json")
    assert _store(tmp_path, clock).view()["runs_today"] == 0
    (tmp_path / "heal.json").write_text(json.dumps({"schema": 999, "runs_today": "x"}))
    assert _store(tmp_path, clock).view()["runs_today"] == 0


def test_store_rollover_on_read_and_persist_on_run(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    store = _store(tmp_path, clock)
    (tmp_path / "heal.json").write_text(json.dumps(
        {"schema": 1, "day": "2026-09-19", "probes_today": 9, "runs_today": 2,
         "last_heal": None, "cooldown_until": None, "consecutive_low_yield": 0,
         "history": []}))
    assert store.view()["probes_today"] == 0  # rollover on read
    pool = _pool(tmp_path)
    out = run_heal(pool, store, clock, trigger="verify")
    assert out["ran"] is True
    assert json.loads((tmp_path / "heal.json").read_text())["day"] == "2026-09-20"


def test_lease_second_runner_no_ops(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    probed: list = []

    def counting(*args, **kwargs):
        probed.append(1)
        return passing_call(*args, **kwargs)

    with store.lease():
        out = h.run_heal(pool, store, trigger="verify", call_fn=counting,
                         stream_fn=passing_stream)
    assert out == {"ran": False, "reason": "busy", "targets": [], "passes": 0,
                   "probes": 0, "skipped": 0, "attempted": 0}
    assert probed == []


def test_lease_threads_contend(tmp_path):
    """Review fix 12: the holder and the contender overlap in time, so the
    test fails with no locking at all."""
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    store = _store(tmp_path, clock)
    entered = threading.Event()
    release = threading.Event()
    results: list = []

    def holder():
        with store.lease():
            entered.set()
            assert release.wait(timeout=10)

    def contender():
        assert entered.wait(timeout=10)
        try:
            with store.lease():
                results.append("entered")
        except h.HealBusy:
            results.append("busy")

    first = threading.Thread(target=holder)
    second = threading.Thread(target=contender)
    first.start()
    second.start()
    second.join(timeout=10)
    release.set()
    first.join(timeout=10)
    assert results == ["busy"]
    with store.lease():
        pass  # released after the holder exits


def test_threading_lock_alone_serializes(monkeypatch, tmp_path):
    """Review fix 13: with flock disabled, the threading lock still
    serializes same-process racers (pins v2.1/F1 specifically)."""
    import fcntl

    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    store = _store(tmp_path, clock)
    monkeypatch.setattr(fcntl, "flock", lambda *args, **kwargs: None)
    entered = threading.Event()
    release = threading.Event()
    results: list = []

    def holder():
        with store.lease():
            entered.set()
            assert release.wait(timeout=10)

    def contender():
        assert entered.wait(timeout=10)
        try:
            with store.lease():
                results.append("entered")
        except h.HealBusy:
            results.append("busy")

    first = threading.Thread(target=holder)
    second = threading.Thread(target=contender)
    first.start()
    second.start()
    second.join(timeout=10)
    release.set()
    first.join(timeout=10)
    assert results == ["busy"]


def _flock_holder(path: str, entered, release):
    import fcntl as _fcntl
    import os as _os

    fd = _os.open(path + ".lock", _os.O_CREAT | _os.O_RDWR, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        entered.set()
        release.wait(timeout=15)
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        _os.close(fd)


def test_flock_excludes_separate_process(tmp_path):
    """Review fix 14: a second PROCESS holding the lock file blocks the run."""
    import multiprocessing as mp

    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    entered = mp.Event()
    release = mp.Event()
    proc = mp.Process(target=_flock_holder,
                      args=(str(tmp_path / "heal.json"), entered, release))
    proc.start()
    try:
        assert entered.wait(timeout=10)
        out = run_heal(pool, store, clock, trigger="verify")
        assert (out["ran"], out["reason"]) == (False, "busy")
    finally:
        release.set()
        proc.join(timeout=10)
        assert proc.exitcode == 0


# --- run_heal: triggers, cooldown, budget ---------------------------------


def run_heal(pool, store, clock, **kwargs):
    kwargs.setdefault("call_fn", passing_call)
    kwargs.setdefault("stream_fn", passing_stream)
    return h.run_heal(pool, store, **kwargs)


def test_healthy_noop_writes_nothing(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    for route in pool.snapshot().routes:
        for feature in ("chat", "tools", "streaming"):
            pool.conformance.record(route.provider, route.model, feature,
                                    status="pass", classification="verified")
    assert pool.managed_status()["tools_ready"] >= 3
    out = run_heal(pool, store, clock, trigger="verify")
    assert out["ran"] is False and out["reason"] == "healthy"
    assert not (tmp_path / "heal.json").exists()


def test_heal_restores_bench_and_records(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    assert pool.managed_status()["tools_ready"] == 0
    out = run_heal(pool, store, clock, trigger="verify")
    # 4 targets selected; per-run cap (12 calls incl. tools followups)
    # stops after 3 — still restored (minimum 3).
    assert out["ran"] is True and out["reason"] == "capped"
    assert out["passes"] == 3 and out["probes"] == 12 and out["skipped"] == 0
    assert pool.managed_status()["tools_ready"] == 3
    saved = json.loads((tmp_path / "heal.json").read_text())
    assert saved["runs_today"] == 1 and len(saved["history"]) == 1
    assert saved["history"][0]["trigger"] == "verify"
    assert set(saved["history"][0]) == {"at", "trigger", "targets", "passes",
                                        "probes", "skipped", "attempted"}


def test_cooldown_blocks_then_expires(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    assert run_heal(pool, store, clock, trigger="verify")["ran"] is True
    # Bench is restored, so force thin again with a fresh pool sharing the store.
    pool2 = _pool(tmp_path, ids=("e", "f", "g", "i"),
                  conformance=ConformanceStore(tmp_path / "c2.json"))
    out = run_heal(pool2, store, clock, trigger="verify")
    assert (out["ran"], out["reason"]) == (False, "cooldown")
    clock.advance(hours=2)
    assert run_heal(pool2, store, clock, trigger="verify")["ran"] is True


def test_backoff_doubles_on_zero_pass_and_resets_on_restore(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    kwargs = dict(call_fn=failing_call, stream_fn=failing_stream)
    assert run_heal(pool, store, clock, trigger="verify", **kwargs)["ran"] is True
    first = store.view()["cooldown_until"]
    assert first is not None
    clock.advance(hours=3)
    pool2 = _pool(tmp_path, ids=("e", "f", "g", "i"),
                  conformance=ConformanceStore(tmp_path / "c2.json"))
    assert run_heal(pool2, store, clock, trigger="verify", **kwargs)["ran"] is True
    second = store.view()["cooldown_until"]
    assert second > first  # doubled
    assert store.view()["consecutive_low_yield"] == 2
    clock.advance(hours=5)
    pool3 = _pool(tmp_path, ids=("j", "k", "l", "m"),
                  conformance=ConformanceStore(tmp_path / "c3.json"))
    assert run_heal(pool3, store, clock, trigger="verify")["ran"] is True
    assert store.view()["consecutive_low_yield"] == 0  # restore resets


def test_daily_caps(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    store = _store(tmp_path, clock)
    pools = [_pool(tmp_path, ids=(f"p{i}a", f"p{i}b", f"p{i}c", f"p{i}d"),
                   conformance=ConformanceStore(tmp_path / f"c{i}.json"))
             for i in range(4)]
    for index in range(3):
        assert run_heal(pools[index], store, clock, trigger="verify")["ran"] is True
        clock.advance(hours=2)
    out = run_heal(pools[3], store, clock, trigger="verify")
    assert (out["ran"], out["reason"]) == (False, "budget")


def test_empty_selector_accounted_without_cooldown(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path, ids=()), _store(tmp_path, clock)
    assert pool.managed_status()["tools_ready"] == 0
    out = run_heal(pool, store, clock, trigger="verify")
    assert (out["ran"], out["reason"]) == (True, "empty")
    assert store.view()["cooldown_until"] is None
    assert store.view()["runs_today"] == 0  # nothing spent
    assert len(json.loads((tmp_path / "heal.json").read_text())["history"]) == 1


def test_wall_box_stops_between_probes(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    calls: list = []

    def slow(*args, **kwargs):
        calls.append(1)
        if len(calls) > 4:
            clock.advance(seconds=400)  # exceed the wall-box mid-run
        return passing_call(*args, **kwargs)

    out = h.run_heal(pool, store, trigger="verify", call_fn=slow,
                     stream_fn=passing_stream, budget_seconds=300.0)
    assert out["ran"] is True and out["reason"] == "wall-box"
    assert out["targets"] != [] and len(out["targets"]) < 4  # partial kept
    assert out["passes"] == len(out["targets"])


def test_ledger_denied_skipped_without_evidence(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)

    def denied(*args, **kwargs):
        raise AllowanceDenied("synthetic", None)

    out = h.run_heal(pool, store, trigger="verify", call_fn=denied,
                     stream_fn=passing_stream)
    assert out["ran"] is True
    assert out["skipped"] > 0 and out["passes"] == 0
    assert pool.managed_status()["tools_ready"] == 0  # no evidence written
    saved = json.loads((tmp_path / "heal.json").read_text())
    assert saved["history"][0]["skipped"] == out["skipped"]
    assert saved["probes_today"] == out["probes"]  # spend counts dispatched only


def test_readmit_skips_stale_targets(tmp_path, monkeypatch):
    """Review fix 15: flap on selection completion, not on an exact
    snapshot() call count (robust to managed_status internals)."""
    import freellmpool.maintenance as maint

    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    thin = _pool(tmp_path, ids=("a",),
                 conformance=ConformanceStore(tmp_path / "thin.json"))
    real_select = maint.select_verification_targets
    state = {"selected": False}

    def select_once(targets, conformance, limit):
        out = real_select(targets, conformance, limit)
        state["selected"] = True
        return out

    real_snapshot = pool.snapshot

    def flapping():
        return thin.snapshot() if state["selected"] else real_snapshot()

    monkeypatch.setattr(maint, "select_verification_targets", select_once)
    monkeypatch.setattr(pool, "snapshot", flapping)
    out = run_heal(pool, store, clock, trigger="verify")
    assert out["ran"] is True
    assert out["skipped"] > 0  # stale targets skipped, not probed


def test_limit_clamped_to_per_run_cap(tmp_path):
    """Review fix 1: a caller limit=32 never widens a heal past 4 targets."""
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool = _pool(tmp_path, ids=tuple(f"p{i}" for i in range(8)),
                 conformance=ConformanceStore(tmp_path / "wide.json"))
    store = _store(tmp_path, clock)
    out = h.run_heal(pool, store, trigger="verify", limit=32,
                     call_fn=passing_call, stream_fn=passing_stream)
    assert out["ran"] is True
    assert len(out["targets"]) <= 4
    # Direct-API negative limit degrades to an empty run, not ValueError.
    pool2 = _pool(tmp_path, ids=tuple(f"q{i}" for i in range(4)),
                 conformance=ConformanceStore(tmp_path / "wide2.json"))
    store2 = _store(tmp_path, clock, name="heal2.json")
    out = h.run_heal(pool2, store2, trigger="verify", limit=-1,
                     call_fn=passing_call, stream_fn=passing_stream)
    assert (out["ran"], out["reason"]) == (True, "empty")


def test_daily_probe_cap_stops_mid_run(tmp_path):
    """Review fix 2: a run starting at 30/36 probes dispatches at most 6."""
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool, store = _pool(tmp_path), _store(tmp_path, clock)
    (tmp_path / "heal.json").write_text(json.dumps(
        {"schema": 1, "day": "2026-09-20", "probes_today": 30, "runs_today": 1,
         "last_heal": None, "cooldown_until": None, "consecutive_low_yield": 0,
         "history": []}))
    out = run_heal(pool, store, clock, trigger="verify")
    assert out["ran"] is True and out["reason"] == "capped"
    assert out["probes"] <= 6
    assert store.view()["probes_today"] <= 36


def test_oserror_aborts_cleanly(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))
    pool = _pool(tmp_path)
    (tmp_path / "block").write_text("not a directory")
    store = h.HealStore(tmp_path / "block" / "heal.json", clock=clock,
                        monotonic=clock.mono)
    out = run_heal(pool, store, clock, trigger="verify")
    assert (out["ran"], out["reason"]) == (True, "io-error")


# --- env parsing ----------------------------------------------------------


def test_autoheal_parsing():
    assert h.autoheal_enabled({}) is False
    assert h.autoheal_enabled({"FREELLMPOOL_AUTOHEAL": "0"}) is False
    assert h.autoheal_enabled({"FREELLMPOOL_AUTOHEAL": "nope"}) is False
    for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
        assert h.autoheal_enabled({"FREELLMPOOL_AUTOHEAL": value}) is True


def test_budget_clamp():
    assert h.heal_budget_seconds({}) == 300.0
    assert h.heal_budget_seconds({"FREELLMPOOL_HEAL_BUDGET_SECONDS": "typo"}) == 300.0
    assert h.heal_budget_seconds({"FREELLMPOOL_HEAL_BUDGET_SECONDS": "5"}) == 60.0
    assert h.heal_budget_seconds({"FREELLMPOOL_HEAL_BUDGET_SECONDS": "9999"}) == 600.0
    assert h.heal_budget_seconds({"FREELLMPOOL_HEAL_BUDGET_SECONDS": "120"}) == 120.0


def test_minimum_matches_status_warning():
    from freellmpool.managed_cli import TOOLS_BENCH_MINIMUM

    assert h.DEFAULT_MINIMUM == TOOLS_BENCH_MINIMUM


# --- CLI: verify --heal, status offer, refresh -----------------------------


def _heal_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FREELLMPOOL_HEAL_PATH", str(tmp_path / "heal.json"))


def test_verify_heal_runs_and_bare_verify_offers(monkeypatch, tmp_path, capsys):
    import argparse

    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    _heal_env(monkeypatch, tmp_path)
    pool = _pool(tmp_path)
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass", "classification": "verified"}})
    args = argparse.Namespace(provider=None, limit=4, features="chat", timeout=30,
                              json=False, heal=True)
    assert managed_cli.cmd_verify(args) == 0
    assert "healing bench" in capsys.readouterr().err

    args = argparse.Namespace(provider=None, limit=4, features="chat", timeout=30,
                              json=False, heal=False)
    assert managed_cli.cmd_verify(args) == 0
    # Thin bench without --heal offers; the offer names the flag.
    assert "--heal" in capsys.readouterr().err


def test_status_offer_never_probes_and_json_keys(monkeypatch, tmp_path, capsys):
    import argparse
    import json as json_mod

    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    _heal_env(monkeypatch, tmp_path)
    pool = _pool(tmp_path)
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr(pool, "probe_call", lambda *a, **k: pytest.fail("status must not probe"))
    assert managed_cli.cmd_status(argparse.Namespace(json=False)) == 0
    assert "verify --heal" in capsys.readouterr().out
    assert managed_cli.cmd_status(argparse.Namespace(json=True)) == 0
    status = json_mod.loads(capsys.readouterr().out)
    assert status["heal_available"] is True
    assert status["heal_cooldown_until"] is None
    assert status["last_heal"] is None
    assert status["heal_probes_today"] == 0


def test_maintenance_refresh_heals_only_with_opt_in(monkeypatch, tmp_path, capsys):
    import argparse

    from freellmpool import maintenance_cli
    from freellmpool.managed import ManagedPool

    _heal_env(monkeypatch, tmp_path)
    report = {"findings": [{"provider": "a", "code": "conformance_expired",
                             "summary": "stale"}]}
    monkeypatch.setattr(maintenance_cli, "run_maintenance", lambda *a, **k: dict(report))
    pool = _pool(tmp_path)
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass", "classification": "verified"}})
    args = argparse.Namespace(refresh=True, public_only=False, output=None,
                              baseline=None, source_revision=None, json=True)
    monkeypatch.delenv("FREELLMPOOL_AUTOHEAL", raising=False)
    assert maintenance_cli.cmd_maintenance(args) == 0
    assert not (tmp_path / "heal.json").exists()
    monkeypatch.setenv("FREELLMPOOL_AUTOHEAL", "1")
    assert maintenance_cli.cmd_maintenance(args) == 0
    assert (tmp_path / "heal.json").exists()


def test_verify_autoheal_env_heals_without_flag(monkeypatch, tmp_path, capsys):
    """Review fix 14: AUTOHEAL=1 lets bare verify heal (timer consent)."""
    import argparse

    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    _heal_env(monkeypatch, tmp_path)
    pool = _pool(tmp_path)
    # pool.env rebuilds from construction env on snapshot(); seed it like
    # from_default_config(env=...) would in production.
    pool._base_env = dict(pool._base_env, FREELLMPOOL_AUTOHEAL="1",
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass", "classification": "verified"}})
    args = argparse.Namespace(provider=None, limit=4, features="chat", timeout=30,
                              json=False, heal=False)
    assert managed_cli.cmd_verify(args) == 0
    assert "healing bench" in capsys.readouterr().err


def test_public_only_refresh_never_heals(monkeypatch, tmp_path, capsys):
    """Review fix 14: credentialless public refresh heals nothing, even
    with AUTOHEAL=1 in the ambient environment."""
    import argparse

    from freellmpool import maintenance_cli

    _heal_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FREELLMPOOL_AUTOHEAL", "1")
    seen: list = []
    monkeypatch.setattr(maintenance_cli, "run_maintenance",
                        lambda *a, **k: seen.append(k) or {"findings": []})
    monkeypatch.setattr(maintenance_cli, "validate_public_report", lambda report: report)
    args = argparse.Namespace(refresh=True, public_only=True, output=None,
                              baseline=None, source_revision=None, json=True)
    assert maintenance_cli.cmd_maintenance(args) == 0
    assert seen and seen[0].get("public_only") is True
    assert not (tmp_path / "heal.json").exists()


def test_status_cooldown_and_budget_lines(monkeypatch, tmp_path, capsys):
    """Review fix 14: thin bench under cooldown/budget explains itself."""
    import argparse
    from datetime import timedelta

    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    _heal_env(monkeypatch, tmp_path)
    pool = _pool(tmp_path)
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    args = argparse.Namespace(json=False)
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / "heal.json").write_text(json.dumps(
        {"schema": 1, "day": yesterday, "probes_today": 0, "runs_today": 0,
         "last_heal": None, "cooldown_until": future, "consecutive_low_yield": 1,
         "history": []}))
    # The stored day is stale relative to the real clock, so rollover
    # applies but the absolute cooldown still gates.
    assert managed_cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "Heal on cooldown until" in out and "verify --heal" not in out

    (tmp_path / "heal.json").write_text(json.dumps(
        {"schema": 1, "day": datetime.now(UTC).strftime("%Y-%m-%d"),
         "probes_today": 36, "runs_today": 3, "last_heal": None,
         "cooldown_until": None, "consecutive_low_yield": 0, "history": []}))
    assert managed_cli.cmd_status(args) == 0
    assert "Heal budget exhausted for today" in capsys.readouterr().out


def test_maintenance_heal_io_error_exits_1(monkeypatch, tmp_path, capsys):
    """Review fix 10: an unwritable heal store fails refresh loudly."""
    import argparse

    from freellmpool import maintenance_cli
    from freellmpool.managed import ManagedPool

    (tmp_path / "block").write_text("not a directory")
    monkeypatch.setenv("FREELLMPOOL_HEAL_PATH", str(tmp_path / "block" / "heal.json"))
    monkeypatch.setenv("FREELLMPOOL_AUTOHEAL", "1")
    monkeypatch.setattr(maintenance_cli, "run_maintenance",
                        lambda *a, **k: {"findings": []})
    pool = _pool(tmp_path)
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    args = argparse.Namespace(refresh=True, public_only=False, output=None,
                              baseline=None, source_revision=None, json=True)
    assert maintenance_cli.cmd_maintenance(args) == 1
    assert "heal aborted" in capsys.readouterr().err
