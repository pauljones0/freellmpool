"""Mid-stream proxy failures: 429-vs-other SSE cause (G41)."""

from __future__ import annotations

import json
import threading
import urllib.request

from helpers import make_post

from freellmpool.errors import AllProvidersExhausted, ProviderHTTPError
from freellmpool.proxy import serve
from freellmpool.router import Pool


def _serve(pool):
    httpd = serve(pool, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _stream_request_body(path: str) -> dict:
    if path == "/v1/responses":
        return {"model": "auto", "stream": True, "input": "hi"}
    if path == "/v1/chat/completions":
        return {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    return {
        "model": "claude-test",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
    }


def _sse_event_pairs(raw: str) -> list[tuple[str, dict]]:
    pairs = []
    for block in raw.split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name is not None and isinstance(data, dict):
            pairs.append((name, data))
    return pairs


def _delta_text(name: str, data: dict) -> str | None:
    if name == "response.output_text.delta":
        return data["delta"]
    if name == "content_block_delta":
        return data["delta"]["text"]
    return None


class FailingLines:
    """Yield one partial delta, then raise the scripted mid-stream error."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def __iter__(self):
        yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
        raise self._exc

    def close(self):
        return None


class ErrorLineStream:
    """Yield one delta line then a 429 error line — no raise, no [DONE]."""

    def __iter__(self):
        yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
        yield (
            'data: {"error": {"type": "rate_limit_exceeded", '
            '"message": "rate limited"}}'
        )

    def close(self):
        return None


def _pool_with_lines(providers, quota, env, calls, lines):
    def stream_post(url, headers, body, timeout):
        calls.append(url)
        if "alpha.test" in url:
            return 200, lines
        return 200, iter(
            [
                'data: {"choices":[{"delta":{"content":"should not happen"}}]}',
                "data: [DONE]",
            ]
        )

    return Pool(
        providers[:2],
        quota=quota,
        env=env,
        post=make_post({}),
        stream_post=stream_post,
    )


def _post_raw(base: str, path: str) -> str:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(_stream_request_body(path)).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as response:  # noqa: S310
        return response.read().decode()


def test_chat_midstream_429_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers,
        quota,
        env,
        calls,
        FailingLines(ProviderHTTPError(429, "rate limited", retryable=True)),
    )
    httpd, base = _serve(pool)
    try:
        body = _post_raw(base, "/v1/chat/completions")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "partial" in body
    assert "stream_rate_limited" in body
    assert "rate_limit_error" in body
    assert '"finish_reason": "stop"' not in body
    assert len(calls) == 1


def test_chat_midstream_other_keeps_generic_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers, quota, env, calls, FailingLines(RuntimeError("upstream exploded"))
    )
    httpd, base = _serve(pool)
    try:
        body = _post_raw(base, "/v1/chat/completions")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "partial" in body
    assert "stream_truncated" in body
    assert '"finish_reason": "stop"' not in body
    assert len(calls) == 1


def test_messages_midstream_429_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers,
        quota,
        env,
        calls,
        FailingLines(ProviderHTTPError(429, "rate limited", retryable=True)),
    )
    httpd, base = _serve(pool)
    try:
        raw = _post_raw(base, "/v1/messages")
    finally:
        httpd.shutdown()
        httpd.server_close()
    pairs = _sse_event_pairs(raw)
    names = [name for name, _data in pairs]
    assert any(_delta_text(name, data) == "partial" for name, data in pairs)
    assert names[-1] == "error"
    assert "message_stop" not in names
    assert pairs[-1][1]["error"]["type"] == "rate_limit_error"
    assert (
        pairs[-1][1]["error"]["message"]
        == "Upstream rate limit hit mid-stream; output is incomplete."
    )
    assert len(calls) == 1


def test_messages_midstream_other_keeps_generic_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers, quota, env, calls, FailingLines(RuntimeError("upstream exploded"))
    )
    httpd, base = _serve(pool)
    try:
        raw = _post_raw(base, "/v1/messages")
    finally:
        httpd.shutdown()
        httpd.server_close()
    pairs = _sse_event_pairs(raw)
    names = [name for name, _data in pairs]
    assert any(_delta_text(name, data) == "partial" for name, data in pairs)
    assert names[-1] == "error"
    assert pairs[-1][1]["error"]["type"] == "api_error"
    assert (
        pairs[-1][1]["error"]["message"] == "Upstream stream failed; output is incomplete."
    )
    assert len(calls) == 1


def test_responses_midstream_429_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers,
        quota,
        env,
        calls,
        FailingLines(ProviderHTTPError(429, "rate limited", retryable=True)),
    )
    httpd, base = _serve(pool)
    try:
        raw = _post_raw(base, "/v1/responses")
    finally:
        httpd.shutdown()
        httpd.server_close()
    pairs = _sse_event_pairs(raw)
    names = [name for name, _data in pairs]
    assert any(_delta_text(name, data) == "partial" for name, data in pairs)
    assert names[-1] == "response.failed"
    assert "response.completed" not in names
    error = pairs[-1][1]["response"]["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert error["message"] == "Upstream rate limit hit mid-stream; output is incomplete."
    assert len(calls) == 1


def test_responses_midstream_other_keeps_generic_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(
        providers, quota, env, calls, FailingLines(RuntimeError("upstream exploded"))
    )
    httpd, base = _serve(pool)
    try:
        raw = _post_raw(base, "/v1/responses")
    finally:
        httpd.shutdown()
        httpd.server_close()
    pairs = _sse_event_pairs(raw)
    names = [name for name, _data in pairs]
    assert any(_delta_text(name, data) == "partial" for name, data in pairs)
    assert names[-1] == "response.failed"
    error = pairs[-1][1]["response"]["error"]
    assert error["code"] == "server_error"
    assert error["message"] == "Upstream stream failed; output is incomplete."
    assert len(calls) == 1


def test_midstream_error_line_end_to_end_cause(providers, env, quota):
    calls = []
    pool = _pool_with_lines(providers, quota, env, calls, ErrorLineStream())
    httpd, base = _serve(pool)
    try:
        body = _post_raw(base, "/v1/chat/completions")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "partial" in body
    assert "stream_rate_limited" in body
    assert '"finish_reason": "stop"' not in body
    assert len(calls) == 1


def test_stream_rate_limited_matrix():
    from freellmpool.proxy import _stream_rate_limited

    assert _stream_rate_limited(AllProvidersExhausted([], client_status=429)) is True
    assert _stream_rate_limited(AllProvidersExhausted([], client_status=502)) is False
    assert _stream_rate_limited(ProviderHTTPError(429, "rate limited", retryable=True)) is True
    assert _stream_rate_limited(ProviderHTTPError(502, "x", retryable=False)) is False
    assert _stream_rate_limited(RuntimeError("boom")) is False
    assert _stream_rate_limited(ValueError("boom")) is False
