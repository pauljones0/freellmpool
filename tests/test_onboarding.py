"""Setup stores secrets privately and never treats a listing as free admission."""

import json
import tomllib

import pytest

from freellmpool.onboarding import read_clipboard, run_onboarding, save_key_values


def _registry():
    def row(name, kind="recurring_quota"):
        return {"id": name, "display_name": name.title(), "credential_env": name.upper() + "_KEY", "grants": [{"kind": kind, "status": "verified"}], "setup": {"signup_url": "https://example.test/signup", "key_url": "https://example.test/keys", "steps": ["Choose the Free plan.", "Create an inference-only key."], "required_env": [name.upper() + "_KEY"]}}
    return {"alpha": row("alpha"), "beta": row("beta"), "paid": row("paid", "paid")}


def test_skip_resume_private_entry_and_no_paid_or_completion_check(tmp_path):
    progress = tmp_path / "progress.json"
    config = tmp_path / "config.toml"
    env = {"FREELLMPOOL_CONFIG_FILE": str(config)}
    output = []
    calls = []
    def check(provider, credentials):
        calls.append(provider)
        assert credentials[provider.upper() + "_KEY"] == "synthetic-private-key"
        return {"status": "ok", "model_count": 3, "note": "Authenticated listing checked."}
    answers = iter(["", "", "q"])
    assert run_onboarding(registry=_registry(), env=env, progress_path=progress, input_fn=lambda _: next(answers), secret_fn=lambda _: "synthetic-private-key", check=check, output=output.append) == 1
    assert calls == ["alpha"]
    assert "synthetic-private-key" not in "\n".join(output) + progress.read_text()
    assert tomllib.loads(config.read_text())["keys"]["ALPHA_KEY"] == "synthetic-private-key"
    assert config.stat().st_mode & 0o777 == 0o600
    answers = iter(["", ""])
    assert run_onboarding(registry=_registry(), env=env, progress_path=progress, input_fn=lambda _: next(answers), secret_fn=lambda _: "synthetic-private-key", check=check, output=output.append) == 0
    assert calls == ["alpha", "beta"]
    assert "paid" not in calls
    assert "not proof of a free allowance" in "\n".join(output)


def test_failed_auth_is_saved_and_resumable_without_reentering_key(tmp_path):
    config = tmp_path / "config.toml"
    progress = tmp_path / "progress.json"
    env = {"FREELLMPOOL_CONFIG_FILE": str(config)}
    answers = iter(["", "", ""])
    kwargs = dict(provider="alpha", registry=_registry(), env=env, progress_path=progress, output=lambda _: None)
    run_onboarding(**kwargs, input_fn=lambda _: next(answers), secret_fn=lambda _: "private-value", check=lambda *_: {"status": "auth_failed"})
    run_onboarding(**kwargs, input_fn=lambda _: "", secret_fn=lambda _: pytest.fail("saved key should be reused"), check=lambda *_: {"status": "ok"})
    assert json.loads(progress.read_text())["providers"]["alpha"]["status"] == "checked"


@pytest.mark.parametrize("status", ["unsupported", "partial", "auth_missing", "auth_failed", "error",
                                   "denied"])
