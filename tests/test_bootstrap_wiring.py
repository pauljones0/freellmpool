"""G24 U2: bounded caller wiring — bootstrap/update/maintenance/setup/main + help."""

import argparse
import json
import time

import pytest

from freellmpool import discovery as d
from freellmpool.cli import _bootstrap_tier_line, main
from freellmpool.errors import NoProvidersConfigured


def _del_opt_out(monkeypatch):
    monkeypatch.delenv("FREELLMPOOL_NO_AUTO_DISCOVERY", raising=False)


@pytest.fixture(autouse=True)
def _no_stdin(monkeypatch):
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")


def _ask_args():
    return ["ask", "hi", "--max-tokens", "1", "--timeout", "5"]


def _row(status, note="n", checked_at=None, models=()):
    return {"status": status, "note": note, "checked_at": checked_at,
            "complete": status == "ok", "models": list(models),
            "last_attempt_at": "2026-09-20T00:00:00+00:00"}


def test_tier_texts_exact():
    transport = {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE)}}
    assert _bootstrap_tier_line(transport) == (
        "freellmpool: could not reach providers (check network connectivity and "
        "proxy settings); retry the command, or run `freellmpool update` when online.")
    deferred = {"providers": {"a": _row("deferred", "Skipped: x")}}
    assert _bootstrap_tier_line(deferred) == (
        "freellmpool: model discovery deferred (time budget); "
        "run `freellmpool update` to complete it.")
    assert _bootstrap_tier_line({"providers": {}}) == (
        "freellmpool: no free routes found - check connectivity, then run "
        "`freellmpool update` to refresh the model catalog.")
    # Mixed network + deferred falls through to the deferred tier (desrev-4).
    mixed = {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE),
                           "b": _row("deferred", "Skipped: x")}}
    assert _bootstrap_tier_line(mixed) == _bootstrap_tier_line(deferred)
    # Any checked_at defeats the transport tier.
    checked = {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE,
                                       checked_at="2026-09-20T00:00:00+00:00")}}
    assert _bootstrap_tier_line(checked) == _bootstrap_tier_line({"providers": {}})


def test_bootstrap_passes_deadline_and_progress(monkeypatch):
    _del_opt_out(monkeypatch)
    seen = {}

    def fake(env, **kwargs):
        seen.update(kwargs)
        return {"providers": {}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: True)
    before = time.monotonic()
    assert main(_ask_args()) == 4
    assert before + 39.0 < seen["deadline"] < before + 41.0
    assert callable(seen["progress"])


def test_bootstrap_honors_budget_env(monkeypatch):
    _del_opt_out(monkeypatch)
    monkeypatch.setenv("FREELLMPOOL_DISCOVERY_BUDGET_SECONDS", "10")
    seen = {}

    def fake(env, **kwargs):
        seen.update(kwargs)
        return {"providers": {}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: True)
    before = time.monotonic()
    assert main(_ask_args()) == 4
    assert before + 9.0 < seen["deadline"] < before + 11.0


def test_progress_order_exact(monkeypatch, capsys):
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        progress = kwargs["progress"]
        progress(provider_id="aaa", index=0, total=2)
        progress(provider_id="aaa", page=1)
        progress(provider_id="aaa", page=2)
        progress(provider_id="bbb", index=1, total=2)
        progress(provider_id="bbb", page=1)
        return {"providers": {"aaa": _row("ok"), "bbb": _row("ok")}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: True)
    assert main(_ask_args()) == 4
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "freellmpool: first run - discovering free routes (one-time)..."
    assert err[1] == "freellmpool: discovering aaa (1/2)..."
    assert err[2] == "freellmpool: discovering aaa page 2..."
    assert err[3] == "freellmpool: discovering bbb (2/2)..."
    assert err[4] == "freellmpool: discovery: 2 ok, 0 deferred, 0 failed (0s)"
    assert not any("page 1" in line for line in err)
    assert not any("deferred/failed providers" in line for line in err)


def test_bootstrap_csv_names_failed_providers(monkeypatch, capsys):
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        return {"providers": {
            "zeta": _row("error", "Catalog HTTP 503; last-good evidence preserved."),
            "alpha": _row("deferred", "Skipped: x")}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    err = capsys.readouterr().err
    assert "freellmpool: deferred/failed providers: zeta, alpha" in err
    assert "freellmpool: discovery: 0 ok, 1 deferred, 1 failed" in err
    assert "model discovery deferred (time budget)" in err


def test_progress_sanitizes_provider_ids(monkeypatch, capsys):
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        kwargs["progress"](provider_id="evil\nline", index=0, total=1)
        return {"providers": {"evil\nline": _row("ok")}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: True)
    assert main(_ask_args()) == 4
    for line in capsys.readouterr().err.splitlines():
        assert "\n" not in line
    assert d.sanitize_pid("evil\nline") == "evil_line"


def test_refused_cold_ask_fast_actionable(monkeypatch, capsys):
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        return {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE),
                              "b": _row("error", d._NOTE_NETWORK_FAILURE)}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    start = time.monotonic()
    assert main(_ask_args()) == 4
    assert time.monotonic() - start < 15
    err = capsys.readouterr().err
    assert ("freellmpool: could not reach providers (check network connectivity and "
            "proxy settings); retry the command, or run `freellmpool update` when online.") in err


