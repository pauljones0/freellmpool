"""Response limits must stop downloads, not merely reject buffered bodies."""

import copy
import gzip
import json
import zlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool.provider_registry import load_registry


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def install(monkeypatch, stream, *, limit, status=200, headers=None):
    spec = copy.deepcopy(load_registry()["openrouter"])
    spec["evidence"] = spec["evidence"][:1]
    monkeypatch.setattr(d, "load_registry", lambda *args, **kwargs: {"openrouter": spec})
    monkeypatch.setattr(d, "_MAX_RESPONSE_BYTES", limit)
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, stream=stream, headers=headers))))
    return spec


def test_catalog_overflow_closes_early_and_preserves_complete_prior_evidence(monkeypatch, tmp_path):
    stream = TrackedStream([b"x" * 1024] * 20)
    install(monkeypatch, stream, limit=1024)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    model = {"id": "test/model:free", "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"openrouter": {
        "status": "ok", "complete": True, "catalog_access": "public", "checked_at": old,
        "models": [model]}}}))
    row = d.refresh_catalog({}, ["openrouter"], public_only=True, path=path)["providers"]["openrouter"]
    assert row["status"] == "partial"
    assert row["models"] == [model] and row["checked_at"] == old and row["complete"] is True
    assert stream.closed and stream.consumed <= 2048


def test_source_overflow_closes_early_without_hash_or_renewal(monkeypatch):
    stream = TrackedStream([b"x" * 1024] * 20)
    install(monkeypatch, stream, limit=1024)
    row = d.check_public_sources()["sources"][0]
    assert row["status"] == "error" and row["sha256"] is None
    assert stream.closed and stream.consumed <= 2048


@pytest.mark.parametrize("kind", ["catalog", "source"])
def test_exact_limit_stream_remains_valid(monkeypatch, kind):
    content = (b'{"data":[{"id":"test/model:free","pricing":{"input":"0","output":"0"}}]}'
               if kind == "catalog" else b"<main>Current free terms</main>")
    stream = TrackedStream([content[index:index + 3] for index in range(0, len(content), 3)])
    spec = install(monkeypatch, stream, limit=len(content))
    row = d._attempt(spec, {}, public_only=True) if kind == "catalog" else d.check_public_sources()["sources"][0]
    assert row["status"] == "ok"
    assert stream.closed and stream.consumed == len(content)


@pytest.mark.parametrize("kind", ["catalog", "source"])
@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_failure_status_never_downloads_irrelevant_body(monkeypatch, kind, status):
    stream = TrackedStream([b"x" * 1024] * 20)
    spec = install(monkeypatch, stream, limit=1024, status=status)
    row = d._attempt(spec, {}, public_only=True) if kind == "catalog" else d.check_public_sources()["sources"][0]
    assert row["status"] != "ok"
    assert stream.closed and stream.consumed == 0


@pytest.mark.parametrize("kind", ["catalog", "source"])
def test_compressed_body_is_bounded_after_decoding(monkeypatch, kind):
    content = gzip.compress(b"x" * 4096)
    stream = TrackedStream([content])
    spec = install(monkeypatch, stream, limit=1024, headers={"content-encoding": "gzip"})
    row = d._attempt(spec, {}, public_only=True) if kind == "catalog" else d.check_public_sources()["sources"][0]
    assert row["status"] != "ok"
    assert stream.closed


@pytest.mark.parametrize("kind", ["catalog", "source"])
def test_oversized_declared_body_is_closed_before_reading(monkeypatch, kind):
    stream = TrackedStream([b"x" * 1024] * 20)
    spec = install(monkeypatch, stream, limit=1024, headers={"content-length": "20480"})
    row = d._attempt(spec, {}, public_only=True) if kind == "catalog" else d.check_public_sources()["sources"][0]
    assert row["status"] != "ok"
    assert stream.closed and stream.consumed == 0


@pytest.mark.parametrize("encoding,raw_deflate", [("gzip", False), ("deflate", False), ("deflate", True)])
@pytest.mark.parametrize("kind", ["catalog", "source"])
def test_supported_compression_remains_usable(monkeypatch, encoding, raw_deflate, kind):
    body = (b'{"data":[{"id":"test/model:free","pricing":{"input":"0","output":"0"}}]}'
            if kind == "catalog" else b"<main>Current free terms</main>")
    encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS if raw_deflate else zlib.MAX_WBITS)
    compressed = gzip.compress(body) if encoding == "gzip" else encoder.compress(body) + encoder.flush()
    stream = TrackedStream([compressed[index:index + 1] for index in range(len(compressed))])
    spec = install(monkeypatch, stream, limit=1024, headers={"content-encoding": encoding})
    row = d._attempt(spec, {}, public_only=True) if kind == "catalog" else d.check_public_sources()["sources"][0]
    assert row["status"] == "ok" and stream.closed


def test_gzip_bomb_never_allocates_unbounded_decoder_output(monkeypatch):
    content = gzip.compress(b"x" * (8 * 1024 * 1024))
    stream = TrackedStream([content])
    calls = []
    constructor = zlib.decompressobj

    class Decoder:
        def __init__(self, *args, **kwargs):
            self.inner = constructor(*args, **kwargs)

        def decompress(self, data, max_length=0):
            calls.append(max_length)
            assert 0 < max_length <= 16385
            return self.inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    install(monkeypatch, stream, limit=16384, headers={"content-encoding": "gzip"})
    monkeypatch.setattr(zlib, "decompressobj", Decoder)
    row = d.check_public_sources()["sources"][0]
    assert row["status"] == "error" and stream.closed and calls


def test_large_compressed_header_is_bounded_even_without_decoded_output(monkeypatch):
    valid = gzip.compress(b"small")
    content = valid[:3] + b"\x08" + valid[4:10] + b"x" * 4096 + b"\0" + valid[10:]
    stream = TrackedStream([content[index:index + 1024] for index in range(0, len(content), 1024)])
    install(monkeypatch, stream, limit=1024, headers={"content-encoding": "gzip"})
    row = d.check_public_sources()["sources"][0]
    assert row["status"] == "error" and stream.closed and stream.consumed <= 2048


@pytest.mark.parametrize("change", ["truncated", "trailing", "corrupt"])
def test_incomplete_or_ambiguous_compression_cannot_establish_evidence(monkeypatch, change):
    content = gzip.compress(b"<main>Free terms</main>")
    if change == "truncated":
        content = content[:-4]
    elif change == "trailing":
        content += b"unverified trailing bytes"
    else:
        content = content[:-8] + b"\0" * 8
    stream = TrackedStream([content])
    install(monkeypatch, stream, limit=1024, headers={"content-encoding": "gzip"})
    row = d.check_public_sources()["sources"][0]
    assert row["status"] == "error" and row["sha256"] is None and stream.closed


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate", "gzip, gzip"])
def test_unsupported_encoding_is_closed_without_reading(monkeypatch, encoding):
    stream = TrackedStream([b"untrusted bytes"])
    install(monkeypatch, stream, limit=1024, headers={"content-encoding": encoding})
    row = d.check_public_sources()["sources"][0]
    assert row["status"] == "error" and stream.closed and stream.consumed == 0
