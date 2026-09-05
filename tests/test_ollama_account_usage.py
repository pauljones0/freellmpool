"""Ollama account usage is telemetry, with two independent read sources."""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from freellmpool import account_observations as a

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
PLAN = {"ID": "87ecb8c0-dca9-43e9-ad19-5df87da47a40", "Plan": "starter"}
USAGE = {
    "activity": {"cost": "private-units-unknown", "period": {
        "type": "last_4_weeks", "starting_at": "2026-08-10T00:00:00Z",
        "ending_at": "2026-09-05T12:00:00Z"}, "models": []},
    "limits": {"monthly": {"usage": 0, "models": [
        {"name": "gemma4:31b", "request_count": 3},
        {"name": "gpt-oss:20b", "request_count": 2}]}}}


@pytest.fixture
def env(tmp_path):
    return {"FREELLMPOOL_OBSERVATIONS_FILE": str(tmp_path / "observations.json"),
            "OLLAMA_API_KEY": "test-ollama-secret"}


def setup(monkeypatch, body=USAGE, *, failure_path=None, status=200):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.host == "ollama.com"
        assert request.headers["Authorization"] == "Bearer test-ollama-secret"
        assert request.content == b""
        if request.url.path == failure_path:
            return httpx.Response(status, text="private-error", headers={"Location": "https://example.invalid"})
        return httpx.Response(200, json=PLAN if request.url.path == "/api/me" else body)
    monkeypatch.setattr(a, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=False))
    monkeypatch.setattr(a, "_now", lambda: NOW)
    return calls


def refresh(env):
    return a.refresh_accounts(env, ["ollama"])["providers"]["ollama"]


def test_verified_two_read_contract_preserves_only_typed_usage(monkeypatch, env):
    calls = setup(monkeypatch)
    record = refresh(env)
    assert [(r.method, str(r.url)) for r in calls] == [
        ("POST", "https://ollama.com/api/me"), ("GET", "https://ollama.com/api/usage")]
    assert record["status"] == "ok"
    rows = record["observations"]
    assert len(rows) == 4
    usage = next(row for row in rows if row["fact"] == "account.reported_usage.monthly")
    assert (usage["kind"], usage["value"], usage["unit"], usage["scope"], usage["window"]) == (
        "reported_usage", "0", "unknown", "account", "month")
    counts = [row for row in rows if row["fact"] == "model.request_count.monthly"]
    assert {row["model_id"]: row["value"] for row in counts} == {"gemma4:31b": 3, "gpt-oss:20b": 2}
    for row in counts:
        assert (row["kind"], row["unit"], row["scope"], row["window"]) == (
            "consumed", "requests", "account_model", "month")
    for row in rows:
        expected = "me" if row["fact"] == "account.plan" else "usage"
        assert row["source_url"] == f"https://ollama.com/api/{expected}"
        assert row["checked_at"] == record["checked_at"]
    encoded = json.dumps(record)
    for ignored in ("activity", "cost", "reset", "starting_at", "ending_at", "private-", PLAN["ID"]):
        assert ignored not in encoded
    assert a.quota_restrictions({"providers": {"ollama": record}}, "ollama", {}, credential_ref="unused") == []
    assert a.load_observations(env)["providers"]["ollama"] == record


@pytest.mark.parametrize("value", [0.046, 1.25, 25])
def test_reported_usage_is_never_scaled_or_clamped(monkeypatch, env, value):
    body = copy.deepcopy(USAGE)
    body["limits"]["monthly"] = {"usage": value, "models": []}
    setup(monkeypatch, body)
    row = next(row for row in refresh(env)["observations"] if row["fact"] == "account.reported_usage.monthly")
    assert row["value"] == str(value) and row["unit"] == "unknown"
    assert a.load_observations(env)["providers"]["ollama"]["status"] == "ok"


