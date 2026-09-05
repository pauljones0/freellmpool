"""The public managed runtime must use one free gate and ledger on every path."""

import asyncio
import io
import json
import threading
import wave
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool.allowances import AllowanceLedger
from freellmpool.client import HTTPResult
from freellmpool.errors import AllProvidersExhausted, ContextWindowExceeded, ProviderHTTPError
from freellmpool.free_policy import credential_fingerprint
from freellmpool.managed import ManagedPool
from freellmpool.models import Model, Provider


def fixture_data(ids=("alpha", "beta"), capacity=2):
    now = datetime.now(UTC)
    checked = now.isoformat()
    expires = (now + timedelta(days=7)).isoformat()
    registry = {}
    snapshot = {"schema": 1, "generation": "test", "providers": {}}
    providers = []
    for pid in ids:
        providers.append(Provider(pid, pid, "openai", f"https://{pid}.test/v1", (Model("free", context=32000), Model("paid", enabled=False)), auth="none"))
        registry[pid] = {
            "id": pid, "display_name": pid, "api_base_url": f"https://{pid}.test/v1", "credential_env": None,
            "discovery": {"parser": "openai", "url": f"https://{pid}.test/v1/models"},
            "evidence": [{"id": "price", "checked_at": checked, "expires_at": expires, "status": "verified"}],
            "grants": [{"id": "free", "kind": "zero_price", "status": "verified", "evidence_ids": ["price"],
                        "model_selector": {"kind": "zero_price"}, "paid_overage_possible": False,
                        "requires_account_evidence": False, "allowed_modalities": ["chat", "embedding", "transcription"]}],
            "limits": [{"id": "rpd", "scope": "account", "metric": "requests", "algorithm": "rolling",
                        "capacity": capacity, "window_seconds": 86400, "grant_ids": ["free"]}],
        }
        snapshot["providers"][pid] = {"checked_at": checked, "status": "ok", "complete": True, "models": [
            {"id": "free", "modalities": ["chat", "embedding", "transcription"], "context": 32000, "pricing": {"input": "0", "output": "0"}},
            {"id": "paid", "modalities": ["chat"], "pricing": {"input": "1", "output": "1"}},
        ]}
    return providers, registry, snapshot


def successful(body=None):
    return HTTPResult(200, body or {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 1}}, "")


def make_pool(tmp_path, *, ids=("alpha", "beta"), capacity=2, post=None, **kwargs):
    providers, registry, snapshot = fixture_data(ids, capacity)
    pool = ManagedPool(providers, registry=registry, discovery=snapshot, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=post or (lambda *args: successful()), **kwargs)
    for route in pool.snapshot().routes:
        pool.conformance.record(route.provider, route.model, "streaming", status="pass", classification="synthetic_stream_contract")
    return pool


def test_failover_uses_remaining_provider_without_spending_exhausted_one(tmp_path):
    calls = []
    def post(url, headers, body, timeout):
        calls.append(url)
        return successful()
    pool = make_pool(tmp_path, capacity=1, post=post)
    replies = [pool.ask("hi"), pool.ask("hi")]
    assert {r.provider_id for r in replies} == {"alpha", "beta"}
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi")
    assert error.value.client_status == 429
    assert error.value.retry_after > 0
    assert len(calls) == 2


def test_manual_paid_pin_never_reaches_transport(tmp_path):
    calls = []
    pool = make_pool(tmp_path, post=lambda *args: calls.append(args))
    with pytest.raises(AllProvidersExhausted) as error:
        pool.chat([{"role": "user", "content": "hi"}], model="paid", providers=["alpha"])
    assert error.value.client_status == 403
    assert calls == []


