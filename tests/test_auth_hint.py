"""R3 auth hints name the key var without ever printing key material (G24 U4)."""

from freellmpool.errors import with_auth_hint


def test_auth_hint_names_key_on_401_and_403():
    assert with_auth_hint("HTTP 401: bad", provider_id="groq", key_env="GROQ_API_KEY",
                          status=401) == "HTTP 401: bad (check key GROQ_API_KEY)"
    assert with_auth_hint("HTTP 403: nope", provider_id="groq", key_env="GROQ_API_KEY_2",
                          status=403) == "HTTP 403: nope (check key GROQ_API_KEY_2)"


def test_auth_hint_keyless_names_rejection():
    assert with_auth_hint("HTTP 401: bad", provider_id="openrouter", key_env=None,
                          status=401) == "HTTP 401: bad (provider rejected the request)"


def test_auth_hint_leaves_other_statuses_unchanged():
    for status in (None, 429, 500, 503):
        assert with_auth_hint("detail", provider_id="p", key_env="K",
                              status=status) == "detail"
        assert with_auth_hint("detail", provider_id="p", key_env=None,
                              status=status) == "detail"
