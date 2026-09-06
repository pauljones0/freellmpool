"""Setup repairs only invalid metadata and follows the active reviewed policy."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool import onboarding as o
from freellmpool.free_policy import admit, credential_fingerprint, load_accounts


def entry(tiers="free", **conditions):
    now = datetime.now(UTC)
    return {"id": "alpha", "credential_env": "ALPHA_KEY", "setup": {"required_env": ["ALPHA_KEY"]},
            "grants": [{"id": "free", "status": "verified", "kind": "recurring_quota", "hard_free_boundary": True,
                        "allowed_modalities": ["chat"], "model_selector": {"kind": "all"}, "evidence_ids": ["terms"],
                        "requires_account_evidence": True, "required_account_tier": tiers,
                        "required_account_conditions": conditions}],
            "evidence": [{"id": "terms", "status": "official", "checked_at": now.isoformat(),
                          "expires_at": (now + timedelta(days=7)).isoformat()}]}


@pytest.fixture
def env(tmp_path):
    return {"ALPHA_KEY": "test-private-key", "FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml"),
            "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json"),
            "FREELLMPOOL_SETUP_STATE_PATH": str(tmp_path / "progress.json")}


def run(env, provider, answers=None):
    responses = iter(answers or ["", ""])
    return o.run_onboarding(provider="alpha", registry={"alpha": provider}, env=env,
                            input_fn=lambda _: next(responses), check=lambda *_: {"status": "ok"}, output=lambda _: None)


@pytest.mark.parametrize("disabled", [False, True])
def test_invalid_account_repair_preserves_real_exclusions_and_other_accounts(env, disabled):
    from pathlib import Path
    account = {"tier": "free", "no_paid_overage": 1, "disabled": disabled,
               "disabled_models": ["operator-excluded"], "manual_models": ["manual-only"],
               "limits": {"remaining": 7}, "notes": "Preserve this note"}
    other = {"tier": "free", "account_ref": "private-reference", "limits": {"remaining": 3}}
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    path.write_text(json.dumps({"schema": 1, "providers": {"alpha": account, "other": other}}))
    assert run(env, entry()) == 0
    stored = json.loads(path.read_text())["providers"]
    assert stored["other"] == other
    assert stored["alpha"]["disabled"] is disabled
    for field in ("disabled_models", "manual_models", "limits", "notes"):
        assert stored["alpha"][field] == account[field]
    assert "no_paid_overage" not in stored["alpha"]
    decision = admit(entry(), {"id": "model", "modalities": ["chat"]}, load_accounts(env)["alpha"],
                     credential_ref=credential_fingerprint("alpha", env["ALPHA_KEY"]))
    assert decision.allowed is not disabled


def test_corrupt_whole_account_file_is_preserved_during_setup(env):
    from pathlib import Path
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    original = '{"schema": 1, "providers": {"private-record": '
    path.write_text(original)
    with pytest.raises(ValueError, match="account"):
        run(env, entry())
    assert path.read_text() == original


def test_setup_uses_active_policy_with_the_effective_environment(env, monkeypatch):
    def load(source=None):
        return {} if source is not None else {"alpha": entry()}
    monkeypatch.setattr("freellmpool.provider_registry.load_registry", load)
    prompts = []
    result = o.run_onboarding(provider="alpha", env=env, input_fn=lambda prompt: prompts.append(prompt) or "s",
                              check=lambda *_: pytest.fail("Deactivated provider must not be checked"), output=lambda _: None)
    assert result == 2 and prompts == []


@pytest.mark.parametrize("status", ["unverified", "unknown", "unsupported", "excluded"])
def test_setup_does_not_offer_a_grant_deactivated_by_review(env, status):
    provider = entry()
    provider["grants"][0]["status"] = status
    assert o.run_onboarding(provider="alpha", registry={"alpha": provider}, env=env,
                            input_fn=lambda _: pytest.fail("Unreviewed grants must not request account setup"),
                            check=lambda *_: pytest.fail("Unreviewed grants must not be checked"), output=lambda _: None) == 2


@pytest.mark.parametrize("choice,tier", [("1", "free"), ("2", "starter")])
def test_supported_tier_list_can_be_selected_and_resumed(env, choice, tier):
    provider = entry(["free", "starter"])
    assert run(env, provider, ["", choice]) == 0
    current = load_accounts(env)["alpha"]
    assert current["tier"] == tier
    assert o._account_current("alpha", provider["grants"], env, "ALPHA_KEY")
    assert admit(provider, {"id": "model", "modalities": ["chat"]}, current,
                 credential_ref=credential_fingerprint("alpha", env["ALPHA_KEY"])).allowed


def test_account_independent_alternative_does_not_prompt_for_an_unneeded_attestation(env):
    provider = entry()
    independent = copy.deepcopy(provider["grants"][0])
    independent.update(id="independent", requires_account_evidence=False)
    provider["grants"].append(independent)
    assert run(env, provider, [""]) == 0


def test_conditions_only_account_uses_admission_plan_fallback_without_key_binding(env):
    from pathlib import Path
    provider = entry(plan="free", paid_balance_zero=True)
    provider["grants"][0]["requires_account_evidence"] = False
    now = datetime.now(UTC)
    current = {"tier": "free", "paid_balance_zero": True, "verified_at": now.isoformat(),
               "expires_at": (now + timedelta(days=1)).isoformat()}
    Path(env["FREELLMPOOL_ACCOUNTS_FILE"]).write_text(json.dumps({"schema": 1, "providers": {"alpha": current}}))
    assert o._account_current("alpha", provider["grants"], env, "ALPHA_KEY")


def test_same_tier_alternative_billing_conditions_can_be_selected(env):
    provider = entry(plan="free", paid_balance_zero=True)
    alternative = copy.deepcopy(provider["grants"][0])
    alternative.update(id="alternative", required_account_conditions={"plan": "free", "automatic_usage_billing": False})
    provider["grants"].append(alternative)
    assert run(env, provider, ["", "2", "CONFIRM"]) == 0
    current = load_accounts(env)["alpha"]
    assert current["automatic_usage_billing"] is False
    assert "paid_balance_zero" not in current
    assert o._account_current("alpha", provider["grants"], env, "ALPHA_KEY")


def test_renewing_account_confirmation_preserves_its_quota_identity(env):
    from pathlib import Path
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    path.write_text(json.dumps({"schema": 1, "providers": {"alpha": {
        "tier": "free", "account_ref": "existing-quota-identity",
        "verified_at": "2020-01-01T00:00:00Z", "expires_at": "2020-02-01T00:00:00Z"}}}))
    assert run(env, entry()) == 0
    assert load_accounts(env)["alpha"]["account_ref"] == "existing-quota-identity"


@pytest.mark.parametrize("field,value", [("disabled", 1), ("disabled_models", "model"), ("manual_models", [1])])
def test_ambiguous_existing_exclusions_are_not_erased_by_account_repair(env, field, value):
    from pathlib import Path
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    original = json.dumps({"schema": 1, "providers": {"alpha": {field: value}}})
    path.write_text(original)
    with pytest.raises(ValueError, match="exclusions"):
        run(env, entry())
    assert path.read_text() == original


@pytest.mark.parametrize("identity", ["", 5, ["uncertain"]])
def test_ambiguous_existing_quota_identity_is_not_reset(env, identity):
    from pathlib import Path
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    original = json.dumps({"schema": 1, "providers": {"alpha": {"account_ref": identity}}})
    path.write_text(original)
    with pytest.raises(ValueError, match="identity"):
        run(env, entry())
    assert path.read_text() == original


def test_independent_grant_repairs_invalid_metadata_without_fabricating_attestation(env):
    from pathlib import Path
    provider = entry()
    provider["grants"][0]["requires_account_evidence"] = False
    path = Path(env["FREELLMPOOL_ACCOUNTS_FILE"])
    original = {"account_ref": "preserved-identity", "no_paid_overage": 1,
                "disabled_models": ["operator-excluded"], "limits": []}
    path.write_text(json.dumps({"schema": 1, "providers": {"alpha": original}}))
    assert not o._account_current("alpha", provider["grants"], env, "ALPHA_KEY")
    assert run(env, provider, [""]) == 0
    current = load_accounts(env)["alpha"]
    assert current == {key: value for key, value in original.items() if key != "no_paid_overage"}
    assert o._account_current("alpha", provider["grants"], env, "ALPHA_KEY")
    assert admit(provider, {"id": "model", "modalities": ["chat"]}, current).allowed
