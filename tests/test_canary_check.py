"""G30 `keys check --canary`: opt-in inference canary for uncheckable providers.

Covers the v2.1 test plan: default-path byte-identical, eligible-set
tripwire + exclusion reasons, grant-terms pins, verdict totality
(401/403+mitigation/402/404/4xx-group/3xx/408/429/timeouts/5xx/
transport), quota attempt-semantics (charge incl. failures, no charge
on connect-phase), max_tokens bound + single dispatch, per-slot keys,
secrecy, JSON purity + additive keys, consent banner, strict matrix,
missing-before-canary, ledger untouched.
"""

from __future__ import annotations

import json
from functools import partial

import httpx
import pytest
from test_keys_check import base_key, make_args, run_check, scrub_env

from freellmpool import discovery as d
from freellmpool.allowances import AllowanceLedger
from freellmpool.client import HTTPResult
from freellmpool.config import load_catalog
from freellmpool.provider_registry import load_registry

ELIGIBLE = {"openrouter", "nvidia", "vercel", "aion", "modelscope"}


def _quota_env(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str = "quota.json"):
    path = tmp_path / name
    monkeypatch.setenv("FREELLMPOOL_QUOTA_PATH", str(path))
    return path


def _quota_counts(path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    assert len(data) == 1  # single-day bucket
    return next(iter(data.values()))


def fake_post(*, status: int = 200, body=None, headers=None, exc=None, calls=None):
    """Fake PostFn: records (url, headers, json) and returns/raises."""
    if body is None:
        body = {"choices": [{"message": {"content": "OK"}}]}

    def post(url, post_headers, json_body, timeout, **kwargs):
        (calls if calls is not None else []).append(
            {"url": url, "headers": dict(post_headers), "json": json_body,
             "timeout": timeout, "kwargs": kwargs})
        if exc is not None:
            raise exc
        return HTTPResult(status=status, body=body, text=json.dumps(body),
                          headers=dict(headers or {}))

    return post


def _canary_verdict(monkeypatch, capsys, tmp_path, pid, post, **args):
    scrub_env(monkeypatch)
    quota = _quota_env(monkeypatch, tmp_path)
    monkeypatch.setattr("freellmpool.client.default_post", post)
    monkeypatch.setenv(base_key(pid), "probe-key")
    params = {"provider": pid, "json": True, "canary": True}
    params.update(args)
    rc, out, err = run_check(make_args(**params), capsys)
    return rc, json.loads(out), err, quota


def _catalog_by_id():
    return {entry.id: entry for entry in load_catalog()}


# --- eligibility + pins -------------------------------------------------


def test_eligible_set_tripwire():
    registry, catalog = load_registry(), _catalog_by_id()
    got = set()
    for pid, record in registry.items():
        key_env = record.get("credential_env")
        env = {key_env: "x"} if isinstance(key_env, str) and key_env else {}
        if d.is_canary_eligible(pid, record, catalog.get(pid), 1, env):
            got.add(pid)
    assert got == ELIGIBLE


def test_eligibility_exclusion_reasons():
    registry, catalog = load_registry(), _catalog_by_id()

    def eligible(pid, **over):
        record = dict(registry[pid])
        record.update(over.get("record", {}))
        key_env = record.get("credential_env")
        env = {key_env: "x"} if isinstance(key_env, str) and key_env else {}
        env.update(over.get("env", {}))
        return d.is_canary_eligible(
            pid, record, over.get("local", catalog.get(pid)),
            over.get("slot", 1), env)

    assert eligible("openrouter") is True
    # llm7: key_optional — a 2xx could never judge the key.
    assert eligible("llm7") is False
    # ollama: conditional paid-overage grant — never under bare --canary.
    assert eligible("ollama") is False
    # groq: listing-checkable keeps the GET-only path.
    assert eligible("groq") is False
    # ovh: no credential_env — no key to judge.
    assert eligible("ovh") is False
    # Unconfigured slot is missing, never canaried.
    assert eligible("openrouter", env={"OPENROUTER_API_KEY": ""}) is False
    # Registry-external providers have no reviewed local entry.
    assert eligible("openrouter", local=None) is False


def test_canary_targets_match_unconditional_hard_free_grants():
    """v2.1/SCOPE-1: pins match grants with paid_overage_possible False
    and no required_account_conditions — bare model_matches_grant would
    also match ollama's conditional grant."""
    from freellmpool.free_policy import model_matches_grant

    registry = load_registry()
    assert set(d.CANARY_TARGETS) == ELIGIBLE
    for pid, model_id in d.CANARY_TARGETS.items():
        grants = registry[pid].get("grants", [])
        model = {"id": model_id, "modalities": ["chat"],
                 "pricing": {"input": 0, "output": 0}}
        assert any(
            model_matches_grant(grant, model)
            and grant.get("paid_overage_possible") is False
            and not grant.get("required_account_conditions")
            for grant in grants), f"{pid}/{model_id} has no unconditional hard-free grant"


def test_canary_default_post_is_single_shot():
    post = d._canary_default_post()
    assert isinstance(post, partial)
    assert post.keywords == {"max_attempts": 1}


# --- verdict totality (CLI level) ----------------------------------------

def test_canary_401_proves_dead(monkeypatch, capsys, tmp_path):
    rc, envelope, _, quota = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter",
        fake_post(status=401, body={"error": {"message": "bad key"}}))
    assert rc == 1
    row = envelope["rows"][0]
    assert (row["verdict"], row["status"]) == ("auth_failed", "auth_failed")
    assert row["fix"].startswith("replace:")
    assert envelope["summary"]["failed"] == 1
    assert _quota_counts(quota) == {"openrouter::" + d.CANARY_TARGETS["openrouter"]: 1}


