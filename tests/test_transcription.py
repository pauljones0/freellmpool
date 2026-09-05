"""Pooled audio transcription: catalog, client.transcribe, Pool.transcribe, failover."""

from __future__ import annotations

import pytest

from freellmpool import client as C
from freellmpool.config import configured_transcribers, load_transcribers
from freellmpool.errors import AllProvidersExhausted, NoProvidersConfigured
from freellmpool.models import Model, Provider
from freellmpool.router import Pool


def _transcriber(tid: str, key_env: str | None) -> Provider:
    return Provider(
        id=tid,
        label=tid,
        adapter="openai",
        base_url=f"https://{tid}.test/v1",
        key_env=key_env,
        models=(Model("whisper-large-v3-turbo"),),
    )


def test_bundled_transcriber_catalog_has_no_reviewed_free_routes():
    assert load_transcribers() == []


def test_transcriber_catalog_loads(tmp_path):
    path = tmp_path / "transcribers.toml"
    path.write_text('''
[[transcriber]]
id = "alpha"
base_url = "https://alpha.test/v1"
key_env = "ALPHA_KEY"
models = [{ name = "accurate" }, { name = "fast", rpd = 100 }]
[[transcriber]]
id = "beta"
base_url = "https://beta.test/v1"
key_env = "BETA_KEY"
models = [{ name = "backup" }]
[[transcriber]]
id = "keyless"
base_url = "https://keyless.test/v1"
auth = "none"
models = [{ name = "fallback" }]
[[provider]]
id = "chat-only"
base_url = "https://chat.test/v1"
models = [{ name = "chat" }]
''')
    cat = load_transcribers(path)
    # Provider and model order determine transcription failover order.
    assert [t.id for t in cat] == ["alpha", "beta", "keyless"]
    assert [m.name for m in cat[0].models] == ["accurate", "fast"]
    assert cat[0].models[1].rpd == 100
    assert cat[0].key_env == "ALPHA_KEY"
    assert cat[-1].key_env is None and cat[-1].auth == "none"


def test_configured_transcribers_filter():
    cat = [
        _transcriber("alpha", "A_KEY"), _transcriber("beta", "B_KEY"),
        _transcriber("keyless", None),
    ]
    assert [t.id for t in configured_transcribers(cat, {"A_KEY": "x"})] == ["alpha", "keyless"]
    assert [t.id for t in configured_transcribers(cat, {"A_KEY": "x", "B_KEY": "y"})] == [
        "alpha", "beta", "keyless",
    ]
    assert [t.id for t in configured_transcribers(cat, {})] == ["keyless"]


def test_client_transcribe_shape():
    def post(url, headers, files, data, timeout):
        assert url.endswith("/audio/transcriptions")
        assert files["file"][0] == "a.wav"
        assert files["file"][1] == b"AUDIO"
        assert data["model"] == "whisper-large-v3-turbo"
        assert headers["Authorization"] == "Bearer k"
        return C.HTTPResult(200, {"text": "hello world"}, "")

    t = _transcriber("groq", "GROQ_API_KEY")
    reply = C.transcribe(
        t, "whisper-large-v3-turbo", b"AUDIO", "a.wav", api_key="k", env={}, post=post
    )
    assert reply.text == "hello world"
    assert reply.provider_id == "groq"


def test_client_transcribe_text_format_plain_body():
    def post(url, headers, files, data, timeout):
        # response_format=text → transport returns plain text body
        return C.HTTPResult(200, {"text": "plain transcript"}, "plain transcript")

    t = _transcriber("groq", "GROQ_API_KEY")
    reply = C.transcribe(
        t,
        "whisper-large-v3-turbo",
        b"A",
        "a.wav",
        api_key="k",
        env={},
        response_format="text",
        post=post,
    )
    assert reply.text == "plain transcript"


def test_client_transcribe_empty_text_is_success_not_failover():
    # Silent audio → {"text": ""} on HTTP 200 is a VALID (empty) transcription, not an error.
    # It must return "" rather than raise (which would force needless failover/exhaustion).
    def post(url, headers, files, data, timeout):
        return C.HTTPResult(200, {"text": ""}, "")

    t = _transcriber("groq", "GROQ_API_KEY")
    reply = C.transcribe(t, "whisper-large-v3", b"AUDIO", "a.wav", api_key="k", env={}, post=post)
    assert reply.text == ""
    assert reply.provider_id == "groq"


def test_client_transcribe_no_text_field_raises():
    # A 200 JSON dict with no text field is malformed → retryable error (drives failover),
    # even when the raw response body text is non-empty (must NOT pass raw JSON as a transcript).
    from freellmpool.errors import ProviderHTTPError

    def post(url, headers, files, data, timeout):
        return C.HTTPResult(200, {"unexpected": "shape"}, '{"unexpected":"shape"}')

    t = _transcriber("groq", "GROQ_API_KEY")
    with pytest.raises(ProviderHTTPError):
        C.transcribe(t, "whisper-large-v3", b"AUDIO", "a.wav", api_key="k", env={}, post=post)


