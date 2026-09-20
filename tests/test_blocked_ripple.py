"""G25 U2: blocked ripple — merge, maintenance, reasons, tiers, footer, setup."""

import argparse
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx

from freellmpool import discovery as d
from freellmpool import maintenance as m
from freellmpool.cli import _bootstrap_tier_line
from freellmpool.provider_registry import load_registry

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
URL = "https://openrouter.ai/docs/api_reference/limits.md"

TIER_BLOCKED = ("freellmpool: model listings blocked; a later re-check via "
                "`freellmpool update --provider PROVIDER` re-verdicts (verdict may persist; "
                "run `freellmpool status` for the reason).")
REASON_N = ("model listing blocked; 2 reviewed fallback candidates (availability unverified; "
            "names in update table); run freellmpool update --provider PROVIDER later to re-check "
            "(re-verdicts; verdict may persist)")
REASON_0 = ("model listing blocked; no reviewed fallback candidates; run freellmpool update "
            "--provider PROVIDER later to re-check (re-verdicts; verdict may persist)")
STATUS_TEXT = ("The model listing was blocked; a later re-check via freellmpool update --provider "
               "PROVIDER re-verdicts and the verdict may persist. Your progress is saved; continue "
               "with other providers.")
FOOTER_BLOCKED = ("Discovery incomplete: 0 ok, 0 deferred, 1 failed; blocked listings re-verdict on "
                  "a later `freellmpool update --provider PROVIDER` re-check, verdict may persist. "
                  "Pricing, account eligibility, and protocol evidence remain separate checks.")
FOOTER_MIXED = ("Discovery incomplete: 0 ok, 0 deferred, 2 failed; run `freellmpool update` to "
                "retry, but blocked listings re-verdict on a later re-check and the verdict may "
                "persist. Pricing, account eligibility, and protocol evidence remain separate checks.")


def _transport(monkeypatch, responder):
    monkeypatch.setattr(d, "_aclient", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(responder), follow_redirects=False))


def test_blocked_merge_fail_closed(monkeypatch, tmp_path):
    kilo = copy.deepcopy(load_registry()["kilo"])
    kilo["discovery"] = {**kilo["discovery"], "url": "https://127.0.0.1:9/catalog"}
    kilo["grants"] = [{"id": "free", "status": "verified", "kind": "zero_price",
                       "model_selector": {"kind": "free_suffix", "suffix": ":free",
                                          "models": ["kilo-auto/free", "openrouter/free",
                                                     "bad\nid", "", 7, "z" * 300]}}]
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {"kilo": kilo})
    old = (NOW - timedelta(days=10)).isoformat()
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": 1, "generation": "old", "updated_at": old, "providers": {
        "kilo": {"status": "ok", "complete": True, "checked_at": NOW.isoformat(),
                 "last_attempt_at": old,
                 "models": [{"id": "kilo-auto/free", "modalities": ["chat"],
                             "pricing": {"input": "0", "output": "0"}}],
                 "source_url": "https://api.kilo.ai/api/gateway/models",
                 "catalog_access": "public", "catalog_ttl_seconds": 86400, "note": "old note"}}}))

    def denied(request):
        return httpx.Response(403, headers={"x-vercel-mitigated": "deny"}, text="{}")

    _transport(monkeypatch, denied)
    entry = d.refresh_catalog({}, ["kilo"], path=path)["providers"]["kilo"]
    assert entry["status"] == "blocked"
    assert entry["note"].startswith("HTTP 403: provider edge refused the listing")
    assert entry["last_attempt_at"] != old
    assert entry["complete"] is False
    assert entry["models"] == []
    assert entry["checked_at"] is None
    assert entry["source_url"] == "https://api.kilo.ai/api/gateway/models"
    assert entry["catalog_access"] == "public"
    assert entry["catalog_ttl_seconds"] == 86400
    assert entry["fallback_models"] == ["kilo-auto/free", "openrouter/free"]


def test_blocked_recovers_to_ok_without_fallback(monkeypatch, tmp_path):
    kilo = copy.deepcopy(load_registry()["kilo"])
    kilo["discovery"] = {**kilo["discovery"], "url": "https://127.0.0.1:9/catalog"}
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {"kilo": kilo})
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": 1, "generation": "old", "providers": {
        "kilo": {"status": "blocked", "complete": False, "checked_at": None,
                 "last_attempt_at": NOW.isoformat(), "models": [],
                 "fallback_models": ["kilo-auto/free"], "note": "old"}}}))
    _transport(monkeypatch, lambda request: httpx.Response(200, json={
        "data": [{"id": "kilo-auto/free", "pricing": {"prompt": "0", "completion": "0"}}]}))
    entry = d.refresh_catalog({}, ["kilo"], path=path)["providers"]["kilo"]
    assert entry["status"] == "ok"
    assert entry["complete"] is True
    assert "fallback_models" not in entry