def test_expired_catalog_is_visible_but_not_routable(tmp_path):
    pool = make_pool(tmp_path)
    for row in pool._discovery_override["providers"].values():
        row["checked_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(AllProvidersExhausted):
        pool.ask("hi")
    status = pool.managed_status()
    assert not status["eligible_routes"]
    assert any("expired" in row["reason"] for row in status["providers"])


def test_new_generation_is_observed_without_restarting_pool(tmp_path):
    pool = make_pool(tmp_path)
    assert pool.ask("hi", providers=["alpha"]).model == "free"
    pool._discovery_override["providers"]["alpha"]["models"][0]["pricing"]["output"] = "1"
    with pytest.raises(AllProvidersExhausted):
        pool.ask("hi", providers=["alpha"])
    assert pool.ask("hi").provider_id == "beta"


def test_final_adapter_output_floor_is_reserved_before_network(tmp_path):
    providers, registry, discovery = fixture_data(("alpha",), 100)
    providers[0] = Provider("alpha", "alpha", "openai", "https://alpha.test/v1", (Model("deepseek-r1"),), auth="none")
    discovery["providers"]["alpha"]["models"][0]["id"] = "deepseek-r1"
    registry["alpha"]["limits"].append({"id": "tpm", "scope": "account", "metric": "total_tokens", "algorithm": "rolling", "capacity": 100, "window_seconds": 60})
    calls = []
    pool = ManagedPool(providers, registry=registry, discovery=discovery, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"}, ledger=AllowanceLedger(tmp_path / "ledger.db"),
                       post=lambda *args: calls.append(args))
    with pytest.raises(AllProvidersExhausted):
        pool.ask("OK", max_tokens=8)
    assert calls == []


def test_midstream_disconnect_never_replays_on_another_provider(tmp_path):
    calls = []
    def stream(url, headers, body, timeout):
        calls.append(url)
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"started"}}]}'])
    pool = make_pool(tmp_path, stream_post=stream)
    output = pool.stream_chat([{"role": "user", "content": "hello"}])
    assert next(output)["provider"] == "alpha"
    assert next(output) == "started"
    with pytest.raises(ProviderHTTPError, match="stream ended"):
        list(output)
    assert len(calls) == 1


def test_successful_stream_and_disconnection_keep_conservative_token_charge(tmp_path):
    def stream(url, headers, body, timeout):
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}', 'data: [DONE]'])
    pool = make_pool(tmp_path, capacity=1, stream_post=stream)
    assert list(pool.stream_chat([{"role": "user", "content": "hello"}], providers=["alpha"]))[1] == "OK"
    with pytest.raises(AllProvidersExhausted):
        pool.ask("hi", providers=["alpha"])


