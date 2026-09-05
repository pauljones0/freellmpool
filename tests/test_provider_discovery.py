"""Authoritative catalog discovery must never create billing entitlement."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool.provider_registry import load_registry


def test_registry_covers_audited_groups_with_independent_evidence():
    registry = load_registry()
    assert len(registry) == 16
    assert "github" not in registry
    for key, row in registry.items():
        assert row["id"] == key
        assert row["setup"]["signup_url"].startswith("https://")
        assert row["evidence"]
        assert row["grants"]
        assert all("expires_at" in evidence for evidence in row["evidence"])
        assert all("hard_free_boundary" in grant for grant in row["grants"])
    assert registry["gemini"]["limits"][0]["scope"] == "project"
    assert any(row["timezone"] == "America/Los_Angeles" for row in registry["gemini"]["limits"])
    assert all(grant["hard_free_boundary"] is True for provider in registry.values() for grant in provider["grants"])
    assert all(grant["kind"] in {"zero_price", "recurring_quota", "recurring_credit"} for provider in registry.values() for grant in provider["grants"])
    assert all(registry[name]["inference_auth"] == "none" for name in ("llm7", "ovh", "kilo", "opencode"))


def test_registry_rejects_unknown_inference_auth_before_runtime(monkeypatch, tmp_path):
    from freellmpool import provider_registry
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema": 1, "providers": [{"id": "test", "inference_auth": "automatic"}]}))
    monkeypatch.setattr(provider_registry, "REGISTRY_PATH", path)
    with pytest.raises(ValueError, match="authentication"):
        provider_registry.load_registry()


def test_model_normalization_preserves_prices_and_exact_route_identity():
    rows = d.normalize_models("fixture", {"data": [
        {"id": "org/model:free", "pricing": {"input": 0, "output": 0}, "supports_tools": True},
        {"id": "org/model:paid", "pricing": {"input": "0.000001", "output": "0.000002"}},
    ]})
    assert {row["id"] for row in rows} == {"org/model:free", "org/model:paid"}
    assert rows[0]["is_free"] is True
    assert rows[0]["supports_tools"] is True
    assert rows[1]["is_free"] is False
    assert rows[1]["pricing"]["input"] == "0.000001"


def test_missing_price_is_unknown_and_contradiction_is_not_free():
    rows = d.normalize_models("fixture", {"data": [
        {"id": "priced", "is_free": True, "pricing": {"input": 1, "output": 0}},
        {"id": "unknown", "is_free": True},
    ]})
    assert rows[0]["is_free"] is False
    assert rows[1]["pricing"] == {}
    assert rows[1]["is_free"] is None


def test_unrecognized_price_dimension_cannot_be_silently_free():
    rows = d.normalize_models("openrouter", {"data": [{"id": "m:free", "pricing": {
        "prompt": "0", "completion": "0", "platform_fee": "0.01"}}]})
    assert rows[0]["is_free"] is False
    assert rows[0]["metadata"]["pricing_unknown"] is True


def test_unknown_fee_overrides_free_hint_and_marks_uncertainty():
    rows = d.normalize_models("fixture", {"data": [{"id": "m", "is_free": True,
        "pricing": {"input": 0, "output": 0, "service_fee": "unknown"}}]})
    assert rows[0]["is_free"] is False
    assert rows[0]["metadata"]["pricing_unknown"] is True


def test_cached_input_price_alias_is_preserved():
    rows = d.normalize_models("aion", {"models": [{"id": "m", "pricing": {
        "prompt": "0", "completion": "0", "input_cache_read": "0.0000002"}}]})
    assert rows[0]["pricing"]["input_cache_reads"] == "0.0000002"
    assert rows[0]["is_free"] is False


def test_conflicting_price_alias_cannot_replace_paid_amount():
    with pytest.raises(ValueError, match="Duplicate"):
        d.normalize_models("openrouter", {"data": [{"id": "m:free", "pricing": {
            "input": "1", "prompt": "0", "completion": "0"}}]})


def test_zero_discount_does_not_hide_free_pricing_or_create_an_unknown_fee():
    rows = d.normalize_models("kilo", {"data": [{"id": "m:free", "pricing": {
        "prompt": "0", "completion": "0", "discount": 0}}]})
    assert rows[0]["is_free"] is True


@pytest.mark.parametrize(("cost", "free"), [("0", True), ("0.01", False)])
def test_tiered_vercel_price_requires_every_tier_free(cost, free):
    rows = d.normalize_models("vercel", {"data": [{"id": "m", "pricing": {
        "input": "0", "output": "0", "input_tiers": [{"cost": "0", "min": 0, "max": 10},
                                                    {"cost": cost, "min": 10}]}}]})
    assert rows[0]["is_free"] is free


def test_gemini_modalities_do_not_grant_chat_to_embeddings():
    rows = d.normalize_models("gemini", {"models": [
        {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/chat", "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 1234, "outputTokenLimit": 99},
    ]})
    assert rows[0]["id"] == "embedding"
    assert rows[0]["modalities"] == ["embedding"]
    assert rows[1]["context"] == 1234


def test_aion_native_listing_uses_models_array():
    rows = d.normalize_models("aion", {"models": [{"id": "aion-labs/aion-2.0",
        "context_length": 131072, "pricing": {"prompt": "0.0000008", "completion": "0.0000016"}}]})
    assert rows[0]["id"] == "aion-labs/aion-2.0"
    assert rows[0]["pricing"]["input"] == "0.0000008"


def transport(monkeypatch, responder):
    client = httpx.Client(transport=httpx.MockTransport(responder), follow_redirects=False)
    monkeypatch.setattr(d, "_client", lambda: client)
    return client


def test_authenticated_pagination_keeps_header_out_of_url(monkeypatch, tmp_path):
    calls = []
    def responder(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == "private-token"
        assert "private-token" not in str(request.url)
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "next"})
        return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]}]})
    transport(monkeypatch, responder)
    snapshot = d.refresh_catalog({"GEMINI_API_KEY": "private-token"}, ["gemini"], path=tmp_path / "d.json")
    entry = snapshot["providers"]["gemini"]
    assert entry["complete"] is True
    assert [m["id"] for m in entry["models"]] == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert len(calls) == 2
    assert "private-token" not in json.dumps(snapshot)


def test_failed_refresh_preserves_last_good_age_and_models(monkeypatch, tmp_path):
    path = tmp_path / "catalog.json"
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    previous = {"schema": 1, "generation": "old", "updated_at": old,
                "providers": {"groq": {"checked_at": old, "complete": True,
                                        "status": "ok", "models": [{"id": "keep", "modalities": ["chat"]}]}}}
    path.write_text(json.dumps(previous))
    transport(monkeypatch, lambda r: httpx.Response(401, text="PRIVATE echoed credentials"))
    snapshot = d.refresh_catalog({"GROQ_API_KEY": "PRIVATE"}, ["groq"], path=path)
    entry = snapshot["providers"]["groq"]
    assert entry["checked_at"] == old
    assert entry["models"] == [{"id": "keep", "modalities": ["chat"]}]
    assert entry["status"] == "auth_failed"
    assert entry["complete"] is True
    assert "PRIVATE" not in path.read_text()


@pytest.mark.parametrize("body", [{"data": []}, {"wrong": []}, {"data": [{"id": ""}]}])
def test_empty_or_malformed_catalog_cannot_erase_last_good(monkeypatch, tmp_path, body):
    path = tmp_path / "d.json"
    previous = {"schema": 1, "generation": "old", "providers": {
        "openrouter": {"checked_at": "2026-01-01T00:00:00+00:00", "complete": True,
                       "models": [{"id": "keep:free", "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}}], "status": "ok"}}}
    path.write_text(json.dumps(previous))
    transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = d.refresh_catalog({}, ["openrouter"], path=path)["providers"]["openrouter"]
    assert result["models"] == [{"id": "keep:free", "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}}]
    assert result["status"] == "partial"


def test_duplicate_model_ids_cannot_claim_a_complete_inventory(monkeypatch, tmp_path):
    transport(monkeypatch, lambda request: httpx.Response(200, json={
        "data": [{"id": "same"}, {"id": "same"}], "total_count": 2}))
    row = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "d.json")["providers"]["openrouter"]
    assert row["status"] == "partial"
    assert row["complete"] is False


def test_duplicate_json_price_keys_cannot_override_paid_price(monkeypatch, tmp_path):
    payload = b'{"data":[{"id":"m:free","pricing":{"prompt":"1","prompt":"0","completion":"0"}}]}'
    transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    row = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "d.json")["providers"]["openrouter"]
    assert row["status"] == "partial"
    assert row["models"] == []


@pytest.mark.parametrize("changes", [{"modalities": None}, {"modalities": "chat"}, {"context": "large"}, {"metadata": []}])
def test_corrupt_normalized_cache_shape_fails_closed(tmp_path, changes):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"openrouter": {
        "complete": True, "models": [{"id": "m", **changes}]}}}))
    assert d.load_discovery({"FREELLMPOOL_DISCOVERY_FILE": str(path)})["providers"] == {}


@pytest.mark.parametrize("tiers", [[], [{"cost": "0", "min": 1}], [{"cost": "0", "min": 0, "max": 10}],
                                    [{"cost": "0", "min": 0, "max": 10}, {"cost": "0", "min": 20}],
                                    [{"cost": "NaN", "min": 0}], [{"cost": "0", "min": False}]])
def test_incomplete_or_malformed_price_tiers_stay_unknown(tiers):
    row = d.normalize_models("vercel", {"data": [{"id": "m", "pricing": {
        "input": "0", "output": "0", "input_tiers": tiers}}]})[0]
    assert row["is_free"] is False
    assert row["metadata"]["pricing_unknown"] is True


def test_cross_origin_pagination_and_redirect_are_not_followed(monkeypatch, tmp_path):
    calls = []
    def responder(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "a"}], "links": {"next": "https://evil.example/models"}})
    transport(monkeypatch, responder)
    result = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "d.json")["providers"]["openrouter"]
    assert result["status"] == "partial"
    assert len(calls) == 1
    assert result["models"] == []


def test_public_only_omits_all_auth_and_skips_private_catalogs(monkeypatch, tmp_path):
    calls = []
    def responder(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "m"}]})
    transport(monkeypatch, responder)
    result = d.refresh_catalog({"GROQ_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
                               ["groq", "openrouter"], public_only=True, path=tmp_path / "d.json")
    assert result["providers"]["groq"]["status"] == "auth_missing"
    assert result["providers"]["openrouter"]["status"] == "ok"
    assert len(calls) == 1


def test_public_refresh_does_not_export_private_last_good_models(monkeypatch, tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"groq": {
        "models": [{"id": "private-account-route"}], "checked_at": "2026-01-01T00:00:00Z",
        "complete": True, "catalog_access": "authenticated", "status": "ok"}}}))
    transport(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "public"}]}))
    result = d.refresh_catalog({}, ["openrouter"], public_only=True, path=path)
    assert "groq" not in result["providers"]
    assert "private-account-route" not in path.read_text()


def test_default_public_refresh_uses_separate_path(monkeypatch, tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    private = d.default_discovery_path(env)
    private.parent.mkdir(parents=True)
    private.write_text('{"private": true}')
    transport(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "public"}]}))
    d.refresh_catalog(env, ["openrouter"], public_only=True)
    assert private.read_text() == '{"private": true}'
    assert private.with_name("public-discovery.json").exists()


@pytest.mark.parametrize("provider_id", ["aion", "modelscope", "nvidia"])
def test_observed_public_lists_never_claim_to_validate_an_api_key(monkeypatch, tmp_path, provider_id):
    def responder(request):
        assert "authorization" not in request.headers
        key = "models" if provider_id == "aion" else "data"
        return httpx.Response(200, json={key: [{"id": "m"}]})
    transport(monkeypatch, responder)
    result = d.refresh_catalog({}, [provider_id], public_only=True, path=tmp_path / "d.json")
    row = result["providers"][provider_id]
    assert row["status"] == "ok"
    assert "key validity" in row["note"]


def test_reviewed_groq_limits_include_model_specific_tokens_and_audio():
    groq = load_registry()["groq"]
    limits = {row["id"]: row for row in groq["limits"]}
    assert limits["tpm"]["model_capacities"]["openai/gpt-oss-120b"] == 8000
    assert limits["tpd"]["model_capacities"]["openai/gpt-oss-120b"] == 200000
    assert limits["rpm"]["capacity"] is None  # new models stay unknown
    assert limits["ash"]["metric"] == "audio_seconds"
    assert groq["model_costs"]["whisper-large-v3"]["minimum_audio_seconds"] == 10


def test_check_provider_is_get_only_and_listing_is_not_entitlement(monkeypatch):
    def responder(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": "m:free", "pricing": {"input": "0", "output": "0"}}]})
    transport(monkeypatch, responder)
    result = d.check_provider("openrouter", {})
    assert result["status"] == "ok"
    assert result["model_count"] == 1
    assert "entitlement" not in result
    assert "listing" in result["note"].lower()


def test_missing_cloudflare_account_or_bad_identifier_never_dispatches(monkeypatch):
    def responder(request):
        pytest.fail("Should not dispatch incomplete account template")
    transport(monkeypatch, responder)
    assert d.check_provider("cloudflare", {"CLOUDFLARE_API_KEY": "a"})["status"] == "auth_missing"


def test_load_invalid_snapshot_fails_closed_and_atomic_file_private(tmp_path, monkeypatch):
    path = tmp_path / "d.json"
    path.write_text('{"schema":999,"providers":{"x":{"models":[]}}}')
    assert d.load_discovery({"FREELLMPOOL_DISCOVERY_FILE": str(path)})["providers"] == {}
    transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    d.refresh_catalog({}, ["openrouter"], path=path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("price", ["not-a-price", "NaN", "Infinity", -1, True])
def test_invalid_advertised_price_is_not_silently_dropped(price):
    with pytest.raises(ValueError):
        d.normalize_models("openrouter", {"data": [{"id": "m", "pricing": {
            "prompt": "0", "completion": "0", "request": price}}]})


def test_dynamic_router_price_sentinel_is_preserved_as_unknown_not_zero():
    rows = d.normalize_models("openrouter", {"data": [{"id": "openrouter/auto", "pricing": {
        "prompt": "-1", "completion": "-1"}}]})
    assert rows[0]["pricing"] == {"input": "-1", "output": "-1"}
    assert rows[0]["is_free"] is False
    assert rows[0]["metadata"]["pricing_unknown"] is True


def test_source_changes_are_review_only_and_do_not_renew_registry(monkeypatch):
    before = load_registry()
    transport(monkeypatch, lambda request: httpx.Response(200, text="Changed pricing document"))
    result = d.check_public_sources(["gemini"])
    assert result["sources"]
    assert all(row["sha256"] for row in result["sources"])
    assert load_registry() == before
    assert "requires human review" in result["note"]


def test_cohere_and_cloudflare_pagination_are_complete(monkeypatch, tmp_path):
    calls = []
    def responder(request):
        calls.append(request)
        if "cohere" in request.url.host:
            assert request.headers["authorization"] == "Bearer cohere-secret"
            if "page_token" not in request.url.params:
                return httpx.Response(200, json={"models": [{"name": "cohere-a", "endpoints": ["chat"]}], "next_page_token": "p2"})
            return httpx.Response(200, json={"models": [{"name": "cohere-b", "endpoints": ["chat"]}]})
        assert request.headers["authorization"] == "Bearer cf-secret"
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"success": True, "result": [{"id": "opaque", "name": f"@cf/model-{page}", "task": {"name": "Text Generation"}}], "result_info": {"total_pages": 2}})
    transport(monkeypatch, responder)
    # A fresh client is needed per provider, as real context managers close it.
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(responder)))
    result = d.refresh_catalog({"COHERE_API_KEY": "cohere-secret", "CLOUDFLARE_API_TOKEN": "cf-secret", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, ["cohere", "cloudflare"], path=tmp_path / "d.json")
    assert len(calls) == 4
    assert {row["id"] for row in result["providers"]["cloudflare"]["models"]} == {"@cf/model-1", "@cf/model-2"}
    assert result["providers"]["cohere"]["models"][1]["modalities"] == ["chat"]


@pytest.mark.parametrize("provider_id", list(load_registry()))
def test_every_registry_parser_has_nonbillable_listing_fixture(provider_id):
    if provider_id == "gemini":
        body = {"models": [{"name": "models/m", "supportedGenerationMethods": ["generateContent"]}]}
    elif provider_id == "cohere":
        body = {"models": [{"name": "m", "endpoints": ["chat"]}]}
    elif provider_id == "ollama":
        body = {"models": [{"name": "m"}]}
    elif provider_id == "aion":
        body = {"models": [{"id": "m"}]}
    elif provider_id == "cloudflare":
        body = {"success": True, "result": [{"id": "opaque", "name": "m", "task": {"name": "Text Generation"}}]}
    else:
        body = {"data": [{"id": "m", "pricing": {"input": "0", "output": "0"}}]}
    rows = d.normalize_models(provider_id, body)
    assert len(rows) == 1
    assert rows[0]["id"] == "m"
    assert rows[0]["modalities"] == ["chat"]


def test_partial_second_page_and_repeated_page_preserve_old_evidence(monkeypatch, tmp_path):
    path = tmp_path / "d.json"
    calls = []
    def responder(request):
        calls.append(request)
        if len(calls) > 1:
            return httpx.Response(200, json={"wrong": []})
        return httpx.Response(200, json={"models": [{"name": "models/a"}], "nextPageToken": "p2"})
    transport(monkeypatch, responder)
    result = d.refresh_catalog({"GEMINI_API_KEY": "secret"}, ["gemini"], path=path)["providers"]["gemini"]
    assert result["status"] == "partial"
    assert result["models"] == []
    assert result["checked_at"] is None


@pytest.mark.parametrize(("status", "expected"), [(302, "partial"), (403, "auth_failed"), (429, "rate_limited"), (500, "error")])
def test_http_errors_are_safe_statuses(monkeypatch, status, expected):
    transport(monkeypatch, lambda r: httpx.Response(status, headers={"Location": "https://evil.example"}, text="secret response body"))
    result = d.check_provider("groq", {"GROQ_API_KEY": "secret"})
    assert result["status"] == expected
    assert "secret" not in json.dumps(result)


def test_unknown_provider_is_never_fetched(tmp_path):
    assert d.check_provider("unknown-fixture-provider", {})["status"] == "unsupported"
    with pytest.raises(ValueError):
        d.refresh_catalog({}, ["unknown-fixture-provider"], path=tmp_path / "d.json")


def test_model_specific_billing_facts_are_machine_readable():
    registry = load_registry()
    cf = registry["cloudflare"]
    assert len(cf["blocked_models"]) == 7
    assert cf["model_costs"]["@cf/zai-org/glm-4.7-flash"]["neurons_per_output_token"] == "0.0364"
    assert registry["zhipu"]["grants"][0]["pricing"] == {"input": "0", "output": "0"}
    assert "gemini-3.8-flash" in registry["gemini"]["grants"][0]["model_selector"]["models"]
