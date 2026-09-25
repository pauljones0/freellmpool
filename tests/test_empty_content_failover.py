"""Content-validated failover parity locks (FCM-router-v2 class).

Every serving path must fail over on a 200 with no usable content instead
of surfacing "": sync Pool (also covered in test_router.py), AsyncPool,
ManagedPool, and zero-delta streams (router empty-stream path). The check
lives at the serving layer, not in client.call: client.call returns the
Reply and each path records its own failure class ("empty completion"),
so tool-call, truncation, refusal, and content-filter replies keep their
distinct behavior. These tests pin that layered contract against drift.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import make_post, openai_body

from freellmpool import client as C
from freellmpool.aio import AsyncPool
from freellmpool.models import Model, Provider
from freellmpool.router import Pool

P = Provider(
    id="x",
    label="X",
    adapter="openai",
    base_url="https://x.test/v1",
    key_env="X_KEY",
    models=(Model("m"),),
)


def _call(body):
    def post(url, headers, payload, timeout):
        return C.HTTPResult(200, body, "")

    return C.call(
        P, "m", [{"role": "user", "content": "hi"}],
        api_key="k", env={}, post=post,
    )


def _async_post(script):
    """Mirror of test_aio._async_post: sync fake transport as awaitable."""
    sync = make_post(script)

    async def apost(url, headers, body, timeout):
        return sync(url, headers, body, timeout)

    apost.calls = sync.calls
    return apost


def _empty_stop_body():
    return {"choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "stop"}]}


def test_client_returns_empty_stop_for_serving_layer_to_judge():
    assert _call(_empty_stop_body()).text == ""


def test_client_returns_tool_call_reply_with_empty_content():
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "f", "arguments": "{}"}}]
    reply = _call({"choices": [{"message": {"role": "assistant", "content": None,
                                             "tool_calls": tc},
                                "finish_reason": "tool_calls"}]})
    assert reply.text == ""


def test_client_returns_truncated_filtered_refused_empties():
    assert _call({"choices": [{"message": {"role": "assistant", "content": ""},
                                "finish_reason": "length"}]}).text == ""
    assert _call({"choices": [{"message": {"role": "assistant", "content": ""},
                                "finish_reason": "content_filter"}]}).text == ""
    assert _call({"choices": [{"message": {"role": "assistant", "content": "",
                                             "refusal": "no"},
                                "finish_reason": "stop"}]}).text == ""


def test_async_fails_over_empty_stop_completion(providers, env, quota):
    apost = _async_post(
        {
            "alpha.test": (200, _empty_stop_body()),
            "beta.test": (200, openai_body("from beta")),
        }
    )
    pool = AsyncPool(Pool(providers, quota=quota, env=env), apost=apost)
    reply = asyncio.run(
        pool.achat([{"role": "user", "content": "hi"}], providers=["alpha", "beta"])
    )
    assert reply.text == "from beta"
    assert reply.provider_id == "beta"
    assert len(apost.calls) == 2


def test_managed_fails_over_empty_stop_completion(tmp_path):
    from test_managed_runtime import make_pool

    calls = []

    def post(url, headers, body, timeout):
        calls.append(url)
        if "alpha.test" in url and "/models" not in url:
            return C.HTTPResult(200, _empty_stop_body(), "")
        return C.HTTPResult(200, {"choices": [{"message": {
            "role": "assistant", "content": "from beta"}}], "usage": {}}, "")

    pool = make_pool(tmp_path, post=post)
    reply = pool.ask("hi")
    assert reply.text == "from beta"
    assert reply.provider_id == "beta"
    chat_calls = [u for u in calls if "/models" not in u]
    assert any("alpha.test" in u for u in chat_calls)  # alpha tried, emptied, skipped
    assert chat_calls[-1].startswith("https://beta.test")


def test_zero_delta_stream_raises_stop_iteration_first_next():
    def stream_post(url, headers, body, timeout):
        return (200, iter(["data: [DONE]"]))

    gen = C.stream_call(P, "m", [{"role": "user", "content": "hi"}],
                        api_key="k", env={}, stream_post=stream_post)
    with pytest.raises(StopIteration):
        next(gen)
