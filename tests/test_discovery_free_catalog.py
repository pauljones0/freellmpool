"""Persist only reviewed free catalog candidates without granting account access."""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool import maintenance as m
from freellmpool.free_policy import admit, model_matches_grant
from freellmpool.provider_registry import load_registry


def provider():
    return copy.deepcopy(load_registry()["openrouter"])


def model(name="example:free", price="0"):
    return {"id": name, "pricing": {"input": price, "output": "0"}, "modalities": ["chat"]}


def install(monkeypatch, spec, rows):
    monkeypatch.setattr(d, "load_registry", lambda *args, **kwargs: {spec["id"]: spec})
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": rows}))))


def refresh(monkeypatch, tmp_path, rows, spec=None):
    install(monkeypatch, provider() if spec is None else spec, rows)
    return d.refresh_catalog({}, ["openrouter"], public_only=True, path=tmp_path / "catalog.json")


@pytest.mark.parametrize("public_only", [False, True])
def test_persisted_catalog_omits_paid_unknown_and_unreviewed_routes(monkeypatch, tmp_path, public_only):
    spec = provider()
    install(monkeypatch, spec, [model(), model("paid:free", "1"),
        {"id": "unknown:free"}, model("unreviewed", "0"), model("dynamic:free", "-1")])
    path = tmp_path / "catalog.json"
    snapshot = d.refresh_catalog({}, ["openrouter"], public_only=public_only, path=path)
    entry = snapshot["providers"]["openrouter"]
    assert entry["status"] == "ok"
    assert entry["complete"] is True
    assert [row["id"] for row in entry["models"]] == ["example:free"]
    assert json.loads(path.read_text()) == snapshot
    assert "paid:free" not in path.read_text()
    assert "unknown:free" not in path.read_text()


def test_price_becomes_paid_replaces_previous_free_catalog_with_complete_empty(monkeypatch, tmp_path):
    first = refresh(monkeypatch, tmp_path, [model()])
    second = refresh(monkeypatch, tmp_path, [model(price="1")])
    row = second["providers"]["openrouter"]
    assert first["providers"]["openrouter"]["models"]
    assert row["status"] == "ok"
    assert row["complete"] is True
    assert row["models"] == []
    assert row["checked_at"] == row["last_attempt_at"]


def test_empty_upstream_remains_partial_and_preserves_free_last_good(monkeypatch, tmp_path):
    first = refresh(monkeypatch, tmp_path, [model()])
    second = refresh(monkeypatch, tmp_path, [])
    assert second["providers"]["openrouter"]["status"] == "partial"
    assert second["providers"]["openrouter"]["models"] == first["providers"]["openrouter"]["models"]
    assert second["providers"]["openrouter"]["checked_at"] == first["providers"]["openrouter"]["checked_at"]


def test_old_cache_projects_current_free_rules_even_when_refresh_fails(monkeypatch, tmp_path):
    path = tmp_path / "catalog.json"
    spec = provider()
    old = {"schema": 1, "providers": {"openrouter": {"models": [model(), model("old-paid:free", "2")],
        "checked_at": "2026-01-01T00:00:00+00:00", "complete": True, "status": "ok"},
        "retired-fixture": {"models": [model()]}}}
    path.write_text(json.dumps(old))
    monkeypatch.setattr(d, "load_registry", lambda *args, **kwargs: {"openrouter": spec})
    env = {"FREELLMPOOL_DISCOVERY_FILE": str(path)}
    loaded = d.load_discovery(env)
    assert list(loaded["providers"]) == ["openrouter"]
    assert loaded["providers"]["openrouter"]["models"] == [model()]
    assert json.loads(path.read_text()) == old  # Read-only status never rewrites state.
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(503))))
    failed = d.refresh_catalog(env, ["openrouter"])
    assert failed["providers"]["openrouter"]["models"] == [model()]
    assert failed["providers"]["openrouter"]["checked_at"] == old["providers"]["openrouter"]["checked_at"]
    assert "old-paid:free" not in path.read_text()


@pytest.mark.parametrize("changes", [{"status": "excluded"}, {"kind": "finite_trial"},
    {"hard_free_boundary": False}, {"allowed_modalities": ["embedding"]}])
def test_unsupported_grant_cannot_put_a_model_in_saved_catalog(monkeypatch, tmp_path, changes):
    spec = provider()
    spec["grants"][0].update(changes)
    snapshot = refresh(monkeypatch, tmp_path, [model()], spec)
    assert snapshot["providers"]["openrouter"]["models"] == []


