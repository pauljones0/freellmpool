"""Public workflow telemetry stays unauthenticated and failure-aware."""

import json
from datetime import timedelta

import httpx
from test_maintenance import NOW

from freellmpool import workflow_health as h


def mock_api(monkeypatch, state="active", conclusion="success", *, status=200, age=0):
    calls = []
    def responder(request):
        calls.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        if status != 200:
            return httpx.Response(status, text="SECRET @everyone")
        body = {"state": state} if not request.url.path.endswith("/runs") else {"workflow_runs": [{
            "status": "completed", "conclusion": conclusion, "head_branch": "main",
            "updated_at": (NOW - timedelta(days=age)).isoformat()}]}
        return httpx.Response(200, json=body)
    monkeypatch.setattr(h, "_client", lambda: httpx.Client(transport=httpx.MockTransport(responder)))
    return calls


def test_successful_observation_then_failure_preserves_last_success(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path), "GITHUB_TOKEN": "SECRET"}
    mock_api(monkeypatch)
    good = h.refresh_workflow(env, now=NOW)
    assert good["status"] == "ok"
    mock_api(monkeypatch, status=503)
    failed = h.refresh_workflow(env, now=NOW + timedelta(hours=1))
    assert failed["status"] == "unknown"
    assert failed["last_success_at"] == good["last_success_at"]
    assert failed["checked_at"] == good["checked_at"]
    assert "SECRET" not in json.dumps(failed)


def test_disabled_and_overdue_and_failed_are_distinct(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    mock_api(monkeypatch, state="disabled_inactivity")
    assert h.refresh_workflow(env, now=NOW)["status"] == "disabled"
    mock_api(monkeypatch, age=9)
    assert h.refresh_workflow(env, now=NOW)["status"] == "overdue"
    mock_api(monkeypatch, conclusion="failure")
    assert h.refresh_workflow(env, now=NOW)["status"] == "failed"


def test_unpublished_repo_is_explicitly_unknown_without_leaking_response(tmp_path, monkeypatch):
    mock_api(monkeypatch, status=404)
    result = h.refresh_workflow({"XDG_STATE_HOME": str(tmp_path)}, now=NOW)
    assert result["status"] == "unknown"
    assert result["last_success_at"] is None
    assert "SECRET" not in json.dumps(result)


def test_status_reads_only_cache_and_recalculates_overdue(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    mock_api(monkeypatch)
    h.refresh_workflow(env, now=NOW)
    monkeypatch.setattr(h, "_client", lambda: (_ for _ in ()).throw(AssertionError("network")))
    assert h.load_workflow_status(env, now=NOW + timedelta(days=9))["status"] == "overdue"
