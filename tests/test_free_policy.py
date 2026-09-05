"""A listed model, account balance, or exact pin cannot establish free access."""

from copy import deepcopy

import pytest

from freellmpool.free_policy import admit, load_accounts, save_account

NOW = 1788609600.0  # 2026-09-05 12:00 UTC


def spec(kind="zero_price", selector=None):
    return {
        "id": "test", "evidence": [{"id": "terms", "url": "https://example.org/pricing",
            "checked_at": "2026-09-05T00:00:00+00:00", "expires_at": "2026-09-12T00:00:00+00:00",
            "status": "verified"}],
        "grants": [{"id": "free", "kind": kind, "status": "verified",
            "model_selector": selector or {"kind": "free_suffix", "suffix": ":free"},
            "requires_account_evidence": False, "paid_overage_possible": False,
            "evidence_ids": ["terms"], "allowed_modalities": ["chat"]}],
    }


def model(**changes):
    return {"id": "model:free", "pricing": {"input": "0", "output": "0"}, "modalities": ["chat"], **changes}


def test_live_positive_price_overrides_free_name():
    assert admit(spec(), model(), now=NOW).allowed
    assert not admit(spec(), model(pricing={"input": "0", "output": "0.1"}), now=NOW).allowed


@pytest.mark.parametrize("pricing", [None, {}, {"input": "0"}, {"input": "NaN", "output": "0"},
                                     {"input": "-1", "output": "0"}, {"input": "0", "output": "0", "request": "0.01"}])
def test_missing_or_invalid_zero_price_evidence_fails_closed(pricing):
    assert not admit(spec(), model(pricing=pricing), now=NOW).allowed


@pytest.mark.parametrize("kind", ["finite_trial", "paid", "unknown"])
def test_trial_balance_and_unknown_grant_are_not_recurring_free(kind):
    assert not admit(spec(kind), model(), now=NOW).allowed


@pytest.mark.parametrize("location", ("status", "kind", "evidence", "required_account_tier"))
def test_malformed_grant_enumerations_fail_closed(location):
    provider = spec()
    account = {"tier": "free", "verified_at": "2026-09-05T00:00:00Z", "expires_at": "2026-09-12T00:00:00Z"}
    if location == "evidence":
        provider["evidence"][0]["status"] = {}
    elif location == "required_account_tier":
        provider["grants"][0].update(requires_account_evidence=True, required_account_tier=1)
    else:
        provider["grants"][0][location] = {}
    assert not admit(provider, model(), account, now=NOW).allowed


def test_unknown_expired_or_wrong_tier_account_cannot_be_renewed_by_catalog():
    provider = spec("recurring_quota", {"kind": "allowlist", "models": ["model:free"]})
    grant = provider["grants"][0]
    grant.update(requires_account_evidence=True, required_account_tier="free")
    account = {"tier": "free", "verified_at": "2026-09-05T00:00:00Z", "expires_at": "2026-09-12T00:00:00Z"}
    assert admit(provider, model(), account, now=NOW).allowed
    assert not admit(provider, model(), now=NOW).allowed
    for change in ({"tier": "paid"}, {"expires_at": "2026-09-04T00:00:00Z"}, {"verified_at": "2027-01-01T00:00:00Z"}):
        assert not admit(provider, model(), account | change, now=NOW).allowed


def test_account_confirmation_cannot_follow_a_key_from_another_project():
    provider = spec("recurring_quota", {"kind": "all"})
    provider["grants"][0].update(requires_account_evidence=True, required_account_tier="free")
    account = {"tier": "free", "verified_at": "2026-09-05T00:00:00Z", "expires_at": "2026-09-12T00:00:00Z", "credential_ref": "original"}
    assert admit(provider, model(), account, now=NOW, credential_ref="original").allowed
    assert not admit(provider, model(), account, now=NOW, credential_ref="replacement").allowed


def test_expired_pricing_terms_cannot_be_renewed_by_account_or_listing():
    provider = spec()
    provider["evidence"][0]["expires_at"] = "2026-09-04T00:00:00Z"
    assert not admit(provider, model(), {"tier": "free"}, now=NOW).allowed


def test_local_no_overage_claim_cannot_admit_billable_grant():
    provider = spec("recurring_credit", {"kind": "all"})
    provider["grants"][0]["paid_overage_possible"] = True
    assert not admit(provider, model(), {"no_paid_overage": True}, now=NOW).allowed
    provider["grants"][0]["hard_free_boundary"] = True
    assert admit(provider, model(), now=NOW).allowed