def test_non_blocked_attempt_drops_stale_fallback(monkeypatch, tmp_path):
    keyed = copy.deepcopy(load_registry()["groq"])
    keyed["discovery"] = {**keyed["discovery"], "url": "https://127.0.0.1:9/catalog"}
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {"groq": keyed})
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": 1, "generation": "old", "providers": {
        "groq": {"status": "blocked", "complete": False, "checked_at": None,
                 "last_attempt_at": NOW.isoformat(), "models": [],
                 "fallback_models": ["stale/free"], "note": "old"}}}))
    _transport(monkeypatch, lambda request: httpx.Response(403, text="{}"))
    entry = d.refresh_catalog({"GROQ_API_KEY": "k"}, ["groq"], path=path)["providers"]["groq"]
    assert entry["status"] == "auth_failed"
    assert "fallback_models" not in entry


def _report_registry(ttl=True):
    disc = {"supports_public": True}
    if ttl:
        disc["catalog_ttl_seconds"] = 3 * 86400
    return {"openrouter": {"id": "openrouter", "credential_env": "OPENROUTER_API_KEY",
                           "discovery": disc, "grants": [], "limits": [],
                           "evidence": [{"id": "terms", "url": URL, "checked_at": NOW.isoformat(),
                                         "expires_at": (NOW + timedelta(days=7)).isoformat(),
                                         "status": "official"}]}}


def _report_catalog(last_attempt, row_ttl=True):
    row = {"status": "blocked", "complete": False, "catalog_access": "public",
           "checked_at": None, "last_attempt_at": last_attempt.isoformat(),
           "models": [], "fallback_models": ["a/free"], "note": "n"}
    if row_ttl:
        row["catalog_ttl_seconds"] = 3 * 86400
    return {"schema": 1, "providers": {"openrouter": row}}


def test_maintenance_blocked_exempt_but_ttl():
    report, _ = m.build_public_report(_report_registry(), _report_catalog(NOW), now=NOW)
    assert report["findings"] == []
    assert report["providers"]["openrouter"]["catalog"]["status"] == "blocked"
    assert m.validate_public_report(report) == report

    report, _ = m.build_public_report(_report_registry(), _report_catalog(NOW, row_ttl=False), now=NOW)
    assert report["findings"] == []

    report, _ = m.build_public_report(_report_registry(ttl=False),
                                      _report_catalog(NOW, row_ttl=False), now=NOW)
    codes = [finding["code"] for finding in report["findings"]]
    assert "catalog_failed" not in codes
    assert "catalog_stale" not in codes

    stale, _ = m.build_public_report(_report_registry(),
                                     _report_catalog(NOW - timedelta(days=10)), now=NOW)
    codes = [finding["code"] for finding in stale["findings"]]
    assert "catalog_failed" not in codes
    assert codes == ["catalog_stale"]
    assert stale["findings"][0]["summary"] == "Model discovery is missing or expired."


def test_maintenance_verdict_due_ignores_hostile_ttl():
    registry = _report_registry()

    def row(ttl, last_attempt=NOW):
        data = _report_catalog(last_attempt)
        data["providers"]["openrouter"]["catalog_ttl_seconds"] = ttl
        return data

    report, _ = m.build_public_report(registry, row("garbage"), now=NOW)
    assert report["findings"] == []
    registry["openrouter"]["discovery"] = {"supports_public": True, "catalog_ttl_seconds": -5}
    report, _ = m.build_public_report(registry, row("garbage"), now=NOW)
    assert [finding["code"] for finding in report["findings"]] == ["catalog_due"]
    registry["openrouter"]["discovery"] = {"supports_public": True}
    report, _ = m.build_public_report(registry, row(True), now=NOW)
    assert [finding["code"] for finding in report["findings"]] == ["catalog_due"]

    report, _ = m.build_public_report(_report_registry(), row(1e300), now=NOW)
    assert [finding["code"] for finding in report["findings"]] == ["catalog_stale"]

    data = _report_catalog(NOW)
    del data["providers"]["openrouter"]["last_attempt_at"]
    report, _ = m.build_public_report(_report_registry(), data, now=NOW)
    assert [finding["code"] for finding in report["findings"]] == ["catalog_stale"]

    assert m._verdict_ttl({"catalog_ttl_seconds": 100}, {}) == 100.0
    assert m._verdict_ttl({}, {"discovery": {"catalog_ttl_seconds": 200}}) == 200.0
    assert m._verdict_ttl({}, {"discovery": "junk"}) == 86400.0
    assert m._verdict_ttl({"catalog_ttl_seconds": float("nan")}, {}) == 86400.0


