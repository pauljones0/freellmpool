"""G25 U1: keyless-403 verdict classifier (blocked vs auth_failed)."""

import copy

import httpx

from freellmpool import discovery as d
from freellmpool.provider_registry import load_registry

RECHECK = ("A later re-check via `freellmpool update --provider PROVIDER` "
           "re-verdicts; the verdict may persist. Run `freellmpool status` for the reason.")


def spec(pid="openrouter", **overrides):
    base = copy.deepcopy(load_registry()[pid])
    disc = {**base["discovery"], "url": "https://127.0.0.1:9/catalog"}
    disc.update(overrides.pop("discovery", {}))
    return {**base, "discovery": disc, **overrides}


def run(monkeypatch, provider, env, handler):
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {provider["id"]: provider})
    monkeypatch.setattr(d, "_aclient", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False))
    return d._attempt(provider, env)


def test_blocked_note_templates_exact(monkeypatch):
    kilo = spec("kilo", credential_env=None)

    def mitigated(request):
        return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, text="{}")

    row = run(monkeypatch, kilo, {}, mitigated)
    assert row["status"] == "blocked"
    assert row["note"] == ("HTTP 403: provider edge refused the listing "
                           "[Vercel mitigation observed]; no account action applies. " + RECHECK)
    assert row["complete"] is False and row["models"] == []

    def plain(request):
        return httpx.Response(403, text="{}")

    row = run(monkeypatch, kilo, {}, plain)
    assert row["status"] == "blocked"
    assert row["note"] == ("HTTP 403: keyless public listing refused (no edge-mitigation "
                           "header observed); no account action applies. " + RECHECK)

    pub = spec("openrouter", credential_env="OPENROUTER_API_KEY")
    row = run(monkeypatch, pub, {"OPENROUTER_API_KEY": "k"}, plain)
    assert row["status"] == "blocked"
    assert row["note"] == ("HTTP 403: keyless public listing refused (no edge-mitigation "
                           "header observed); keyed listing not attempted. " + RECHECK)

    keyed = spec("groq", credential_env="GROQ_API_KEY")
    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, mitigated)
    assert row["status"] == "blocked"
    assert "[Vercel mitigation observed]" in row["note"]


def test_classify_denied_fallback_is_401_only(monkeypatch):
    """H2: no 403 may reach the auth_failed fallback (choke-point invariant).

    A 403 never establishes an invalid credential (RFC 9110 15.5.4), even on
    paths today's _prepare_attempt cannot produce (unauthenticated private
    listing); such refusals are denied, never auth_failed.
    """
    status, note = d._classify_denied(403, {}, key_env="GROQ_API_KEY",
                                      key_present=True, authenticated=False,
                                      supports_public=False)
    assert status == "denied"
    assert "no key proven bad" in note
    assert "auth_failed" not in status
    status, _ = d._classify_denied(401, {}, key_env="GROQ_API_KEY",
                                   key_present=True, authenticated=True,
                                   supports_public=False)
    assert status == "auth_failed"


def test_keyed_401_403_split_auth_failed_denied(monkeypatch):
    # 018 contract change: this test pinned keyed-403 → auth_failed, the exact
    # falsehood root reproduced (a permission-denied key is not proven dead).
    # 401 keeps auth_failed; authenticated 403 without mitigation is denied.
    # Preserved intent: neither becomes blocked/WAF language without evidence.
    keyed = spec("groq", credential_env="GROQ_API_KEY")

    def rejected(request):
        return httpx.Response(401, text="{}")

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, rejected)
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401: authentication failed; the credential did not "
                            "authenticate; listing did not establish entitlement.")

    def forbidden(request):
        return httpx.Response(403, text="{}")

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, forbidden)
    assert row["status"] == "denied"


def test_keyed_403_without_mitigation_is_denied_not_dead(monkeypatch):
    """018: authenticated 403 (permission/scope) is denied, never auth_failed.

    HTTP 403 does not establish an invalid credential (RFC 9110 15.5.4): a
    scoped/valid key without listing permission must never read as dead.
    """
    keyed = spec("groq", credential_env="GROQ_API_KEY")

    def forbidden(request):
        return httpx.Response(403, json={"error": {
            "type": "permission_error", "code": "insufficient_scope",
            "message": "Authenticated credential lacks model-listing permission"}})

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, forbidden)
    assert row["status"] == "denied"
    assert row["note"] == ("HTTP 403: listing denied for this credential (often permission "
                           "scope or account verification); key NOT proven bad; listing did not "
                           "establish entitlement.")
    assert row["complete"] is False and row["models"] == []


def test_keyed_401_stays_auth_failed(monkeypatch):
    """018: authenticated 401 still proves the credential dead."""
    keyed = spec("groq", credential_env="GROQ_API_KEY")

    def rejected(request):
        return httpx.Response(401, json={"error": {
            "type": "authentication_error", "code": "invalid_api_key",
            "message": "Invalid API key"}})

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, rejected)
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401: authentication failed; the credential did not "
                           "authenticate; listing did not establish entitlement.")


