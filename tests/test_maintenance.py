"""Maintenance must retain review work and keep private state local."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool import maintenance as m
from freellmpool.free_policy import credential_fingerprint
from freellmpool.provider_registry import policy_digest

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
SHA = "a" * 40
URL = "https://openrouter.ai/docs/api_reference/limits.md"


def registry():
    return {"openrouter": {"id": "openrouter", "credential_env": "OPENROUTER_API_KEY",
        "discovery": {"supports_public": True}, "grants": [], "limits": [],
        "evidence": [{"id": "terms", "url": URL, "checked_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=7)).isoformat(), "status": "official"}]}}


def catalog(price="0", *, status="ok", access="public"):
    return {"schema": 1, "providers": {"openrouter": {
        "status": status, "complete": True, "catalog_access": access,
        "checked_at": NOW.isoformat(), "last_attempt_at": NOW.isoformat(),
        "models": [{"id": "test/model", "modalities": ["chat"], "pricing": {"input": price, "output": "0"},
                    "private_note": "DO-NOT-EXPORT"}]}}}


def public(data=None, baseline=None, **kwargs):
    return m.build_public_report(registry(), data or catalog(), baseline=baseline,
                                 now=NOW, source_revision=SHA, **kwargs)


def test_missing_baseline_is_not_all_models_added_and_public_whitelists():
    report, baseline = public()
    assert report["findings"] == []
    assert baseline["pending_changes"] == []
    assert "DO-NOT-EXPORT" not in json.dumps([report, baseline])
    assert m.validate_public_report(report) == report
    assert m.validate_public_baseline(baseline) == baseline


def test_price_change_survives_two_successful_runs_then_proven_reversal():
    _, first = public()
    changed, second = public(catalog("1"), first)
    same, third = public(catalog("1"), second)
    assert changed["findings"][0]["code"] == "price_changed"
    assert same["findings"] == changed["findings"]
    assert same["resolutions"] == []
    reverse, fourth = public(catalog(), third)
    assert reverse["findings"] == []
    assert reverse["resolutions"][0]["fingerprint"] == changed["findings"][0]["fingerprint"]
    assert fourth["pending_changes"] == []


def test_failed_or_partial_catalog_never_resolves_review_or_renews_baseline():
    _, first = public()
    changed, second = public(catalog("1"), first)
    failed, third = public(catalog("0", status="partial"), second)
    assert changed["findings"][0] in failed["findings"]
    assert failed["resolutions"] == []
    assert third["providers"] == second["providers"]
    recovered, _ = public(catalog("1"), third)
    assert any(row["kind"] == "incident" for row in recovered["resolutions"])
    assert any(row["code"] == "price_changed" for row in recovered["findings"])


def test_manual_acknowledgement_is_explicit_and_preserved():
    _, first = public()
    changed, second = public(catalog("1"), first)
    fingerprint = changed["findings"][0]["fingerprint"]
    report, _ = public(catalog("1"), second, acknowledged=[fingerprint])
    assert report["findings"] == []
    assert report["resolutions"][0]["fingerprint"] == fingerprint


def test_private_catalog_cannot_be_exported():
    report, baseline = public(catalog(access="authenticated"))
    assert report["providers"]["openrouter"]["catalog"]["model_count"] == 0
    assert baseline["providers"] == {}
    assert "test/model" not in json.dumps([report, baseline])


@pytest.mark.parametrize("field,value", [("account", {"token": "secret"}), ("raw_error", "secret")])
def test_public_validator_rejects_arbitrary_extra_fields(field, value):
    report, _ = public()
    report[field] = value
    with pytest.raises(ValueError):
        m.validate_public_report(report)


def test_corrupt_baseline_cannot_inject_pending_issue_or_model():
    _, first = public()
    first["pending_changes"] = [{"id": "evil", "summary": "@everyone SECRET"}]
    with pytest.raises(ValueError):
        m.validate_public_baseline(first)


def test_account_expiry_attention_is_readable_private_and_deduplicated(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "OPENROUTER_API_KEY": "secret"}
    accounts = {"openrouter": {"verified_at": (NOW - timedelta(days=30)).isoformat(),
        "expires_at": (NOW - timedelta(seconds=1)).isoformat(), "tier": "free"}}
    spec = registry()
    spec["openrouter"]["grants"] = [{"status": "conditional", "kind": "zero_price", "hard_free_boundary": True,
        "allowed_modalities": ["chat"], "model_selector": {"kind": "all"},
        "requires_account_evidence": True, "required_account_tier": "free"}]
    report = m.build_private_report(spec, catalog(), accounts=accounts, env=env, now=NOW)
    sent = []
    m.emit_attention(report, env, notifier=lambda title, body: sent.append((title, body)))
    m.emit_attention(report, env, notifier=lambda title, body: sent.append((title, body)))
    assert len(sent) == 1
    attention = tmp_path / "freellmpool" / "maintenance-attention.txt"
    assert "freellmpool setup --provider openrouter" in attention.read_text()
    assert "secret" not in attention.read_text()
    assert attention.stat().st_mode & 0o777 == 0o600
    assert "account_expired" in {row["code"] for row in report["findings"]}


def test_unchanged_unsupported_account_checks_do_not_raise_attention():
    observations = {"providers": {"openrouter": {"status": "auth_missing", "coverage": "unsupported"}}}
    report = m.build_private_report(registry(), catalog(), observations=observations, env={}, now=NOW)
    assert report["findings"] == []
    assert report["providers"]["openrouter"]["account_observation"]["status"] == "auth_missing"


def test_local_latest_failure_is_visible_while_prior_evidence_is_fresh():
    observations = {"providers": {"openrouter": {"status": "auth_failed", "coverage": "supported",
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "last_attempt_at": NOW.isoformat(), "raw_error": "SECRET"}}}
    report = m.build_private_report(registry(), catalog(), observations=observations,
                                   env={"OPENROUTER_API_KEY": "secret"}, now=NOW)
    assert any(row["code"] == "account_check_failed" for row in report["findings"])
    assert "SECRET" not in json.dumps(report)


def test_public_attention_is_never_sent_or_written(tmp_path):
    report, _ = public(catalog(status="error"))
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        m.emit_attention(report, {"XDG_STATE_HOME": str(tmp_path)})
    assert list(tmp_path.iterdir()) == before


def test_report_validation_checks_nested_fields_and_resolution_identity():
    _, first = public()
    report, _ = public(catalog("1"), first)
    dirty = copy.deepcopy(report)
    dirty["findings"][0]["source_url"] = "https://attacker.invalid/leak"
    with pytest.raises(ValueError):
        m.validate_public_report(dirty)


def test_model_addition_reversal_closes_original_without_new_removal_issue():
    _, first = public()
    added = catalog()
    added["providers"]["openrouter"]["models"].append({"id": "new/model", "pricing": {}})
    report, second = public(added, first)
    reversed_report, _ = public(catalog(), second)
    assert reversed_report["findings"] == []
    assert reversed_report["resolutions"][0]["fingerprint"] == report["findings"][0]["fingerprint"]


def test_public_refresh_never_reads_account_or_policy_state(tmp_path, monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("private path entered")
    def refresh_catalog(env, **kwargs):
        assert env == {}
        assert kwargs["public_only"] is True
        calls.append("catalog")
        return catalog()
    services = {"catalog": refresh_catalog, "evidence": lambda env, **kwargs: {},
                "accounts": forbidden, "policy": forbidden, "proposals": lambda spec: {}}
    monkeypatch.setattr(m, "_public_registry", registry)
    report = m.run_maintenance({"OPENROUTER_API_KEY": "DO-NOT-READ"}, public_only=True,
        baseline_path=tmp_path / "public-baseline.json", source_revision=SHA, now=NOW, refreshers=services)
    assert report["visibility"] == "public"
    assert calls == ["catalog"]
    assert "DO-NOT-READ" not in json.dumps(report)
    assert m.validate_public_baseline(json.loads((tmp_path / "public-baseline.json").read_text()))


def test_status_is_read_only_and_does_not_call_refresh(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network or mutation from status")
    env = {"XDG_STATE_HOME": str(tmp_path), "FREELLMPOOL_DISCOVERY_FILE": str(tmp_path / "catalog.json"),
           "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json"),
           "FREELLMPOOL_CONFORMANCE_FILE": str(tmp_path / "conformance.json")}
    (tmp_path / "catalog.json").write_text(json.dumps(catalog()))
    monkeypatch.setattr(m, "run_maintenance", forbidden)
    monkeypatch.setattr(m, "_write", forbidden)
    before = set(tmp_path.rglob("*"))
    report = m.status_report(env, now=NOW)
    assert report["visibility"] == "private"
    assert set(tmp_path.rglob("*")) == before


def test_private_refresh_saves_private_baseline_and_attention(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path), "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json"),
           "FREELLMPOOL_CONFORMANCE_FILE": str(tmp_path / "conformance.json")}
    monkeypatch.setattr("freellmpool.provider_registry.load_registry", lambda *args, **kwargs: registry())
    calls = []
    services = {"catalog": lambda env, **kwargs: catalog(), "evidence": lambda env, **kwargs: {},
                "accounts": lambda env: {"providers": {}},
                "policy": lambda env: calls.append("policy") or {"status": "error"},
                "proposals": lambda spec: {}}
    report = m.run_maintenance(env, now=NOW, refreshers=services, notifier=lambda *args: None)
    assert calls == ["policy"]
    assert report["policy_channel"]["status"] == "error"
    baseline = json.loads((tmp_path / "freellmpool" / "maintenance-baseline.json").read_text())
    assert baseline["visibility"] == "private"
    with pytest.raises(ValueError):
        m.validate_public_baseline(baseline)


def test_unconfigured_private_providers_remain_visible_without_action_noise():
    spec = registry()
    spec["openrouter"]["evidence"][0]["expires_at"] = (NOW - timedelta(days=1)).isoformat()
    report = m.build_private_report(spec, {"providers": {}}, env={}, now=NOW)
    assert report["findings"] == []
    assert report["providers"]["openrouter"]["catalog"]["status"] == "not_checked"


def test_approaching_deadline_alerts_then_expiry_gets_one_new_alert(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "OPENROUTER_API_KEY": "secret"}
    spec = registry()
    spec["openrouter"]["grants"] = [{"status": "conditional", "kind": "zero_price", "hard_free_boundary": True,
        "allowed_modalities": ["chat"], "model_selector": {"kind": "all"},
        "requires_account_evidence": True, "required_account_tier": "free"}]
    accounts = {"openrouter": {"verified_at": (NOW - timedelta(days=29)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(), "tier": "free",
        "credential_ref": credential_fingerprint("openrouter", "secret")}}
    report = m.build_private_report(spec, catalog(), accounts=accounts, env=env, now=NOW)
    sent = []
    def notify(*args):
        sent.append(args)
    m.emit_attention(report, env, notifier=notify)
    m.emit_attention(report, env, notifier=notify)
    assert len(sent) == 1
    expired = m.build_private_report(spec, catalog(), accounts=accounts, env=env, now=NOW + timedelta(hours=2))
    m.emit_attention(expired, env, notifier=notify)
    m.emit_attention(expired, env, notifier=notify)
    assert len(sent) == 2


def test_quota_proposal_survives_parser_outage_and_resolves_reviewed_rule():
    spec = registry()
    spec["openrouter"]["limits"] = [{"id": "rpd", "capacity": 10}]
    parsed = {"providers": {"openrouter": {"status": "ok", "source_url": URL, "source_sha256": "b" * 64,
        "proposals": [{"rule_id": "rpd", "model_id": "test/model", "metric": "requests", "window_seconds": 86400,
                       "old_capacity": 10, "new_capacity": 5}]}}}
    report, first = m.build_public_report(spec, catalog(), proposals=parsed, now=NOW)
    outage = {"providers": {"openrouter": {"status": "error", "proposals": []}}}
    failed, second = m.build_public_report(spec, catalog(), baseline=first, proposals=outage, now=NOW)
    assert failed["proposals"] == report["proposals"]
    spec["openrouter"]["limits"][0]["capacity"] = 5
    reviewed = {"providers": {"openrouter": {"status": "ok", "proposals": []}}}
    recovered, third = m.build_public_report(spec, catalog(), baseline=second, proposals=reviewed, now=NOW)
    assert recovered["findings"] == []
    assert recovered["proposals"] == []
    assert third["proposals"] == []


@pytest.mark.parametrize("algorithm", ["visible_text_v1", "discourse_first_post_v1"])
def test_source_change_remains_pending_through_failed_check_then_matches_review(algorithm):
    spec = registry()
    spec["openrouter"]["evidence"][0]["source_hash"] = {"algorithm": algorithm, "sha256": "a" * 64}
    changed = {"providers": {"openrouter": {"terms": {"status": "review_required", "sha256": "b" * 64,
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}}}}
    report, first = m.build_public_report(spec, catalog(), evidence=changed, now=NOW)
    failed = copy.deepcopy(changed)
    failed["providers"]["openrouter"]["terms"]["last_status"] = "check_failed"
    outage, second = m.build_public_report(spec, catalog(), baseline=first, evidence=failed, now=NOW)
    assert any(row["code"] == "source_changed" for row in outage["findings"])
    restored = copy.deepcopy(changed)
    restored["providers"]["openrouter"]["terms"].update(status="unchanged", sha256="a" * 64,
        url=URL, hash_algorithm=algorithm, policy_sha256=policy_digest(spec["openrouter"]))
    recovered, _ = m.build_public_report(spec, catalog(), baseline=second, evidence=restored, now=NOW)
    assert recovered["findings"] == []
    assert report["findings"][0]["fingerprint"] in {row["fingerprint"] for row in recovered["resolutions"]}


def test_verification_keeps_useful_proof_and_reserves_scouting(tmp_path):
    from freellmpool.conformance import ConformanceStore
    from freellmpool.models import Provider
    from freellmpool.router import Target
    store = ConformanceStore(tmp_path / "conformance.json")
    provider = Provider("example", "Example", "openai", "https://example.invalid/v1", ())
    useful = [Target(provider, f"known-{index}", 0) for index in range(4)]
    scout = Target(provider, "new", 0)
    for target in useful:
        for feature in ("chat", "tools", "streaming"):
            store.record(provider, target.model, feature, status="pass", classification="verified")
    selected = m.select_verification_targets([*useful, scout], store, 4)
    assert len(selected) == 4
    assert scout in selected
    assert len([target for target in selected if target in useful]) == 3
    assert m.select_verification_targets([scout], store, 0) == []
    with pytest.raises(ValueError):
        m.select_verification_targets([scout], store, True)


@pytest.mark.parametrize("section,key,value", [
    ("provider", "coverage", []), ("catalog", "status", []),
    ("source", "id", []), ("source", "status", []),
    ("catalog", "model_count", True), ("catalog", "account_id", "SECRET"),
])
def test_malformed_public_nested_shapes_raise_safe_value_error(section, key, value):
    report, _ = public()
    provider = report["providers"]["openrouter"]
    target = provider if section == "provider" else provider["catalog"] if section == "catalog" else provider["sources"][0]
    target[key] = value
    with pytest.raises(ValueError):
        m.validate_public_report(report)


def test_duplicate_public_findings_are_rejected():
    _, first = public()
    report, _ = public(catalog("1"), first)
    report["findings"].append(copy.deepcopy(report["findings"][0]))
    with pytest.raises(ValueError):
        m.validate_public_report(report)


def test_incorrect_conformance_target_fingerprint_cannot_claim_useful_proof():
    features = {feature: {"status": "pass", "verified_at": NOW.isoformat(), "probe_version": 2}
                for feature in ("chat", "tools", "streaming")}
    conformance = {"targets": {"openrouter/test/model": {"fingerprint": "wrong", "features": features}}}
    spec = registry()
    spec["openrouter"]["api_base_url"] = "https://openrouter.ai/api/v1"
    report = m.build_private_report(spec, catalog(), conformance=conformance, now=NOW)
    assert report["providers"]["openrouter"]["conformance"]["useful_targets"] == 0


def test_changed_unverified_source_does_not_hide_expired_policy():
    spec = registry()
    expired = (NOW - timedelta(hours=1)).isoformat()
    spec["openrouter"]["evidence"][0]["expires_at"] = expired
    overlay = {"providers": {"openrouter": {"terms": {"status": "review_required", "sha256": "b" * 64,
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}}}}
    report, _ = m.build_public_report(spec, catalog(), evidence=overlay, now=NOW)
    assert report["providers"]["openrouter"]["sources"][0]["expires_at"] == expired
    assert any(row["code"] == "source_expired" for row in report["findings"])


@pytest.mark.parametrize("disabled,excluded", [(True, False), (False, True)])
def test_disabled_or_excluded_accounts_do_not_receive_expiry_attention(disabled, excluded):
    spec = registry()
    spec["openrouter"]["grants"] = [{"requires_account_evidence": True, "status": "excluded" if excluded else "conditional"}]
    accounts = {"openrouter": {"disabled": disabled, "expires_at": (NOW - timedelta(days=1)).isoformat()}}
    report = m.build_private_report(spec, catalog(), accounts=accounts, env={"OPENROUTER_API_KEY": "secret"}, now=NOW)
    assert report["findings"] == []


def test_real_dynamic_catalog_alias_is_not_a_malformed_model():
    data = catalog()
    data["providers"]["openrouter"]["models"].append({"id": "~z-ai/glm-flash-latest", "pricing": {"input": "-1", "output": "-1"}})
    report, baseline = public(data)
    assert report["findings"] == []
    assert "~z-ai/glm-flash-latest" in baseline["providers"]["openrouter"]["models"]


def test_unreviewed_source_needs_baseline_instead_of_claiming_a_change():
    evidence = {"providers": {"openrouter": {"terms": {"status": "review_required", "sha256": "a" * 64,
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}}}}
    report, first = public(evidence=evidence)
    assert [row["code"] for row in report["findings"]] == ["source_baseline_needed"]
    assert "changed" not in report["findings"][0]["summary"]
    same, second = public(baseline=first, evidence=evidence)
    assert same["findings"] == report["findings"]
    spec = registry()
    spec["openrouter"]["evidence"][0]["source_hash"] = {"algorithm": "visible_text_v1", "sha256": "a" * 64}
    evidence["providers"]["openrouter"]["terms"].update(status="unchanged", url=URL,
        hash_algorithm="visible_text_v1", policy_sha256=policy_digest(spec["openrouter"]))
    reviewed, _ = m.build_public_report(spec, catalog(), evidence=evidence, baseline=second, now=NOW)
    assert reviewed["findings"] == []
    assert reviewed["resolutions"][0]["fingerprint"] == report["findings"][0]["fingerprint"]


def test_expired_useful_proof_is_renewed_before_unseen_scouts(tmp_path):
    from freellmpool.conformance import ConformanceStore
    from freellmpool.models import Provider
    from freellmpool.router import Target
    path = tmp_path / "conformance.json"
    store = ConformanceStore(path)
    provider = Provider("example", "Example", "openai", "https://example.invalid/v1", ())
    known = Target(provider, "known", 0)
    for feature in ("chat", "tools", "streaming"):
        store.record(provider, known.model, feature, status="pass", classification="verified")
    value = json.loads(path.read_text())
    for row in value["targets"][known.name]["features"].values():
        row["verified_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(value))
    scouts = [Target(provider, f"new-{i}", 0) for i in range(10)]
    selected = m.select_verification_targets([known, *scouts], store, 2)
    assert known in selected
    assert len(selected) == 2


def test_private_baseline_accepts_reviewed_same_origin_source_path_change():
    spec = registry()
    spec["openrouter"]["evidence"][0].update(url="https://openrouter.ai/docs/new-reviewed-limits",
        source_hash={"algorithm": "visible_text_v1", "sha256": "a" * 64})
    evidence = {"providers": {"openrouter": {"terms": {"status": "review_required", "sha256": "b" * 64}}}}
    report, baseline = m.build_public_report(spec, catalog(), evidence=evidence, now=NOW, _private=True)
    again = m.build_private_report(spec, catalog(), evidence=evidence, baseline=baseline, now=NOW,
                                  env={"OPENROUTER_API_KEY": "secret"})
    assert again["findings"] == report["findings"]
    with pytest.raises(ValueError):
        m.validate_public_baseline(baseline)