def test_blackhole_cold_ask_bounded_loud_actionable(monkeypatch, capsys):
    """Loud + actionable pinned here; the wall bound is proven live (U5)."""
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        time.sleep(0.2)
        return {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE)}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    start = time.monotonic()
    assert main(_ask_args()) == 4
    assert time.monotonic() - start < 60
    err = capsys.readouterr().err
    assert "first run - discovering free routes" in err
    assert "could not reach providers" in err
    assert "freellmpool: discovery: 0 ok, 0 deferred, 1 failed" in err
    assert "freellmpool: deferred/failed providers: a" in err


def test_bootstrap_busy_proceeds_loud(monkeypatch, capsys):
    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    assert ("freellmpool: another catalog refresh is running; proceeding without fresh "
            "discovery (run `freellmpool update` later to refresh).") in capsys.readouterr().err


def _help_output(capsys, argv):
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert exited.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def test_ask_timeout_help_names_discovery_budget(monkeypatch, capsys):
    for argv in (["ask", "--help"], ["tokenmax", "--help"], ["battle", "--help"],
                ["recipe", "run", "--help"]):
        text = _help_output(capsys, argv)
        assert ("(covers inference only; catalog discovery has a separate budget, "
                "default 40s, excluding system DNS time; cold start stays under ~60s)") in text, argv


def test_proxy_help_names_discovery_budget(capsys):
    assert ("(first run: catalog discovery up to the discovery budget, default 40s, "
            "excluding system DNS time, before serving)") in _help_output(capsys, ["proxy", "--help"])


def test_mcp_help_names_discovery_budget(capsys):
    assert ("(first run: catalog discovery up to the discovery budget, default 40s, "
            "excluding system DNS time, before serving)") in _help_output(capsys, ["mcp", "--help"])


def test_jobs_run_help_names_discovery_budget(capsys):
    assert ("(first run may spend the discovery budget, default 40s, excluding system DNS time, "
            "before running)") in _help_output(capsys, ["jobs", "run", "--help"])


def test_update_footer_both_branches(monkeypatch, capsys):
    from freellmpool import managed_cli

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"a": _row("ok")}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    out = capsys.readouterr().out
    assert "Discovery updated. Pricing, account eligibility, and protocol evidence remain separate checks." in out

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"a": _row("ok"), "b": _row("deferred", "x"),
                                                       "c": _row("error", "y")}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    out = capsys.readouterr().out
    assert ("Discovery incomplete: 1 ok, 1 deferred, 1 failed; run `freellmpool update` to retry. "
            "Pricing, account eligibility, and protocol evidence remain separate checks.") in out


def test_update_subset_footer_counts_requested(monkeypatch, capsys):
    from freellmpool import managed_cli

    # G36: subset ids must be registry-known (unknown literals exit 2).
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"groq": _row("ok"), "gemini": _row("error", "y")}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=["groq"])) == 0
    out = capsys.readouterr().out
    assert "groq " in out and "gemini " not in out.splitlines()[0]
    assert "Discovery updated. Pricing" in out


def test_update_busy_shows_last_good(monkeypatch, capsys, tmp_path):
    from freellmpool import managed_cli

    def busy(env, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", busy)
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"a": _row("ok", models=[{
        "id": "m", "modalities": ["chat"], "pricing": {}}])}}))
    monkeypatch.setenv("FREELLMPOOL_DISCOVERY_FILE", str(path))
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    captured = capsys.readouterr()
    assert ("freellmpool: another catalog refresh is running; showing last-good catalog "
            "without refreshing (retry `freellmpool update`).") in captured.err
    assert "a " in captured.out and "Discovery incomplete:" in captured.out