def test_canary_403_denied_vs_mitigated_blocked(monkeypatch, capsys, tmp_path):
    rc, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post(status=403))
    assert rc == 0
    row = envelope["rows"][0]
    assert (row["verdict"], row["status"]) == ("denied", "denied")
    assert "listing denied" not in row["note"] and "NOT proven bad" in row["note"]
    assert row["fix"].startswith("scope:") and "--canary" in row["fix"]

    rc, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter",
        fake_post(status=403, headers={"x-vercel-mitigated": "deny"}))
    assert rc == 0
    assert envelope["rows"][0]["verdict"] == "blocked"


def test_canary_402_denied(monkeypatch, capsys, tmp_path):
    rc, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "aion", fake_post(status=402))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "denied" and "billing" in row["note"]


@pytest.mark.parametrize("status", [400, 405, 409, 413, 422])
def test_canary_4xx_group_is_error(monkeypatch, capsys, tmp_path, status):
    rc, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "vercel", fake_post(status=status))
    assert rc == 0
    row = envelope["rows"][0]
    assert row["verdict"] == "error" and "NOT judged" in row["note"]
    assert row["fix"].startswith("retry:") and "--canary" in row["fix"]


def test_canary_404_names_drift_and_3xx_is_error(monkeypatch, capsys, tmp_path):
    _, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "nvidia", fake_post(status=404))
    assert envelope["rows"][0]["verdict"] == "error"
    assert "drift" in envelope["rows"][0]["note"]

    _, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "nvidia",
        fake_post(status=302, headers={"location": "https://x.test/"}))
    assert envelope["rows"][0]["verdict"] == "error"


def test_canary_408_and_504_are_deferred(monkeypatch, capsys, tmp_path):
    for status in (408, 504):
        rc, envelope, _, _ = _canary_verdict(
            monkeypatch, capsys, tmp_path, "aion", fake_post(status=status))
        assert rc == 0
        assert envelope["rows"][0]["verdict"] == "deferred"


def test_canary_429_ignores_retry_after(monkeypatch, capsys, tmp_path):
    rc, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "modelscope",
        fake_post(status=429, headers={"retry-after": "120"}))
    assert rc == 0
    assert envelope["rows"][0]["verdict"] == "rate_limited"


