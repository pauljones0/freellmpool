"""G16: privacy routing + redaction — labels, scrubbing, strict mode."""

from __future__ import annotations

import json

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool import privacy
from freellmpool.errors import AllProvidersExhausted


def test_policy_file_covers_all_registry_providers():
    registry = json.loads(privacy.REGISTRY_PATH.read_text()) if hasattr(privacy, "REGISTRY_PATH") else None
    assert registry is None  # policies live in their own file, not the quota registry
    assert privacy.validate_policies() == []
    ids = {p["id"] for p in json.loads(privacy.PROVIDER_REGISTRY_PATH.read_text())["providers"]}
    assert set(privacy.load_policies()["policies"]) == ids


def test_known_training_labels():
    assert privacy.training_policy("groq") == "api-no-train"
    assert privacy.training_policy("cohere") == "api-no-train"
    assert privacy.training_policy("cloudflare") == "api-no-train"
    assert privacy.training_policy("ollama") == "api-no-train"
    assert privacy.training_policy("gemini") == "trains-by-default"


def test_unknown_provider_fails_closed():
    assert privacy.training_policy("no-such-provider") == "unknown"


def test_policy_validation_rejects_bad_entries(tmp_path, monkeypatch):
    bad = {"schema": 1, "policies": {
        "x": {"training": "sometimes", "as_of": "yesterday", "source": 42, "retention": "?"}}}
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(bad))
    monkeypatch.setattr(privacy, "POLICIES_PATH", path)
    privacy.load_policies.cache_clear()
    try:
        assert len(privacy.validate_policies()) >= 3
    finally:
        privacy.load_policies.cache_clear()


REDACT_CASES = [
    ("key is sk-live-abcDEF1234567890 ok", "sk-live-abcDEF1234567890", "API_KEY"),
    ("token ghp_abcdefghij1234567890 here", "ghp_abcdefghij1234567890", "API_KEY"),
    ("aws AKIAIOSFODNN7EXAMPLE x", "AKIAIOSFODNN7EXAMPLE", "API_KEY"),
    ("groq gsk_abcDEF1234567890abcdef z", "gsk_abcDEF1234567890abcdef", "API_KEY"),
    ("Bearer sk-or-v1-abcdef1234567890", "sk-or-v1-abcdef1234567890", "BEARER"),
    ("mail me at jane.doe+work@example.com!", "jane.doe+work@example.com", "EMAIL"),
    ("call +1 (415) 555-2671 now", "(415) 555-2671", "PHONE"),
    ("ssn 123-45-6789 here", "123-45-6789", "SSN"),
    ("card 4242 4242 4242 4242 end", "4242 4242 4242 4242", "CARD"),
    ("password: s3cr3t-hunter2!", "s3cr3t-hunter2", "SECRET"),
    ('{"api_key": "abcdef1234567890"}', "abcdef1234567890", "SECRET"),
    ("?token=abcdef1234567890&x=1", "abcdef1234567890", "SECRET"),
    ("KEY='abcdef1234567890' done", "abcdef1234567890", "SECRET"),
]


@pytest.mark.parametrize("text,secret,kind", REDACT_CASES)
def test_redact_scrubs_secrets_and_pii(text, secret, kind):
    scrubbed, hits = privacy.redact_text(text)
    assert secret not in scrubbed
    assert f"[REDACTED_{kind}]" in scrubbed
    assert kind in hits


def test_redact_covers_provider_key_shapes():
    for secret in (
        "AIzaSyAbcDefGhIjKlMnOpQrStUvWx",
        "csk-abcDEF1234567890abcdef",
        "hf_abcDEF1234567890abcdef",
        "sk-or-abcDEF1234567890abcdef",
    ):
        scrubbed, hits = privacy.redact_text(f"key value {secret} here")
        assert secret not in scrubbed, secret
        assert "API_KEY" in hits, secret


def test_redact_messages_scrubs_tool_calls_and_function_call():
    secret = "sk-abcDEF1234567890abcdef"
    other = "csk-zyxWVU9876543210fedcba"
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": f'{{"query": "{secret}"}}'},
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "lookup", "arguments": f"key={secret}"},
        },
        {"role": "tool", "tool_call_id": "call-1", "content": f"result {secret}"},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": f"https://x.test/?q={other}"}}],
        },
    ]
    scrubbed, hits = privacy.redact_messages(messages)
    dumped = json.dumps(scrubbed)
    assert secret not in dumped
    assert other not in dumped
    assert "API_KEY" in hits


def test_redact_private_key_block():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIBsecret\n-----END RSA PRIVATE KEY-----"
    scrubbed, hits = privacy.redact_text("use " + block)
    assert "MIIBsecret" not in scrubbed and "PRIVATE" in hits


def test_redact_leaves_innocent_text_alone():
    for clean in ["my keyboard is loud", "monkey business as usual", "call me later",
                  "the token bucket refills", "a password manager helps", "version 1.2.3"]:
        scrubbed, hits = privacy.redact_text(clean)
        assert (scrubbed, hits) == (clean, []), clean


def _logged_only(monkeypatch):
    monkeypatch.setattr(privacy, "training_policy", lambda pid: "unknown")


def test_private_routes_to_no_train_only(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "training_policy",
                        lambda pid: "api-no-train" if pid == "beta" else "trains-by-default")
    pool = make_pool(tmp_path)
    reply = pool.chat([{"role": "user", "content": "hi"}], private=True)
    assert reply.provider_id == "beta"


def test_private_refuses_when_only_logging_routes(tmp_path, monkeypatch):
    _logged_only(monkeypatch)
    pool = make_pool(tmp_path)
    with pytest.raises(AllProvidersExhausted) as error:
        pool.chat([{"role": "user", "content": "hi"}], private=True)
    assert "private" in (error.value.client_message or "").lower()


def test_redact_scrubs_upstream_body_and_reports(tmp_path):
    bodies = []

    def post(url, headers, body, timeout):
        bodies.append(body)
        return successful()

    pool = make_pool(tmp_path, ids=("alpha",), post=post)
    reply = pool.chat([{"role": "user", "content": "my key is sk-live-abcDEF1234567890 ok?"}],
                      redact=True)
    sent = json.dumps(bodies[0])
    assert "sk-live-abcDEF1234567890" not in sent
    assert "[REDACTED_API_KEY]" in sent
    assert "API_KEY" in reply.redactions


def test_flags_off_leaves_request_untouched(tmp_path):
    bodies = []

    def post(url, headers, body, timeout):
        bodies.append(body)
        return successful()

    pool = make_pool(tmp_path, ids=("alpha",), post=post)
    reply = pool.chat([{"role": "user", "content": "call +1 (415) 555-2671"}])
    assert "(415) 555-2671" in json.dumps(bodies[0])
    assert reply.redactions == ()


def test_proxy_threads_privacy_flags(tmp_path, monkeypatch):
    import threading
    import urllib.request

    from freellmpool import proxy as proxy_module

    monkeypatch.setattr(privacy, "training_policy",
                        lambda pid: "api-no-train" if pid == "beta" else "trains-by-default")
    bodies = []

    def post(url, headers, body, timeout):
        bodies.append(body)
        return successful()

    pool = make_pool(tmp_path, post=post)
    httpd = proxy_module.serve(pool, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "k sk-live-abcDEF1234567890"}],
                             "redact": True, "private": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (localhost test)
            body = json.load(resp)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "sk-live-abcDEF1234567890" not in json.dumps(bodies[0])
    assert body["model"].startswith("beta/")