def test_discovery_outcomes_do_not_enable_routes_or_leak_keys(tmp_path, status):
    output = []
    answers = iter(["", "", ""])
    path = tmp_path / "progress.json"
    run_onboarding(provider="alpha", registry=_registry(), env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml")}, progress_path=path, input_fn=lambda _: next(answers), secret_fn=lambda _: "private-value", check=lambda *_: {"status": status, "note": "private-value\x1b[31m"}, output=output.append)
    assert "private-value" not in "\n".join(output) + path.read_text()
    assert "\x1b" not in "\n".join(output)
    assert json.loads(path.read_text())["providers"]["alpha"]["status"] == status


def test_key_writer_preserves_nested_existing_configuration_and_rejects_corruption(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[settings]\nnames = ["one", "two"]\n[settings.nested]\nflag = true\n[keys]\nOLD_KEY = "old"\n')
    before = tomllib.loads(path.read_text())
    save_key_values({"NEW_KEY": "private-value"}, path)
    after = tomllib.loads(path.read_text())
    assert after["settings"] == before["settings"]
    assert after["keys"] == {"OLD_KEY": "old", "NEW_KEY": "private-value"}
    path.write_text("[broken")
    with pytest.raises(ValueError, match="invalid"):
        save_key_values({"NEW_KEY": "private-value"}, path)
    assert path.read_text() == "[broken"


@pytest.mark.parametrize("key", ["", "two\nlines", "bad\x1bkey"])
def test_invalid_secret_never_replaces_config(tmp_path, key):
    path = tmp_path / "config.toml"
    with pytest.raises(ValueError):
        save_key_values({"ALPHA_KEY": key}, path)
    assert not path.exists()


def test_clipboard_uses_argument_vector_and_never_shell(monkeypatch):
    monkeypatch.setattr("freellmpool.onboarding.shutil.which", lambda name: "/usr/bin/" + name if name == "wl-paste" else None)
    def run(command, **kwargs):
        assert command == ["wl-paste", "--no-newline"]
        assert "shell" not in kwargs
        return type("Result", (), {"stdout": "private-value"})()
    monkeypatch.setattr("freellmpool.onboarding.subprocess.run", run)
    assert read_clipboard() == "private-value"


def test_keyless_setup_does_not_request_optional_provider_credential(tmp_path):
    registry = _registry()
    registry["alpha"]["setup"]["required_env"] = []
    run_onboarding(provider="alpha", registry=registry, env={}, progress_path=tmp_path / "progress.json", input_fn=lambda _: "", secret_fn=lambda _: pytest.fail("anonymous route must not require a key"), check=lambda *_: {"status": "ok"}, output=lambda _: None)


@pytest.mark.parametrize("confirmation", ["CONFIRM", ""])
def test_conditional_account_flags_require_explicit_combined_confirmation(tmp_path, confirmation):
    registry = _registry()
    conditions = {"plan": "free_starter", "paid_balance_zero": True, "automatic_usage_billing": False}
    registry["alpha"]["grants"][0].update({"requires_account_evidence": True, "required_account_tier": "free", "required_account_conditions": conditions})
    saved = []
    answers = iter(["", "", confirmation])
    run_onboarding(provider="alpha", registry=registry, env={"ALPHA_KEY": "already-saved", "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json")}, progress_path=tmp_path / "progress.json", input_fn=lambda _: next(answers), account_saver=lambda *args: saved.append(args), check=lambda *_: {"status": "ok"}, output=lambda _: None)
    if confirmation:
        assert {key: saved[0][1][key] for key in conditions} == conditions
    else:
        assert not saved
        assert json.loads((tmp_path / "progress.json").read_text())["providers"]["alpha"]["status"] == "account_unverified"


def test_tier_attestation_is_bound_to_the_key_being_checked(tmp_path):
    from freellmpool.free_policy import credential_fingerprint
    registry = _registry()
    registry["alpha"]["grants"][0].update(requires_account_evidence=True, required_account_tier="free")
    saved = []
    answers = iter(["", "", ""])
    run_onboarding(provider="alpha", registry=registry,
                   env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml")},
                   progress_path=tmp_path / "progress.json", input_fn=lambda _: next(answers),
                   secret_fn=lambda _: "new-private-key", account_saver=lambda *args: saved.append(args),
                   check=lambda *_: {"status": "ok"}, output=lambda _: None)
    assert saved[0][1]["credential_ref"] == credential_fingerprint("alpha", "new-private-key")
    assert "new-private-key" not in json.dumps(saved[0][1])


@pytest.mark.parametrize("change", ["key", "age", "account"])
def test_resume_rechecks_changed_key_expired_check_or_expired_account(tmp_path, change):
    from datetime import UTC, datetime, timedelta

    from freellmpool.free_policy import save_account

    registry = {"alpha": _registry()["alpha"]}
    registry["alpha"]["grants"][0].update(requires_account_evidence=True, required_account_tier="free")
    env = {"ALPHA_KEY": "first-key", "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json")}
    progress = tmp_path / "progress.json"
    calls = []
    kwargs = dict(registry=registry, env=env, progress_path=progress, input_fn=lambda _: "", check=lambda *_: calls.append(True) or {"status": "ok"}, output=lambda _: None)
    assert run_onboarding(**kwargs) == 0
    if change == "key":
        env["ALPHA_KEY"] = "replacement-key"
    elif change == "age":
        state = json.loads(progress.read_text())
        state["providers"]["alpha"]["updated_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        progress.write_text(json.dumps(state))
    else:
        save_account("alpha", {"tier": "free", "verified_at": "2020-01-01T00:00:00Z", "expires_at": "2020-02-01T00:00:00Z"}, env)
    assert run_onboarding(**kwargs) == 0
    assert len(calls) == 2


def test_failed_existing_key_can_be_replaced_and_checked_in_same_wizard(tmp_path):
    env = {"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml")}
    save_key_values({"ALPHA_KEY": "bad-old-key"}, tmp_path / "config.toml")
    answers = iter(["", "k", "c"])
    checked = []
    def check(_provider, credentials):
        checked.append(credentials["ALPHA_KEY"])
        return {"status": "auth_failed" if len(checked) == 1 else "ok"}
    assert run_onboarding(provider="alpha", registry=_registry(), env=env, progress_path=tmp_path / "progress.json", input_fn=lambda _: next(answers), clipboard_fn=lambda: "working-new-key", check=check, output=lambda _: None) == 0
    assert checked == ["bad-old-key", "working-new-key"]
    assert tomllib.loads((tmp_path / "config.toml").read_text())["keys"]["ALPHA_KEY"] == "working-new-key"


def test_wizard_opens_exact_provider_key_page_and_then_continues(tmp_path):
    opened = []
    answers = iter(["o", "", ""])
    assert run_onboarding(provider="alpha", registry=_registry(), env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml")}, progress_path=tmp_path / "progress.json", input_fn=lambda _: next(answers), secret_fn=lambda _: "private-key", check=lambda *_: {"status": "ok"}, output=lambda _: None, open_url=opened.append) == 0
    assert opened == ["https://example.test/keys"]


AUTH_FAILED_TEXT = ("Authentication was rejected (HTTP 401). The credential did not "
                    "authenticate; check the key before continuing.")
CLOUDFLARE_AUTH_FAILED_HINT = ("Cloudflare note: a wrong CLOUDFLARE_ACCOUNT_ID is rejected here "
                               "with a valid token — re-verify the account ID before replacing the key.")


def test_auth_failed_text_is_authentication_only_with_cloudflare_hint(tmp_path):
    """019: auth_failed (401) no longer claims a permission verdict; Cloudflare
    rows warn that a wrong account ID surfaces here with a valid token."""
    from freellmpool.onboarding import _STATUS_TEXT

    assert _STATUS_TEXT["auth_failed"] == AUTH_FAILED_TEXT
    assert "permission" not in _STATUS_TEXT["auth_failed"].lower()

    def flow(provider_id):
        registry = _registry()
        registry[provider_id] = {"id": provider_id, "display_name": provider_id.title(),
                                 "credential_env": provider_id.upper() + "_KEY",
                                 "grants": [{"kind": "recurring_quota", "status": "verified"}],
                                 "setup": {"signup_url": "https://example.test/signup",
                                           "key_url": "https://example.test/keys",
                                           "steps": ["Choose the Free plan."],
                                           "required_env": [provider_id.upper() + "_KEY"]}}
        output = []
        run_onboarding(provider=provider_id, registry=registry,
                       env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / f"{provider_id}.toml")},
                       progress_path=tmp_path / f"{provider_id}.json",
                       input_fn=lambda _: "", secret_fn=lambda _: "private-value",
                       check=lambda *_: {"status": "auth_failed", "note": "diagnostic-note"},
                       output=output.append)
        return output

    cloudflare_out = flow("cloudflare")
    assert CLOUDFLARE_AUTH_FAILED_HINT in cloudflare_out
    # M2: guidance reads after the diagnostic it explains, not before it.
    assert cloudflare_out.index("diagnostic-note") < cloudflare_out.index(
        CLOUDFLARE_AUTH_FAILED_HINT)
    assert CLOUDFLARE_AUTH_FAILED_HINT not in flow("alpha")