def test_canary_transport_split_and_quota_boundary(monkeypatch, capsys, tmp_path):
    # ReadTimeout: deferred + charged (past connect phase).
    rc, envelope, _, quota = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter",
        fake_post(exc=httpx.ReadTimeout("slow")))
    assert rc == 0 and envelope["rows"][0]["verdict"] == "deferred"
    assert sum(_quota_counts(quota).values()) == 1
    # Connect-phase failures: no charge (DNS/refused/TLS indistinguishable).
    # Each iteration gets a fresh quota file (no cross-case charges).
    cases = ((httpx.ConnectError("dns"), "error"),
             (httpx.ConnectTimeout("ct"), "deferred"),
             (httpx.PoolTimeout("pool"), "deferred"))
    for i, (exc, verdict) in enumerate(cases):
        scrub_env(monkeypatch)
        quota = _quota_env(monkeypatch, tmp_path, name=f"quota-{i}.json")
        monkeypatch.setattr("freellmpool.client.default_post", fake_post(exc=exc))
        monkeypatch.setenv(base_key("openrouter"), "probe-key")
        rc, out, _ = run_check(
            make_args(provider="openrouter", json=True, canary=True), capsys)
        row = json.loads(out)["rows"][0]
        assert (rc, row["verdict"]) == (0, verdict)
        assert _quota_counts(quota) == {}, f"connect-phase must not record: {exc!r}"


def test_canary_2xx_ok_even_empty_choices(monkeypatch, capsys, tmp_path):
    rc, envelope, _, quota = _canary_verdict(
        monkeypatch, capsys, tmp_path, "vercel",
        fake_post(body={"choices": [{"message": {"content": "OK"}}]}))
    assert rc == 0
    row = envelope["rows"][0]
    assert (row["verdict"], row["status"]) == ("ok", "ok")
    assert "canary" in row["note"] and row["fix"] is None
    assert sum(_quota_counts(quota).values()) == 1

    # Empty choices: the key was accepted; text is irrelevant.
    _, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "vercel", fake_post(body={"choices": []}))
    assert envelope["rows"][0]["verdict"] == "ok"


def test_canary_request_shape_bound_and_single_dispatch(monkeypatch, capsys, tmp_path):
    calls: list = []
    rc, _, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post(calls=calls))
    assert rc == 0 and len(calls) == 1  # single-shot: no retries
    call = calls[0]
    assert call["json"]["model"] == d.CANARY_TARGETS["openrouter"]
    assert call["json"]["max_tokens"] == 16  # thinking floor disabled
    assert call["json"]["temperature"] == 0
    assert call["headers"]["Authorization"] == "Bearer probe-key"
    assert call["url"].endswith("/chat/completions")
    assert call["kwargs"] == {"max_attempts": 1}

    # 429 also dispatches exactly once (no backoff sleep/retry).
    calls.clear()
    _canary_verdict(monkeypatch, capsys, tmp_path, "openrouter",
                    fake_post(status=429, calls=calls))
    assert len(calls) == 1


def test_canary_multi_slot_split_and_per_slot_key(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    quota = _quota_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-one")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "key-two")
    seen: list = []

    def post(url, headers, body, timeout, **kwargs):
        seen.append(headers["Authorization"])
        status = 200 if headers["Authorization"] == "Bearer key-one" else 401
        payload = {"choices": [{"message": {"content": "OK"}}]}
        return HTTPResult(status=status, body=payload, text=json.dumps(payload))

    monkeypatch.setattr("freellmpool.client.default_post", post)
    rc, out, _ = run_check(
        make_args(provider="openrouter", json=True, canary=True), capsys)
    assert rc == 1
    rows = json.loads(out)["rows"]
    assert [(r["slot"], r["verdict"]) for r in rows] == [(1, "ok"), (2, "auth_failed")]
    assert seen == ["Bearer key-one", "Bearer key-two"]
    assert sum(_quota_counts(quota).values()) == 2


def test_canary_missing_before_canary_and_strict_matrix(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    _quota_env(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("no dispatch without a configured slot")

    monkeypatch.setattr("freellmpool.client.default_post", boom)
    rc, out, _ = run_check(
        make_args(provider="openrouter", json=True, canary=True), capsys)
    assert rc == 0
    assert json.loads(out)["rows"][0]["verdict"] == "missing"

    # --slot filter + strict: denied fails strict, ok passes.
    _, envelope, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post(status=403),
        slot=1, strict=True)
    assert envelope["rows"][0]["verdict"] == "denied"
    rc, _, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post(status=403),
        slot=1, strict=True)
    assert rc == 1
    rc, _, _, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post(), slot=1,
        strict=True)
    assert rc == 0


