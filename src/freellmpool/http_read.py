"""Bounded HTTP body decoding shared by read-only maintenance adapters."""

from __future__ import annotations

import zlib
from typing import Any

import httpx

ACCEPT_ENCODING = "gzip, deflate"


def _decode_preamble(response: httpx.Response, max_bytes: int) -> str:
    length = response.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > max_bytes):
        raise ValueError("HTTP response is too large or has an invalid length")
    encoding = str(response.headers.get("content-encoding", "identity")).strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError("Unsupported HTTP content encoding")
    return encoding


class _BodyDecoder:
    """One tested chunk-driven machine behind the sync/async bounded readers."""

    def __init__(self, encoding: str, max_bytes: int) -> None:
        self._encoding = encoding
        self._max_bytes = max_bytes
        self._body = bytearray()
        self._prefix = bytearray()
        self._encoded_bytes = 0
        self._decoder: Any = (zlib.decompressobj(zlib.MAX_WBITS | 16)
                              if encoding == "gzip" else None)

    def feed(self, chunk: bytes) -> None:
        self._encoded_bytes += len(chunk)
        if self._encoded_bytes > self._max_bytes:
            raise ValueError("Encoded HTTP response is too large")
        if self._encoding == "deflate" and self._decoder is None:
            # Accept the standard zlib wrapper and the raw deflate variant.
            # Wait for the complete header even when transport chunks split it.
            self._prefix.extend(chunk)
            if len(self._prefix) < 2:
                return
            prefix = self._prefix
            wrapped = (prefix[0] & 15 == 8 and prefix[0] >> 4 <= 7
                       and (prefix[0] * 256 + prefix[1]) % 31 == 0)
            self._decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            chunk = bytes(prefix)
            self._prefix.clear()
        try:
            while chunk:
                decoded = (self._decoder.decompress(chunk, self._max_bytes - len(self._body) + 1)
                           if self._decoder else chunk)
                if len(self._body) + len(decoded) > self._max_bytes:
                    raise ValueError("Decoded HTTP response is too large")
                self._body.extend(decoded)
                if self._decoder and self._decoder.unused_data:
                    raise ValueError("Trailing compressed HTTP data")
                chunk = self._decoder.unconsumed_tail if self._decoder else b""
        except zlib.error:
            raise ValueError("Invalid compressed HTTP response") from None

    def result(self) -> bytes:
        # No unbounded flush: a complete stream has emitted all output and its
        # end marker/checksum was validated by decompress(..., max_length).
        if self._encoding != "identity" and (self._decoder is None or not self._decoder.eof):
            raise ValueError("Incomplete compressed HTTP response")
        return bytes(self._body)


def bounded_response_bytes(response: httpx.Response, max_bytes: int) -> bytes:
    """Bound encoded and decoded bodies before allocating decompressor output."""
    encoding = _decode_preamble(response, max_bytes)
    if response.is_stream_consumed:
        # Injected HTTPX responses may already contain decoded test data. Real
        # streamed responses take the raw-byte path below and bypass its decoder.
        content = response.content
        if len(content) > max_bytes:
            raise ValueError("HTTP response is too large")
        return content
    decoder = _BodyDecoder(encoding, max_bytes)
    for chunk in response.iter_raw(chunk_size=min(65536, max_bytes + 1)):
        decoder.feed(chunk)
    return decoder.result()


async def abounded_response_bytes(response: httpx.Response, max_bytes: int) -> bytes:
    """Async twin of bounded_response_bytes over the same decode machine."""
    encoding = _decode_preamble(response, max_bytes)
    if response.is_stream_consumed:
        content = response.content
        if len(content) > max_bytes:
            raise ValueError("HTTP response is too large")
        return content
    decoder = _BodyDecoder(encoding, max_bytes)
    if isinstance(response.stream, httpx.AsyncByteStream):
        iterator = response.aiter_raw(chunk_size=min(65536, max_bytes + 1))
        async for chunk in iterator:
            decoder.feed(chunk)
    else:
        # In-memory sync test doubles served through an async client.
        for chunk in response.iter_raw(chunk_size=min(65536, max_bytes + 1)):
            decoder.feed(chunk)
    return decoder.result()
