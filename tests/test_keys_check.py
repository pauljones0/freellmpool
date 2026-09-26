"""G29 `keys check`: per-slot key validation over the GET-only listing path.

Covers the AC5 matrix: verdict mapping (blocked/partial/deferred/timeout/
config_error), exit codes 0/1/2, --strict flips, filters, suffix gaps,
blank slots, config.toml-sourced keys, slot-1 logical equality, zero-network
public providers (call counting), the checkable-set tripwire, canary secrecy,
--json purity, ledger logical-equality, progress bytes, and parser tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets

import httpx
import pytest

from freellmpool import cli as cli_mod
from freellmpool import discovery as d
from freellmpool.allowances import AllowanceLedger
from freellmpool.catalog import ExternalProvider
from freellmpool.key_inventory import redact_secrets
from freellmpool.models import Provider
from freellmpool.provider_registry import load_registry

CHECKABLE = {"cloudflare", "cohere", "gemini", "groq", "mistral", "zhipu"}
VALID_ACCOUNT_ID = "a" * 32


def base_key(pid: str) -> str:
    key_env = load_registry()[pid]["credential_env"]
    assert isinstance(key_env, str)
    return key_env


def scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every credential + policy var so the real environment can't leak in."""
    for provider in load_registry().values():
        key_env = provider.get("credential_env")
        if not key_env:
            continue
        monkeypatch.delenv(key_env, raising=False)
        for slot in range(2, 10):
            monkeypatch.delenv(f"{key_env}_{slot}", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FREELLMPOOL_POLICY_BUNDLE_FILE", raising=False)
    monkeypatch.delenv("FREELLMPOOL_POLICY_STATUS_FILE", raising=False)
    monkeypatch.delenv("FREELLMPOOL_POLICY_UPDATES", raising=False)


@pytest.fixture(autouse=True)
def isolate_match_sources(monkeypatch):
    """Unfiltered runs stay deterministic: no user catalog, no external cache."""
    import freellmpool.catalog as catalog_mod
    import freellmpool.config as config_mod

    monkeypatch.setattr(config_mod, "load_catalog", lambda path=None: [])
    monkeypatch.setattr(catalog_mod, "load_external_catalog", lambda path=None: [])
    return monkeypatch


def install_fake(monkeypatch: pytest.MonkeyPatch, handler) -> list:
    """Route discovery HTTP through a counting mock transport. Returns calls."""
    calls: list = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        d,
        "_aclient",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(counting), follow_redirects=False
        ),
    )
    return calls


def ok_body(pid: str) -> dict:
    if pid in {"gemini", "cohere"}:
        return {"models": [{"name": "models/probe-free"}]}
    if pid == "cloudflare":
        return {"result": [{"name": "@cf/probe-free"}], "success": True}
    return {"data": [{"id": "probe-free"}]}


def malformed_body(pid: str) -> dict:
    if pid in {"gemini", "cohere"}:
        return {"models": "not-an-array"}
    if pid == "cloudflare":
        return {"success": False, "result": []}
    return {"data": "not-an-array"}


def slot_env(pid: str, value: str = "probe-key") -> dict[str, str]:
    env = {base_key(pid): value}
    if pid == "cloudflare":
        env["CLOUDFLARE_ACCOUNT_ID"] = VALID_ACCOUNT_ID
    return env


def make_args(**overrides) -> argparse.Namespace:
    params = {"provider": None, "slot": None, "json": False,
              "timeout": 180.0, "strict": False}
    params.update(overrides)
    return argparse.Namespace(**params)


