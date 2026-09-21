"""G36 provider-literal honesty: unknown --provider values exit 2 naming the literal.

Update/verify validate-first (no traceback, no heal burn, no exit-3 misdirect);
setup names the literal on the setup channel. Offline: real packaged registry
(16 ids) + fixture pools + monkeypatched refresh/heal/pool-load.
"""

from __future__ import annotations

import argparse

from test_managed_runtime import make_pool

from freellmpool import managed_cli
from freellmpool.conformance import ConformanceStore
from freellmpool.managed import ManagedPool


def _pool(tmp_path, ids=("a", "b", "c", "d")):
    return make_pool(tmp_path, ids=ids,
                     conformance=ConformanceStore(tmp_path / "c.json"))


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))


def _verify_args(**kwargs):
    base = {"provider": None, "limit": 4, "features": "chat", "timeout": 30,
            "json": False, "heal": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _update_args(**kwargs):
    base = {"public_only": False, "provider": None, "renew_evidence": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _setup_args(**kwargs):
    base = {"provider": None, "resume": True, "no_clients": True,
            "no_start": True, "stdin": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _row(status, models=()):
    return {"status": status, "note": "n", "checked_at": None,
            "complete": status == "ok", "models": list(models),
            "last_attempt_at": "2026-09-20T00:00:00+00:00"}


# --- update (A1-A4, A17-update, A18) ---

def test_update_unknown_single_exit_2_no_refresh(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("refresh must not run for an unknown literal")
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _boom)
    assert managed_cli.cmd_update(_update_args(provider=["NOSUCH"])) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "Known registry ids:" in captured.err
    assert "groq" in captured.err
    assert "Traceback" not in captured.err


def test_update_mixed_known_unknown_names_only_unknown(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("refresh must not run for a mixed literal")
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _boom)
    monkeypatch.setattr("freellmpool.discovery.refresh_evidence", _boom)
    args = _update_args(provider=["groq", "NOSUCH"], renew_evidence=True)
    assert managed_cli.cmd_update(args) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


def test_update_canonical_case_refreshes_and_displays(monkeypatch, capsys):
    seen = {}

    def _fake(env, **kwargs):
        seen.update(kwargs)
        return {"providers": {"groq": _row("ok")}}
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _fake)
    assert managed_cli.cmd_update(_update_args(provider=["GROQ"])) == 0
    assert seen.get("provider_ids") == ["groq"]
    assert "groq" in capsys.readouterr().out


def test_update_strips_whitespace_literal(monkeypatch, capsys):
    seen = {}

    def _fake(env, **kwargs):
        seen.update(kwargs)
        return {"providers": {"groq": _row("ok")}}
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _fake)
    assert managed_cli.cmd_update(_update_args(provider=[" groq "])) == 0
    assert seen.get("provider_ids") == ["groq"]


def test_update_empty_literal_is_unknown(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("refresh must not run for an empty literal")
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _boom)
    assert managed_cli.cmd_update(_update_args(provider=[""])) == 2
    captured = capsys.readouterr()
    assert "freellmpool: unknown provider ''. Known registry ids:" in captured.err


def test_update_unknown_dedup_echoes_first_verbatim(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("refresh must not run")
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", _boom)
    assert managed_cli.cmd_update(_update_args(provider=["NOSUCH", " nosuch "])) == 2
    err = capsys.readouterr().err
    assert "unknown provider 'NOSUCH'" in err
    assert "nosuch '" not in err and "' nosuch '" not in err


# --- verify (A5-A8, A16, A19) ---

def test_verify_unknown_never_burns_heal(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path))

    def _boom(*args, **kwargs):
        raise AssertionError("heal must not run for an unknown literal")
    monkeypatch.setattr("freellmpool.heal.run_heal", _boom)
    args = _verify_args(provider=["NOSUCH"], heal=True)
    assert managed_cli.cmd_verify(args) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err


def test_verify_unknown_json_keeps_stdout_empty(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path))
    args = _verify_args(provider=["NOSUCH"], json=True)
    assert managed_cli.cmd_verify(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown provider 'NOSUCH'" in captured.err


def test_verify_bad_features_beats_bad_literal(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path))
    args = _verify_args(provider=["NOSUCH"], features="bogus")
    assert managed_cli.cmd_verify(args) == 2
    captured = capsys.readouterr()
    assert "freellmpool verify:" in captured.err
    assert "unknown provider" not in captured.err


def test_verify_canonical_case_filters_routes(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path, ids=("groq", "gemini")))
    args = _verify_args(provider=["GROQ"])
    assert managed_cli.cmd_verify(args) == 0
    assert "groq" in capsys.readouterr().out


