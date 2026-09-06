"""Bounded HTTP body decoding shared by read-only maintenance adapters."""

from __future__ import annotations

import zlib

import httpx

ACCEPT_ENCODING = "gzip, deflate"


def bounded_response_bytes(response: httpx.Response, max_bytes: int) -> bytes:
    """Bound encoded and decoded bodies before allocating decompressor output."""
    length = response.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > max_bytes):
        raise ValueError("HTTP response is too large or has an invalid length")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError("Unsupported HTTP content encoding")
    if response.is_stream_consumed:
        # Injected HTTPX responses may already contain decoded test data. Real
        # streamed responses take the raw-byte path below and bypass its decoder.
        content = response.content
        if len(content) > max_bytes:
            raise ValueError("HTTP response is too large")
        return content
    body = bytearray()
    prefix = bytearray()
    encoded_bytes = 0
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None
    try:
        for chunk in response.iter_raw(chunk_size=min(65536, max_bytes + 1)):
            encoded_bytes += len(chunk)
            if encoded_bytes > max_bytes:
                raise ValueError("Encoded HTTP response is too large")
            if encoding == "deflate" and decoder is None:
                # Accept the standard zlib wrapper and the raw deflate variant.
                # Wait for the complete header even when transport chunks split it.
                prefix.extend(chunk)
                if len(prefix) < 2:
                    continue
                wrapped = prefix[0] & 15 == 8 and prefix[0] >> 4 <= 7 and (prefix[0] * 256 + prefix[1]) % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                chunk = bytes(prefix)
                prefix.clear()
            while chunk:
                decoded = decoder.decompress(chunk, max_bytes - len(body) + 1) if decoder else chunk
                if len(body) + len(decoded) > max_bytes:
                    raise ValueError("Decoded HTTP response is too large")
                body.extend(decoded)
                if decoder and decoder.unused_data:
                    raise ValueError("Trailing compressed HTTP data")
                chunk = decoder.unconsumed_tail if decoder else b""
        # No unbounded flush: a complete stream has emitted all output and its
        # end marker/checksum was validated by decompress(..., max_length).
        if encoding != "identity" and (decoder is None or not decoder.eof):
            raise ValueError("Incomplete compressed HTTP response")
    except zlib.error:
        raise ValueError("Invalid compressed HTTP response") from None
    return bytes(body)