def run_check(args: argparse.Namespace, capsys) -> tuple[int, str, str]:
    rc = cli_mod.cmd_keys_check(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# --- discovery: checkability rule + tripwire --------------------------------


def test_checkable_set_equality_tripwire():
    """Computed checkable set must equal the reviewed six; drift fails loud."""
    computed = {pid for pid, p in load_registry().items()
                if d.is_listing_checkable(p)}
    assert computed == CHECKABLE


@pytest.mark.parametrize("pid,expected", [
    ("groq", True), ("gemini", True), ("cloudflare", True),
    ("openrouter", False), ("nvidia", False), ("aion", False),
    ("ollama", False), ("vercel", False),
    ("llm7", False), ("kilo", False), ("ovh", False), ("opencode", False),
])
def test_is_listing_checkable_registry_spot(pid, expected):
    assert d.is_listing_checkable(load_registry()[pid]) is expected


@pytest.mark.parametrize("provider,expected", [
    ({"credential_env": "K", "discovery": {"url": "https://x.test/v1",
      "auth": "bearer", "supports_public": False}}, True),
    ({"credential_env": None, "discovery": {"url": "https://x.test/v1",
      "auth": "bearer", "supports_public": False}}, False),
    ({"credential_env": "K", "discovery": {"url": "",
      "auth": "bearer", "supports_public": False}}, False),
    ({"credential_env": "K", "discovery": {"url": "https://x.test/v1",
      "auth": "none", "supports_public": False}}, False),
    ({"credential_env": "K", "discovery": {"url": "https://x.test/v1",
      "auth": "bearer", "supports_public": True}}, False),
    ({"credential_env": "K", "discovery": {}}, False),
    ({"credential_env": "K"}, False),
])
def test_is_listing_checkable_rule(provider, expected):
    assert d.is_listing_checkable(provider) is expected


def test_slot_env_var_naming():
    assert d.slot_env_var("GROQ_API_KEY", 1) == "GROQ_API_KEY"
    assert d.slot_env_var("GROQ_API_KEY", 2) == "GROQ_API_KEY_2"
    assert d.slot_env_var("GROQ_API_KEY", 9) == "GROQ_API_KEY_9"
    for bad in (0, 10, -1, True):
        with pytest.raises(ValueError):
            d.slot_env_var("GROQ_API_KEY", bad)


def test_check_provider_slot_rejects_bad_slot():
    with pytest.raises(ValueError):
        d.check_provider_slot("groq", {}, 0)
    with pytest.raises(ValueError):
        d.check_provider_slot("groq", {}, 10)


def test_check_provider_slot_unknown_provider_is_unsupported(monkeypatch):
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    row = d.check_provider_slot("nope", {}, 1)
    assert row["verdict"] == "unsupported"
    assert row["status"] is None
    assert row["attempted"] is False
    assert row["fix"] is None
    assert calls == []


def test_check_provider_slot_missing_zero_network(monkeypatch):
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing slot must not touch the network")

    install_fake(monkeypatch, explode)
    row = d.check_provider_slot("groq", {}, 1)
    assert row["verdict"] == "missing"
    assert row["status"] is None
    assert row["attempted"] is False
    assert row["slot"] == 1 and row["env_var"] == "GROQ_API_KEY"
    assert row["fix"] is None


def test_check_provider_slot_blank_is_missing(monkeypatch):
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    row = d.check_provider_slot("groq", {"GROQ_API_KEY": ""}, 1)
    assert row["verdict"] == "missing"
    assert row["status"] is None


def test_check_provider_slot_unsupported_zero_network(monkeypatch):
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    row = d.check_provider_slot("openrouter", {"OPENROUTER_API_KEY": "k"}, 1)
    assert row["verdict"] == "unsupported"
    assert row["status"] is None
    assert row["attempted"] is False
    assert row["note"] == "listing check does not authenticate; no key judgment"
    assert row["fix"] is None
    assert "catalog_access" in row and row["catalog_access"] is None
    assert calls == []


def test_check_provider_slot_ok(monkeypatch):
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    row = d.check_provider_slot("groq", slot_env("groq"), 1)
    assert row["verdict"] == "ok"
    assert row["status"] == "ok"
    assert row["attempted"] is True
    assert row["catalog_access"] == "authenticated"
    assert row["fix"] is None
    assert len(calls) == 1


def test_check_provider_slot_sends_suffix_value(monkeypatch):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=ok_body("groq"))

    install_fake(monkeypatch, handler)
    env = {"GROQ_API_KEY": "slot-one", "GROQ_API_KEY_2": "slot-two"}
    row = d.check_provider_slot("groq", env, 2)
    assert row["verdict"] == "ok"
    assert row["env_var"] == "GROQ_API_KEY_2"
    assert seen == ["Bearer slot-two"]
    assert env == {"GROQ_API_KEY": "slot-one", "GROQ_API_KEY_2": "slot-two"}


def test_check_provider_slot_unmapped_status_fails_loud(monkeypatch):
    monkeypatch.setattr(d, "check_provider",
                        lambda pid, env, **_: {"status": "future-status"})
    with pytest.raises(ValueError, match="future-status"):
        d.check_provider_slot("groq", slot_env("groq"), 1)


def _scenario_handler(scenario: str, pid: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "ok":
            return httpx.Response(200, json=ok_body(pid))
        if scenario == "auth_failed":
            return httpx.Response(401, json={})
        if scenario == "rate_limited":
            return httpx.Response(429, json={})
        if scenario == "blocked":
            return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, json={})
        if scenario == "error":
            return httpx.Response(500, json={})
        if scenario == "partial":
            return httpx.Response(200, json=malformed_body(pid))
        raise AssertionError(scenario)
    return handler


@pytest.mark.parametrize("scenario", ["ok", "auth_failed", "rate_limited",
                                      "blocked", "error", "partial"])
@pytest.mark.parametrize("pid", sorted(CHECKABLE))
def test_slot1_logical_equality_with_check_provider(monkeypatch, pid, scenario):
    """Slot 1 equals check_provider modulo verdict mapping + timestamps."""
    install_fake(monkeypatch, _scenario_handler(scenario, pid))
    env = slot_env(pid)
    raw = d.check_provider(pid, dict(env))
    row = d.check_provider_slot(pid, dict(env), 1)
    assert raw["status"] == scenario
    if pid == "cloudflare" and scenario == "auth_failed":
        # H1: joint (token, account ID) auth — the 401 cannot prove the key dead.
        assert row["verdict"] == "denied"
    else:
        assert row["verdict"] == scenario
    assert row["status"] == raw["status"]
    assert row["catalog_access"] == raw.get("catalog_access")
    assert row["attempted"] is True
    assert row["slot"] == 1 and row["env_var"] == base_key(pid)