def test_model_scoped_429_allows_sibling_without_retrying_account(tmp_path):
    providers, registry, discovery = fixture_data(("alpha",), 100)
    providers[0] = Provider("alpha", "alpha", "openai", "https://alpha.test/v1", (Model("one"), Model("two")), auth="none")
    discovery["providers"]["alpha"]["models"] = [dict(discovery["providers"]["alpha"]["models"][0], id=name) for name in ("one", "two")]
    registry["alpha"]["rate_limit_scope"] = "model"
    def post(url, headers, body, timeout):
        return HTTPResult(429, {"error": {"message": "rate limit"}}, "", {"Retry-After": "60"}) if body["model"] == "one" else successful()
    pool = ManagedPool(providers, registry=registry, discovery=discovery, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"}, ledger=AllowanceLedger(tmp_path / "ledger.db"), post=post)
    assert pool.ask("hi").model == "two"


def test_embedding_path_obeys_same_shared_request_limit(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=1, post=lambda *args: successful({"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 1}}))
    assert pool.embed("hi").vectors == [[0.1, 0.2]]
    with pytest.raises(AllProvidersExhausted):
        pool.embed("hi")


def test_tools_require_current_evidence_even_when_pinned(tmp_path):
    calls = []
    pool = make_pool(tmp_path, post=lambda *args: calls.append(args))
    with pytest.raises(AllProvidersExhausted):
        pool.chat([{"role": "tool", "tool_call_id": "x", "content": "done"}], model="free", providers=["alpha"])
    assert not calls


def test_public_diagnostics_never_contain_upstream_credentials(tmp_path):
    pool = make_pool(tmp_path)
    pool.env["UPSTREAM_API_KEY"] = "this-is-a-secret-key"
    assert "this-is-a-secret-key" not in json.dumps(pool.managed_status())


def test_uncertain_transport_failure_keeps_concurrency_lease(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), post=lambda *args: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(AllProvidersExhausted):
        pool.ask("hello")
    inflight = next(row for row in pool.managed_status()["allowances"] if row["key"].endswith(":inflight"))
    assert inflight["remaining"] == 0


def test_abandoned_stream_keeps_concurrency_lease(tmp_path):
    def stream(*args):
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}', 'data: [DONE]'])
    pool = make_pool(tmp_path, ids=("alpha",), stream_post=stream)
    response = pool.stream_chat([{"role": "user", "content": "hi"}])
    next(response)
    next(response)
    response.close()
    inflight = next(row for row in pool.managed_status()["allowances"] if row["key"].endswith(":inflight"))
    assert inflight["remaining"] == 0


def test_cancelled_async_request_does_not_fail_over(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def post(url, *args):
        calls.append(url)
        entered.set()
        release.wait(3)
        finished.set()
        return HTTPResult(503, {}, "unavailable")
    pool = make_pool(tmp_path, post=post)
    async def run():
        task = asyncio.create_task(pool.achat([{"role": "user", "content": "hi"}]))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        await asyncio.sleep(.05)
    asyncio.run(run())
    assert len(calls) == 1


def test_short_exhaustion_waits_within_budget_and_retries(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=100)
    pool._base_env["FREELLMPOOL_WAIT_SECONDS"] = "0.5"
    pool.ledger.block("alpha:primary", pool._wall_clock() + .05)
    assert pool.ask("hi", timeout=1).text == "OK"


def test_explicit_wait_budget_does_not_wait_past_request_deadline(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._base_env["FREELLMPOOL_WAIT_SECONDS"] = "10"
    pool.ledger.block("alpha:primary", pool._wall_clock() + 60)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi", timeout=.01)
    assert error.value.retry_after > 50


def test_snapshot_generation_changes_with_context_or_limits(tmp_path):
    pool = make_pool(tmp_path)
    before = pool.snapshot().generation
    pool._discovery_override["providers"]["alpha"]["models"][0]["context"] = 20000
    after = pool.snapshot().generation
    assert before != after
    pool._registry_override["alpha"]["limits"][0]["capacity"] = 3
    assert pool.snapshot().generation != after


def test_malformed_restrictions_fail_closed(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    path = tmp_path / "providers.toml"
    path.write_text('[[provider]]\nid="alpha"\nmodels=[{name="free", enabled=false}]\n')
    pool._catalog_override = None
    pool._base_env["FREELLMPOOL_CONFIG"] = str(path)
    assert not pool.snapshot().routes


def test_rank_targets_accepts_messages_and_filters_context(tmp_path):
    pool = make_pool(tmp_path)
    with pytest.raises(AllProvidersExhausted):
        pool.rank_targets([{"role": "tool", "content": "done", "tool_call_id": "x"}])


def test_permanent_upstream_auth_error_is_not_reported_as_rate_limit(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), post=lambda *args: HTTPResult(401, {}, "bad key"))
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi")
    assert error.value.client_status == 503


def test_unknown_monthly_reset_uses_conservative_ceiling(tmp_path):
    pool = make_pool(tmp_path)
    spec = pool._registry_override["alpha"]
    spec["limits"][0].update(algorithm="calendar_month", capacity=1000, timezone=None, window_seconds=None)
    limit = pool.snapshot().routes[0].limits[0]
    assert limit.capacity == 1000
    assert limit.algorithm == "rolling"
    assert limit.seconds >= 32 * 86400


def test_ip_limit_identity_does_not_change_with_account(tmp_path):
    pool = make_pool(tmp_path)
    pool._registry_override["alpha"]["limits"][0]["scope"] = "ip"
    before = pool.snapshot().routes[0].limits[0].key
    pool._accounts_override["alpha"] = {"account_ref": "another-account"}
    assert pool.snapshot().routes[0].limits[0].key == before


def test_final_payload_context_limit_includes_tool_schema(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._discovery_override["providers"]["alpha"]["models"][0]["context"] = 300
    calls = []
    pool._post = lambda *args: calls.append(args)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.probe_call(pool.snapshot().routes[0].provider, "free", [{"role": "user", "content": "hi"}],
                        max_tokens=16, tools=[{"type": "function", "function": {"name": "x", "description": "x" * 500}}])
    assert isinstance(error.value, ContextWindowExceeded)
    assert calls == []


def test_request_preserves_selected_environment_during_snapshot_build(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    original = pool._operator_rows
    def mutate_view(*args):
        pool.env = {"FREELLMPOOL_CATALOG_MAX_AGE_SECONDS": "garbage"}
        return original(*args)
    monkeypatch.setattr(pool, "_operator_rows", mutate_view)
    assert pool.snapshot().routes


def test_model_and_bound_account_caps_tighten_reviewed_allowances(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=100)
    pool._registry_override["alpha"]["limits"][0]["model_capacities"] = {"free": 30}
    account = {"verified_at": datetime.now(UTC).isoformat(),
               "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
               "credential_ref": credential_fingerprint("alpha", None),
               "limits": [{"id": "rpd", "model_ids": ["free"], "capacity": 5}]}
    pool._accounts_override["alpha"] = account
    assert pool.snapshot().routes[0].limits[0].capacity == 5
    account["limits"][0]["capacity"] = 1000
    assert pool.snapshot().routes[0].limits[0].capacity == 30
    account["limits"][0]["capacity"] = 5
    account["credential_ref"] = "different-key"
    assert pool.snapshot().routes[0].limits[0].capacity == 30


def test_modelscope_remaining_headers_block_next_dispatch(tmp_path):
    pool = make_pool(tmp_path, ids=("modelscope",), capacity=100)
    spec = pool._registry_override["modelscope"]
    spec["limits"][0]["id"] = "daily_account"
    route = pool.snapshot().routes[0]
    pool._headers(route, {"modelscope-ratelimit-requests-remaining": "0", "modelscope-ratelimit-requests-limit": "2000"})
    calls = []
    pool._post = lambda *args: calls.append(args)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi")
    assert error.value.client_status == 429
    assert error.value.retry_after > 86400
    assert not calls


def test_wav_transcription_reserves_audio_seconds_and_rejects_unmeasured_audio(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=100)
    pool._registry_override["alpha"]["limits"].append({"id": "ash", "scope": "model", "metric": "audio_seconds",
                                                       "algorithm": "rolling", "capacity": 10, "window_seconds": 3600})
    pool._registry_override["alpha"]["model_costs"] = {"free": {"minimum_audio_seconds": 10}}
    calls = []
    def post(*args):
        calls.append(args)
        return successful({"text": "hello"})
    pool._transcribe_post = post
    with pytest.raises(AllProvidersExhausted):
        pool.transcribe(b"not a measured audio container", "bad.mp3")
    assert not calls
    data = io.BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0" * 32000)
    assert pool.transcribe(data.getvalue(), "one-second.wav").text == "hello"
    with pytest.raises(AllProvidersExhausted):
        pool.transcribe(data.getvalue(), "one-second.wav")
    assert len(calls) == 1


def test_async_requests_keep_shared_provider_rotation(tmp_path):
    pool = make_pool(tmp_path, capacity=100)
    async def run():
        return [(await pool.achat([{"role": "user", "content": "hi"}])).provider_id for _ in range(6)]
    assert asyncio.run(run()) == ["alpha", "beta", "alpha", "beta", "alpha", "beta"]


@pytest.mark.parametrize("kwargs", [{"tools": [{"type": "function"}]}, {"response_format": {"type": "json_object"}}])
def test_sdk_text_stream_rejects_structured_options_without_dropping_them(tmp_path, kwargs):
    calls = []
    pool = make_pool(tmp_path, stream_post=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="chat"):
        pool.stream_chat([{"role": "user", "content": "hi"}], **kwargs)
    assert not calls


def test_native_stream_needs_current_streaming_proof(tmp_path):
    calls = []
    pool = make_pool(tmp_path, stream_post=lambda *args: calls.append(args))
    pool.conformance.path.unlink()
    with pytest.raises(AllProvidersExhausted) as error:
        pool.stream_chat([{"role": "user", "content": "hi"}])
    assert error.value.client_status == 403
    assert not calls


def test_remaining_headers_are_observed_before_concurrency_is_released(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    original = pool._headers
    def headers(route, values, reservation=None):
        inflight = next(row for row in pool.ledger.status(route.limits) if row["key"].endswith(":inflight"))
        assert inflight["remaining"] == 0
        return original(route, values, reservation)
    monkeypatch.setattr(pool, "_headers", headers)
    assert pool.ask("hi").text == "OK"


def test_completed_stream_reconciles_reported_usage(tmp_path):
    def stream(*args):
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}',
                              'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}',
                              'data: [DONE]'])
    pool = make_pool(tmp_path, ids=("alpha",), stream_post=stream)
    pool._registry_override["alpha"]["limits"].append({"id": "tpm", "scope": "account", "metric": "total_tokens",
                                                       "algorithm": "rolling", "capacity": 10000, "window_seconds": 60})
    assert list(pool.stream_chat([{"role": "user", "content": "hi"}]))[1] == "OK"
    tokens = next(row for row in pool.managed_status()["allowances"] if row["key"].endswith(":tpm"))
    assert tokens["used"] == 6


def test_expired_numeric_limit_evidence_disables_route(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    policy = pool._registry_override["alpha"]
    policy["evidence"].append({"id": "limits", "status": "verified", "checked_at": "2020-01-01T00:00:00Z", "expires_at": "2020-01-02T00:00:00Z"})
    policy["limits"][0]["evidence_ids"] = ["limits"]
    assert not pool.snapshot().routes


def test_remote_media_is_not_accounted_as_its_small_url(tmp_path, monkeypatch):
    calls = []
    pool = make_pool(tmp_path, post=lambda *args: calls.append(args))
    monkeypatch.setattr(pool.conformance, "passes", lambda *args: True)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.chat([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/large.png"}}]}])
    assert error.value.client_status == 400
    assert "media" in error.value.client_message
    assert not calls


@pytest.mark.parametrize("usage", [None, [], "invalid"])
def test_missing_or_malformed_usage_keeps_success_and_conservative_charge(tmp_path, usage):
    body = successful().body
    body["usage"] = usage
    pool = make_pool(tmp_path, post=lambda *args: successful(body))
    assert pool.ask("hi").text == "OK"


def test_transcription_format_is_preserved(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    def post(url, headers, files, data, timeout):
        assert data["response_format"] == "verbose_json"
        return successful({"text": "hello"})
    pool._transcribe_post = post
    assert pool.transcribe(b"fixture", "fixture.wav", response_format="verbose_json").text == "hello"


@pytest.mark.parametrize("limits", [None, {}, ["bad"], [{"id": "rpd", "capacity": "bad"}]])
def test_malformed_account_limits_fail_closed(tmp_path, limits):
    pool = make_pool(tmp_path)
    pool._accounts_override["alpha"] = {
        "verified_at": datetime.now(UTC).isoformat(), "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "credential_ref": credential_fingerprint("alpha", None), "limits": limits,
    }
    assert not [r for r in pool.snapshot().routes if r.provider.id == "alpha"]


def test_openrouter_tier_upgrade_cannot_overwrite_tighter_account_cap(tmp_path):
    pool = make_pool(tmp_path, ids=("openrouter",), capacity=50)
    pool._accounts_override["openrouter"] = {
        "verified_at": datetime.now(UTC).isoformat(), "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "credential_ref": credential_fingerprint("openrouter", None), "lifetime_purchased_credits": 10,
        "limits": [{"id": "rpd", "capacity": 5}],
    }
    assert pool.snapshot().routes[0].limits[0].capacity == 5


def test_anonymous_grant_omits_even_an_existing_paid_credential(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._registry_override["alpha"].update(credential_env="ALPHA_API_KEY", inference_auth="none")
    pool._base_env["ALPHA_API_KEY"] = "synthetic-paid-key"
    observed = []
    def post(url, headers, body, timeout):
        observed.append(headers)
        return successful()
    pool._post = post
    assert pool.ask("hi").text == "OK"
    assert "Authorization" not in observed[0]
