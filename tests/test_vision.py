"""G9: vision on the Anthropic bridge — translate, bound, never silently drop."""

from __future__ import annotations

import base64
import struct
import zlib

import pytest
from test_managed_runtime import make_pool

from freellmpool import media
from freellmpool.anthropic_shim import request_to_chat
from freellmpool.conformance import FEATURE_TOOLS, FEATURE_VISION, required_features
from freellmpool.errors import AllProvidersExhausted


def _png(width: int, height: int) -> bytes:
    def chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw * height)) + chunk(b"IEND", b""))


def test_png_dimensions_and_tile_tokens():
    url = "data:image/png;base64," + base64.b64encode(_png(100, 200)).decode()
    assert media.image_dimensions(url) == (100, 200)
    assert media.image_input_tokens(url) == 170 + 85  # one 512px tile + base


def test_large_image_costs_more_tiles():
    url = "data:image/png;base64," + base64.b64encode(_png(600, 600)).decode()
    assert media.image_input_tokens(url) == 4 * 170 + 85


def test_remote_url_gets_flat_estimate():
    assert media.image_input_tokens("https://example.com/pic.png") == media.REMOTE_IMAGE_TOKENS


def test_oversize_image_rejected():
    big = "data:image/png;base64," + "A" * (media.MAX_IMAGE_BYTES * 2)
    with pytest.raises(ValueError, match="too large"):
        media.check_image_url(big)


def test_shim_translates_base64_image():
    data = base64.b64encode(_png(10, 10)).decode()
    body = {"model": "auto", "max_tokens": 10, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "what color?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        ]}]}
    chat = request_to_chat(body)
    content = chat["messages"][-1]["content"]
    assert {"type": "text", "text": "what color?"} in content
    assert {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}} in content


def test_shim_translates_url_image():
    body = {"model": "auto", "max_tokens": 10, "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://example.com/pic.png"}},
        ]}]}
    chat = request_to_chat(body)
    content = chat["messages"][-1]["content"]
    assert {"type": "image_url", "image_url": {"url": "https://example.com/pic.png"}} in content


def test_shim_rejects_oversize_image_loudly():
    body = {"model": "auto", "max_tokens": 10, "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "A" * (media.MAX_IMAGE_BYTES * 2)}},
        ]}]}
    with pytest.raises(ValueError, match="too large"):
        request_to_chat(body)


def test_vision_and_tools_features_combine():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    feats = required_features(messages, tools=[{"type": "function", "function": {"name": "f"}}])
    assert {FEATURE_VISION, FEATURE_TOOLS} <= set(feats)


def _vision_messages():
    url = "data:image/png;base64," + base64.b64encode(_png(8, 8)).decode()
    return [{"role": "user", "content": [{"type": "text", "text": "what color?"},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


def test_no_vision_routes_is_honest_400(tmp_path):
    pool = make_pool(tmp_path)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.chat(_vision_messages(), max_tokens=10)
    assert error.value.client_status == 400
    assert "vision" in (error.value.client_message or "").lower()


def test_vision_request_succeeds_on_verified_route(tmp_path):

    pool = make_pool(tmp_path)
    route = pool.snapshot().routes[0]
    pool.conformance.record(route.provider, route.model, "vision",
                            status="pass", classification="verified")
    reply = pool.chat(_vision_messages(), max_tokens=10)
    assert reply.text == "OK"


def test_estimate_counts_image_tokens():
    from freellmpool.context import estimate_input_tokens

    url = "data:image/png;base64," + base64.b64encode(_png(8, 8)).decode()
    assert estimate_input_tokens(_vision_messages()) >= media.image_input_tokens(url)
