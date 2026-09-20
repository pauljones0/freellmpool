"""G25 U3: fallback plumbing (_load, busy path) + update-table display + free gate."""

import argparse
import json

from freellmpool import discovery as d
from freellmpool import managed_cli


def test_load_sanitizes_fallback_per_row(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"schema": 1, "generation": "g", "providers": {
        "a": {"status": "blocked", "complete": False, "checked_at": None,
              "last_attempt_at": "2026-09-20T00:00:00+00:00", "models": [],
              "fallback_models": ["a/free", "bad\nid", {"d": 1}, "z" * 300, "ok2/x"],
              "note": "n"},
        "b": {"status": "ok", "complete": True, "checked_at": "2026-09-20T00:00:00+00:00",
              "last_attempt_at": "2026-09-20T00:00:00+00:00",
              "models": [{"id": "m", "modalities": ["chat"]}]},
        "c": {"status": "blocked", "complete": False, "checked_at": None,
              "last_attempt_at": "2026-09-20T00:00:00+00:00", "models": [],
              "fallback_models": "garbage-string", "note": "n"}}}))
    loaded = d._load(path)
    assert set(loaded["providers"]) == {"a", "b", "c"}
    assert loaded["providers"]["a"]["fallback_models"] == ["a/free", "ok2/x"]
    assert loaded["providers"]["b"]["models"] == [{"id": "m", "modalities": ["chat"]}]
    assert "fallback_models" not in loaded["providers"]["b"]
    assert loaded["providers"]["c"]["fallback_models"] == []


def test_read_last_good_uses_load(tmp_path):
    dup = tmp_path / "dup.json"
    dup.write_text('{"schema": 1, "providers": {"a": {"status": "ok", "models": []}, '
                   '"a": {"status": "blocked", "models": []}}}')
    assert managed_cli._read_last_good(dup)["providers"] == {}

    dirty = tmp_path / "dirty.json"
    dirty.write_text(json.dumps({"schema": 1, "providers": {
        "a": {"status": "blocked", "models": [], "fallback_models": ["a/free", "bad\nid"]}}}))
    assert managed_cli._read_last_good(dirty)["providers"]["a"]["fallback_models"] == ["a/free"]

    assert managed_cli._read_last_good(tmp_path / "missing.json")["providers"] == {}


def test_update_table_renders_fallback_line(monkeypatch, capsys):
    names = [f"m{i:02d}/free" for i in range(12)]
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog",
                        lambda *a, **k: {"providers": {
                            "kilo": {"status": "blocked", "models": [],
                                     "fallback_models": names, "note": "n"}}})
    assert managed_cli.cmd_update(argparse.Namespace(public_only=False, provider=None)) == 0
    out = capsys.readouterr().out
    expected = ("    reviewed fallback candidates (availability unverified): "
                + ", ".join(names[:10]) + " +2 more")
    assert expected in out.splitlines()


def test_fallback_render_strips_control():
    row = {"status": "blocked",
           "fallback_models": ["a/b", "x\x1by", "m\nn", "ok_1.2:3", "", {"d": 1}, 7, "z" * 300]}
    line = managed_cli._fallback_line(row)
    assert line is not None
    assert "\n" not in line and "\x1b" not in line
    assert "a/b" in line and "x_y" in line and "m_n" in line and "ok_1.2:3" in line
    assert "z" * 300 not in line and "z" * 256 in line
    assert managed_cli._fallback_line({"status": "blocked", "fallback_models": []}) is None
    assert managed_cli._fallback_line({"status": "blocked", "fallback_models": "junk"}) is None
    assert managed_cli._fallback_line({"status": "blocked"}) is None
    assert managed_cli._fallback_line({"status": "ok", "fallback_models": ["a/free"]}) is None


def test_zero_routes_from_blocked_snapshot(tmp_path):
    from datetime import UTC, datetime, timedelta

    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool
    from freellmpool.models import Model, Provider

    now = datetime.now(UTC)
    checked, expires = now.isoformat(), (now + timedelta(days=7)).isoformat()

    def spec(pid):
        return {"id": pid, "display_name": pid, "api_base_url": f"https://{pid}.test/v1",
                "credential_env": None, "inference_auth": "none",
                "discovery": {"parser": "openai", "supports_public": True},
                "evidence": [{"id": "price", "checked_at": checked, "expires_at": expires,
                              "status": "verified"}],
                "grants": [{"id": "free", "kind": "zero_price", "status": "verified",
                            "evidence_ids": ["price"], "model_selector": {"kind": "zero_price"},
                            "paid_overage_possible": False, "requires_account_evidence": False,
                            "hard_free_boundary": True, "allowed_modalities": ["chat"]}],
                "limits": [{"id": "rpd", "scope": "account", "metric": "requests",
                            "algorithm": "rolling", "capacity": 2, "window_seconds": 86400,
                            "grant_ids": ["free"]}]}

    providers = [Provider("alpha", "alpha", "openai", "https://alpha.test/v1",
                          (Model("free", context=32000),), auth="none"),
                 Provider("beta", "beta", "openai", "https://beta.test/v1",
                          (Model("free", context=32000),), auth="none")]
    snapshot = {"schema": 1, "generation": "test", "providers": {
        "alpha": {"checked_at": checked, "last_attempt_at": checked, "status": "ok",
                  "complete": True, "catalog_access": "public", "models": [
                      {"id": "free", "modalities": ["chat"], "context": 32000,
                       "pricing": {"input": "0", "output": "0"}}]},
        "beta": {"checked_at": None, "last_attempt_at": checked, "status": "blocked",
                 "complete": False, "catalog_access": "public", "models": [],
                 "fallback_models": ["beta/free", "alpha/free"], "note": "n"}}}
    pool = ManagedPool(providers, registry={"alpha": spec("alpha"), "beta": spec("beta")},
                       discovery=snapshot, accounts={}, env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=lambda *args: (_ for _ in ()).throw(AssertionError("no transport")))
    taken = pool.snapshot()
    assert [route.provider.id for route in taken.routes] == ["alpha"]
    beta = [row for row in taken.providers if row["id"] == "beta"][0]
    assert beta["eligible"] == 0
    assert beta["reason"].startswith("model listing blocked; 2 reviewed fallback candidates")