# --- default-path preservation + envelope -------------------------------

def test_canary_default_path_zero_inference(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    quota = _quota_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")

    def boom(*args, **kwargs):
        raise AssertionError("no inference without --canary")

    monkeypatch.setattr("freellmpool.client.default_post", boom)
    rc, out, _ = run_check(make_args(provider="openrouter", json=True), capsys)
    assert rc == 0
    envelope = json.loads(out)
    assert envelope["rows"][0]["verdict"] == "unsupported"
    assert set(envelope["rows"][0]) == {"provider", "slot", "env_var", "verdict",
                                        "status", "note", "fix"}
    assert not quota.exists()


def test_canary_never_touches_checkable(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    _quota_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")

    def boom(*args, **kwargs):
        raise AssertionError("checkable providers stay GET-only")

    monkeypatch.setattr("freellmpool.client.default_post", boom)
    import freellmpool.discovery as disc

    monkeypatch.setattr(
        disc, "_aclient",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": "probe-free"}]})),
            follow_redirects=False))
    calls: list = []
    real = disc.check_provider

    def spy(pid, env):
        calls.append(pid)
        return real(pid, env)

    monkeypatch.setattr(disc, "check_provider", spy)
    rc, out, _ = run_check(
        make_args(provider="groq", json=True, canary=True), capsys)
    assert rc == 0 and calls == ["groq"]
    assert json.loads(out)["rows"][0]["verdict"] == "ok"


def test_canary_json_additive_keys_and_consent_once(monkeypatch, capsys, tmp_path):
    rc, envelope, err, _ = _canary_verdict(
        monkeypatch, capsys, tmp_path, "openrouter", fake_post())
    assert rc == 0
    row = envelope["rows"][0]
    assert row["via"] == "canary"
    assert row["canary_model"] == d.CANARY_TARGETS["openrouter"]
    assert err.count("spends ONE inference call") == 1

    # Listing rows gain nothing.
    scrub_env(monkeypatch)
    _quota_env(monkeypatch, tmp_path)
    import freellmpool.discovery as disc

    monkeypatch.setattr(
        disc, "_aclient",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": "probe-free"}]})),
            follow_redirects=False))
    monkeypatch.setenv("GROQ_API_KEY", "probe-key")
    rc, out, _ = run_check(
        make_args(provider="groq", json=True, canary=True), capsys)
    row = json.loads(out)["rows"][0]
    assert "via" not in row and "canary_model" not in row


def test_canary_ledger_untouched_and_secrecy(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    _quota_env(monkeypatch, tmp_path)
    key = "canary-secret-" + "k" * 8
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    ledger = AllowanceLedger()
    before = (ledger.summary(), ledger.export())
    # Transport failure (nothing dispatched): static note, no charge, no leak.
    monkeypatch.setattr(
        "freellmpool.client.default_post",
        fake_post(exc=httpx.ConnectError(f"unreachable echoing {key}")))
    rc, out, err = run_check(
        make_args(provider="openrouter", json=True, canary=True), capsys)
    assert rc == 0
    assert json.loads(out)["rows"][0]["verdict"] == "error"
    assert key not in out and key not in err
    assert (ledger.summary(), ledger.export()) == before

    # Key-echoing error BODIES also stay out: canary notes are static and
    # never interpolate upstream content (review nit a).
    for status in (401, 500):
        monkeypatch.setattr(
            "freellmpool.client.default_post",
            fake_post(status=status, body={"error": f"rejected {key}"}))
        rc, out, err = run_check(
            make_args(provider="openrouter", json=True, canary=True), capsys)
        assert key not in out and key not in err
    assert (ledger.summary(), ledger.export()) == before


def test_canary_timeout_rows_carry_canary_fix(monkeypatch, capsys, tmp_path):
    scrub_env(monkeypatch)
    _quota_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")
    rc, out, err = run_check(
        make_args(provider="openrouter", json=True, canary=True, timeout=0.5),
        capsys)
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert [r["verdict"] for r in rows] == ["timeout"]
    assert rows[0]["fix"].startswith("retry:") and "--canary" in rows[0]["fix"]
