"""Automatic server tools must not bypass the selected free grant's exclusions."""

from dataclasses import replace

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool.errors import AllProvidersExhausted
from freellmpool.models import Model


def model_pool(tmp_path, monkeypatch, model, *, provider="groq", prohibited=("web_search", "paid_tools")):
    calls = []

    def post(*args):
        calls.append(args)
        return successful()

    def stream(*args):
        calls.append(args)
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}', "data: [DONE]"])

    pool = make_pool(tmp_path, ids=(provider,), post=post, stream_post=stream)
    pool._catalog_override = [replace(pool._catalog_override[0], models=(Model(model),))]
    pool._discovery_override["providers"][provider]["models"] = [
        {"id": model, "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}},
    ]
    pool._registry_override[provider]["grants"][0]["prohibited_addons"] = list(prohibited)
    monkeypatch.setattr(pool.conformance, "passes", lambda *_: True)
    return pool, calls


@pytest.mark.parametrize("model", ["groq/compound", "groq/compound-mini"])
@pytest.mark.parametrize("path", ["automatic", "pinned", "stream"])
def test_compound_is_held_before_dispatch_when_automatic_tools_conflict(tmp_path, monkeypatch, model, path):
    pool, calls = model_pool(tmp_path, monkeypatch, model)
    snapshot = pool.snapshot()
    assert snapshot.routes == ()
    assert "automatic built-in tools" in snapshot.providers[0]["reason"]
    assert "paid access" not in snapshot.providers[0]["reason"]
    with pytest.raises(AllProvidersExhausted):
        if path == "stream":
            list(pool.stream_chat([{"role": "user", "content": "fixture"}], model=model))
        else:
            pool.ask("fixture", **({"model": model} if path == "pinned" else {}))
    assert not calls
    assert pool.ledger.summary()["reservations"] == 0


@pytest.mark.parametrize("model,provider,prohibited", [
    ("openai/gpt-oss-120b", "groq", ["web_search", "paid_tools"]),
    ("openai/gpt-oss-20b", "groq", ["web_search", "paid_tools"]),
    ("llama-3.3-70b-versatile", "groq", ["web_search", "paid_tools"]),
    ("groq/compound", "alpha", ["web_search", "paid_tools"]),
    ("groq/compound", "groq", ["byok_fallback"]),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_guard_preserves_models_without_the_exact_builtin_conflict(tmp_path, monkeypatch, model, provider, prohibited, streaming):
    pool, calls = model_pool(tmp_path, monkeypatch, model, provider=provider, prohibited=prohibited)
    if streaming:
        assert list(pool.stream_chat([{"role": "user", "content": "fixture"}], model=model))[1] == "OK"
    else:
        assert pool.ask("fixture", model=model).text == "OK"
    assert len(calls) == 1
    assert "tools" not in calls[0][2]


@pytest.mark.parametrize("prohibited", [["web_search"], ["paid_tools"]])
@pytest.mark.parametrize("streaming", [False, True])
def test_pre_dispatch_recheck_also_enforces_builtin_conflict(tmp_path, monkeypatch, prohibited, streaming):
    pool, calls = model_pool(tmp_path, monkeypatch, "groq/compound", prohibited=())
    route = pool.snapshot().routes[0]
    route.grant["prohibited_addons"] = prohibited
    transport = pool._stream_for(route, {}) if streaming else pool._post_for(route, {})
    with pytest.raises(ValueError, match="automatic built-in tools"):
        transport("https://groq.test/v1/chat/completions", {}, {"model": route.model, "messages": [], "max_tokens": 16}, 1)
    assert not calls
    assert pool.ledger.summary()["reservations"] == 0
