"""G32 Cloudflare token-verify disambiguation (spike v2.1).

Listing-401 on Cloudflare jointly authenticates (token, account ID); two
verify probes (A: account endpoint, B: user endpoint) disambiguate a dead
token from a wrong account ID from a scope problem. All tests offline via
counting MockTransport; no token/account values appear in notes.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
import pytest

from freellmpool import discovery as d

CF_KEY_ENV = "CLOUDFLARE_API_TOKEN"
CF_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
CF_TOKEN = "cf-probe-token-value"
CF_ACCOUNT = "c" * 32

H1_NOTE = ("HTTP 401 does not isolate a bad token from a wrong "
           "CLOUDFLARE_ACCOUNT_ID; key NOT proven bad")


def cf_env(token: str = CF_TOKEN, account: str = CF_ACCOUNT) -> dict[str, str]:
    return {CF_KEY_ENV: token, CF_ACCOUNT_ENV: account}


def verify_body(status: str = "active", success: bool = True) -> dict:
    return {"success": success, "result": {"id": "tok-id", "status": status}}


def install_cf_fake(monkeypatch: pytest.MonkeyPatch, *, listing, probe_a,
                    probe_b) -> list:
    """Route listing + verify URLs to per-endpoint canned answers.

    Each spec is (status, json-or-bytes) or an exception instance to raise.
    Returns the calls list.
    """
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/ai/models/search"):
            spec = listing
        elif path.endswith("/user/tokens/verify"):
            spec = probe_b
        elif path.endswith("/tokens/verify") and "/accounts/" in path:
            spec = probe_a
        else:  # pragma: no cover - routing is total over our URLs
            raise AssertionError(f"unexpected URL {request.url}")
        if isinstance(spec, Exception):
            raise spec
        status, payload = spec
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        d, "_aclient",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 follow_redirects=False))
    return calls


def install_counting(monkeypatch: pytest.MonkeyPatch, handler) -> list:
    """Route discovery HTTP through a counting mock transport. Returns calls."""
    calls: list = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        d, "_aclient",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(counting),
                                 follow_redirects=False))
    return calls


def url_kinds(calls: list) -> dict[str, int]:
    kinds = {"listing": 0, "a": 0, "b": 0}
    for request in calls:
        path = request.url.path
        if path.endswith("/ai/models/search"):
            kinds["listing"] += 1
        elif path.endswith("/user/tokens/verify"):
            kinds["b"] += 1
        elif "/tokens/verify" in path:
            kinds["a"] += 1
    return kinds


def check_cf(monkeypatch, env, slot=1, cache=":fresh:"):
    if cache == ":fresh:":
        cache = {}
    return d.check_provider_slot("cloudflare", env, slot, cf_probe_cache=cache)


# --- T1: verdict matrix (listing 401) ---

def test_pair_ok_is_denied_with_scope_fix(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(200, verify_body("active")),
                            probe_b=(401, {}))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["status"] == "auth_failed"
    assert row["note"] == ("pair verified at the account verify endpoint; "
                           "listing refused (scope or account verification)")
    assert row["fix"] == d.keys_check_scope_fix("cloudflare", 1)
    assert url_kinds(calls)["b"] == 0  # B skipped on A-ok


def test_wrong_account_is_config_error(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "config_error"
    assert row["status"] == "auth_failed"
    assert row["note"] == ("token valid at the user endpoint but rejected for "
                           "this account: re-verify CLOUDFLARE_ACCOUNT_ID "
                           "(or grant the token account access)")
    assert row["fix"] == d.KEYS_CHECK_ACCOUNT_ID_FIX
    assert url_kinds(calls) == {"listing": 1, "a": 1, "b": 1}


def test_dual_401_is_auth_failed_with_agreement_caveat(monkeypatch):
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=(401, {}), probe_b=(401, {}))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "auth_failed"
    assert row["status"] == "auth_failed"
    assert row["note"] == ("Cloudflare account and user verifiers both reject "
                           "this token (both agree: dead); replace the key")
    assert "both" in row["note"] and "agree" in row["note"]
    assert row["fix"] == d.keys_check_replace_fix("cloudflare", 1)


def test_expired_is_auth_failed_and_b_never_runs(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(200, verify_body("expired")),
                            probe_b=(200, verify_body("active")))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "auth_failed"
    assert row["note"] == ("Cloudflare token is expired (verify reports "
                           "status=expired); replace the key")
    assert row["fix"] == d.keys_check_replace_fix("cloudflare", 1)
    assert url_kinds(calls)["b"] == 0  # expired is terminal


def test_b_expired_is_auth_failed(monkeypatch):
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=(401, {}),
                    probe_b=(200, verify_body("expired")))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "auth_failed"
    assert "status=expired" in row["note"]


@pytest.mark.parametrize("spec", [
    (500, {}),  # verify plane down
    (200, b"not-json"),  # garbage body
    (200, {"success": True, "result": {"status": "disabled"}}),  # unknown status
    (200, {"success": True, "result": {}}),  # missing status
    (200, {"success": False, "errors": [{"code": 9999}]}),  # non-1000 code on 200
    (302, {}),  # redirect: fail closed, never followed
    (201, verify_body("active")),  # other 2xx: unlisted signal
    (400, {}),  # other 4xx: unlisted signal
])
def test_ambiguous_a_falls_back_to_h1_without_b(monkeypatch, spec):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=spec,
                            probe_b=(200, verify_body("active")))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE
    assert url_kinds(calls)["b"] == 0


def test_ambiguous_b_falls_back_to_h1(monkeypatch):
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=(401, {}), probe_b=(500, {}))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE


def test_transport_failure_is_ambiguous(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=httpx.ConnectError("down"),
                            probe_b=(200, verify_body("active")))
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE
    assert url_kinds(calls)["b"] == 0


@pytest.mark.parametrize("which", ["a", "b"])
def test_rate_limited_probe_keeps_denied_with_retry_note(monkeypatch, which):
    specs: dict = {"listing": (401, {}), "probe_a": (401, {}),
                   "probe_b": (200, verify_body("active"))}
    if which == "a":
        specs["probe_a"] = (429, {})
    else:
        specs["probe_b"] = (429, {})
    install_cf_fake(monkeypatch, **specs)
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE + " (verify endpoint rate-limited; retry later)"


def test_unknown_outcome_token_fails_closed_to_h1():
    row = d._map_slot_row("cloudflare", 1, CF_KEY_ENV,
                          {"status": "auth_failed", "catalog_access": None},
                          cf_probe="bogus-future-token")
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE


# --- T2: no probes unless CF listing 401 ---

@pytest.mark.parametrize(("status", "verdict"), [(200, "ok"), (403, "denied"),
                                                (429, "rate_limited"),
                                                (500, "error")])
def test_no_probe_on_non_401_listing(monkeypatch, status, verdict):
    body: Any = ({"result": [{"name": "@cf/probe-free"}], "success": True}
                 if status == 200 else {})
    calls = install_cf_fake(monkeypatch, listing=(status, body),
                            probe_a=(200, verify_body("active")),
                            probe_b=(200, verify_body("active")))
    row = check_cf(monkeypatch, cf_env())
    assert url_kinds(calls) == {"listing": 1, "a": 0, "b": 0}
    assert row["verdict"] == verdict


def test_no_probe_without_cache_even_on_401(monkeypatch):
    calls = install_counting(monkeypatch,
                             lambda request: httpx.Response(401, json={}))
    row = d.check_provider_slot("cloudflare", cf_env(), 1)
    assert row["verdict"] == "denied"  # legacy H1
    assert row["note"] == H1_NOTE
    assert len(calls) == 1  # listing only


# --- T3: non-Cloudflare untouched ---

def test_non_cloudflare_401_probes_nothing(monkeypatch):
    calls = install_counting(monkeypatch,
                             lambda request: httpx.Response(401, json={}))
    cache: dict = {}
    row = d.check_provider_slot("groq", {"GROQ_API_KEY": "probe-key"}, 1,
                                cf_probe_cache=cache)
    assert row["verdict"] == "auth_failed"
    assert len(calls) == 1
    assert cache == {}


# --- T5: multi-slot dedup ---

def test_same_token_shares_probes_across_slots(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    env = {CF_KEY_ENV: CF_TOKEN, f"{CF_KEY_ENV}_2": CF_TOKEN,
           CF_ACCOUNT_ENV: CF_ACCOUNT}
    cache: dict = {}
    row1 = d.check_provider_slot("cloudflare", env, 1, cf_probe_cache=cache)
    row2 = d.check_provider_slot("cloudflare", env, 2, cf_probe_cache=cache)
    assert (row1["verdict"], row2["verdict"]) == ("config_error",) * 2
    assert url_kinds(calls) == {"listing": 2, "a": 1, "b": 1}


def test_distinct_slot_tokens_do_not_conflate(monkeypatch):
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    env = {CF_KEY_ENV: CF_TOKEN, f"{CF_KEY_ENV}_2": "different-token",
           CF_ACCOUNT_ENV: CF_ACCOUNT}
    cache: dict = {}
    d.check_provider_slot("cloudflare", env, 1, cf_probe_cache=cache)
    d.check_provider_slot("cloudflare", env, 2, cf_probe_cache=cache)
    assert url_kinds(calls) == {"listing": 2, "a": 2, "b": 2}


def test_b_memoizes_across_accounts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/ai/models/search"):
            return httpx.Response(401, json={})
        if path.endswith("/user/tokens/verify"):
            return httpx.Response(200, json=verify_body("active"))
        if "/tokens/verify" in path:
            return httpx.Response(401, json={})
        raise AssertionError(path)

    calls: list = []
    monkeypatch.setattr(
        d, "_aclient",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 follow_redirects=False))
    cache: dict = {}
    env1, env2 = cf_env(), cf_env(account="d" * 32)
    d.check_provider_slot("cloudflare", env1, 1, cf_probe_cache=cache)
    d.check_provider_slot("cloudflare", env2, 1, cf_probe_cache=cache)
    assert url_kinds(calls) == {"listing": 2, "a": 2, "b": 1}


def test_memo_keys_carry_no_token_values(monkeypatch):
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=(401, {}),
                    probe_b=(200, verify_body("active")))
    cache: dict = {}
    check_cf(monkeypatch, cf_env(), cache=cache)
    blob = repr(sorted(cache.items()))
    assert CF_TOKEN not in blob
    want = hashlib.sha256(CF_TOKEN.encode()).hexdigest()[:16]
    assert any(k.startswith(("a:" + want, "b:" + want)) for k in cache)


# --- T6: deadline accounting (helper level: deterministic) ---

def test_exhausted_budget_skips_both_probes(monkeypatch):
    import time

    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(200, verify_body("active")),
                            probe_b=(200, verify_body("active")))
    monkeypatch.setattr(d, "_MIN_PROBE_SECONDS", 10 ** 9)
    cache: dict = {}
    # Generous deadline, absurd floor: remaining=100 < MIN → both skipped.
    outcome = asyncio.run(d._aprobe_cloudflare_token(
        CF_TOKEN, CF_ACCOUNT, deadline=time.monotonic() + 100.0, cache=cache))
    assert outcome == "inconclusive"
    assert url_kinds(calls) == {"listing": 0, "a": 0, "b": 0}


def test_scripted_clock_partial_budget_skips_b(monkeypatch):
    import time
    import types

    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    # Script discovery's clock only: asyncio.run consumes the global clock
    # itself, so exact ticks are pinnable only on d.time (two reads: A, B).
    ticks = iter([0.0, 99.5])
    monkeypatch.setattr(d, "time",
                        types.SimpleNamespace(monotonic=lambda: next(ticks)))
    cache: dict = {}
    # deadline=100: A sees remaining=100 (runs), B sees 0.5 (skipped).
    outcome = asyncio.run(d._aprobe_cloudflare_token(
        CF_TOKEN, CF_ACCOUNT, deadline=100.0, cache=cache))
    assert outcome == "inconclusive"
    assert url_kinds(calls) == {"listing": 0, "a": 1, "b": 0}
    assert time.monotonic() > 0  # real clock untouched by the script


def test_memo_replays_without_budget(monkeypatch):
    import time

    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    cache: dict = {}
    first = asyncio.run(d._aprobe_cloudflare_token(
        CF_TOKEN, CF_ACCOUNT, deadline=time.monotonic() + 100.0, cache=cache))
    assert first == "wrong_account"
    before = url_kinds(calls)
    # Second call with an expired deadline replays the memoized outcome.
    second = asyncio.run(d._aprobe_cloudflare_token(
        CF_TOKEN, CF_ACCOUNT, deadline=0.0, cache=cache))
    assert second == "wrong_account"
    assert url_kinds(calls) == before  # rule 0: ZERO new calls on replay


# --- T10: probe-path redaction (static notes only) ---

SECRET_TOKEN = "marker-secret-token-9f8e7d"
SECRET_ACCOUNT = "9f8e7d6c5b4a394857463728190eb09d"


@pytest.mark.parametrize("outcome_specs", [
    # (probe_a, probe_b) per outcome: pair_ok, wrong_account, token_dead,
    # token_expired, inconclusive, inconclusive_retry.
    ((200, verify_body("active")), (401, {})),
    ((401, {}), (200, verify_body("active"))),
    ((401, {}), (401, {})),
    ((200, verify_body("expired")), (401, {})),
    ((500, {}), (401, {})),
    ((429, {}), (401, {})),
])
def test_probe_notes_carry_no_secrets_urls_or_errors(monkeypatch, outcome_specs):
    probe_a, probe_b = outcome_specs
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=probe_a, probe_b=probe_b)
    env = {CF_KEY_ENV: SECRET_TOKEN, CF_ACCOUNT_ENV: SECRET_ACCOUNT}
    cache: dict = {}
    row = d.check_provider_slot("cloudflare", env, 1, cf_probe_cache=cache)
    rendered = f"{row['note']} || {row['fix']} || {row['verdict']}"
    assert SECRET_TOKEN not in rendered
    assert SECRET_ACCOUNT not in rendered
    assert "https://" not in rendered and "tokens/verify" not in rendered
    assert "Traceback" not in rendered and "Error" not in rendered


def test_probe_exception_text_never_rendered(monkeypatch):
    install_cf_fake(monkeypatch, listing=(401, {}),
                    probe_a=httpx.ConnectError("sentry-marker-boom"),
                    probe_b=(401, {}))
    env = {CF_KEY_ENV: SECRET_TOKEN, CF_ACCOUNT_ENV: SECRET_ACCOUNT}
    row = d.check_provider_slot("cloudflare", env, 1, cf_probe_cache={})
    assert row["verdict"] == "denied"
    assert "sentry-marker-boom" not in row["note"]
    assert SECRET_TOKEN not in row["note"]


# --- Refresh chain (wizard producer path) + T4 ---

def test_refresh_with_cache_probes_and_channels_outcome(monkeypatch, tmp_path):
    """Adversarial-1: the refresh→_aattempt chain runs probes, the outcome
    is readable via cf_probe_outcome, and the persisted snapshot carries no
    probe keys (wizard reads the in-memory row only)."""
    import json

    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(401, {}),
                            probe_b=(200, verify_body("active")))
    cache: dict = {}
    path = tmp_path / "d.json"
    snapshot = d.refresh_catalog(cf_env(), ["cloudflare"], path=path,
                                 cf_probe_cache=cache)
    assert url_kinds(calls) == {"listing": 1, "a": 1, "b": 1}
    assert d.cf_probe_outcome(cache) == "wrong_account"
    assert snapshot["providers"]["cloudflare"]["status"] == "auth_failed"
    on_disk = json.loads(path.read_text())
    assert "cloudflare_probe" not in json.dumps(on_disk)
    assert "cloudflare_probe" not in snapshot["providers"]["cloudflare"]


def test_refresh_without_cache_issues_no_verify_calls(monkeypatch, tmp_path):
    """T4 (refresh leg): legacy callers keep zero new I/O."""
    calls = install_cf_fake(monkeypatch, listing=(401, {}),
                            probe_a=(200, verify_body("active")),
                            probe_b=(200, verify_body("active")))
    snapshot = d.refresh_catalog(cf_env(), ["cloudflare"],
                                 path=tmp_path / "d.json")
    assert url_kinds(calls) == {"listing": 1, "a": 0, "b": 0}
    assert snapshot["providers"]["cloudflare"]["status"] == "auth_failed"


# --- T12: probe never raises ---

def test_probe_client_failure_is_inconclusive(monkeypatch):
    def explode() -> httpx.AsyncClient:
        raise RuntimeError("client boom")

    monkeypatch.setattr(d, "_aclient", explode)
    cache: dict = {}
    outcome = asyncio.run(d._aprobe_cloudflare_token(
        CF_TOKEN, CF_ACCOUNT, deadline=100.0, cache=cache))
    assert outcome == "inconclusive"


def test_probe_unexpected_throw_is_h1_row(monkeypatch):
    install_counting(monkeypatch,
                     lambda request: httpx.Response(401, json={}))

    async def boom(*args, **kwargs):
        raise RuntimeError("fetch-layer boom")

    # An unexpected throw the per-probe classifier cannot see must still
    # fail closed via the helper's outer except-Exception, never break rows.
    monkeypatch.setattr(d, "_afetch_verify_probe", boom)
    row = check_cf(monkeypatch, cf_env())
    assert row["verdict"] == "denied"
    assert row["note"] == H1_NOTE
