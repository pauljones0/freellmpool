"""Reviewed candidates can survive documented omissions from model listings."""

import copy
import json

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool.free_policy import admit
from freellmpool.provider_registry import load_registry

MODELS = {"glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"}
PAID = {"id": "glm-5.3-flash", "pricing": {"input": "0.075", "output": "0.25"}}


def provider():
    spec = copy.deepcopy(load_registry()["zhipu"])
    spec["discovery"]["supplement_from_reviewed_grants"] = ["free"]
    return spec


def install(monkeypatch, spec, body, status=200):
    monkeypatch.setattr(d, "load_registry", lambda *args, **kwargs: {"zhipu": spec})
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=body))))


def refresh(monkeypatch, tmp_path, spec, body=None, status=200):
    install(monkeypatch, spec, {"data": [PAID]} if body is None else body, status)
    return d.refresh_catalog({"ZHIPU_API_KEY": "fixture-key"}, ["zhipu"],
                             path=tmp_path / "catalog.json")["providers"]["zhipu"]


def test_complete_listing_preserves_listed_model_and_adds_only_reviewed_missing_ids(monkeypatch, tmp_path):
    spec = provider()
    before = copy.deepcopy(spec)
    row = refresh(monkeypatch, tmp_path, spec, {"data": [{"id": "glm-4.5-flash"}, PAID]})
    assert row["status"] == "ok" and row["complete"] is True
    models = {model["id"]: model for model in row["models"]}
    assert set(models) == MODELS
    assert "listing_source" not in models["glm-4.5-flash"]["metadata"]
    for name in MODELS - {"glm-4.5-flash"}:
        candidate = models[name]
        assert candidate["pricing"] == {}  # Current grant supplies prices at admission.
        assert candidate["supports_tools"] is None and candidate["stream"] is None
        assert candidate["metadata"]["listing_source"] == "reviewed_policy"
        assert candidate["metadata"]["unlisted"] is True
        assert candidate["metadata"]["availability"] == "unverified"
        assert candidate["metadata"]["grant_id"] == "free"
        assert candidate["metadata"]["policy_evidence"] == [{
            key: spec["evidence"][0][key] for key in ("id", "url", "checked_at", "expires_at")
        }]
    assert "unlisted" in row["note"] and "unverified" in row["note"]
    assert spec == before


def test_complete_paid_only_listing_does_not_hide_all_reviewed_free_candidates(monkeypatch, tmp_path):
    row = refresh(monkeypatch, tmp_path, provider())
    assert {model["id"] for model in row["models"]} == MODELS


def test_advertised_positive_price_is_never_replaced_by_policy_candidate(monkeypatch, tmp_path):
    row = refresh(monkeypatch, tmp_path, provider(), {"data": [
        {"id": "glm-4.5-flash", "pricing": {"input": "1", "output": "0"}}, PAID,
    ]})
    assert {model["id"] for model in row["models"]} == MODELS - {"glm-4.5-flash"}


@pytest.mark.parametrize("changes", [
    {"status": "conditional"}, {"status": "unverified"}, {"kind": "recurring_quota"},
    {"hard_free_boundary": False}, {"paid_overage_possible": True},
    {"model_selector": {"kind": "all"}}, {"model_selector": {"kind": "suffix", "value": "flash"}},
    {"pricing": {"input": "1", "output": "0"}}, {"pricing": {"input": "0"}},
    {"pricing": {"input": False, "output": "0"}},
    {"pricing": {"input": "0", "output": "0", "request": "0.01"}},
    {"allowed_modalities": ["embedding"]}, {"evidence_ids": []}, {"evidence_ids": ["missing"]},
])
def test_unsafe_or_unreviewed_grants_cannot_supplement(monkeypatch, tmp_path, changes):
    spec = provider()
    spec["grants"][0].update(changes)
    assert refresh(monkeypatch, tmp_path, spec)["models"] == []


@pytest.mark.parametrize("changes", [
    {"status": "unknown"}, {"checked_at": "2026-09-05T00:00:00"},
    {"expires_at": None}, {"url": ""},
])
def test_missing_or_malformed_source_provenance_cannot_supplement(monkeypatch, tmp_path, changes):
    spec = provider()
    spec["evidence"][0].update(changes)
    assert refresh(monkeypatch, tmp_path, spec)["models"] == []


