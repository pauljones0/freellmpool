"""Adversarial regressions for async fairness and header/stream boundaries."""

import asyncio

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool.client import HTTPResult
from freellmpool.errors import AllProvidersExhausted


def test_sequential_async_requests_keep_one_global_fairness_sequence(tmp_path):
    pool = make_pool(tmp_path, capacity=100)

    async def run():
        return [(await pool.achat([{"role": "user", "content": "hi"}])).provider_id for _ in range(6)]

    assert asyncio.run(run()) == ["alpha", "beta", "alpha", "beta", "alpha", "beta"]


@pytest.mark.parametrize("options", [
    {"tools": [{"type": "function", "function": {"name": "record_number"}}]},
    {"response_format": {"type": "json_object"}},
])
def test_text_only_sdk_stream_rejects_structured_options_before_dispatch(tmp_path, options, monkeypatch):
    calls = []
    pool = make_pool(tmp_path, stream_post=lambda *args: calls.append(args))
    monkeypatch.setattr(pool.conformance, "passes", lambda *_: True)
    with pytest.raises(ValueError):
        pool.stream_chat([{"role": "user", "content": "hi"}], **options)
    assert not calls


def test_native_stream_requires_current_streaming_evidence(tmp_path):
    calls = []

    def stream(*args):
        calls.append(args)
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}', 'data: [DONE]'])

    pool = make_pool(tmp_path, stream_post=stream)
    pool.conformance.path.unlink(missing_ok=True)
    with pytest.raises(AllProvidersExhausted):
        list(pool.stream_chat([{"role": "user", "content": "hi"}]))
    assert not calls


def test_remaining_header_is_recorded_before_another_request_can_enter(tmp_path, monkeypatch):
    calls = []

    def post(*args):
        calls.append(args)
        return HTTPResult(200, successful().body, "", {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "1h",
        })

    pool = make_pool(tmp_path, ids=("groq",), capacity=100, post=post)
    settle = pool.ledger.settle
    nested = False

    def race(reservation, actual=None):
        nonlocal nested
        settle(reservation, actual)
        if not nested:
            nested = True
            # A second thread can reserve at this boundary, because settlement
            # releases the provider's concurrency lease. Emulate it precisely.
            try:
                pool.ask("next")
            except AllProvidersExhausted:
                pass

    monkeypatch.setattr(pool.ledger, "settle", race)
    assert pool.ask("first").text == "OK"
    assert len(calls) == 1


def test_default_transcription_transport_accepts_the_single_attempt_guard(tmp_path, monkeypatch):
    from freellmpool import client

    calls = []

    def once(*args):
        calls.append(args)
        return HTTPResult(200, {"text": "OK"}, "")

    monkeypatch.setattr(client, "_multipart_once", once)
    pool = make_pool(tmp_path, ids=("alpha",))
    assert pool.transcribe(b"synthetic audio", "test.wav").text == "OK"
    assert len(calls) == 1


def test_async_transport_accepts_an_awaitable_future(tmp_path):
    pool = make_pool(tmp_path)

    async def run():
        result = asyncio.get_running_loop().create_future()
        result.set_result(successful())
        return await pool.achat([{"role": "user", "content": "hi"}], apost=lambda *args: result)

    assert asyncio.run(run()).text == "OK"