@pytest.mark.parametrize("account_id", ["", "not-hex", "abc123"])
def test_cloudflare_bad_account_id_is_config_error(monkeypatch, account_id):
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("account-id failure must precede network")

    install_fake(monkeypatch, explode)
    env = {"CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ACCOUNT_ID": account_id}
    row = d.check_provider_slot("cloudflare", env, 1)
    assert row["verdict"] == "config_error"
    assert row["status"] == "auth_missing"
    assert row["attempted"] is True
    assert "CLOUDFLARE_ACCOUNT_ID" in row["note"]
    assert row["fix"] == "set a valid CLOUDFLARE_ACCOUNT_ID"


def test_cloudflare_valid_account_id_passes_through(monkeypatch):
    calls = install_fake(
        monkeypatch, lambda request: httpx.Response(200, json=ok_body("cloudflare")))
    row = d.check_provider_slot("cloudflare", slot_env("cloudflare"), 1)
    assert row["verdict"] == "ok"
    assert len(calls) == 1


def test_classify_denied_keyless_403_is_blocked():
    """Mapping-level: keyless public 403 (no mitigation header) => blocked."""
    status, note = d._classify_denied(
        403, {}, key_env="OPENROUTER_API_KEY", key_present=True,
        authenticated=False, supports_public=True)
    assert status == "blocked"
    assert "keyed listing not attempted" in note


# --- CLI: parser ------------------------------------------------------------


def test_parser_keys_check_registered_with_defaults():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["keys", "check"])
    assert args.func is cli_mod.cmd_keys_check
    assert args.provider is None and args.slot is None
    assert args.json is False and args.timeout == 180.0 and args.strict is False


def test_parser_keys_check_flags():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["keys", "check", "--provider", "groq", "--slot", "2",
                              "--json", "--timeout", "30", "--strict"])
    assert args.provider == "groq" and args.slot == 2
    assert args.json is True and args.timeout == 30.0 and args.strict is True


def test_parser_keys_check_short_provider():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["keys", "check", "-p", "gemini"])
    assert args.provider == "gemini"


def test_parser_keys_check_help_points_at_checklist(capsys):
    with pytest.raises(SystemExit) as caught:
        cli_mod.main(["keys", "--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "checklist" in out
    assert "disjoint from keys checklist" in out


def test_parser_garbage_timeout_exits_2(capsys):
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["keys", "check", "--timeout", "soon"])
    assert caught.value.code == 2


def test_parser_garbage_slot_exits_2(capsys):
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["keys", "check", "--slot", "many"])
    assert caught.value.code == 2


def test_setup_command_exists_for_recovery_line():
    """AC3 guard: the auth_failed fix cites a real command."""
    from freellmpool.managed_cli import cmd_setup

    parser = cli_mod.build_parser()
    args = parser.parse_args(["setup", "--provider", "groq"])
    assert args.func is cmd_setup


# --- CLI: usage errors ------------------------------------------------------


def test_unknown_provider_exit_2_lists_ids(monkeypatch, capsys):
    scrub_env(monkeypatch)
    rc, out, err = run_check(make_args(provider="nope"), capsys)
    assert rc == 2
    assert out == ""
    assert "unknown provider" in err and "groq" in err and "gemini" in err


def test_unknown_provider_exit_2_json_silent(monkeypatch, capsys):
    scrub_env(monkeypatch)
    rc, out, err = run_check(make_args(provider="nope", json=True), capsys)
    assert rc == 2
    assert out == ""
    assert "unknown provider" in err


@pytest.mark.parametrize("slot", [0, 10, -1])
def test_slot_out_of_range_exit_2(monkeypatch, capsys, slot):
    scrub_env(monkeypatch)
    rc, out, err = run_check(make_args(slot=slot), capsys)
    assert rc == 2
    assert out == ""
    assert "--slot" in err


@pytest.mark.parametrize("timeout", [0, -1.5, float("nan"), float("inf")])
def test_bad_timeout_exit_2(monkeypatch, capsys, timeout):
    scrub_env(monkeypatch)
    rc, out, err = run_check(make_args(timeout=timeout), capsys)
    assert rc == 2
    assert out == ""
    assert "--timeout" in err


def test_provider_validated_before_slot(monkeypatch, capsys):
    scrub_env(monkeypatch)
    rc, out, err = run_check(make_args(provider="nope", slot=99), capsys)
    assert rc == 2
    assert "unknown provider" in err


def test_provider_match_case_insensitive_no_label_alias(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(provider="GROQ", json=True), capsys)
    assert rc == 0
    row = json.loads(out)["rows"][0]
    assert row["provider"] == "groq" and row["verdict"] == "ok"
    rc, out, err = run_check(make_args(provider="Google Gemini"), capsys)
    assert rc == 2
    assert "unknown provider" in err