@pytest.mark.parametrize("path", ["/api/me", "/api/usage"])
@pytest.mark.parametrize("status,expected", [(401, "auth_failed"), (429, "rate_limited"), (500, "error"), (302, "error")])
def test_either_read_failure_preserves_complete_old_generation(monkeypatch, env, path, status, expected):
    setup(monkeypatch)
    original = refresh(env)
    calls = setup(monkeypatch, failure_path=path, status=status)
    monkeypatch.setattr(a, "_now", lambda: NOW + timedelta(hours=1))
    record = refresh(env)
    assert record["status"] == expected
    assert len(calls) == (1 if path == "/api/me" else 2)
    for field in ("checked_at", "expires_at", "observations"):
        assert record[field] == original[field]
    assert record["last_attempt_at"] != original["last_attempt_at"]
    assert "private-error" not in json.dumps(record)


@pytest.mark.parametrize("monthly", [
    None, {}, {"usage": 0}, {"usage": True, "models": []},
    {"usage": "0", "models": []}, {"usage": -1, "models": []},
    {"usage": 0, "models": {}},
    {"usage": 0, "models": [{"name": "gemma4:31b", "request_count": True}]},
    {"usage": 0, "models": [{"name": "gemma4:31b", "request_count": -1}]},
    {"usage": 0, "models": [{"name": "gemma4:31b", "request_count": 1.5}]},
    {"usage": 0, "models": [{"name": "bad model", "request_count": 1}]},
    {"usage": 0, "models": [{"name": "x", "request_count": 1}] * 2},
    {"usage": 0, "models": [{"name": f"model-{i}", "request_count": 1} for i in range(1001)]},
])
def test_invalid_usage_does_not_publish_partial_identity(monkeypatch, env, monthly):
    setup(monkeypatch, {"limits": {"monthly": monthly}})
    record = refresh(env)
    assert record["status"] == "malformed"
    assert record["checked_at"] is None and record["observations"] == []


@pytest.mark.parametrize("mutation", ["source_plan", "source_usage", "duplicate", "missing_usage", "missing_plan", "scope", "count"])
def test_cache_requires_complete_sources_and_valid_usage(monkeypatch, env, mutation):
    setup(monkeypatch)
    snapshot = a.refresh_accounts(env, ["ollama"])
    rows = snapshot["providers"]["ollama"]["observations"]
    plan = next(row for row in rows if row["fact"] == "account.plan")
    usage = next(row for row in rows if row["fact"] == "account.reported_usage.monthly")
    count = next(row for row in rows if row["fact"] == "model.request_count.monthly")
    if mutation == "source_plan":
        plan["source_url"] = "https://ollama.com/api/usage"
    elif mutation == "source_usage":
        usage["source_url"] = "https://ollama.com/api/me"
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(count))
    elif mutation == "missing_usage":
        rows[:] = [plan]  # Legacy identity-only snapshot cannot attest both reads.
    elif mutation == "missing_plan":
        rows.remove(plan)
    elif mutation == "scope":
        del count["model_id"]
    else:
        count["value"] = -1
    a.default_observations_path(env).write_text(json.dumps(snapshot))
    record = a.load_observations(env)["providers"]["ollama"]
    assert record["status"] == "not_checked" and record["observations"] == []


def test_key_rotation_between_reads_invalidates_both(monkeypatch, env):
    keys = []
    def respond(request):
        keys.append(request.headers["Authorization"])
        env["OLLAMA_API_KEY"] = "rotated-secret"
        return httpx.Response(200, json=PLAN if request.url.path == "/api/me" else USAGE)
    monkeypatch.setattr(a, "_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    record = refresh(env)
    assert keys == ["Bearer test-ollama-secret"] * 2
    assert record["status"] == "credential_changed"
    assert record["observations"] == [] and record["checked_at"] is None


def test_reflected_key_in_model_name_is_never_persisted(monkeypatch, env):
    body = copy.deepcopy(USAGE)
    body["limits"]["monthly"]["models"][0]["name"] = env["OLLAMA_API_KEY"]
    setup(monkeypatch, body)
    record = refresh(env)
    assert record["status"] == "malformed"
    assert env["OLLAMA_API_KEY"] not in a.default_observations_path(env).read_text()