def test_count_snapshot_counts_blocked_failed():
    snapshot = {"providers": {"kilo": {"status": "blocked", "models": []}}}
    assert d.count_snapshot(snapshot) == (0, 0, 1)
    assert d.deferred_failed_csv(snapshot) == "freellmpool: deferred/failed providers: kilo"


def test_tier_blocked_order():
    def row(status, note="n", checked_at=None):
        return {"status": status, "note": note, "checked_at": checked_at,
                "complete": status == "ok", "models": [],
                "last_attempt_at": "2026-09-20T00:00:00+00:00"}

    assert _bootstrap_tier_line({"providers": {"a": row("blocked")}}) == TIER_BLOCKED
    deferred = {"providers": {"a": row("deferred"), "b": row("blocked")}}
    assert _bootstrap_tier_line(deferred) == ("freellmpool: model discovery deferred (time budget); "
                                              "run `freellmpool update` to complete it.")
    transport = {"providers": {"a": row("error", d._NOTE_NETWORK_FAILURE), "b": row("blocked")}}
    assert _bootstrap_tier_line(transport).startswith("freellmpool: could not reach providers")
    mixed = {"providers": {"a": row("error", "boom"), "b": row("blocked")}}
    assert _bootstrap_tier_line(mixed) == TIER_BLOCKED


def _managed_pool(tmp_path, env, fallback):
    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool
    from freellmpool.models import Model, Provider

    providers = [Provider("alpha", "alpha", "openai", "https://alpha.test/v1",
                          (Model("free", context=32000),))]
    registry = {"alpha": {
        "id": "alpha", "display_name": "alpha", "api_base_url": "https://alpha.test/v1",
        "credential_env": "ALPHA_KEY", "discovery": {"supports_public": False},
        "evidence": [], "grants": [], "limits": []}}
    snapshot = {"schema": 1, "generation": "test", "providers": {"alpha": {
        "status": "blocked", "complete": False, "checked_at": None,
        "last_attempt_at": NOW.isoformat(), "models": [],
        "fallback_models": fallback, "note": "n"}}}
    pool = ManagedPool(providers, registry=registry, discovery=snapshot, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0", **env},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=lambda *args: (_ for _ in ()).throw(AssertionError("no transport")))
    return pool.snapshot().providers[0]


def test_is_configured_wins_over_blocked(tmp_path):
    status = _managed_pool(tmp_path, {}, ["a/free", "b/free"])
    assert status["reason"] == "API key or required account field missing"


def test_managed_blocked_reason_variants(tmp_path):
    assert _managed_pool(tmp_path, {"ALPHA_KEY": "k"}, ["a/free", "b/free"])["reason"] == REASON_N
    assert _managed_pool(tmp_path, {"ALPHA_KEY": "k"}, [])["reason"] == REASON_0


def test_update_footer_blocked_variant(monkeypatch, capsys):
    from freellmpool import managed_cli

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"kilo": {"status": "blocked", "models": []}}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    assert FOOTER_BLOCKED in capsys.readouterr().out

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"kilo": {"status": "blocked", "models": []},
                                                       "groq": {"status": "auth_failed", "models": []}}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    assert FOOTER_MIXED in capsys.readouterr().out

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {"kilo": {"status": "blocked", "models": []},
                                                       "llm7": {"status": "deferred", "models": []}}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    out = capsys.readouterr().out
    assert ("Discovery incomplete: 0 ok, 1 deferred, 1 failed; run `freellmpool update` to "
            "retry, but blocked listings re-verdict on a later re-check and the verdict may "
            "persist.") in out


def test_setup_blocked_status_text_and_break(tmp_path):
    from freellmpool.onboarding import _STATUS_TEXT, run_onboarding

    assert _STATUS_TEXT["blocked"] == STATUS_TEXT

    def row(name):
        return {"id": name, "display_name": name.title(), "credential_env": "ALPHA_KEY",
                "grants": [{"kind": "recurring_quota", "status": "verified"}],
                "setup": {"signup_url": "https://example.test/signup",
                          "key_url": "https://example.test/keys", "steps": ["Choose the Free plan."],
                          "required_env": ["ALPHA_KEY"]}}

    def flow(status):
        prompts, output = [], []
        run_onboarding(provider="alpha", registry={"alpha": row("alpha")},
                       env={"FREELLMPOOL_CONFIG_FILE": str(tmp_path / f"{status}.toml")},
                       progress_path=tmp_path / f"{status}.json",
                       input_fn=lambda prompt: (prompts.append(prompt), "")[1],
                       secret_fn=lambda _: "private-value",
                       check=lambda *_: {"status": status}, output=output.append)
        return prompts, output

    prompts, output = flow("blocked")
    assert STATUS_TEXT in output
    assert not any("r=retry check" in prompt for prompt in prompts)
    prompts, _ = flow("error")
    assert any("r=retry check" in prompt for prompt in prompts)