@pytest.mark.parametrize("eligible", [False, True])
def test_public_catalog_does_not_claim_key_was_validated(tmp_path, eligible):
    output = []
    run_onboarding(provider="alpha", registry=_registry(), env={"ALPHA_KEY": "unvalidated-key"}, progress_path=tmp_path / "progress.json", input_fn=lambda _: "", check=lambda *_: {"status": "ok", "note": "Public listing checked; API key validity and free eligibility remain unverified."}, eligibility=lambda _: eligible, output=output.append)
    rendered = "\n".join(output)
    assert "Key accepted" not in rendered
    assert "Free access is ready" not in rendered
    assert "API key validity" in rendered


def _cf_flow(tmp_path, result, prompts):
    """Run the Cloudflare wizard leg with a canned check result."""
    registry = _registry()
    registry["cloudflare"] = {"id": "cloudflare", "display_name": "Cloudflare",
                              "credential_env": "CLOUDFLARE_KEY",
                              "grants": [{"kind": "recurring_quota", "status": "verified"}],
                              "setup": {"signup_url": "https://example.test/signup",
                                        "key_url": "https://example.test/keys",
                                        "steps": ["Choose the Free plan."],
                                        "required_env": ["CLOUDFLARE_KEY"]}}
    output = []

    def ask(prompt):
        prompts.append(prompt)
        return ""

    run_onboarding(provider="cloudflare", registry=registry,
                   env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "cf.toml")},
                   progress_path=tmp_path / "cf.json", input_fn=ask,
                   secret_fn=lambda _: "private-value",
                   check=lambda *_: result, output=output.append)
    return output


