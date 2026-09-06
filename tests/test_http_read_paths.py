"""Every maintenance HTTP adapter applies bounds before decompression output."""

import gzip
import json
import zlib

import httpx
import pytest
from test_discovery_stream_bounds import TrackedStream
from test_limit_sources import document, registry

from freellmpool import account_observations as a
from freellmpool import discovery as d
from freellmpool import limit_sources as limits
from freellmpool import policy_updates as policy
from freellmpool import workflow_health as workflow
from freellmpool.provider_registry import load_registry

KINDS = ["account", "policy", "limits", "workflow", "catalog", "source"]


def run_path(kind, client, monkeypatch):
    if kind == "account":
        with client:
            return a._read_endpoint(client, a._ENDPOINTS["openrouter"], {"Authorization": "Bearer test-key"})
    if kind == "policy":
        with client:
            return policy._fetch(client, "https://example.invalid/policy")
    if kind == "workflow":
        with client:
            return workflow._get(client, "https://example.invalid/workflow")
    if kind == "limits":
        monkeypatch.setattr(limits, "_client", lambda: client)
        return limits.collect_proposals(registry())["providers"]["groq"]
    monkeypatch.setattr(d, "_client", lambda: client)
    spec = load_registry()["openrouter"]
    if kind == "catalog":
        return d._attempt(spec, {}, public_only=True)
    spec["evidence"] = spec["evidence"][:1]
    return d.check_public_sources(registry={"openrouter": spec})["sources"][0]


@pytest.mark.parametrize("kind", KINDS)
def test_all_paths_bound_compressed_output_before_allocating(monkeypatch, kind):
    compressed = gzip.compress(b"x" * (8 * 1024 * 1024))
    stream = TrackedStream([compressed])
    constructor = zlib.decompressobj
    calls = []

    class Decoder:
        def __init__(self, *args, **kwargs):
            self.inner = constructor(*args, **kwargs)

        def decompress(self, data, max_length=0):
            calls.append(max_length)
            assert 0 < max_length <= 16385
            return self.inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    for module in (a, policy, limits, workflow):
        monkeypatch.setattr(module, "_MAX_BYTES", 16384)
    monkeypatch.setattr(d, "_MAX_RESPONSE_BYTES", 16384)
    monkeypatch.setattr(zlib, "decompressobj", Decoder)
    client = httpx.Client(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})))
    if kind in {"account", "policy", "workflow"}:
        with pytest.raises(ValueError):
            run_path(kind, client, monkeypatch)
    else:
        assert run_path(kind, client, monkeypatch)["status"] in {"partial", "error", "review_required"}
    assert calls and stream.closed


@pytest.mark.parametrize("kind", KINDS)
def test_all_paths_keep_normal_gzip_and_negotiate_supported_encodings(monkeypatch, kind):
    body = (document().encode() if kind == "limits" else
            json.dumps({"data": [{"id": "test/model:free", "pricing": {"input": "0", "output": "0"}}]}).encode()
            if kind == "catalog" else b"{}")
    stream = TrackedStream([gzip.compress(body)])

    def respond(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})

    result = run_path(kind, httpx.Client(transport=httpx.MockTransport(respond)), monkeypatch)
    if kind in {"catalog", "source", "limits"}:
        assert result["status"] == "ok"
    elif kind == "account":
        assert result == ("ok", {})
    elif kind == "policy":
        assert result == body
    else:
        assert result == {}
    assert stream.closed
