"""G35 heal honesty: attempted accounting, no-contact exemption, announced skips.

A heal run that dispatches nothing must say what it attempted and why,
must not burn budget for zero upstream contact, and every skipped gate
must announce itself. Offline fixtures (fake probe fns + tmp stores +
injected clocks); cross-file helpers imported from test_heal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_heal import Clock, failing_call, failing_stream, passing_call, passing_stream
from test_managed_runtime import make_pool

from freellmpool import heal as h
from freellmpool.allowances import AllowanceDenied
from freellmpool.conformance import ConformanceStore


def _pool(tmp_path, ids=("a", "b", "c", "d"), conf="c.json", **kwargs):
    kwargs.setdefault("conformance", ConformanceStore(tmp_path / conf))
    return make_pool(tmp_path, ids=ids, **kwargs)


def _store(tmp_path, clock, name="heal.json"):
    return h.HealStore(tmp_path / name, clock=clock, monotonic=clock.mono)


def _run(pool, store, **kwargs):
    emitted: list = []
    kwargs.setdefault("call_fn", passing_call)
    kwargs.setdefault("stream_fn", passing_stream)
    out = h.run_heal(pool, store, trigger="verify", out=emitted.append, **kwargs)
    return out, emitted


def _clock():
    return Clock(datetime(2026, 9, 20, 12, tzinfo=UTC))


def _timeout_call(provider, model, messages, **kwargs):
    raise TimeoutError("synthetic")


def _timeout_stream(provider, model, messages, **kwargs):
    raise TimeoutError("synthetic")
    yield "unreachable"


def _transport_call(provider, model, messages, **kwargs):
    raise ConnectionError("synthetic")


def _transport_stream(provider, model, messages, **kwargs):
    raise ConnectionError("synthetic")
    yield "unreachable"


# --- attempt accounting --------------------------------------------------


@pytest.mark.parametrize(
    "call_fn,stream_fn,cause",
    [(failing_call, failing_stream, "rate_limit"),
     (_timeout_call, _timeout_stream, "timeout"),
     (_transport_call, _transport_stream, "transport")],
)
def test_thrown_probes_count_attempted_with_likely_cause(
        tmp_path, call_fn, stream_fn, cause):
    """Invisible throws (G35 hole 1): entries past the wallbox check
    count as attempted; the summary names the dominant class."""
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, emitted = _run(pool, store, call_fn=call_fn, stream_fn=stream_fn)
    assert out["attempted"] == 12  # 4 targets x 3 features, all threw
    assert out["probes"] == 0 and out["passes"] == 0
    line = next(m for m in emitted if "re-verified" in m)
    assert line == f"heal: 0/4 re-verified, 0 probes (attempted 12, likely {cause})"


def test_wallbox_pre_contact_attempted_zero(tmp_path):
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, _ = _run(pool, store, budget_seconds=0.0)
    assert out["attempted"] == 0  # wallbox tripped before any entry
    assert out["reason"] == "no-contact"


def test_denied_entries_counted_attempted_and_skipped(tmp_path):
    def denied(*args, **kwargs):
        raise AllowanceDenied("synthetic", None)

    def denied_stream(*args, **kwargs):
        raise AllowanceDenied("synthetic", None)
        yield "unreachable"

    pool, store = _pool(tmp_path, ids=("a", "b")), _store(tmp_path, _clock())
    out, _ = _run(pool, store, call_fn=denied, stream_fn=denied_stream)
    assert out["reason"] == "ok"  # 2 targets stay under the run cap
    assert out["attempted"] == 6 and out["skipped"] == 6
    assert out["probes"] == 0


# --- cause helper unit ----------------------------------------------------


def test_dominant_cause_plurality_and_ties():
    assert h._dominant_cause(["rate_limit", "rate_limit", "timeout"], 0, False) == \
        "likely rate_limit"
    assert h._dominant_cause(["timeout", "rate_limit"], 0, False) == \
        "likely rate_limit"  # alphabetical tie-break


def test_dominant_cause_excludes_verified():
    assert h._dominant_cause(["verified", "verified", "timeout"], 0, False) == \
        "likely timeout"


def test_dominant_cause_quota_uniform():
    assert h._dominant_cause(["quota", "quota", "rate_limit"], 0, False) == \
        "likely quota"  # no per-class branches, dead or alive


def test_dominant_cause_fallbacks_bare():
    assert h._dominant_cause([], 4, False) == "denied"
    assert h._dominant_cause([], 0, True) == "interrupted"
    assert h._dominant_cause([], 0, False) == "unknown"  # defensive


# --- no-contact exemption -------------------------------------------------


def _preset(tmp_path, **overrides):
    state = {"schema": 1, "day": "2026-09-20", "probes_today": 0,
             "runs_today": 0, "last_heal": None, "cooldown_until": None,
             "consecutive_low_yield": 0, "history": []}
    state.update(overrides)
    (tmp_path / "heal.json").write_text(json.dumps(state))


def test_capped_no_contact_exempt_as_capped(tmp_path):
    _preset(tmp_path, probes_today=35)  # remaining 1 < worst-case 4
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, emitted = _run(pool, store)
    # Frozen budget state keeps reason "capped" (daemon consumes;
    # no 60s futile loop) while the run itself is exempt.
    assert (out["ran"], out["reason"]) == (True, "capped")
    assert out["attempted"] == 0 and out["targets"] == []
    saved = store.view()
    assert saved["runs_today"] == 0 and saved["cooldown_until"] is None
    assert saved["consecutive_low_yield"] == 0  # previous values kept
    assert len(saved["history"]) == 1  # the run stays visible
    line = next(m for m in emitted if "re-verified" in m)
    assert line == "heal: 0/0 re-verified, 0 probes (no upstream contact; run not counted)"


def test_wallbox_early_no_contact_exempt(tmp_path):
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, _ = _run(pool, store, budget_seconds=0.0)
    assert (out["ran"], out["reason"]) == (True, "no-contact")
    saved = store.view()
    assert saved["runs_today"] == 0 and saved["cooldown_until"] is None


def test_stale_flip_no_contact_carries_skipped_suffix(tmp_path, monkeypatch):
    """All-stale selection: attempted 0, skipped carried + shown."""
    import freellmpool.maintenance as maint

    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    assert pool.snapshot().routes
    real_select = maint.select_verification_targets
    state = {"selected": False}

    def select_once(targets, conformance, limit):
        out = real_select(targets, conformance, limit)
        state["selected"] = True
        return out

    real_snapshot = pool.snapshot
    empty = _pool(tmp_path, ids=(), conformance=ConformanceStore(tmp_path / "e.json"))
    assert not empty.snapshot().routes

    def flapping():
        return empty.snapshot() if state["selected"] else real_snapshot()

    monkeypatch.setattr(maint, "select_verification_targets", select_once)
    monkeypatch.setattr(pool, "snapshot", flapping)
    out, emitted = _run(pool, store)
    assert out["attempted"] == 0 and out["reason"] == "capped"
    # 3 stale x 3 features; the 4th trips the run cap (caps count
    # skipped): capped-no-contact, exempt but consumed downstream.
    assert out["skipped"] == 9
    line = next(m for m in emitted if "re-verified" in m)
    assert line == ("heal: 0/0 re-verified, 0 probes, 9 skipped "
                    "(no upstream contact; run not counted)")


def test_partition_empty_vs_no_contact(tmp_path):
    empty_pool = _pool(tmp_path, ids=())
    assert not empty_pool.snapshot().routes
    out, _ = _run(empty_pool, _store(tmp_path, _clock()))
    assert (out["ran"], out["reason"]) == (True, "empty")
    assert out["attempted"] == 0
    pool = _pool(tmp_path)
    assert pool.snapshot().routes  # selection non-empty...
    out, _ = _run(pool, _store(tmp_path, _clock()), budget_seconds=0.0)
    assert (out["ran"], out["reason"]) == (True, "no-contact")  # ...but no contact


def test_features_empty_vacuous_ok_not_no_contact(tmp_path):
    """Defense-in-depth: degenerate features=() passes vacuously and
    must not read as no-contact (probed non-empty guards it)."""
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, emitted = _run(pool, store, features=())
    assert (out["ran"], out["reason"]) == (True, "ok")
    assert out["attempted"] == 0 and out["passes"] == 4
    line = next(m for m in emitted if "re-verified" in m)
    assert line == "heal: 4/4 re-verified, 0 probes"


def test_attempted_positive_still_paces(tmp_path):
    """V1-hole regression: all-throw runs burn + double (G31 intact)."""
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, _ = _run(pool, store, call_fn=failing_call, stream_fn=failing_stream)
    assert out["reason"] == "ok" and out["attempted"] == 12
    saved = store.view()
    assert saved["runs_today"] == 1
    assert saved["consecutive_low_yield"] == 1
    assert saved["cooldown_until"] == "2026-09-20T14:00:00+00:00"  # base x2


def test_all_denied_burns_with_denied_cause(tmp_path):
    def denied(*args, **kwargs):
        raise AllowanceDenied("synthetic", None)

    def denied_stream(*args, **kwargs):
        raise AllowanceDenied("synthetic", None)
        yield "unreachable"

    pool, store = _pool(tmp_path, ids=("a", "b")), _store(tmp_path, _clock())
    out, emitted = _run(pool, store, call_fn=denied, stream_fn=denied_stream)
    assert store.view()["runs_today"] == 1  # attempted>0: paced
    line = next(m for m in emitted if "re-verified" in m)
    assert "(attempted 6, denied)" in line and "likely" not in line


# --- gate skips announce ----------------------------------------------------


def test_gate_cooldown_announced(tmp_path):
    _preset(tmp_path, cooldown_until="2026-09-20T14:00:00+00:00")
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, emitted = _run(pool, store)
    assert (out["ran"], out["reason"]) == (False, "cooldown")
    assert out["attempted"] == 0
    assert emitted == ["heal skipped: on cooldown until 2026-09-20T14:00:00+00:00"]


def test_gate_budget_announced(tmp_path):
    _preset(tmp_path, probes_today=36)
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())
    out, emitted = _run(pool, store)
    assert out == {"ran": False, "reason": "budget", "targets": [],
                   "passes": 0, "probes": 0, "skipped": 0, "attempted": 0}
    assert emitted == ["heal skipped: daily heal budget exhausted (runs/probes)"]


def _verify_pool(tmp_path, monkeypatch, ids=("a", "b", "c", "d")):
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path, ids=ids)
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    return pool


def test_gate_in_verify_announced_and_falls_through(tmp_path, monkeypatch, capsys):
    """Parent-pain composition: gated heal inside verify announces
    the skip AND the verify phase still proceeds."""
    from datetime import timedelta

    from freellmpool import managed_cli

    now = datetime.now(UTC)
    until = (now + timedelta(hours=2)).isoformat()  # real clock: must be future
    (tmp_path / "heal.json").write_text(json.dumps(
        {"schema": 1, "day": now.date().isoformat(), "probes_today": 0,
         "runs_today": 0, "last_heal": None, "cooldown_until": until,
         "consecutive_low_yield": 0, "history": []}))
    _verify_pool(tmp_path, monkeypatch)
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass",
                                                 "classification": "verified"}})
    assert managed_cli.cmd_verify(_verify_args(heal=True)) == 0
    captured = capsys.readouterr()
    assert f"heal skipped: on cooldown until {until}" in captured.err
    assert "a/free" in captured.out  # verify phase proceeded past the skip


def test_busy_in_verify_announced_and_falls_through(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    _verify_pool(tmp_path, monkeypatch)
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass",
                                                 "classification": "verified"}})
    store = h.HealStore(tmp_path / "heal.json")
    with store.lease():
        assert managed_cli.cmd_verify(_verify_args(heal=True)) == 0
    captured = capsys.readouterr()
    assert "heal already running; skipping" in captured.err
    assert "a/free" in captured.out  # verify phase proceeded


# --- executor retain vs consume ----------------------------------------------


def _executor(tmp_path, run_fn):
    from types import SimpleNamespace

    from freellmpool.heal import HealExecutor, HealStore, TickStore

    pool = SimpleNamespace(env={"FREELLMPOOL_AUTOHEAL": "1"},
                           managed_status=lambda: {"tools_ready": 0})
    ticks = TickStore(tmp_path / "ticks.json")
    return HealExecutor(pool, ticks, HealStore(tmp_path / "h.json"), run_fn=run_fn), ticks


def test_executor_retains_demand_on_no_contact(tmp_path):
    ex, ticks = _executor(tmp_path, lambda p, s: {"ran": True, "reason": "no-contact"})
    for _ in range(5):
        ticks.record(now=1000.0)
    assert ex.iterate_once(now=1000.0)["acted"] is True
    assert ticks.demand(now=1000.0) is True  # transient probe-free: retry


def test_executor_consumes_on_ok(tmp_path):
    ex, ticks = _executor(tmp_path, lambda p, s: {"ran": True, "reason": "ok"})
    for _ in range(5):
        ticks.record(now=1000.0)
    assert ex.iterate_once(now=1000.0)["acted"] is True
    assert ticks.demand(now=1000.0) is False  # evaluated: re-arm on fresh ticks


def test_executor_consumes_on_capped_no_contact(tmp_path):
    """Frozen budget state must not spin a 60s futile loop: capped
    consumes even with zero attempted; fresh ticks re-arm."""
    ex, ticks = _executor(tmp_path, lambda p, s: {"ran": True, "reason": "capped"})
    for _ in range(5):
        ticks.record(now=1000.0)
    assert ex.iterate_once(now=1000.0)["acted"] is True
    assert ticks.demand(now=1000.0) is False


# --- maintenance mapping ------------------------------------------------------


def test_maintenance_no_contact_line_and_zero(tmp_path, monkeypatch, capsys):
    from freellmpool import maintenance_cli

    canned = {"ran": True, "reason": "no-contact", "targets": [], "passes": 0,
              "probes": 0, "skipped": 0, "attempted": 0}
    monkeypatch.setattr("freellmpool.heal.run_heal", lambda *a, **k: canned)
    out = maintenance_cli._maybe_heal_after_refresh(
        {"FREELLMPOOL_AUTOHEAL": "1"}, {"findings": []})
    assert out == "no-contact"
    assert "heal: no-contact (0/0 re-verified, 0 probes)" in capsys.readouterr().err


# --- status 6-cell --------------------------------------------------------------


def _status_pool(tmp_path, monkeypatch, ids=("a", "b", "c", "d"), **preset_kw):
    from freellmpool.managed import ManagedPool

    _preset(tmp_path, **preset_kw)
    pool = _pool(tmp_path, ids=ids)
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    return pool


def _status_args(**kwargs):
    import argparse

    base = {"json": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_status_chat0_open_prescribes_update_or_setup(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    _status_pool(tmp_path, monkeypatch, ids=())
    assert cmd_status(_status_args()) == 0
    out = capsys.readouterr().out
    assert ("Bench thin with no healable routes (0 fresh, need 3); "
            "run freellmpool update, or freellmpool setup to connect access") in out
    assert "run freellmpool verify --heal" not in out


def test_status_chat0_cooldown_still_prescribes_update(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    _status_pool(tmp_path, monkeypatch, ids=(),
                 cooldown_until="2026-09-20T14:00:00+00:00")
    assert cmd_status(_status_args()) == 0
    out = capsys.readouterr().out
    assert "run freellmpool update, or freellmpool setup" in out
    assert "Heal on cooldown" not in out  # actionable first step wins


def test_status_chat0_budget_still_prescribes_update(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    _status_pool(tmp_path, monkeypatch, ids=(), probes_today=36)
    assert cmd_status(_status_args()) == 0
    out = capsys.readouterr().out
    assert "run freellmpool update, or freellmpool setup" in out
    assert "budget exhausted" not in out


def test_status_thin_open_heal_line_exact(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    _status_pool(tmp_path, monkeypatch)
    assert cmd_status(_status_args()) == 0
    assert "Bench thin: run freellmpool verify --heal (0 fresh, need 3)" in \
        capsys.readouterr().out


def test_status_json_carries_chat_routes(tmp_path, monkeypatch, capsys):
    import argparse

    from freellmpool.managed_cli import cmd_status

    _status_pool(tmp_path, monkeypatch)
    assert cmd_status(argparse.Namespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["chat_routes"] == 4


# --- verify cold shapes ---------------------------------------------------------


def _verify_args(**kwargs):
    import argparse

    base = {"provider": None, "limit": 4, "features": "chat", "timeout": 30,
            "json": False, "heal": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_verify_bare_cold_single_update_line(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path, ids=())
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    assert managed_cli.cmd_verify(_verify_args()) == 3
    captured = capsys.readouterr()
    assert captured.out == ("No verifiable routes: run freellmpool update, "
                            "or freellmpool setup to connect access.\n")
    assert "Bench thin" not in captured.err


def test_verify_heal_cold_single_line_with_setup_half(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path, ids=())
    pool._base_env = dict(pool._base_env,
                          FREELLMPOOL_HEAL_PATH=str(tmp_path / "heal.json"))
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    assert managed_cli.cmd_verify(_verify_args(heal=True)) == 3
    captured = capsys.readouterr()
    assert ("heal: no verification targets; run freellmpool update, "
            "or freellmpool setup to connect access") in captured.err
    assert "No verifiable routes" not in captured.out  # suppressed duplicate


def test_verify_filtered_zero_keeps_offer(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path)
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    assert managed_cli.cmd_verify(_verify_args(provider=["nope"])) == 3
    captured = capsys.readouterr()
    assert "Bench thin: run freellmpool verify --heal" in captured.err
    assert captured.out == ("No current free route is ready to verify. "
                            "Run freellmpool status or setup.\n")


def test_verify_thin_with_targets_offer_identical(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path)
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries",
                        lambda *a, **k: {"chat": {"status": "pass",
                                                 "classification": "verified"}})
    assert managed_cli.cmd_verify(_verify_args()) == 0
    assert "Bench thin: run freellmpool verify --heal (0 fresh, need 3)" in \
        capsys.readouterr().err


def test_verify_all_unavailable_exit_3(tmp_path, monkeypatch):
    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    pool = _pool(tmp_path)
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))
    monkeypatch.setattr(pool, "probe_call", failing_call)
    monkeypatch.setattr(pool, "probe_stream", failing_stream)
    assert managed_cli.cmd_verify(_verify_args()) == 3


def test_verify_help_legend(tmp_path, capsys):
    import argparse

    from freellmpool.managed_cli import add_commands

    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["verify", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for code in ("exit 0", "exit 1", "exit 2", "exit 3"):
        assert code in out


# --- exempt io-error + exact dicts -------------------------------------------------


def test_exempt_io_error_attempted_zero(tmp_path, monkeypatch):
    """Exempt-path save failure: the shared tail still fails loudly
    (lease succeeds here, unlike the block-file acquisition shape)."""
    pool, store = _pool(tmp_path), _store(tmp_path, _clock())

    def fail_save(state):
        raise OSError("synthetic")

    monkeypatch.setattr(store, "save", fail_save)
    out, emitted = _run(pool, store, budget_seconds=0.0)
    assert (out["ran"], out["reason"]) == (True, "io-error")
    assert out["attempted"] == 0
    assert any("state unwritable" in m for m in emitted)


def test_exact_dicts_carry_attempted_zero(tmp_path):
    pool = _pool(tmp_path, conf="healthy.json")
    for route in pool.snapshot().routes:
        for feature in ("chat", "tools", "streaming"):
            pool.conformance.record(route.provider, route.model, feature,
                                    status="pass", classification="verified")
    out, _ = _run(pool, _store(tmp_path, _clock()))
    assert out == {"ran": False, "reason": "healthy", "targets": [],
                   "passes": 0, "probes": 0, "skipped": 0, "attempted": 0}
    gate_store = h.HealStore(tmp_path / "g.json", clock=_clock(),
                             monotonic=_clock().mono)
    (tmp_path / "g.json").write_text(json.dumps(
        {"schema": 1, "day": "2026-09-20", "probes_today": 0, "runs_today": 0,
         "last_heal": None, "cooldown_until": "2026-09-20T14:00:00+00:00",
         "consecutive_low_yield": 0, "history": []}))
    out, _ = _run(_pool(tmp_path), gate_store)
    assert out == {"ran": False, "reason": "cooldown", "targets": [],
                   "passes": 0, "probes": 0, "skipped": 0, "attempted": 0}
    out, _ = _run(_pool(tmp_path, ids=()), _store(tmp_path, _clock()))
    assert out == {"ran": True, "reason": "empty", "targets": [],
                   "passes": 0, "probes": 0, "skipped": 0, "attempted": 0}