def test_pool_transcribe_failover():
    def post(url, headers, files, data, timeout):
        if "alpha.test" in url:
            return C.HTTPResult(429, {"error": {"message": "rl"}}, "")
        return C.HTTPResult(200, {"text": "ok"}, "")

    transcribers = [_transcriber("alpha", "A_KEY"), _transcriber("beta", "B_KEY")]
    pool = Pool(
        [], env={"A_KEY": "a", "B_KEY": "b"}, transcribers=transcribers, transcribe_post=post
    )
    reply = pool.transcribe(b"AUDIO", "a.wav")
    assert reply.provider_id == "beta"  # alpha 429 → failover
    assert reply.text == "ok"


def test_pool_transcribe_skips_disabled_models():
    seen = []

    def post(url, headers, files, data, timeout):
        seen.append(data["model"])
        return C.HTTPResult(200, {"text": "ok"}, "")

    tr = _transcriber("alpha", "A_KEY")
    tr = Provider(**{**tr.__dict__, "models": (Model("retired", enabled=False), Model("working"))})
    reply = Pool(
        [], env={"A_KEY": "a"}, transcribers=[tr], transcribe_post=post
    ).transcribe(b"AUDIO", "a.wav")
    assert reply.model == "working"
    assert seen == ["working"]


def test_pool_transcribe_provider_pin():
    def post(url, headers, files, data, timeout):
        return C.HTTPResult(200, {"text": url}, "")

    transcribers = [_transcriber("alpha", "A_KEY"), _transcriber("beta", "B_KEY")]
    pool = Pool(
        [], env={"A_KEY": "a", "B_KEY": "b"}, transcribers=transcribers, transcribe_post=post
    )
    reply = pool.transcribe(b"AUDIO", "a.wav", providers=["beta"])
    assert reply.provider_id == "beta"
    assert "beta.test" in reply.text


def test_pool_transcribe_no_transcribers_raises():
    pool = Pool([], env={}, transcribers=[])
    with pytest.raises(NoProvidersConfigured):
        pool.transcribe(b"AUDIO", "a.wav")


def test_pool_transcribe_all_models_disabled_raises_no_providers():
    tr = _transcriber("alpha", "A_KEY")
    tr = Provider(**{**tr.__dict__, "models": (Model("retired", enabled=False),)})
    pool = Pool([], env={"A_KEY": "a"}, transcribers=[tr])
    with pytest.raises(NoProvidersConfigured):
        pool.transcribe(b"AUDIO", "a.wav")


def test_pool_transcribe_unknown_pin_raises_no_providers():
    # pinning a provider/model that matches nothing must be NoProvidersConfigured (→ 503),
    # not AllProvidersExhausted([]) (→ 502).
    def post(url, headers, files, data, timeout):
        return C.HTTPResult(200, {"text": "ok"}, "")

    pool = Pool(
        [], env={"A_KEY": "a"}, transcribers=[_transcriber("alpha", "A_KEY")], transcribe_post=post
    )
    with pytest.raises(NoProvidersConfigured):
        pool.transcribe(b"AUDIO", "a.wav", providers=["nonexistent"])
    with pytest.raises(NoProvidersConfigured):
        pool.transcribe(b"AUDIO", "a.wav", model="no-such-model")


def test_pool_transcribe_all_fail_raises():
    def post(url, headers, files, data, timeout):
        return C.HTTPResult(500, {}, "")

    pool = Pool(
        [], env={"A_KEY": "a"}, transcribers=[_transcriber("alpha", "A_KEY")], transcribe_post=post
    )
    with pytest.raises(AllProvidersExhausted) as exc_info:
        pool.transcribe(b"AUDIO", "a.wav")
    assert exc_info.value.client_status is None


@pytest.mark.parametrize(
    ("status", "message", "expected_client_status"),
    [
        (400, "bad audio input", 400),
        (402, "You have depleted your monthly included credits", None),
    ],
)
def test_pool_transcribe_classifies_nonretryable_error(
    status, message, expected_client_status
):
    def post(url, headers, files, data, timeout):
        return C.HTTPResult(status, {"error": {"message": message}}, "")

    pool = Pool(
        [],
        env={"A_KEY": "a"},
        transcribers=[_transcriber("alpha", "A_KEY")],
        transcribe_post=post,
    )
    with pytest.raises(AllProvidersExhausted) as exc_info:
        pool.transcribe(b"AUDIO", "a.wav")

    assert exc_info.value.client_status == expected_client_status
    if expected_client_status is not None:
        assert message in (exc_info.value.client_message or "")