@pytest.mark.parametrize(("outcome", "note"), [
    ("pair_ok", "Cloudflare note: token and account ID are verified — "
                "the listing refusal is a scope or account-verification "
                "issue, not a bad key."),
    ("wrong_account", "Cloudflare note: the token is valid but rejected for this "
                      "account — re-verify CLOUDFLARE_ACCOUNT_ID (or grant the "
                      "token account access)."),
    ("token_dead", "Cloudflare note: both account and user verifiers reject "
                   "this token (both agree: dead) — replace the key."),
    ("token_expired", "Cloudflare note: the token is expired (verify reports "
                      "status=expired) — replace the key."),
])
def test_wizard_probe_outcome_prints_own_note_and_suppresses_m2(tmp_path, outcome, note):
    """G32 T7: definitive probe outcomes get their own note; the generic
    019/M2 account-ID warning is suppressed (COMP gap 4)."""
    prompts: list = []
    output = _cf_flow(tmp_path, {"status": "auth_failed", "note": "diagnostic-note",
                                 "cloudflare_probe": outcome}, prompts)
    assert note in output
    assert CLOUDFLARE_AUTH_FAILED_HINT not in output


@pytest.mark.parametrize("outcome", [None, "inconclusive", "inconclusive_retry",
                                     "bogus-future-token"])
def test_wizard_inconclusive_or_unknown_outcome_keeps_m2(tmp_path, outcome):
    """G32 T7: absent/inconclusive/unknown outcomes fail closed to the M2 note."""
    prompts: list = []
    result: dict = {"status": "auth_failed", "note": "diagnostic-note"}
    if outcome is not None:
        result["cloudflare_probe"] = outcome
    output = _cf_flow(tmp_path, result, prompts)
    assert CLOUDFLARE_AUTH_FAILED_HINT in output


def test_wizard_pair_ok_breaks_without_replace_key_offer(tmp_path):
    """G32 T7: a verified pair never offers replace-key (COMP#3 row-1 fix)."""
    prompts: list = []
    _cf_flow(tmp_path, {"status": "auth_failed", "note": "diagnostic-note",
                        "cloudflare_probe": "pair_ok"}, prompts)
    assert not [p for p in prompts if "k=replace key" in p]


def test_wizard_token_dead_still_offers_replace_key(tmp_path):
    """G32 T7: a dead token keeps the replace-key offer (it re-collects)."""
    prompts: list = []
    _cf_flow(tmp_path, {"status": "auth_failed", "note": "diagnostic-note",
                        "cloudflare_probe": "token_dead"}, prompts)
    assert [p for p in prompts if "k=replace key" in p]
