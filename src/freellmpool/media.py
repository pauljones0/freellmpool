"""Bounded image accounting for vision requests.

Token bound (documented): parsed PNG/JPEG/GIF dimensions use the
high-detail tile formula ``tiles * 170 + 85`` with 512px tiles
(``tiles = ceil(w / 512) * ceil(h / 512)``). Remote URLs and formats
whose dimensions cannot be parsed use a flat ``REMOTE_IMAGE_TOKENS``
estimate. Data URLs larger than ``MAX_IMAGE_BYTES`` decoded are
rejected loudly — the gateway never downscales (no imaging dependency)
and never silently drops images.
"""

from __future__ import annotations

import base64
import binascii
import struct

MAX_IMAGE_BYTES = 5 * 1024 * 1024
REMOTE_IMAGE_TOKENS = 2000
UNKNOWN_IMAGE_TOKENS = 2000
_TILE = 512


def _decode_data_url(url: str) -> tuple[str, bytes]:
    """Split a data URL into (media_type, decoded bytes)."""
    if not url.startswith("data:"):
        raise ValueError("not a data URL")
    header, _, payload = url[5:].partition(",")
    if ";base64" not in header:
        raise ValueError("only base64 data URLs are supported")
    media_type = header.split(";")[0] or "application/octet-stream"
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 image payload") from exc
    return media_type, raw


def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if raw[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _gif_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 10 or raw[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", raw[6:10])
    return (width, height) if width > 0 and height > 0 else None


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 <= len(raw):
        if raw[pos] != 0xFF:
            return None
        marker = raw[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # start-of-frame: height/width follow
            height, width = struct.unpack(">HH", raw[pos + 5:pos + 9])
            return (width, height) if width > 0 and height > 0 else None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > len(raw):
            return None
        size = struct.unpack(">H", raw[pos + 2:pos + 4])[0]
        if size < 2:
            return None
        pos += 2 + size
    return None


def image_dimensions(url: str) -> tuple[int, int] | None:
    """Pixel dimensions for supported data URLs, else None (remote/unknown)."""
    if not url.startswith("data:"):
        return None
    try:
        _, raw = _decode_data_url(url)
    except ValueError:
        return None
    for parser in (_png_dimensions, _gif_dimensions, _jpeg_dimensions):
        dims = parser(raw)
        if dims is not None:
            return dims
    return None


def image_input_tokens(url: str) -> int:
    """Conservative input-token estimate for one image."""
    dims = image_dimensions(url)
    if dims is None:
        return REMOTE_IMAGE_TOKENS if not url.startswith("data:") else UNKNOWN_IMAGE_TOKENS
    width, height = dims
    tiles = -(-width // _TILE) * -(-height // _TILE)
    return tiles * 170 + 85


def check_image_url(url: str) -> None:
    """Reject oversize or malformed image payloads loudly."""
    if not url.startswith("data:"):
        return
    _, raw = _decode_data_url(url)  # raises on malformed payloads
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image too large: {len(raw)} bytes decoded exceeds the {MAX_IMAGE_BYTES}-byte bound")