def test_verify_mixed_known_unknown_rejects(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path, ids=("groq", "gemini")))
    args = _verify_args(provider=["groq", "NOSUCH"])
    assert managed_cli.cmd_verify(args) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


def test_verify_registry_load_failure_skips_validation(tmp_path, monkeypatch, capsys):
    _patch_pool(monkeypatch, _pool(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("simulated corrupt install")
    monkeypatch.setattr(managed_cli, "load_registry", _boom)
    assert managed_cli.cmd_verify(_verify_args(provider=["NOSUCH"])) == 3
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "unknown provider" not in captured.err
    assert "No current free route is ready to verify" in captured.out


# --- setup / run_onboarding (A9-A12, A17-setup) ---

def test_setup_unknown_names_literal_on_stdout(monkeypatch, capsys):
    assert managed_cli.cmd_setup(_setup_args(provider="NOSUCH")) == 2
    captured = capsys.readouterr()
    assert "Unknown provider 'NOSUCH'" in captured.out
    assert "Known registry ids:" in captured.out


def test_run_onboarding_unknown_names_literal(tmp_path):
    from freellmpool.onboarding import run_onboarding

    output = []
    rc = run_onboarding(provider="NOSUCH", registry={"a": {}},
                        env={}, progress_path=tmp_path / "p.json",
                        output=output.append)
    assert rc == 2
    assert any("Unknown provider 'NOSUCH'" in line for line in output)


def test_run_onboarding_canonical_case_proceeds(tmp_path):
    from freellmpool.onboarding import run_onboarding

    registry = {"groq": {"display_name": "G",
                         "grants": [{"kind": "zero_price", "status": "verified"}]}}
    prompts = []
    rc = run_onboarding(provider="GROQ", registry=registry, env={},
                        progress_path=tmp_path / "p.json",
                        input_fn=lambda prompt: prompts.append(prompt) or "q",
                        output=lambda line: None)
    assert prompts  # proceeded past selection (unknown would rc2 silently)
    assert rc == 1  # scripted quit at the first prompt


def test_setup_trial_only_keeps_generic_skip(tmp_path):
    from freellmpool.onboarding import run_onboarding

    registry = {"trialx": {"display_name": "T",
                           "grants": [{"kind": "one_time_credit",
                                       "status": "verified"}]}}
    output = []
    rc = run_onboarding(provider="trialx", registry=registry, env={},
                        progress_path=tmp_path / "p.json",
                        output=output.append)
    assert rc == 2
    assert any("Trial/paid-only providers are skipped" in line for line in output)
    assert not any("Unknown provider" in line for line in output)


def test_setup_whitespace_literal_is_unknown(monkeypatch, capsys):
    assert managed_cli.cmd_setup(_setup_args(provider="  ")) == 2
    captured = capsys.readouterr()
    assert "Unknown provider '  '. Known registry ids:" in captured.out


# --- drift composition (A13-A14) ---

def test_drift_probe_unknown_propagates_rc2_clean_stdout(monkeypatch, capsys):
    args = argparse.Namespace(probe=True, provider=["NOSUCH"], limit=8,
                              features="chat,tools,streaming", timeout=30,
                              json=True, emit=None)
    assert managed_cli.cmd_drift(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown provider 'NOSUCH'" in captured.err


def test_drift_non_probe_ignores_provider(tmp_path, monkeypatch, capsys):
    from freellmpool.conformance import ConformanceStore

    monkeypatch.setenv("FREELLMPOOL_DRIFT_DIR", str(tmp_path / "drift"))
    pool = make_pool(tmp_path, ids=("a",),
                     conformance=ConformanceStore(tmp_path / "c.json"))
    _patch_pool(monkeypatch, pool)
    args = argparse.Namespace(probe=False, provider=["NOSUCH"], limit=8,
                              features="chat,tools,streaming", timeout=30,
                              json=False, emit=None)
    assert managed_cli.cmd_drift(args) == 0
    assert "baseline recorded" in capsys.readouterr().out


def test_resolve_provider_ids_unit_shape():
    from freellmpool.provider_registry import resolve_provider_ids

    registry = {"groq": {}, "gemini": {}}
    canonical, unknown = resolve_provider_ids(["GROQ", "NOSUCH", " nosuch "],
                                              registry)
    assert canonical == ["groq"]
    assert unknown == ["NOSUCH"]
    canonical, unknown = resolve_provider_ids([""], registry)
    assert canonical == [] and unknown == [""]