def test_expired_policy_candidates_remain_observable_but_cannot_be_admitted(monkeypatch, tmp_path):
    spec = provider()
    spec["evidence"][0].update(checked_at="2020-01-01T00:00:00+00:00", expires_at="2020-01-02T00:00:00+00:00")
    row = refresh(monkeypatch, tmp_path, spec)
    assert {model["id"] for model in row["models"]} == MODELS
    for model in row["models"]:
        assert model["metadata"]["policy_evidence"][0]["expires_at"] == "2020-01-02T00:00:00+00:00"
        decision = admit(spec, model)
        assert not decision.allowed and "expired" in decision.reason


@pytest.mark.parametrize("configured", [None, [], ["other"], "free", [None]])
def test_only_explicit_configured_grants_are_supplemented(monkeypatch, tmp_path, configured):
    spec = provider()
    spec["discovery"]["supplement_from_reviewed_grants"] = configured
    assert refresh(monkeypatch, tmp_path, spec)["models"] == []


@pytest.mark.parametrize("status,body", [
    (503, {}), (401, {}), (429, {}), (200, {"data": []}),
    (200, {"data": [{"missing": "id"}]}),
    (200, {"data": [PAID], "total_count": 2}),
    (200, {"data": [PAID], "links": {"next": "https://other.test/models"}}),
])
def test_failed_or_incomplete_listing_never_creates_or_renews_candidates(monkeypatch, tmp_path, status, body):
    spec = provider()
    monkeypatch.setattr(d, "_now", lambda: "2026-09-05T01:00:00+00:00")
    first = refresh(monkeypatch, tmp_path, spec)
    assert len(first["models"]) == 3
    monkeypatch.setattr(d, "_now", lambda: "2026-09-05T02:00:00+00:00")
    second = refresh(monkeypatch, tmp_path, spec, body, status)
    assert second["status"] != "ok"
    assert second["checked_at"] == first["checked_at"]
    assert second["models"] == first["models"]
    (tmp_path / "catalog.json").unlink()
    empty = refresh(monkeypatch, tmp_path, spec, body, status)
    assert empty["models"] == [] and empty["checked_at"] is None


@pytest.mark.parametrize("change", ["price", "configuration", "grant", "grant_id", "source"])
def test_loading_old_policy_candidates_revalidates_current_grant(monkeypatch, tmp_path, change):
    spec = provider()
    refresh(monkeypatch, tmp_path, spec)
    path = tmp_path / "catalog.json"
    before = path.read_text()
    if change == "price":
        spec["grants"][0]["pricing"]["input"] = "1"
    elif change == "configuration":
        spec["discovery"].pop("supplement_from_reviewed_grants")
    elif change == "grant":
        spec["grants"][0]["status"] = "unverified"
    elif change == "grant_id":
        spec["grants"][0]["id"] = "replacement"
    else:
        spec["evidence"][0]["status"] = "unknown"
    loaded = d.load_discovery({"FREELLMPOOL_DISCOVERY_FILE": str(path)})
    assert loaded["providers"]["zhipu"]["models"] == []
    assert path.read_text() == before


def test_cache_drops_a_model_removed_from_current_reviewed_allowlist(monkeypatch, tmp_path):
    spec = provider()
    refresh(monkeypatch, tmp_path, spec)
    spec["grants"][0]["model_selector"]["models"].remove("glm-4.5-flash")
    loaded = d.load_discovery({"FREELLMPOOL_DISCOVERY_FILE": str(tmp_path / "catalog.json")})
    assert {row["id"] for row in loaded["providers"]["zhipu"]["models"]} == MODELS - {"glm-4.5-flash"}


def test_cache_cannot_supply_fabricated_price_to_override_current_policy(monkeypatch, tmp_path):
    spec = provider()
    refresh(monkeypatch, tmp_path, spec)
    path = tmp_path / "catalog.json"
    snapshot = json.loads(path.read_text())
    for model in snapshot["providers"]["zhipu"]["models"]:
        model["pricing"] = {"input": "0", "output": "0"}
    path.write_text(json.dumps(snapshot))
    spec["grants"][0]["pricing"]["input"] = "1"
    assert d.load_discovery({"FREELLMPOOL_DISCOVERY_FILE": str(path)})["providers"]["zhipu"]["models"] == []