def test_discovery_main_busy_exit2_stderr_only(monkeypatch, capsys):
    def busy(*args, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", busy)
    assert d.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("freellmpool: another catalog refresh is running; scheduled refresh skipped "
            "(retry later).") in captured.err


def test_discovery_main_stdout_stays_pure_json(monkeypatch, capsys):
    def fake(env, providers=None, **kwargs):
        kwargs["progress"](provider_id="a", index=0, total=1)
        return {"generation": "g", "providers": {"a": _row("ok")}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    assert d.main([]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["generation"] == "g"
    assert "discovering a" in captured.err


def test_maintenance_busy_is_loud_nonzero(monkeypatch, capsys):
    from freellmpool import maintenance_cli

    def busy(*args, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.maintenance_cli.run_maintenance", busy)
    args = argparse.Namespace(baseline=None, public_only=False, refresh=True,
                              output=None, json=False, source_revision=None)
    assert maintenance_cli.cmd_maintenance(args) == 2
    assert ("freellmpool: another catalog refresh is running; maintenance refresh "
            "skipped (retry later).") in capsys.readouterr().err


def test_busy_lines_exact():
    from freellmpool import cli, maintenance_cli, managed_cli

    assert cli._BOOTSTRAP_BUSY_LINE == (
        "freellmpool: another catalog refresh is running; proceeding without fresh "
        "discovery (run `freellmpool update` later to refresh).")
    assert managed_cli._UPDATE_BUSY_LINE == (
        "freellmpool: another catalog refresh is running; showing last-good catalog "
        "without refreshing (retry `freellmpool update`).")
    assert d._MAIN_BUSY_LINE == (
        "freellmpool: another catalog refresh is running; scheduled refresh skipped "
        "(retry later).")
    assert maintenance_cli._MAINTENANCE_BUSY_LINE == (
        "freellmpool: another catalog refresh is running; maintenance refresh "
        "skipped (retry later).")


def test_bootstrap_progress_survives_closed_stderr(monkeypatch):
    import sys

    from freellmpool.cli import _ensure_first_run_discovery

    _del_opt_out(monkeypatch)
    snapshot = {"providers": {"a": _row("ok")}}
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", lambda *a, **k: snapshot)

    class Closed:
        def write(self, *args, **kwargs):
            raise BrokenPipeError("closed")

        def flush(self, *args, **kwargs):
            raise BrokenPipeError("closed")

    monkeypatch.setattr(sys, "stderr", Closed())
    assert _ensure_first_run_discovery() == snapshot


def test_update_busy_skips_renew_evidence_loudly(monkeypatch, capsys):
    from freellmpool import managed_cli

    def busy(env, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", busy)

    def renewed(*args, **kwargs):
        raise AssertionError("evidence must not renew on a busy update")

    monkeypatch.setattr("freellmpool.discovery.refresh_evidence", renewed)
    args = argparse.Namespace(public_only=False, provider=None, renew_evidence=True)
    assert managed_cli.cmd_update(args) == 0
    assert managed_cli._UPDATE_BUSY_RENEW_SKIP in capsys.readouterr().err


def test_recipe_run_auth_failure_names_key(monkeypatch, tmp_path, capsys):
    from test_key_rotation import make_keyed_pool

    from freellmpool.client import HTTPResult
    from freellmpool.router import Pool

    pool = make_keyed_pool(tmp_path, {"ALPHA_API_KEY": "bogus"},
                           post=lambda *args: HTTPResult(401, {}, "bad key"))
    monkeypatch.setattr(Pool, "from_default_config", classmethod(lambda cls: pool))
    patch = tmp_path / "patch.diff"
    patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    assert main(["recipe", "run", "pr-review", "--input", str(patch)]) == 3
    assert "(check key ALPHA_API_KEY)" in capsys.readouterr().err


def _captured_setup_check(monkeypatch):
    from freellmpool import managed_cli

    captured = {}

    def onboarding(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("freellmpool.onboarding.run_onboarding", onboarding)
    monkeypatch.setattr(managed_cli, "cmd_setup_clients", lambda args: 0)
    managed_cli.cmd_setup(argparse.Namespace(provider=None, resume=True, no_clients=False,
                                             no_start=True))
    return captured["check"]


def test_setup_checks_keep_own_budget(monkeypatch):
    deadlines = []

    def fake(env, **kwargs):
        deadlines.append(kwargs["deadline"])
        return {"providers": {}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    check = _captured_setup_check(monkeypatch)
    first_before = time.monotonic()
    check("aaa", {})
    time.sleep(0.3)
    second_before = time.monotonic()
    check("bbb", {})
    # Each check arms a fresh full budget: no shared deadline (CTO-2).
    assert first_before + 39.0 < deadlines[0] < first_before + 41.0
    assert second_before + 39.0 < deadlines[1] < second_before + 41.0
    assert deadlines[1] - deadlines[0] > 0.2


def test_setup_busy_returns_error_row(monkeypatch):
    def busy(env, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", busy)
    row = _captured_setup_check(monkeypatch)("aaa", {})
    assert row["status"] == "error"
    assert row["note"] == "Another catalog refresh is running; retry this provider later."


def test_setup_check_renders_deferred_text():
    from freellmpool.onboarding import _STATUS_TEXT

    assert _STATUS_TEXT["deferred"] == ("Discovery deferred (time budget); run "
                                        "freellmpool update, then retry this provider.")


def test_recipe_zero_route_exit3(monkeypatch, tmp_path, capsys):
    from freellmpool.router import Pool

    _del_opt_out(monkeypatch)
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {}})
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)

    class EmptyPool:
        @classmethod
        def from_default_config(cls):
            return cls()

        def ask(self, prompt, **kwargs):
            raise NoProvidersConfigured("no free routes found")

    monkeypatch.setattr(Pool, "from_default_config", EmptyPool.from_default_config)
    patch = tmp_path / "patch.diff"
    patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    assert main(["recipe", "run", "pr-review", "--input", str(patch)]) == 3


def test_tier_exit_transport_is_4(monkeypatch, capsys):
    _del_opt_out(monkeypatch)
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE)}})
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    assert "could not reach providers" in capsys.readouterr().err


def test_tier_exit_zero_routes_is_4(monkeypatch, capsys):
    _del_opt_out(monkeypatch)
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {}})
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    assert "no free routes found" in capsys.readouterr().err


