"""Provider namespaces are valid model identities, not arbitrary report text."""

import copy
from datetime import UTC, datetime

import pytest

from freellmpool import maintenance as m

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
MODEL = "@cf/meta/llama-3.1-8b-instruct"
SECOND = "@cf/qwen/qwen3-30b-a3b-fp8"
URL = "https://developers.cloudflare.com/workers-ai/models/"


@pytest.fixture
def registry(monkeypatch):
    # A synthetic public catalog exercises the public artifact boundary too;
    # the real Cloudflare listing remains authenticated and private.
    result = {"cloudflare": {"id": "cloudflare", "credential_env": "CLOUDFLARE_API_TOKEN",
        "discovery": {"supports_public": True, "url": URL},
        "grants": [], "limits": [], "evidence": []}}
    monkeypatch.setattr(m, "_public_registry", lambda: result)
    return result


def catalog(models=None, *, access="public"):
    return {"providers": {"cloudflare": {"status": "ok", "complete": True,
        "catalog_access": access, "checked_at": NOW.isoformat(), "last_attempt_at": NOW.isoformat(),
        "models": models if models is not None else [{"id": MODEL, "pricing": {"input": "0"}}]}}}


def build(registry, data, *, private=False, baseline=None, proposals=None):
    report, saved = m.build_public_report(registry, data, _private=private,
        baseline=baseline, proposals=proposals, now=NOW)
    if private:
        # run_maintenance converts the reconciliation envelope before storage.
        saved["visibility"] = "private"
        m._validate_private_baseline(saved)
    else:
        m.validate_public_report(report)
        m.validate_public_baseline(saved)
    return report, saved


@pytest.mark.parametrize("private", [False, True])
def test_successful_namespaced_catalog_remains_complete(registry, private):
    data = catalog(access="authenticated" if private else "public")
    report, baseline = build(registry, data, private=private)
    assert report["providers"]["cloudflare"]["catalog"]["status"] == "ok"
    assert baseline["providers"]["cloudflare"]["models"] == {MODEL: {"input": "0"}}
    assert not any(row["code"] == "catalog_failed" for row in report["findings"])
    if private:
        registry["cloudflare"]["discovery"]["supports_public"] = False
        local = m.build_private_report(registry, data,
            env={"CLOUDFLARE_API_TOKEN": "test-secret"}, now=NOW)
        assert local["providers"]["cloudflare"]["catalog"]["status"] == "ok"
        assert not any(row["code"] == "catalog_failed" for row in local["findings"])
        public, _ = m.build_public_report(registry, data, now=NOW)
        assert public["providers"]["cloudflare"]["catalog"]["model_count"] == 0


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("change", ["model_added", "model_removed", "price_changed"])
def test_namespaced_model_deltas_survive_validation_and_recovery(registry, private, change):
    _, first = build(registry, catalog(), private=private)
    models = [{"id": MODEL, "pricing": {"input": "0"}}]
    if change == "model_added":
        models.append({"id": SECOND, "pricing": {}})
    elif change == "model_removed":
        models = []
    else:
        models[0]["pricing"] = {"input": "1"}
    changed, second = build(registry, catalog(models), baseline=first, private=private)
    assert [row["code"] for row in changed["findings"]] == [change]
    repeated, third = build(registry, catalog(models), baseline=second, private=private)
    assert repeated["findings"] == changed["findings"]
    recovered, fourth = build(registry, catalog(), baseline=third, private=private)
    assert recovered["findings"] == []
    assert recovered["resolutions"][0]["fingerprint"] == changed["findings"][0]["fingerprint"]
    assert fourth["pending_changes"] == []


def test_limit_model_namespace_is_valid_but_other_identifiers_stay_strict(registry):
    proposal = {"rule_id": "daily", "model_id": MODEL, "metric": "requests",
        "window_seconds": 86400, "old_capacity": 10, "new_capacity": 5}
    parsed = {"providers": {"cloudflare": {"status": "ok", "source_url": URL,
        "source_sha256": "a" * 64, "proposals": [proposal]}}}
    report, baseline = build(registry, catalog(), proposals=parsed)
    assert report["findings"][0]["subject"] == f"daily/{MODEL}"
    assert report["proposals"][0]["model_id"] == MODEL
    for field in ("provider", "rule_id", "metric"):
        bad = copy.deepcopy(report)
        bad["proposals"][0][field] = MODEL
        with pytest.raises(ValueError):
            m.validate_public_report(bad)
    bad = copy.deepcopy(baseline)
    bad["providers"][MODEL] = bad["providers"].pop("cloudflare")
    with pytest.raises(ValueError):
        m.validate_public_baseline(bad)


@pytest.mark.parametrize("identifier", [
    "@cf/model\n", "@cf/model\x00", "@cf/model`", "@cf/$(id)", "@cf/<tag>",
    "@cf/../private", "@cf/./model", "@cf//model", "@cf/", "@/model", "@@cf/model",
    "@everyone", "@cf/model@other", "/absolute/model", "../private", "https://example.invalid/model",
    "@cf/" + "x" * 253,
])
def test_unsafe_model_ids_never_enter_catalog_or_baseline(registry, identifier):
    report, _ = m.build_public_report(registry, catalog([{"id": identifier, "pricing": {}}]), now=NOW)
    assert report["providers"]["cloudflare"]["catalog"]["status"] == "partial"
    _, baseline = build(registry, catalog())
    baseline["providers"]["cloudflare"]["models"] = {identifier: {}}
    with pytest.raises(ValueError):
        m.validate_public_baseline(baseline)


def test_namespaces_cannot_be_injected_into_nonmodel_finding_subjects(registry):
    report, _ = build(registry, catalog())
    report["findings"] = [m._finding("cloudflare", "source_changed", MODEL, before="old", after="new")]
    with pytest.raises(ValueError):
        m.validate_public_report(report)


@pytest.mark.parametrize("identifier", ["~z-ai/glm-flash-latest", "gpt-oss:120b", "org/model:free", "@cf/model", "@vendor/model-v1"])
def test_existing_aliases_and_namespaced_model_forms_are_preserved(registry, identifier):
    report, baseline = build(registry, catalog([{"id": identifier, "pricing": {}}]))
    assert report["providers"]["cloudflare"]["catalog"]["status"] == "ok"
    assert identifier in baseline["providers"]["cloudflare"]["models"]