def test_conditional_quota_catalog_does_not_attest_private_account(monkeypatch, tmp_path):
    spec = provider()
    now = datetime.now(UTC)
    for evidence in spec["evidence"]:
        evidence.update(checked_at=now.isoformat(), expires_at=(now + timedelta(days=7)).isoformat())
    spec["grants"][0].update(kind="recurring_quota", status="conditional", requires_account_evidence=True,
        required_account_tier="free", model_selector={"kind": "allowlist", "models": ["example"]})
    snapshot = refresh(monkeypatch, tmp_path, [model("example", "2")], spec)
    row = snapshot["providers"]["openrouter"]["models"][0]
    assert row["id"] == "example"
    admission = admit(spec, row, {}, now=now.timestamp())
    assert admission.allowed is False
    assert "account tier needs verification" in admission.reason
    assert "account" not in snapshot["providers"]["openrouter"]


def test_free_hint_without_zero_price_evidence_stays_out(monkeypatch, tmp_path):
    spec = provider()
    spec["grants"][0]["model_selector"] = {"kind": "allowlist", "models": ["unknown", "free", "paid"]}
    snapshot = refresh(monkeypatch, tmp_path, [
        {"id": "unknown", "is_free": True},
        {"id": "free", "is_free": True, "pricing": {"input": 0, "output": 0}},
        {"id": "paid", "is_free": True, "pricing": {"input": 2, "output": 0}},
    ], spec)
    assert [row["id"] for row in snapshot["providers"]["openrouter"]["models"]] == ["free"]


def test_complete_empty_free_catalog_removes_last_model_from_report_baseline():
    now = datetime.now(UTC)
    spec = provider()
    for evidence in spec["evidence"]:
        evidence.update(checked_at=now.isoformat(), expires_at=(now + timedelta(days=7)).isoformat())
    catalog = {"providers": {"openrouter": {"status": "ok", "complete": True,
        "catalog_access": "public", "checked_at": now.isoformat(), "models": [model()]}}}
    _, baseline = m.build_public_report({"openrouter": spec}, catalog, now=now, source_revision="a" * 40)
    catalog["providers"]["openrouter"]["models"] = []
    report, updated = m.build_public_report({"openrouter": spec}, catalog, baseline=baseline,
        now=now, source_revision="a" * 40)
    assert updated["providers"]["openrouter"]["models"] == {}
    assert any(row["code"] == "model_removed" and row["subject"] == "example:free" for row in report["findings"])
    assert not any(row["code"] == "catalog_failed" for row in report["findings"])


@pytest.mark.parametrize("pricing", [{}, {"input": "0"}, {"input": "0", "output": "0", "request": "0.01"}])
def test_runtime_admission_uses_same_complete_zero_price_gate(pricing):
    spec = provider()
    spec["grants"][0]["model_selector"] = {"kind": "zero_price"}
    now = datetime.now(UTC)
    for evidence in spec["evidence"]:
        evidence.update(checked_at=now.isoformat(), expires_at=(now + timedelta(days=7)).isoformat())
    candidate = {**model("org/model:upstream"), "pricing": pricing, "is_free": True,
                 "upstream_provider": "upstream"}
    assert not model_matches_grant(spec["grants"][0], candidate)
    assert not admit(spec, candidate, now=now.timestamp()).allowed


def test_reviewed_allowlist_static_price_fills_only_missing_price():
    spec = provider()
    spec["grants"][0].update(model_selector={"kind": "allowlist", "models": ["reviewed"]},
                             pricing={"input": "0", "output": "0"})
    missing = {**model("reviewed"), "pricing": {}}
    assert d.free_catalog_models(spec, [missing, model("reviewed", "1")]) == [missing]
    spec["blocked_models"] = ["reviewed"]
    assert d.free_catalog_models(spec, [missing]) == []


@pytest.mark.parametrize("changes", [{"model_selector": {"kind": "all", "exclude": None}},
    {"allowed_modalities": [None]}, {"allowed_modalities": None}, {"status": {}}, {"kind": {}}])
def test_malformed_reviewed_grant_cannot_create_a_catalog_candidate(changes):
    grant = {**provider()["grants"][0], **changes}
    assert not model_matches_grant(grant, model())