def test_401_keyless_variants(monkeypatch):
    def denied(request):
        return httpx.Response(401, text="{}")

    kilo = spec("kilo", credential_env=None)
    row = run(monkeypatch, kilo, {}, denied)
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401: keyless public listing refused; the provider now "
                           "requires authentication and no credential applies to this provider.")

    pub = spec("openrouter", credential_env="OPENROUTER_API_KEY")
    row = run(monkeypatch, pub, {"OPENROUTER_API_KEY": "k"}, denied)
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401: keyless public listing refused; the provider now "
                           "requires authentication via OPENROUTER_API_KEY (keyed listing not "
                           "attempted). Run `freellmpool status` for the reason.")

    row = run(monkeypatch, pub, {}, denied)
    assert row["status"] == "auth_failed"
    assert row["note"] == ("HTTP 401: keyless public listing refused; the provider now "
                           "requires authentication via OPENROUTER_API_KEY. Add the credential, "
                           "then run `freellmpool update` to retry.")


def test_401_with_header_stays_auth_failed(monkeypatch):
    kilo = spec("kilo", credential_env=None)

    def denied(request):
        return httpx.Response(401, headers={"x-vercel-mitigated": "deny"}, text="{}")

    row = run(monkeypatch, kilo, {}, denied)
    assert row["status"] == "auth_failed"
    assert "requires authentication and no credential applies" in row["note"]


def test_cloudflare_keyed_403_is_denied(monkeypatch):
    # 018 contract change: was pinned auth_failed; an authenticated 403 is
    # denied (scope/verification), never dead-key proof.
    cf = spec("cloudflare", credential_env="CLOUDFLARE_API_TOKEN")
    cf["discovery"] = {**cf["discovery"], "supports_public": False,
                       "url": "https://127.0.0.1:9/" + "a" * 32 + "/catalog"}

    def denied(request):
        return httpx.Response(403, text="{}")

    env = {"CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}
    row = run(monkeypatch, cf, env, denied)
    assert row["status"] == "denied"
    assert row["note"].startswith("HTTP 403: listing denied for this credential")


def test_blocked_short_circuits_pages(monkeypatch):
    kilo = spec("kilo", credential_env=None)
    calls = []

    def pages(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={"data": [{"id": "kilo-auto/free"}],
                                             "links": {"next": "https://127.0.0.1:9/page2"}})
        return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, text="{}")

    row = run(monkeypatch, kilo, {}, pages)
    assert row["status"] == "blocked"
    assert row["models"] == []
    assert len(calls) == 2


def test_429_ignores_header(monkeypatch):
    kilo = spec("kilo", credential_env=None)

    def mitigated(request):
        return httpx.Response(429, headers={"x-vercel-mitigated": "deny"}, text="{}")

    def plain(request):
        return httpx.Response(429, text="{}")

    for handler in (mitigated, plain):
        row = run(monkeypatch, kilo, {}, handler)
        assert row["status"] == "rate_limited"
        assert row["note"] == "Listing rate limited; prior evidence age is unchanged."
        assert "fallback_models" not in row


def test_grant_fallback_models_ignores_malformed():
    assert d._grant_fallback_models({}) == []
    assert d._grant_fallback_models({"grants": "junk"}) == []
    assert d._grant_fallback_models({"grants": ["junk", {"status": "draft"}]}) == []
    assert d._grant_fallback_models({"grants": [{"status": "verified"}],
                                     "blocked_models": "junk"}) == []
    assert d._grant_fallback_models({"grants": [{"status": "verified",
                                                 "model_selector": {"models": "junk"}}]}) == []
    assert d._grant_fallback_models({"grants": [{"status": "verified",
                                                 "model_selector": {"models": ["a", "a"],
                                                                   "exclude": ["a"]}}]}) == []


def test_mitigation_evidence_is_presence_only(monkeypatch):
    kilo = spec("kilo", credential_env=None)

    def odd_value(request):
        return httpx.Response(403, headers={"x-vercel-mitigated": "1"}, text="{}")

    row = run(monkeypatch, kilo, {}, odd_value)
    assert row["status"] == "blocked"
    assert "[Vercel mitigation observed]" in row["note"]


def test_blocked_row_construction_pins(monkeypatch):
    kilo = spec("kilo", credential_env=None)

    def denied(request):
        return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, text="{}")

    row = run(monkeypatch, kilo, {}, denied)
    assert row["catalog_ttl_seconds"] == kilo["discovery"].get("catalog_ttl_seconds", 86400)
    assert row["catalog_access"] == "public"
    assert row["fallback_models"] == ["kilo-auto/free", "openrouter/free"]
    assert row["checked_at"] is None

    keyed = spec("groq", credential_env="GROQ_API_KEY")

    def denied_plain(request):
        return httpx.Response(403, text="{}")

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, denied_plain)
    assert row["status"] == "denied"  # 018: was pinned auth_failed
    assert "fallback_models" not in row
    assert "catalog_ttl_seconds" not in row

    def denied_header(request):
        return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, text="{}")

    row = run(monkeypatch, keyed, {"GROQ_API_KEY": "k"}, denied_header)
    assert row["status"] == "blocked"
    assert "catalog_access" not in row
    assert row["catalog_ttl_seconds"] == keyed["discovery"].get("catalog_ttl_seconds", 86400)
