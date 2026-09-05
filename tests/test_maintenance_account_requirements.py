"""Account attention follows the billing requirements that can hold routing."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool.free_policy import admit, credential_fingerprint
from freellmpool.maintenance import build_private_report

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
ENV = {"OLLAMA_API_KEY": "test-private-key"}
MODEL = {"id": "free-model", "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}}


def grant(**changes):
    return {"id": "free", "status": "conditional", "kind": "zero_price",
            "hard_free_boundary": True, "paid_overage_possible": False,
            "allowed_modalities": ["chat"], "model_selector": {"kind": "all"},
            "evidence_ids": ["terms"], "requires_account_evidence": True,
            "required_account_tier": "free", "required_account_conditions": {
                "plan": "free", "paid_balance_zero": True, "automatic_usage_billing": False}, **changes}


def provider(grants=None):
    return {"id": "ollama", "credential_env": "OLLAMA_API_KEY", "grants": grants or [grant()],
            "evidence": [{"id": "terms", "url": "https://ollama.com/pricing", "status": "official",
                          "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}]}


def account(**changes):
    return {"tier": "free", "plan": "free", "paid_balance_zero": True, "automatic_usage_billing": False,
            "credential_ref": credential_fingerprint("ollama", ENV["OLLAMA_API_KEY"]),
            "verified_at": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=7)).isoformat(), **changes}


def report(details, *, grants=None, models=None, status="ok", complete=True, env=ENV):
    catalog = {"providers": {"ollama": {"status": status, "complete": complete,
        "checked_at": NOW.isoformat(), "models": [MODEL] if models is None else models}}}
    return build_private_report({"ollama": provider(grants)}, catalog, accounts={"ollama": details}, env=env, now=NOW)


def codes(result):
    return {row["code"] for row in result["findings"] if row["code"].startswith("account_")}


@pytest.mark.parametrize("field,value", [
    ("paid_balance_zero", None), ("paid_balance_zero", 1),
    ("automatic_usage_billing", None), ("automatic_usage_billing", 0),
    ("plan", "starter"), ("tier", "paid"), ("tier", None),
    ("credential_ref", None), ("credential_ref", "old-binding"),
    ("verified_at", None), ("verified_at", (NOW + timedelta(hours=1)).isoformat()),
    ("verified_at", "2026-09-05T00:00:00"), ("expires_at", None),
    ("expires_at", "invalid"),
])
def test_fresh_expiry_cannot_hide_unsatisfied_account_requirements(field, value):
    details = account(**{field: value})
    before = copy.deepcopy(details)
    result = report(details)
    assert codes(result) == {"account_verification"}
    finding = next(row for row in result["findings"] if row["code"] == "account_verification")
    assert finding["command"] == "freellmpool setup --provider ollama"
    assert ENV["OLLAMA_API_KEY"] not in json.dumps(result)
    assert details == before
    assert not admit(provider(), MODEL, details, now=NOW.timestamp(),
                     credential_ref=credential_fingerprint("ollama", ENV["OLLAMA_API_KEY"])).allowed


def test_matching_account_is_quiet_and_routable():
    assert codes(report(account())) == set()
    assert admit(provider(), MODEL, account(), now=NOW.timestamp(),
                 credential_ref=credential_fingerprint("ollama", ENV["OLLAMA_API_KEY"])).allowed


@pytest.mark.parametrize("hours,expected", [(-1, "account_expired"), (1, "account_due")])
def test_valid_confirmation_retains_due_and_expired_distinctions(hours, expected):
    assert codes(report(account(expires_at=(NOW + timedelta(hours=hours)).isoformat()))) == {expected}


def test_missing_billing_conditions_need_confirmation_before_approaching_expiry():
    assert codes(report(account(paid_balance_zero=None, expires_at=(NOW + timedelta(hours=1)).isoformat()))) == {
        "account_verification"}


def test_matching_alternative_grant_satisfies_provider_confirmation():
    grants = [grant(required_account_tier="other"), grant(id="alternative", required_account_tier=["free", "starter"])]
    assert codes(report(account(), grants=grants)) == set()


def test_relevant_account_independent_alternative_needs_no_confirmation():
    grants = [grant(), grant(id="independent", requires_account_evidence=False, required_account_conditions={})]
    assert codes(report({}, grants=grants)) == set()


def test_unrelated_account_independent_grant_does_not_silence_held_model():
    grants = [grant(), grant(id="unrelated", requires_account_evidence=False, required_account_conditions={},
                            model_selector={"kind": "allowlist", "models": ["other-model"]})]
    assert codes(report({}, grants=grants)) == {"account_verification"}


def test_conditions_only_follow_plan_tier_fallback_without_credential_requirement():
    details = account()
    del details["plan"]
    del details["credential_ref"]
    grants = [grant(requires_account_evidence=False)]
    assert codes(report(details, grants=grants)) == set()
    assert admit(provider(grants), MODEL, details, now=NOW.timestamp(), credential_ref="different").allowed


@pytest.mark.parametrize("case", ["unconfigured", "disabled", "excluded", "models_disabled"])
def test_intentionally_inactive_accounts_are_quiet(case):
    details, grants, env = {}, [grant()], ENV
    if case == "unconfigured":
        env = {}
    elif case == "disabled":
        details["disabled"] = True
    elif case == "excluded":
        grants[0]["status"] = "excluded"
    else:
        details["disabled_models"] = [MODEL["id"]]
    assert codes(report(details, grants=grants, env=env)) == set()


@pytest.mark.parametrize("status,complete", [("error", False), ("partial", False), ("not_checked", False)])
def test_unavailable_catalog_does_not_hide_needed_account_setup(status, complete):
    assert codes(report({}, models=[], status=status, complete=complete)) == {"account_verification"}


def test_complete_empty_free_catalog_needs_no_account_confirmation():
    assert codes(report({}, models=[])) == set()
