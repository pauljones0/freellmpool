"""Account reads provide private telemetry, never billing entitlement."""

import copy
import json
import stat
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freellmpool import account_observations as a
from freellmpool.provider_registry import load_registry

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
KEYS = {
    "openrouter": "OPENROUTER_API_KEY", "ollama": "OLLAMA_API_KEY",
    "vercel": "AI_GATEWAY_API_KEY", "mistral": "MISTRAL_ADMIN_API_KEY",
}
PAYLOADS = {
    "openrouter": {"data": {
        "label": "private-key-label", "creator_user_id": "private-person",
        "is_free_tier": False, "limit": 100, "limit_remaining": 74.5,
        "limit_reset": "monthly", "usage": 25.5, "usage_daily": 1.25,
        "usage_weekly": 10, "usage_monthly": 25.5,
        "byok_usage": 0, "byok_usage_daily": 0, "byok_usage_weekly": 0,
        "byok_usage_monthly": 0, "include_byok_in_limit": False,
        "expires_at": None,
        "rate_limit": {"requests": 1000, "interval": "1h"},
    }},
    "ollama": {"id": "87ecb8c0-dca9-43e9-ad19-5df87da47a40",
               "email": "private@example.com", "name": "private-person", "plan": "starter"},
    "vercel": {"balance": "4.25", "total_used": "0.75"},
    "mistral": {"requests_per_second": 1, "tokens_limits_by_model": {
        "mistral-small-latest": {"tokens_per_minute": 500000, "tokens_per_month": 1000000000}}},
}
OLLAMA_USAGE = {"limits": {"monthly": {"usage": 0, "models": []}}}


def environment(tmp_path, provider="openrouter"):
    return {"FREELLMPOOL_OBSERVATIONS_FILE": str(tmp_path / "private" / "observations.json"),
            KEYS[provider]: "test-observation-secret"}


def transport(monkeypatch, responder):
    monkeypatch.setattr(a, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(responder), follow_redirects=False))
    monkeypatch.setattr(a, "_now", lambda: NOW)


def facts(record):
    return {row["fact"]: row for row in record["observations"]}


@pytest.mark.parametrize(("provider", "method", "url", "auth"), [
    ("openrouter", "GET", "https://openrouter.ai/api/v1/key", "Authorization"),
    ("ollama", "POST", "https://ollama.com/api/me", "Authorization"),
    ("vercel", "GET", "https://ai-gateway.vercel.sh/v1/credits", "Authorization"),
    ("mistral", "GET", "https://api.mistral.ai/v1/admin/rate-limit", "x-api-key"),
])
def test_exact_read_contract_and_private_normalization(monkeypatch, tmp_path, provider, method, url, auth):
    env = environment(tmp_path, provider)
    env["OPENAI_BASE_URL"] = "https://untrusted.example/"
    calls = []
    def respond(request):
        calls.append(request)
        assert request.headers[auth].endswith("test-observation-secret")
        assert request.content == b""
        if provider == "ollama" and request.url.path == "/api/usage":
            assert request.method == "GET" and str(request.url) == "https://ollama.com/api/usage"
            return httpx.Response(200, json=OLLAMA_USAGE)
        assert request.method == method and str(request.url) == url
        return httpx.Response(200, json=PAYLOADS[provider])
    transport(monkeypatch, respond)
    result = a.refresh_accounts(env, [provider])
    assert len(calls) == (2 if provider == "ollama" else 1)
    assert set(result["providers"]) == set(load_registry())
    record = result["providers"][provider]
    assert record["status"] == "ok"
    assert record["checked_at"] == NOW.isoformat()
    assert record["expires_at"] == (NOW + timedelta(days=1)).isoformat()
    assert record["observations"]
    for row in record["observations"]:
        expected_url = ("https://ollama.com/api/usage" if provider == "ollama" and
                        row["fact"] != "account.plan" else url)
        assert row["source_url"] == expected_url
        assert row["checked_at"] == record["checked_at"]
        assert row["expires_at"] == record["expires_at"]
        assert row["unit"] and row["scope"]
    encoded = json.dumps(result)
    for private in ("test-observation-secret", "private-key-label", "private-person",
                    "private@example.com", PAYLOADS["ollama"]["id"]):
        assert private not in encoded
    assert stat.S_IMODE(a.default_observations_path(env).stat().st_mode) == 0o600
    assert not (tmp_path / "accounts.json").exists()