def test_tier_exit_deferred_partial_proceeds(monkeypatch, capsys):
    _del_opt_out(monkeypatch)
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"a": _row("deferred", "Skipped: x")}})

    class EmptyPool:
        @classmethod
        def from_default_config(cls):
            return cls()

        def ask(self, prompt, **kwargs):
            raise NoProvidersConfigured("no free routes found")

    from freellmpool.router import Pool

    monkeypatch.setattr(Pool, "from_default_config", EmptyPool.from_default_config)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: True)
    assert main(_ask_args()) == 3
    assert "model discovery deferred" not in capsys.readouterr().err


def test_busy_exit_paths(monkeypatch, tmp_path, capsys):
    from freellmpool import managed_cli

    def busy(*args, **kwargs):
        raise d.DiscoveryBusy("another catalog refresh is running")

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", busy)
    _del_opt_out(monkeypatch)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    assert "proceeding without fresh discovery" in capsys.readouterr().err
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    assert d.main([]) == 2


def test_ask_json_stdout_pure(monkeypatch, capsys):
    from freellmpool.models import Reply
    from freellmpool.router import Pool

    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        kwargs["progress"](provider_id="a", index=0, total=1)
        return {"providers": {"a": _row("ok")}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)

    class FakePool:
        def ask(self, prompt, **kwargs):
            return Reply(text='{"answer": 1}', provider_id="fake", model="m", raw={})

    monkeypatch.setattr(Pool, "from_default_config", classmethod(lambda cls: FakePool()))
    assert main(["ask", "hi", "--json", "--max-tokens", "1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"answer": 1}
    assert "discovering" in captured.err


def test_blackhole_stderr_has_no_url_or_account_id(monkeypatch, capsys):
    import re

    _del_opt_out(monkeypatch)

    def fake(env, **kwargs):
        kwargs["progress"](provider_id="https://registry.invalid/x?a=" + "b" * 32,
                           index=0, total=1)
        return {"providers": {"a": _row("error", d._NOTE_NETWORK_FAILURE)}}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    monkeypatch.setattr("freellmpool.cli._snapshot_has_chat_routes", lambda: False)
    assert main(_ask_args()) == 4
    err = capsys.readouterr().err
    assert "https://" not in err
    assert not re.search(r"[a-f0-9]{32}", err)


def test_bogus_key_names_var_and_canary_clean(monkeypatch, tmp_path, capsys):
    import secrets

    from test_key_rotation import make_keyed_pool

    from freellmpool.client import HTTPResult
    from freellmpool.router import Pool

    canary = "canary-" + secrets.token_hex(8)
    pool = make_keyed_pool(tmp_path, {"ALPHA_API_KEY": canary},
                           post=lambda *args: HTTPResult(401, {}, "bad key"))
    monkeypatch.setattr(Pool, "from_default_config", classmethod(lambda cls: pool))
    discovery = tmp_path / "discovery.json"
    discovery.write_text(json.dumps({"schema": 1, "providers": {}}))
    monkeypatch.setenv("FREELLMPOOL_DISCOVERY_FILE", str(discovery))
    assert main(["ask", "hi", "--max-tokens", "1"]) == 4
    captured = capsys.readouterr()
    assert "(check key ALPHA_API_KEY)" in captured.err
    assert canary not in captured.out and canary not in captured.err
    assert canary not in discovery.read_text()
