"""Quota coverage describes effective reviewed limits, not empty defaults."""

from datetime import UTC, datetime

import pytest

from freellmpool.maintenance import build_private_report, format_report


def report_for(rules, models):
    registry = {"openrouter": {"id": "openrouter", "limits": rules, "grants": [],
        "evidence": [], "discovery": {"supports_public": True}}}
    catalog = {"providers": {"openrouter": {"status": "ok", "complete": True,
        "models": [{"id": model, "pricing": {}} for model in models]}}}
    return build_private_report(registry, catalog, now=datetime(2026, 9, 6, tzinfo=UTC))


def test_per_model_values_are_not_reported_as_unknown_defaults():
    report = report_for([{"id": "rpm", "capacity": None,
        "model_capacities": {"one": 30, "two": 10}}], ["one", "two"])
    provider = report["providers"]["openrouter"]
    assert provider["unknown_limits"] == 0
    assert provider["limit_coverage"][0]["status"] == "known"


def test_partial_coverage_identifies_only_models_missing_a_capacity():
    report = report_for([{"id": "rpm", "model_capacities": {"one": 30}}], ["one", "two"])
    provider = report["providers"]["openrouter"]
    assert provider["unknown_limits"] == 1
    assert provider["limit_coverage"] == [{"id": "rpm", "status": "partial", "unknown_models": ["two"]}]
    assert "openrouter" in format_report(report)
    assert "partial" in format_report(report)


@pytest.mark.parametrize("rule", [
    {"capacity": 0}, {"maximum_documented": 0},
    {"capacity": None, "maximum_documented": 200},
])
def test_fallback_capacities_include_zero_and_need_no_model_listing(rule):
    provider = report_for([{"id": "rpm", **rule}], [])["providers"]["openrouter"]
    assert provider["unknown_limits"] == 0
    assert provider["limit_coverage"][0]["status"] == "known"


def test_absent_catalog_does_not_prove_complete_per_model_coverage():
    provider = report_for([{"id": "rpm", "model_capacities": {"one": 30}}], [])["providers"]["openrouter"]
    assert provider["unknown_limits"] == 1
    assert provider["limit_coverage"][0]["status"] == "unassessed"


def test_model_scoped_rule_does_not_mark_unrelated_models_unknown():
    provider = report_for([{"id": "rpm", "model_ids": ["one"],
        "model_capacities": {"one": 30}}], ["one", "two"])["providers"]["openrouter"]
    assert provider["unknown_limits"] == 0


def test_absent_rate_rules_are_not_presented_as_complete_knowledge():
    report = report_for([], ["one"])
    assert "No reviewed quota rules: openrouter" in format_report(report)


def test_grant_scoped_rule_only_assesses_matching_models():
    from freellmpool.maintenance import _limit_coverage

    spec = {"limits": [{"id": "rpm", "grant_ids": ["free"], "model_capacities": {"one": 30}}],
        "grants": [{"id": "free", "status": "verified", "kind": "recurring_quota",
            "hard_free_boundary": True, "allowed_modalities": ["chat"],
            "model_selector": {"kind": "allowlist", "models": ["one"]}}]}
    rows = _limit_coverage(spec, {"models": [{"id": "one", "modalities": ["chat"]}, {"id": "two", "modalities": ["chat"]}]})
    assert rows == [{"id": "rpm", "status": "known", "unknown_models": []}]


def test_malformed_catalog_preserves_maintenance_diagnostics():
    report = report_for([{"id": "rpm", "model_capacities": {"one": 30}}], ["bad model"])
    provider = report["providers"]["openrouter"]
    assert provider["unknown_limits"] == 1
    assert provider["limit_coverage"][0]["status"] == "unassessed"