def test_registry_precedence_over_user_catalog(monkeypatch, capsys):
    import freellmpool.config as config_mod

    scrub_env(monkeypatch)
    calls = install_fake(
        monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setattr(config_mod, "load_catalog", lambda path=None: [
        Provider(id="groq", label="Shadow", adapter="openai",
                 base_url="https://shadow.test/v1", models=(), key_env="WRONG_ENV")])
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    row = json.loads(out)["rows"][0]
    assert row["env_var"] == "GROQ_API_KEY" and row["verdict"] == "ok"
    assert len(calls) == 1


# --- CLI: enumeration, filters, slots ---------------------------------------


def test_unfiltered_empty_env_all_missing_or_keyless(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    rc, out, err = run_check(make_args(json=True), capsys)
    assert rc == 0
    envelope = json.loads(out)
    registry = load_registry()
    assert [row["provider"] for row in envelope["rows"]] == list(registry)
    keyed = sum(1 for p in registry.values() if p.get("credential_env"))
    assert envelope["summary"]["checked"] == 0
    assert envelope["summary"]["uncheckable"] == len(registry)
    assert sum(1 for r in envelope["rows"] if r["verdict"] == "missing") == keyed
    assert sum(1 for r in envelope["rows"] if r["verdict"] == "unsupported") == len(registry) - keyed
    assert all(r["status"] is None for r in envelope["rows"])
    assert "checked 0 slots" in err


def test_slot_filter_without_provider_covers_all_providers(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    rc, out, err = run_check(make_args(slot=2, json=True), capsys)
    assert rc == 0
    envelope = json.loads(out)
    assert len(envelope["rows"]) == len(load_registry())
    keyed_rows = [r for r in envelope["rows"] if r["env_var"] is not None]
    assert keyed_rows and all(r["slot"] == 2 for r in keyed_rows)
    assert all(r["verdict"] == "missing" for r in keyed_rows)
    assert all(r["env_var"].endswith("_2") for r in keyed_rows)


def test_provider_and_slot_filter_single_row(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "one")
    monkeypatch.setenv("GROQ_API_KEY_2", "two")
    rc, out, err = run_check(make_args(provider="groq", slot=2, json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "groq" and rows[0]["slot"] == 2
    assert rows[0]["env_var"] == "GROQ_API_KEY_2" and rows[0]["verdict"] == "ok"


def test_suffix_gap_reports_missing_suffix(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setenv("GROQ_API_KEY_3", "three")
    rc, out, err = run_check(make_args(provider="groq", slot=2, json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["verdict"] == "missing" and rows[0]["status"] is None
    assert rows[0]["env_var"] == "GROQ_API_KEY_2"


def test_blank_slot_is_unconfigured(monkeypatch, capsys):
    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setenv("GROQ_API_KEY", "")
    rc, out, err = run_check(make_args(provider="groq", slot=1, json=True), capsys)
    assert rc == 0
    row = json.loads(out)["rows"][0]
    assert row["verdict"] == "missing"
    assert row["note"] == "slot 1 unconfigured (GROQ_API_KEY not set or blank)"
    assert calls == []


def test_config_toml_sourced_key_detected(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[keys]\nGROQ_API_KEY = "cfg-file-key"\n')
    monkeypatch.setenv("FREELLMPOOL_CONFIG_FILE", str(config_path))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer cfg-file-key"
        return httpx.Response(200, json=ok_body("groq"))

    calls = install_fake(monkeypatch, handler)
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    assert json.loads(out)["rows"][0]["verdict"] == "ok"
    assert len(calls) == 1


def test_real_env_wins_over_config_file(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[keys]\nGROQ_API_KEY = "cfg-file-key"\n')
    monkeypatch.setenv("FREELLMPOOL_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("GROQ_API_KEY", "env-wins")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer env-wins"
        return httpx.Response(200, json=ok_body("groq"))

    install_fake(monkeypatch, handler)
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    assert json.loads(out)["rows"][0]["verdict"] == "ok"


# --- CLI: verdicts + exit codes ---------------------------------------------


def _verdict(monkeypatch, capsys, pid, handler, **args):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, handler)
    monkeypatch.setenv(base_key(pid), "probe-key")
    if pid == "cloudflare":
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", VALID_ACCOUNT_ID)
    params = {"provider": pid, "json": True}
    params.update(args)
    rc, out, err = run_check(make_args(**params), capsys)
    return rc, json.loads(out), err


def test_permission_403_is_denied_not_dead(monkeypatch, capsys):
    """018 regression (root probe free-g29-auth-verdict-1151): an authenticated
    403 permission denial is verdict denied/inconclusive/rc0 — never dead-key
    language, never a replace-key fix."""
    rc, envelope, err = _verdict(
        monkeypatch, capsys, "groq",
        lambda request: httpx.Response(403, json={"error": {
            "type": "permission_error", "code": "insufficient_scope",
            "message": "Authenticated credential lacks model-listing permission"}}))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "denied" and row["status"] == "denied"
    assert row["note"] == ("listing denied: often permission scope or account "
                           "verification; key not proven bad")
    assert row["fix"] == ("scope: verify model-listing permission / account verification "
                          "for groq, then: freellmpool keys check --provider groq --slot 1")
    assert "replace:" not in (row["fix"] or "")
    assert envelope["summary"] == {"checked": 1, "ok": 0, "failed": 0,
                                   "inconclusive": 1, "uncheckable": 0}
    assert "inconclusive" in err


def test_denied_fails_strict(monkeypatch, capsys):
    """denied is not proven good: --strict exits 1 on it."""
    rc, envelope, err = _verdict(
        monkeypatch, capsys, "groq",
        lambda request: httpx.Response(403, json={"error": "forbidden"}),
        strict=True)
    assert rc == 1
    assert envelope["rows"][0]["verdict"] == "denied"


def test_slot2_401_while_slot1_ok(monkeypatch, capsys):
    scrub_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer slot-two":
            return httpx.Response(401, json={})
        return httpx.Response(200, json=ok_body("groq"))

    install_fake(monkeypatch, handler)
    monkeypatch.setenv("GROQ_API_KEY", "slot-one")
    monkeypatch.setenv("GROQ_API_KEY_2", "slot-two")
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 1
    envelope = json.loads(out)
    assert [(r["slot"], r["verdict"]) for r in envelope["rows"]] == [(1, "ok"), (2, "auth_failed")]
    dead = envelope["rows"][1]
    assert dead["status"] == "auth_failed"
    assert "keys add groq --slot 2" in dead["fix"]
    assert "setup --provider groq" in dead["fix"]
    assert envelope["summary"] == {"checked": 2, "ok": 1, "failed": 1,
                                   "inconclusive": 0, "uncheckable": 0}


def test_all_slots_dead_exit_1(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(401, json={}))
    monkeypatch.setenv("GROQ_API_KEY", "one")
    monkeypatch.setenv("GROQ_API_KEY_2", "two")
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 1
    envelope = json.loads(out)
    assert [r["verdict"] for r in envelope["rows"]] == ["auth_failed", "auth_failed"]
    assert envelope["summary"]["failed"] == 2


def test_rate_limited_exit_0_with_retry_fix(monkeypatch, capsys):
    rc, envelope, err = _verdict(monkeypatch, capsys, "groq",
                                 lambda request: httpx.Response(429, json={}))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "rate_limited" and row["status"] == "rate_limited"
    assert row["fix"] == "retry: freellmpool keys check --provider groq --slot 1"
    assert envelope["summary"]["inconclusive"] == 1
    assert "inconclusive" in err


def test_blocked_via_mitigation_header(monkeypatch, capsys):
    rc, envelope, err = _verdict(
        monkeypatch, capsys, "groq",
        lambda request: httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, json={}))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "blocked" and row["status"] == "blocked"
    assert "NOT judged" in row["note"]
    assert row["fix"].startswith("retry: freellmpool keys check")


def test_partial_via_malformed_catalog(monkeypatch, capsys):
    rc, envelope, err = _verdict(monkeypatch, capsys, "groq",
                                 lambda request: httpx.Response(200, json={"data": "nope"}))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "partial" and row["status"] == "partial"
    assert "NOT proven" in row["note"]
    assert envelope["summary"]["inconclusive"] == 1
    rc, _, _ = _verdict(monkeypatch, capsys, "groq",
                        lambda request: httpx.Response(200, json={"data": "nope"}),
                        strict=True)
    assert rc == 1


def test_error_verdict_is_transport_labeled(monkeypatch, capsys):
    rc, envelope, err = _verdict(monkeypatch, capsys, "groq",
                                 lambda request: httpx.Response(500, json={}))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "error" and row["status"] == "error"
    assert "transport/HTTP" in row["note"]
    assert row["fix"].startswith("retry:")


def test_deferred_via_stalled_page(monkeypatch, capsys):
    scrub_env(monkeypatch)
    monkeypatch.setattr(d, "_WIZARD_CHECK_SECONDS", 1.0)
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")

    class StallStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(5.0)
            yield b"{}"

        async def aclose(self):
            pass

    install_fake(monkeypatch, lambda request: httpx.Response(200, stream=StallStream()))
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    row = json.loads(out)["rows"][0]
    assert row["verdict"] == "deferred" and row["status"] == "deferred"
    assert row["fix"].startswith("retry:")


def test_timeout_rows_never_attempted(monkeypatch, capsys):
    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "one")
    monkeypatch.setenv("GROQ_API_KEY_2", "two")
    rc, out, err = run_check(make_args(provider="groq", timeout=0.5, json=True), capsys)
    assert rc == 0
    envelope = json.loads(out)
    assert [r["verdict"] for r in envelope["rows"]] == ["timeout", "timeout"]
    assert all(r["status"] is None for r in envelope["rows"])
    assert all(r["fix"].startswith("retry:") for r in envelope["rows"])
    assert envelope["summary"]["checked"] == 0
    assert "keys check [" not in err
    assert calls == []


@pytest.mark.parametrize("scenario,default_rc,strict_rc", [
    ("ok", 0, 0),
    ("auth_failed", 1, 1),
    ("rate_limited", 0, 1),
    ("blocked", 0, 1),
    ("error", 0, 1),
    ("partial", 0, 1),
])
def test_strict_flip_matrix(monkeypatch, capsys, scenario, default_rc, strict_rc):
    rc, _, _ = _verdict(monkeypatch, capsys, "groq", _scenario_handler(scenario, "groq"))
    assert rc == default_rc
    rc, _, _ = _verdict(monkeypatch, capsys, "groq", _scenario_handler(scenario, "groq"),
                        strict=True)
    assert rc == strict_rc


def test_strict_all_ok_exit_0(monkeypatch, capsys):
    rc, _, _ = _verdict(monkeypatch, capsys, "groq",
                        lambda request: httpx.Response(200, json=ok_body("groq")),
                        strict=True)
    assert rc == 0


def test_strict_fails_when_nothing_checked(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    rc, out, err = run_check(make_args(provider="groq", strict=True, json=True), capsys)
    assert rc == 1
    assert json.loads(out)["summary"]["checked"] == 0


def test_strict_allows_uncheckable_rows(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(strict=True, json=True), capsys)
    assert rc == 0
    verdicts = {r["verdict"] for r in json.loads(out)["rows"]}
    assert verdicts <= {"ok", "unsupported", "missing"}


# --- CLI: uncheckable providers ------------------------------------------------


def test_keyless_provider_single_unsupported_row(monkeypatch, capsys):
    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    rc, out, err = run_check(make_args(provider="kilo", json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "kilo" and rows[0]["slot"] == 1
    assert rows[0]["env_var"] is None and rows[0]["verdict"] == "unsupported"
    assert rows[0]["status"] is None and rows[0]["fix"] is None
    assert calls == []


def test_public_keyed_provider_unsupported_zero_network(monkeypatch, capsys):
    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    rc, out, err = run_check(make_args(provider="openrouter", json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert [(r["slot"], r["verdict"]) for r in rows] == [(1, "unsupported"), (2, "unsupported")]
    assert all(r["status"] is None for r in rows)
    assert calls == []


def _user_provider(pid="customco", key_env="CUSTOMCO_API_KEY"):
    return Provider(id=pid, label="Custom Co", adapter="openai",
                    base_url="https://customco.test/v1", models=(), key_env=key_env)


def test_user_catalog_provider_never_probed(monkeypatch, capsys):
    import freellmpool.config as config_mod

    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(config_mod, "load_catalog", lambda path=None: [_user_provider()])
    monkeypatch.setenv("CUSTOMCO_API_KEY", "k")
    rc, out, err = run_check(make_args(provider="CustomCo", json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "customco" and rows[0]["verdict"] == "unsupported"
    assert rows[0]["env_var"] == "CUSTOMCO_API_KEY" and rows[0]["status"] is None
    assert calls == []
    monkeypatch.delenv("CUSTOMCO_API_KEY")
    rc, out, err = run_check(make_args(provider="customco", json=True), capsys)
    assert rc == 0
    assert json.loads(out)["rows"][0]["verdict"] == "missing"


def test_blank_key_env_treated_as_keyless(monkeypatch, capsys):
    """key_env == '' behaves like None (house: falsy means absent)."""
    import freellmpool.config as config_mod

    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(config_mod, "load_catalog",
                        lambda path=None: [_user_provider(key_env="")])
    rc, out, err = run_check(make_args(provider="customco", json=True), capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["verdict"] == "unsupported" and rows[0]["env_var"] is None
    assert calls == []


def test_external_provider_never_probed(monkeypatch, capsys):
    import freellmpool.catalog as catalog_mod

    scrub_env(monkeypatch)
    calls = install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    external = ExternalProvider(name="Free LLM Hub", slug="free-llm-hub", category=None,
                                url=None, base_url=None, description="", model_count=3,
                                best_rpd=0, best_rpm=0, best_tpd=0, generous_score=0)
    monkeypatch.setattr(catalog_mod, "load_external_catalog", lambda path=None: [external])
    for needle in ("free-llm-hub", "FREE-LLM-HUB", "Free LLM Hub"):
        rc, out, err = run_check(make_args(provider=needle, json=True), capsys)
        assert rc == 0
        rows = json.loads(out)["rows"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "free-llm-hub" and rows[0]["verdict"] == "unsupported"
        assert rows[0]["slot"] == 1 and rows[0]["env_var"] is None
        assert rows[0]["status"] is None
    assert calls == []


# --- CLI: config_error rows ----------------------------------------------------


@pytest.mark.parametrize("broken", ["empty", "raising"])
def test_empty_registry_single_star_row(monkeypatch, capsys, broken):
    import freellmpool.provider_registry as registry_mod

    scrub_env(monkeypatch)
    if broken == "empty":
        monkeypatch.setattr(registry_mod, "load_registry", lambda *a, **k: {})
    else:
        def explode(*args, **kwargs):
            raise ValueError("policy bundle corrupt")

        monkeypatch.setattr(registry_mod, "load_registry", explode)
    rc, out, err = run_check(make_args(json=True), capsys)
    assert rc == 1
    envelope = json.loads(out)
    assert len(envelope["rows"]) == 1
    row = envelope["rows"][0]
    assert row["provider"] == "*" and row["slot"] is None and row["env_var"] is None
    assert row["verdict"] == "config_error" and row["status"] is None
    assert row["fix"] == "freellmpool update --renew-evidence"
    assert envelope["summary"]["failed"] == 1


def test_cloudflare_account_id_cli_exit_1(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    rc, out, err = run_check(make_args(provider="cloudflare", json=True), capsys)
    assert rc == 1
    row = json.loads(out)["rows"][0]
    assert row["verdict"] == "config_error" and row["status"] == "auth_missing"
    assert "CLOUDFLARE_ACCOUNT_ID" in row["note"]


def test_wrapper_exception_becomes_config_error(monkeypatch, capsys):
    scrub_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")

    def explode(pid, env, **_):
        raise RuntimeError("wrapped boom sk-abcdefgh12345678")

    monkeypatch.setattr(d, "check_provider", explode)
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 1
    row = json.loads(out)["rows"][0]
    assert row["verdict"] == "config_error" and row["status"] is None
    assert "RuntimeError" in row["note"]
    assert "sk-abcdefgh12345678" not in out and "[redacted]" in row["note"]
    assert "Traceback" not in err


# --- CLI: output contracts -----------------------------------------------------


def test_json_envelope_contract_and_purity(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    envelope = json.loads(out)
    assert envelope["version"] == 1
    assert set(envelope["summary"]) == {"checked", "ok", "failed", "inconclusive", "uncheckable"}
    assert len(envelope["rows"]) == 1
    assert set(envelope["rows"][0]) == {"provider", "slot", "env_var", "verdict",
                                        "status", "note", "fix"}
    assert "catalog_access" not in out
    assert "source_url" not in out
    assert "keys check [1/1]" in err


def test_human_table_columns_fix_line_and_footer(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(401, json={}))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(provider="groq"), capsys)
    assert rc == 1
    lines = out.splitlines()
    assert lines[0].split() == ["PROVIDER", "SLOT", "ENV_VAR", "VERDICT", "NOTE"]
    assert any(line.startswith("  fix: ") and "keys add groq" in line for line in lines)
    assert lines[-1] == "keys check: checked=1 ok=0 failed=1 inconclusive=0 uncheckable=0"


def test_human_footer_always_present_on_empty_scopes(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    rc, out, err = run_check(make_args(provider="groq"), capsys)
    assert rc == 0
    assert out.splitlines()[-1].startswith("keys check: checked=0")


def test_progress_lines_byte_exact_per_network_attempt(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "one")
    monkeypatch.setenv("GROQ_API_KEY_2", "two")
    rc, out, err = run_check(make_args(provider="groq"), capsys)
    assert rc == 0
    assert ("freellmpool: keys check [1/2] groq slot 1\n"
            "freellmpool: keys check [2/2] groq slot 2\n") in err


def test_zero_cost_rows_emit_no_progress(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json={}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")
    rc, out, err = run_check(make_args(provider="openrouter"), capsys)
    assert rc == 0
    assert "keys check [" not in err


def test_never_reads_stdin(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")

    def explode(prompt=""):
        raise AssertionError("must not read stdin")

    monkeypatch.setattr("builtins.input", explode)
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0


# --- secrecy + zero-inference --------------------------------------------------


@pytest.mark.parametrize("as_json", [False, True])
def test_unprefixed_canary_never_emitted(monkeypatch, capsys, as_json):
    scrub_env(monkeypatch)
    canary = "canary" + secrets.token_hex(16)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", canary)
    monkeypatch.setenv("GROQ_API_KEY_2", canary)
    rc, out, err = run_check(make_args(provider="groq", json=as_json), capsys)
    assert rc == 0
    assert canary not in out and canary not in err


def test_redact_secrets_unit_sk_and_bearer_shapes():
    assert redact_secrets("leak sk-abcdefgh12345678 done") == "leak [redacted] done"
    assert "[redacted]" in redact_secrets("Authorization: Bearer abcdefgh12345678")
    assert redact_secrets("plain note, no secrets") == "plain note, no secrets"


def _cf_routed_handler(*, listing, probe_a, probe_b):
    """Route listing + verify URLs to per-endpoint canned (status, json)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ai/models/search"):
            status, payload = listing
        elif path.endswith("/user/tokens/verify"):
            status, payload = probe_b
        elif path.endswith("/tokens/verify"):
            status, payload = probe_a
        else:
            raise AssertionError(f"unexpected URL {request.url}")
        return httpx.Response(status, json=payload)
    return handler


def test_cloudflare_401_without_probes_is_denied_not_dead(monkeypatch, capsys):
    """H1 legacy path: without a probe cache, a Cloudflare 401 jointly
    authenticates (token, account ID), so it cannot isolate a bad token from
    a wrong account ID. Verdict denied (inconclusive, scope-family fix) —
    never auth_failed, never replace-key."""
    install_fake(monkeypatch, _scenario_handler("auth_failed", "cloudflare"))
    row = d.check_provider_slot("cloudflare", slot_env("cloudflare"), 1)
    assert row["verdict"] == "denied"
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401 does not isolate a bad token from a wrong "
                           "CLOUDFLARE_ACCOUNT_ID; key NOT proven bad")
    assert row["fix"] == ("scope: re-verify CLOUDFLARE_ACCOUNT_ID for cloudflare (a wrong "
                          "account ID fails auth with a valid token), then: freellmpool keys "
                          "check --provider cloudflare --slot 1")
    assert row["attempted"] is True


def test_cloudflare_dual_401_is_dead_and_fails(monkeypatch, capsys):
    """G32 T8 (intended behavior change): when both verifiers reject the
    token, the CF row is auth_failed/failed and the run exits 1. The
    all-401 fake answers 401 to the probes too, so dual-401 is honest."""
    rc, envelope, _ = _verdict(monkeypatch, capsys, "cloudflare",
                               _scenario_handler("auth_failed", "cloudflare"))
    assert rc == 1
    assert envelope["summary"] == {"checked": 1, "ok": 0, "failed": 1,
                                   "inconclusive": 0, "uncheckable": 0}
    row = envelope["rows"][0]
    assert row["verdict"] == "auth_failed"
    assert row["status"] == "auth_failed"
    assert "both" in row["note"] and "agree" in row["note"]
    assert set(row) == {"provider", "slot", "env_var", "verdict", "status",
                        "note", "fix"}  # no new JSON keys (SCOPE#7)
    rc, _, _ = _verdict(monkeypatch, capsys, "cloudflare",
                        _scenario_handler("auth_failed", "cloudflare"), strict=True)
    assert rc == 1


def test_cloudflare_inconclusive_probes_stay_rc0(monkeypatch, capsys):
    """G32 T8: ambiguous probes fail closed to denied/inconclusive/rc0."""
    handler = _cf_routed_handler(listing=(401, {}), probe_a=(500, {}),
                                 probe_b=(200, {"success": True,
                                                "result": {"status": "active"}}))
    rc, envelope, err = _verdict(monkeypatch, capsys, "cloudflare", handler)
    assert rc == 0
    assert envelope["summary"] == {"checked": 1, "ok": 0, "failed": 0,
                                   "inconclusive": 1, "uncheckable": 0}
    assert "verify scope with the provider(s)" in err
    rc, _, _ = _verdict(monkeypatch, capsys, "cloudflare", handler, strict=True)
    assert rc == 1  # --strict fails closed on inconclusive rows


def test_non_cloudflare_auth_failed_note_unchanged(monkeypatch):
    install_fake(monkeypatch, _scenario_handler("auth_failed", "groq"))
    row = d.check_provider_slot("groq", slot_env("groq"), 1)
    assert row["verdict"] == "auth_failed"
    assert row["note"] == d.KEYS_CHECK_AUTH_FAILED_NOTE


def test_internal_error_note_value_redacts_unprefixed_key(monkeypatch, capsys):
    """M4: the per-slot exception note is the only dynamic keys-check string;
    an unprefixed key echoed in str(exc) must be value-redacted, not leaked."""
    scrub_env(monkeypatch)
    key = f"probe-unprefixed-{secrets.token_hex(8)}"
    monkeypatch.setenv("GROQ_API_KEY", key)

    def boom(pid, env, **_):
        raise ValueError(f"synthetic failure echoing {key}")

    monkeypatch.setattr(d, "check_provider", boom)
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 1
    assert key not in out and key not in err
    assert "[redacted]" in out
    assert json.loads(out)["rows"][0]["verdict"] == "config_error"


def test_emit_buckets_partition_rows_loudly():
    """L3: every verdict lands in exactly one summary bucket; an unknown
    future verdict fails loudly instead of silently unbalancing the summary."""
    rows = [{"provider": "x", "slot": 1, "env_var": "X", "verdict": "future",
             "status": "future", "note": "n", "fix": None, "attempted": True}]
    with pytest.raises(AssertionError):
        cli_mod._keys_check_emit(make_args(), rows)


def test_keys_check_writes_no_state_and_touches_no_rotation(monkeypatch, capsys, tmp_path):
    """L2: a check run leaves discovery/accounts bytes identical and never
    instantiates the rotation cursor (in-memory per-object state)."""
    import freellmpool.key_rotation as rotation_mod

    scrub_env(monkeypatch)
    discovery = tmp_path / "discovery.json"
    discovery.write_text(json.dumps({"schema": 1, "providers": {}}))
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"providers": {}}))
    monkeypatch.setenv("FREELLMPOOL_DISCOVERY_FILE", str(discovery))
    monkeypatch.setenv("FREELLMPOOL_ACCOUNTS_FILE", str(accounts))
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")

    def no_rotation(self, *args, **kwargs):
        pytest.fail("keys check must not instantiate KeyRotator")

    monkeypatch.setattr(rotation_mod.KeyRotator, "__init__", no_rotation)
    rc, _, _ = run_check(make_args(provider="groq", json=True), capsys)
    assert rc == 0
    assert discovery.read_text() == json.dumps({"schema": 1, "providers": {}})
    assert accounts.read_text() == json.dumps({"providers": {}})


def test_zero_inference_ledger_logical_equality(monkeypatch, capsys):
    scrub_env(monkeypatch)
    install_fake(monkeypatch, lambda request: httpx.Response(200, json=ok_body("groq")))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    ledger = AllowanceLedger()
    before = (ledger.summary(), ledger.export())
    rc, out, err = run_check(make_args(provider="groq", json=True), capsys)
    after = (ledger.summary(), ledger.export())
    assert rc == 0
    assert before == after


def test_probe_run_ledger_logical_equality(monkeypatch, capsys):
    """G32 T9: verify probes are read-only GETs — a CF-401 probe run moves
    neither the allowance ledger nor any cursor/snapshot state."""
    scrub_env(monkeypatch)
    install_fake(monkeypatch, _cf_routed_handler(
        listing=(401, {}), probe_a=(401, {}),
        probe_b=(200, {"success": True, "result": {"status": "active"}})))
    monkeypatch.setenv(base_key("cloudflare"), "probe-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", VALID_ACCOUNT_ID)
    ledger = AllowanceLedger()
    before = (ledger.summary(), ledger.export())
    rc, out, err = run_check(make_args(provider="cloudflare", json=True), capsys)
    after = (ledger.summary(), ledger.export())
    assert before == after
    assert json.loads(out)["rows"][0]["verdict"] == "config_error"