def test_openrouter_money_cannot_become_free_requests(monkeypatch, tmp_path):
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    result = a.refresh_accounts(environment(tmp_path), ["openrouter"])
    rows = facts(result["providers"]["openrouter"])
    assert rows["key.limit_remaining"]["value"] == "74.5"
    assert rows["key.limit_remaining"]["unit"] == "USD"
    assert rows["key.usage_daily"]["window"] == "day"
    assert rows["key.is_free_tier"]["value"] is False
    assert not any("rate_limit" in key or "purchased" in key for key in rows)
    assert a.quota_restrictions(result, "openrouter", {}, credential_ref="anything") == []


def test_vercel_mixed_credit_balance_cannot_establish_a_free_grant(monkeypatch, tmp_path):
    env = environment(tmp_path, "vercel")
    account_path = tmp_path / "accounts.json"
    account_path.write_text('{"schema":1,"providers":{"vercel":{"tier":"unknown"}}}')
    env["FREELLMPOOL_ACCOUNTS_FILE"] = str(account_path)
    before = account_path.read_bytes()
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["vercel"]))
    result = a.refresh_accounts(env, ["vercel"])
    rows = facts(result["providers"]["vercel"])
    assert set(rows) == {"team.balance", "team.total_used"}
    assert rows["team.balance"]["value"] == "4.25"
    assert rows["team.balance"]["unit"] == "gateway_credits"
    assert a.quota_restrictions(result, "vercel", {}, credential_ref="anything") == []
    assert account_path.read_bytes() == before
    assert "paid_balance_zero" not in json.dumps(result)
    assert "free_balance" not in json.dumps(result)


def test_null_budget_and_zero_balance_are_distinct(monkeypatch, tmp_path):
    body = {"data": {"is_free_tier": True, "limit": None, "limit_remaining": None,
                     "limit_reset": None, "usage": 0}}
    transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    rows = facts(a.refresh_accounts(environment(tmp_path), ["openrouter"])["providers"]["openrouter"])
    assert rows["key.limit"]["value"] is None
    assert rows["key.usage"]["value"] == "0"


def test_plan_and_usage_do_not_write_billing_attestations(monkeypatch, tmp_path):
    transport(monkeypatch, lambda request: httpx.Response(
        200, json=OLLAMA_USAGE if request.url.path == "/api/usage" else PAYLOADS["ollama"]))
    record = a.refresh_accounts(environment(tmp_path, "ollama"), ["ollama"])["providers"]["ollama"]
    rows = facts(record)
    assert rows["account.plan"]["value"] == "starter"
    assert set(rows) == {"account.plan", "account.reported_usage.monthly"}
    assert "paid_balance_zero" not in json.dumps(record)


def test_unknown_plan_does_not_retain_untrusted_strings(monkeypatch, tmp_path):
    body = {**PAYLOADS["ollama"], "plan": "private-person-malicious-plan"}
    transport(monkeypatch, lambda request: httpx.Response(
        200, json=OLLAMA_USAGE if request.url.path == "/api/usage" else body))
    record = a.refresh_accounts(environment(tmp_path, "ollama"), ["ollama"])["providers"]["ollama"]
    assert facts(record)["account.plan"]["value"] == "unknown"
    assert "private-person" not in json.dumps(record)


def test_ollama_live_go_field_spelling_is_supported_without_ambiguous_aliases(monkeypatch, tmp_path):
    body = {"ID": PAYLOADS["ollama"]["id"], "Plan": "starter", "Email": "private@example.com"}
    transport(monkeypatch, lambda request: httpx.Response(
        200, json=OLLAMA_USAGE if request.url.path == "/api/usage" else body))
    env = environment(tmp_path, "ollama")
    record = a.refresh_accounts(env, ["ollama"])["providers"]["ollama"]
    assert record["status"] == "ok"
    assert facts(record)["account.plan"]["value"] == "starter"
    body["plan"] = "max"
    record = a.refresh_accounts(env, ["ollama"])["providers"]["ollama"]
    assert record["status"] == "malformed"
    assert facts(record)["account.plan"]["value"] == "starter"


