"""Source attention must use the same reviewed binding as routing evidence."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool import maintenance as m
from freellmpool import provider_registry as p

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def registry():
    return {"openrouter": {"id": "openrouter", "credential_env": "OPENROUTER_API_KEY",
        "discovery": {"supports_public": True}, "limits": [], "grants": [],
        "evidence": [{"id": "terms", "url": "https://openrouter.ai/docs/api_reference/limits.md",
            "status": "official", "checked_at": (NOW - timedelta(days=8)).isoformat(),
            "expires_at": (NOW - timedelta(days=1)).isoformat(),
            "source_hash": {"algorithm": "visible_text_v1", "sha256": "a" * 64}}]}}


def renewal(spec):
    return {"schema": 1, "providers": {"openrouter": {"terms": {
        "status": "unchanged", "url": spec["openrouter"]["evidence"][0]["url"],
        "policy_sha256": p.policy_digest(spec["openrouter"]),
        "hash_algorithm": "visible_text_v1", "sha256": "a" * 64,
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}}}}


@pytest.mark.parametrize("field,value", [
    ("policy_sha256", "b" * 64), ("sha256", "b" * 64), ("hash_algorithm", "raw_body_v1"),
    ("url", "https://openrouter.ai/docs/another-source"), ("policy_sha256", None),
    ("checked_at", (NOW + timedelta(days=1)).isoformat()),
    ("expires_at", (NOW + timedelta(days=8)).isoformat()),
])
def test_unbound_renewal_cannot_hide_expiry_or_resolve_prior_incident(field, value):
    spec = registry()
    _, baseline = m.build_public_report(spec, {}, now=NOW)
    evidence = renewal(spec)
    evidence["providers"]["openrouter"]["terms"][field] = value
    report, _ = m.build_public_report(spec, {}, evidence=evidence, baseline=baseline, now=NOW)
    source = report["providers"]["openrouter"]["sources"][0]
    assert source["expires_at"] == spec["openrouter"]["evidence"][0]["expires_at"]
    assert source["status"] != "unchanged"
    assert any(row["code"] == "source_expired" for row in report["findings"])
    assert not any(row["code"] == "source_expired" for row in report["resolutions"])


def test_current_bound_renewal_resolves_expired_source():
    spec = registry()
    _, baseline = m.build_public_report(spec, {}, now=NOW)
    report, _ = m.build_public_report(spec, {}, evidence=renewal(spec), baseline=baseline, now=NOW)
    assert not any(row["code"] == "source_expired" for row in report["findings"])
    assert any(row["code"] == "source_expired" for row in report["resolutions"])


@pytest.mark.parametrize("changed_policy", [False, True])
def test_read_only_status_agrees_with_routing_renewal_binding(tmp_path, monkeypatch, changed_policy):
    monkeypatch.setattr(p.time, "time", lambda: NOW.timestamp())
    spec = registry()
    evidence = renewal(spec)
    if changed_policy:
        spec["openrouter"]["blocked_models"] = ["retired/model"]
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"schema": 1, "providers": list(spec.values())}))
    monkeypatch.setattr(p, "REGISTRY_PATH", registry_path)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    env = {"FREELLMPOOL_POLICY_UPDATES": "0", "FREELLMPOOL_EVIDENCE_FILE": str(path),
           "XDG_STATE_HOME": str(tmp_path), "OPENROUTER_API_KEY": "test-key",
           "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json"),
           "FREELLMPOOL_CONFORMANCE_FILE": str(tmp_path / "conformance.json"),
           "FREELLMPOOL_DISCOVERY_FILE": str(tmp_path / "discovery.json")}
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    routing_source = p.load_registry(env)["openrouter"]["evidence"][0]
    report = m.status_report(env, now=NOW)
    source = report["providers"]["openrouter"]["sources"][0]
    assert source["expires_at"] == routing_source["expires_at"]
    assert any(row["code"] == "source_expired" for row in report["findings"]) is changed_policy
    assert before == {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}


def test_failed_latest_read_keeps_valid_original_age_and_incident():
    spec = registry()
    evidence = renewal(spec)
    original = copy.deepcopy(evidence["providers"]["openrouter"]["terms"])
    evidence["providers"]["openrouter"]["terms"].update(last_status="check_failed", last_attempt_at=NOW.isoformat())
    report, _ = m.build_public_report(spec, {}, evidence=evidence, now=NOW)
    source = report["providers"]["openrouter"]["sources"][0]
    assert source["expires_at"] == original["expires_at"]
    assert source["status"] == "check_failed"
    assert any(row["code"] == "source_check_failed" for row in report["findings"])
