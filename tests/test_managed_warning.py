"""G34 warning-while-serving: preserved adverse-verdict rows warn, still serve.

A provider whose last listing was adverse (denied scope cut, auth
failure, error, ...) keeps serving last-good routes while fresh. The
managed row must carry a warning naming the verdict; admission is
untouched. All fixtures offline.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from freellmpool.allowances import AllowanceLedger
from freellmpool.managed import ManagedPool
from freellmpool.models import Model, Provider


def _fixture(ids=("alpha",), capacity=2):
    now = datetime.now(UTC)
    checked = now.isoformat()
    expires = (now + timedelta(days=7)).isoformat()
    registry = {}
    snapshot = {"schema": 1, "generation": "test", "providers": {}}
    providers = []
    for pid in ids:
        providers.append(Provider(pid, pid, "openai", f"https://{pid}.test/v1",
                                  (Model("free", context=32000),),
                                  auth="none"))
        registry[pid] = {
            "id": pid, "display_name": pid,
            "api_base_url": f"https://{pid}.test/v1", "credential_env": None,
            "discovery": {"parser": "openai",
                          "url": f"https://{pid}.test/v1/models"},
            "evidence": [{"id": "price", "checked_at": checked,
                          "expires_at": expires, "status": "verified"}],
            "grants": [{"id": "free", "kind": "zero_price", "status": "verified",
                        "evidence_ids": ["price"],
                        "model_selector": {"kind": "zero_price"},
                        "paid_overage_possible": False,
                        "requires_account_evidence": False,
                        "allowed_modalities": ["chat"]}],
            "limits": [{"id": "rpd", "scope": "account", "metric": "requests",
                        "algorithm": "rolling", "capacity": capacity,
                        "window_seconds": 86400, "grant_ids": ["free"]}],
        }
        snapshot["providers"][pid] = {
            "checked_at": checked, "status": "ok", "complete": True,
            "models": [{"id": "free", "modalities": ["chat"], "context": 32000,
                        "pricing": {"input": "0", "output": "0"}}],
        }
    return providers, registry, snapshot


def _pool(tmp_path, rows=None, **kwargs):
    """ManagedPool with crafted discovery rows: {pid: {status,...}}."""
    providers, registry, snapshot = _fixture()
    for pid, patch in (rows or {}).items():
        snapshot["providers"][pid].update(patch)
    return ManagedPool(providers, registry=registry, discovery=snapshot,
                       accounts={}, env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       **kwargs)


def _row(pool, pid="alpha"):
    return next(r for r in pool.managed_status()["providers"] if r["id"] == pid)


WARN_CASES = [
    ("denied", "verify credential scope or account verification with the provider"),
    ("auth_failed", "check the credential"),
    ("auth_missing", "check the credential"),
    ("unsupported", "listing unsupported for this provider"),
    ("error", "re-check"),
    ("partial", "re-check"),
    ("rate_limited", "transient, retry later"),
    ("deferred", "time budget, retry on next update"),
]


@pytest.mark.parametrize("status,guidance", WARN_CASES)
def test_adverse_preserved_row_warns_while_serving(tmp_path, status, guidance):
    pool = _pool(tmp_path, {"alpha": {"status": status, "note": "synthetic"}})
    row = _row(pool)
    assert row["eligible"] > 0  # still serves: admission untouched
    assert row["reason"] == "ready"
    assert row["warning"] == (
        f"serving {row['eligible']} preserved routes; last listing "
        f"{status}: {guidance}; run freellmpool update --provider alpha "
        "to re-check")


def test_ok_row_silent_with_empty_warning(tmp_path):
    row = _row(_pool(tmp_path))
    assert row["eligible"] > 0
    assert row["warning"] == ""


def test_unknown_future_status_fails_loud(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "frobnicated"}})
    row = _row(pool)
    assert row["eligible"] > 0
    assert "frobnicated" in row["warning"] and "re-check" in row["warning"]


def test_excluded_rows_silent_with_reasons(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "blocked", "complete": False,
                                      "models": []}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert "blocked" in row["reason"]


def test_deferred_previous_incomplete_excluded_silent(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "deferred", "complete": False,
                                      "models": []}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert "deferred" in row["reason"]


def test_warning_charset_and_length(tmp_path):
    allowed = re.compile(r"^[A-Za-z0-9 .,:;_()/+-]+$")
    for status, _ in WARN_CASES:
        pool = _pool(tmp_path, {"alpha": {"status": status}})
        warning = _row(pool)["warning"]
        assert allowed.match(warning), status
        assert len(warning) <= 300, status


def test_denied_incomplete_excluded_silent(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "denied", "complete": False,
                                      "models": []}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert "denied" in row["reason"]


def test_stale_expired_silent(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "denied",
                                      "checked_at": "2020-01-01T00:00:00+00:00"}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert "expired" in row["reason"]


def test_empty_models_adverse_silent(tmp_path):
    pool = _pool(tmp_path, {"alpha": {"status": "denied", "complete": True,
                                      "models": []}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert row["reason"] == "no eligible models"


def test_auth_missing_unconfigured_silent(tmp_path):
    providers, registry, snapshot = _fixture()
    providers = [Provider("alpha", "alpha", "openai", "https://alpha.test/v1",
                          (Model("free", context=32000),),
                          auth="bearer", key_env="ALPHA_API_KEY")]
    registry["alpha"]["credential_env"] = "ALPHA_API_KEY"
    snapshot["providers"]["alpha"].update(status="auth_missing")
    pool = ManagedPool(providers, registry=registry, discovery=snapshot,
                       accounts={}, env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"))
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert "missing" in row["reason"]


def test_zero_eligible_grant_exclusion_silent(tmp_path):
    paid = {"id": "free", "modalities": ["chat"], "context": 32000,
            "pricing": {"input": "1", "output": "1"}}
    pool = _pool(tmp_path, {"alpha": {"status": "denied", "complete": True,
                                      "models": [paid]}})
    row = _row(pool)
    assert row["eligible"] == 0 and row["warning"] == ""
    assert row["reason"] != "ready"


def test_warning_key_present_but_empty_on_ok_and_excluded(tmp_path):
    assert "warning" in _row(_pool(tmp_path))
    pool = _pool(tmp_path, {"alpha": {"status": "blocked", "complete": False,
                                      "models": []}})
    assert _row(pool)["warning"] == ""


def test_admission_identity_degraded_vs_ok(tmp_path):
    degraded = _pool(tmp_path, {"alpha": {"status": "denied"}})
    healthy = _pool(tmp_path)
    assert [r.name for r in degraded.snapshot().routes] == \
        [r.name for r in healthy.snapshot().routes]
    assert _row(degraded)["eligible"] == _row(healthy)["eligible"]


def test_status_text_golden_warns(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    pool = _pool(tmp_path, {"alpha": {"status": "denied"}})
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls: pool))
    assert cmd_status(SimpleNamespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "ready; WARNING: serving 1 preserved routes" in out
    assert "update --provider alpha" in out


def test_providers_text_golden_warns(tmp_path, monkeypatch, capsys):
    from freellmpool.cli import cmd_providers

    pool = _pool(tmp_path, {"alpha": {"status": "denied"}})
    monkeypatch.setattr("freellmpool.cli.Pool",
                        SimpleNamespace(from_default_config=lambda: pool))
    assert cmd_providers(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "ready; WARNING: serving 1 preserved routes" in out


def test_status_json_carries_warning(tmp_path, monkeypatch, capsys):
    from freellmpool.managed_cli import cmd_status

    pool = _pool(tmp_path, {"alpha": {"status": "denied"}})
    monkeypatch.setattr(ManagedPool, "from_default_config",
                        classmethod(lambda cls: pool))
    assert cmd_status(SimpleNamespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    row = next(r for r in status["providers"] if r["id"] == "alpha")
    assert row["warning"].startswith("serving 1 preserved routes")


def test_quota_summary_warns_degraded(tmp_path):
    from freellmpool.mcp_server import _quota_summary

    pool = _pool(tmp_path, {"alpha": {"status": "denied"}})
    assert "alpha: WARNING: serving 1 preserved routes" in _quota_summary(pool)


def test_proxy_status_flow_through(tmp_path):
    from freellmpool.proxy import serve

    pool = _pool(tmp_path, {"alpha": {"status": "denied"}})
    httpd = serve(pool, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/status") as resp:  # noqa: S310
            payload = json.load(resp)
    finally:
        httpd.shutdown()
        httpd.server_close()
    row = next(r for r in payload["eligibility"] if r["id"] == "alpha")
    assert row["warning"].startswith("serving 1 preserved routes")


def test_models_lockin_unchanged(tmp_path, monkeypatch, capsys):
    from freellmpool.cli import cmd_models

    def run(pool):
        monkeypatch.setattr("freellmpool.cli.Pool",
                            SimpleNamespace(from_default_config=lambda: pool))
        args = SimpleNamespace(providers=None, json=True)
        assert cmd_models(args) == 0
        return capsys.readouterr().out

    degraded = run(_pool(tmp_path, {"alpha": {"status": "denied"}}))
    healthy = run(_pool(tmp_path))
    def scrub(s):
        return re.sub(r"\d{9,}\.\d+", "T", s)  # wall-clock only
    assert scrub(degraded) == scrub(healthy)  # catalog/health split
    assert "WARNING" not in degraded


def test_readiness_lockin_ready(tmp_path):
    from freellmpool.proxy import _readiness_snapshot

    snap = _readiness_snapshot(_pool(tmp_path, {"alpha": {"status": "denied"}}))
    provider = next(p for p in snap.providers if p.id == "alpha")
    assert provider.ready is True  # routability axis, not listing health