def test_mistral_requires_separate_credential_and_limits_remain_telemetry(monkeypatch, tmp_path):
    calls = []
    transport(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=PAYLOADS["mistral"]))
    env = {"MISTRAL_API_KEY": "inference-secret", "FREELLMPOOL_OBSERVATIONS_FILE": str(tmp_path / "o.json")}
    first = a.refresh_accounts(env, ["mistral"])
    assert first["providers"]["mistral"]["status"] == "auth_missing"
    assert not calls
    env["MISTRAL_ADMIN_API_KEY"] = "admin-secret"
    result = a.refresh_accounts(env, ["mistral"])
    rows = result["providers"]["mistral"]["observations"]
    assert rows[0]["scope"] == "organization"
    assert any(row.get("model_id") == "mistral-small-latest" for row in rows)
    assert a.quota_restrictions(result, "mistral", {"account_ref": "primary"}, credential_ref="anything") == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_failed_attempt_preserves_same_key_last_good_age(monkeypatch, tmp_path, status):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    original = a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]
    monkeypatch.setattr(a, "_now", lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(a, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="secret upstream body", headers={"Location": "https://evil.example"}))))
    record = a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]
    assert record["status"] != "ok"
    assert record["checked_at"] == original["checked_at"]
    assert record["expires_at"] == original["expires_at"]
    assert record["observations"] == original["observations"]
    assert record["last_attempt_at"] != original["last_attempt_at"]
    assert "secret upstream" not in json.dumps(record)


def test_partial_openrouter_response_does_not_renew_key_budget_evidence(monkeypatch, tmp_path):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    original = a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]
    transport(monkeypatch, lambda request: httpx.Response(200, json={"data": {"is_free_tier": True}}))
    monkeypatch.setattr(a, "_now", lambda: NOW + timedelta(hours=1))
    row = a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]
    assert row["status"] == "malformed"
    assert row["checked_at"] == original["checked_at"] and row["observations"] == original["observations"]


def test_new_or_removed_key_invalidates_old_observations_without_request(monkeypatch, tmp_path):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    a.refresh_accounts(env, ["openrouter"])
    env["OPENROUTER_API_KEY"] = "different-secret"
    record = a.load_observations(env)["providers"]["openrouter"]
    assert record["status"] == "credential_changed"
    assert record["checked_at"] is None and not record["observations"]
    del env["OPENROUTER_API_KEY"]
    assert not a.load_observations(env)["providers"]["openrouter"]["observations"]


def test_key_changed_during_refresh_discards_response(monkeypatch, tmp_path):
    env = environment(tmp_path)
    def respond(request):
        env["OPENROUTER_API_KEY"] = "new-key"
        return httpx.Response(200, json=PAYLOADS["openrouter"])
    transport(monkeypatch, respond)
    record = a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]
    assert record["status"] == "credential_changed"
    assert not record["observations"] and record["checked_at"] is None


def test_key_changed_while_another_provider_is_refreshing_is_also_invalidated(monkeypatch, tmp_path):
    env = environment(tmp_path)
    env["OLLAMA_API_KEY"] = "ollama-secret"
    def respond(request):
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json=PAYLOADS["openrouter"])
        env["OPENROUTER_API_KEY"] = "changed-after-openrouter-read"
        return httpx.Response(200, json=OLLAMA_USAGE if request.url.path == "/api/usage" else PAYLOADS["ollama"])
    transport(monkeypatch, respond)
    record = a.refresh_accounts(env, ["openrouter", "ollama"])["providers"]["openrouter"]
    assert record["status"] == "credential_changed" and not record["observations"]


def test_expiration_is_independent_and_failed_attempt_remains_visible(monkeypatch, tmp_path):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    a.refresh_accounts(env, ["openrouter"])
    monkeypatch.setattr(a, "_now", lambda: NOW + timedelta(days=2))
    record = a.load_observations(env)["providers"]["openrouter"]
    assert record["status"] == "stale" and record["checked_at"] == NOW.isoformat()