def test_conditional_free_wallet_requires_all_account_conditions():
    provider = spec("recurring_credit", {"kind": "all"})
    provider["grants"][0]["required_account_conditions"] = {"plan": "free", "paid_balance_zero": True, "automatic_usage_billing": False}
    account = {"tier": "free", "paid_balance_zero": True, "automatic_usage_billing": False,
               "verified_at": "2026-09-05T00:00:00Z", "expires_at": "2026-09-12T00:00:00Z"}
    assert admit(provider, model(), account, now=NOW).allowed
    assert not admit(provider, model(), account | {"paid_balance_zero": False}, now=NOW).allowed
    assert not admit(provider, model(), account | {"expires_at": "2020-01-01T00:00:00Z"}, now=NOW).allowed


def test_allowlisted_free_route_requires_exact_identity_and_zero_price():
    provider = spec(selector={"kind": "allowlist", "models": ["org/model:upstream"]})
    candidate = model(id="org/model:upstream", metadata={"is_free": True, "upstream_provider": "upstream"})
    assert admit(provider, candidate, now=NOW).allowed
    assert not admit(provider, candidate | {"id": "org/model"}, now=NOW).allowed
    assert not admit(provider, candidate | {"pricing": {"input": "1", "output": "0"}}, now=NOW).allowed


def test_multiple_grants_do_not_let_paid_credit_class_hide_zero_price_route():
    provider = spec("paid")
    provider["grants"].append(deepcopy(spec()["grants"][0]) | {"id": "zero"})
    assert admit(provider, model(), now=NOW).grant["id"] == "zero"


def test_unknown_model_and_modality_are_excluded():
    provider = spec("recurring_quota", {"kind": "allowlist", "models": ["small"]})
    assert not admit(provider, model(id="unknown"), now=NOW).allowed
    assert not admit(provider, model(id="small"), modality="embedding", now=NOW).allowed


def test_paid_required_exclusions_override_account_wide_grant():
    provider = spec("recurring_quota", {"kind": "all", "exclude": ["model:free"]})
    assert not admit(provider, model(), now=NOW).allowed
    provider["grants"][0]["model_selector"].pop("exclude")
    provider["blocked_models"] = ["model:free"]
    assert not admit(provider, model(), now=NOW).allowed


def test_official_allowlist_price_can_fill_missing_api_price_but_not_override_paid():
    provider = spec(selector={"kind": "allowlist", "models": ["model:free"]})
    provider["grants"][0]["pricing"] = {"input": "0", "output": "0"}
    assert admit(provider, model(pricing={}), now=NOW).allowed
    assert not admit(provider, model(pricing={"input": "0", "output": "1"}), now=NOW).allowed
    provider["grants"][0]["model_selector"] = {"kind": "all"}
    assert not admit(provider, model(pricing={}), now=NOW).allowed


def test_account_state_is_private_and_unknown_schema_fails_closed(tmp_path):
    env = {"FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json")}
    save_account("test", {"tier": "free", "account_ref": "primary"}, env)
    assert load_accounts(env)["test"]["tier"] == "free"
    assert (tmp_path / "accounts.json").stat().st_mode & 0o077 == 0
    (tmp_path / "accounts.json").write_text('{"schema":999,"providers":{"test":{"tier":"free"}}}')
    assert load_accounts(env) == {}


@pytest.mark.parametrize("account", [
    {"disabled_models": None}, {"disabled_models": "model:free"}, {"disabled_models": [{}]},
    {"disabled": "false"}, {"tier": []}, {"account_ref": {}},
])
def test_malformed_account_restrictions_fail_closed_without_crashing(account):
    assert not admit(spec(), model(), account, now=NOW).allowed


def test_corrupt_account_row_does_not_drop_restrictions_and_enable_routes(tmp_path):
    import json
    path = tmp_path / "accounts.json"
    env = {"FREELLMPOOL_ACCOUNTS_FILE": str(path)}
    path.write_text(json.dumps({"schema": 1, "providers": {"test": {"disabled_models": None}}}))
    account = load_accounts(env)["test"]
    assert account["disabled"] is True
    assert not admit(spec(), model(), account, now=NOW).allowed


@pytest.mark.parametrize("selector", [
    {"kind": "allowlist", "models": "prefix-model:free-suffix"},
    {"kind": "free_suffix", "suffix": 42},
    {"kind": "all", "exclude": None},
])
def test_malformed_selectors_cannot_admit_by_substring_or_crash(selector):
    assert not admit(spec(selector=selector), model(), now=NOW).allowed


def test_duplicate_evidence_ids_are_ambiguous_and_cannot_admit():
    provider = spec()
    provider["evidence"].append(dict(provider["evidence"][0]))
    assert not admit(provider, model(), now=NOW).allowed