@pytest.mark.parametrize(("provider", "body"), [
    ("openrouter", {"data": {"is_free_tier": True, "limit": True}}),
    ("openrouter", {"data": {"is_free_tier": True, "usage": "NaN"}}),
    ("openrouter", {"data": {"is_free_tier": True, "usage": -1}}),
    ("openrouter", {"data": {"is_free_tier": "false"}}),
    ("openrouter", {"data": {"is_free_tier": True, "limit": 10, "limit_remaining": 20}}),
    ("openrouter", {"data": {"is_free_tier": True, "limit_reset": "quarterly"}}),
    ("ollama", {"plan": "starter"}),
    ("ollama", {"id": "not-an-id", "plan": "starter"}),
    ("vercel", {"balance": "4"}),
    ("vercel", {"balance": "Infinity", "total_used": "1"}),
    ("mistral", {"requests_per_second": True, "tokens_limits_by_model": {}}),
    ("mistral", {"requests_per_second": 1, "tokens_limits_by_model": []}),
    ("mistral", {"requests_per_second": 1, "tokens_limits_by_model": {
        "@mention": {"tokens_per_minute": 1, "tokens_per_month": 1}}}),
])
def test_malformed_or_conflicting_schema_never_refreshes(monkeypatch, tmp_path, provider, body):
    transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    record = a.refresh_accounts(environment(tmp_path, provider), [provider])["providers"][provider]
    assert record["status"] == "malformed"
    assert record["checked_at"] is None and not record["observations"]


def test_duplicate_fields_and_oversized_response_rejected(monkeypatch, tmp_path):
    transport(monkeypatch, lambda request: httpx.Response(200, content=b'{"data":{"is_free_tier":true,"is_free_tier":false}}'))
    env = environment(tmp_path)
    assert a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]["status"] == "malformed"
    monkeypatch.setattr(a, "_MAX_BYTES", 10)
    assert a.refresh_accounts(env, ["openrouter"])["providers"]["openrouter"]["status"] == "malformed"


def test_all_provider_coverage_and_unsupported_refreshes_do_not_send_keys(monkeypatch, tmp_path):
    def fail(request):
        pytest.fail("No supported credentials configured")
    transport(monkeypatch, fail)
    env = {"FREELLMPOOL_OBSERVATIONS_FILE": str(tmp_path / "o.json"), "GROQ_API_KEY": "secret"}
    result = a.refresh_accounts(env)
    assert set(result["providers"]) == set(load_registry())
    for provider, row in result["providers"].items():
        assert row["note"] and row["coverage"]
        assert row["status"] == ("auth_missing" if provider in KEYS else "unsupported")
    with pytest.raises(ValueError):
        a.refresh_accounts(env, ["unknown-provider"])


def test_corrupt_cache_cannot_replay_arbitrary_fields(monkeypatch, tmp_path):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    result = a.refresh_accounts(env, ["openrouter"])
    malicious = copy.deepcopy(result)
    malicious["providers"]["openrouter"]["raw_body"] = "private-data"
    a.default_observations_path(env).write_text(json.dumps(malicious))
    clean = a.load_observations(env)
    assert "private-data" not in json.dumps(clean)
    assert clean["providers"]["openrouter"]["status"] == "not_checked"


def test_corrupt_cache_cannot_claim_success_without_evidence(monkeypatch, tmp_path):
    env = environment(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(200, json=PAYLOADS["openrouter"]))
    result = a.refresh_accounts(env, ["openrouter"])
    row = result["providers"]["openrouter"]
    row.update(checked_at=None, expires_at=None, observations=[], credential_ref=None)
    a.default_observations_path(env).write_text(json.dumps(result))
    assert a.load_observations(env)["providers"]["openrouter"]["status"] == "not_checked"


def test_reflected_inference_key_in_admin_model_name_is_not_persisted(monkeypatch, tmp_path):
    env = environment(tmp_path, "mistral")
    env["MISTRAL_API_KEY"] = "private-inference-key"
    body = {"requests_per_second": 1, "tokens_limits_by_model": {
        "private-inference-key": {"tokens_per_minute": 100, "tokens_per_month": 200}}}
    transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    result = a.refresh_accounts(env, ["mistral"])
    assert result["providers"]["mistral"]["status"] == "malformed"
    assert "private-inference-key" not in json.dumps(result)


def test_network_errors_are_static(monkeypatch, tmp_path):
    def fail(request):
        raise httpx.ConnectError("private echo secret")
    transport(monkeypatch, fail)
    record = a.refresh_accounts(environment(tmp_path), ["openrouter"])["providers"]["openrouter"]
    assert record["status"] == "error"
    assert "private echo" not in json.dumps(record)
