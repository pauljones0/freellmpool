"""Public maintenance transport, provenance and issue-ownership regressions."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from scripts import fetch_maintenance_baseline as baseline
from scripts import sync_maintenance_issues as issues

REPOSITORY = "pauljones0/freellmpool"
REVISION = "a" * 40
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def archive(document, name="public-baseline.json"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, json.dumps(document))
    return stream.getvalue()


def finding(kind="incident", fingerprint="b" * 64):
    return {
        "id": "groq:catalog:check_failed", "provider": "groq", "kind": kind,
        "code": "catalog_failed", "subject": "catalog", "status": "open",
        "summary": "A catalog check failed", "command": "freellmpool maintenance",
        "fingerprint": fingerprint,
    }


def report(entries=None, resolutions=None):
    return {"schema": 1, "visibility": "public", "checked_at": NOW.isoformat(),
            "source_revision": REVISION, "providers": {"groq": {"catalog": {
                "status": "ok", "complete": True, "checked_at": NOW.isoformat(),
                "last_attempt_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=2)).isoformat()}, "sources": []}},
            "findings": entries or [],
            "pending_changes": [], "resolutions": resolutions or [], "proposals": []}


class MemoryAPI:
    repository = REPOSITORY

    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows or [])
        self.calls = []

    def json(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and "?" in path:
            return copy.deepcopy(self.rows)
        if method == "POST":
            result = {"number": len(self.rows) + 1, "user": {"login": "github-actions[bot]"},
                      "state": "open", **payload}
            self.rows.append(result)
            return copy.deepcopy(result)
        number = int(path.rsplit("/", 1)[-1])
        target = next(row for row in self.rows if row["number"] == number)
        if method == "PATCH":
            target.update(payload)
        return copy.deepcopy(target)

    @property
    def mutations(self):
        return [row for row in self.calls if row[0] != "GET"]


def test_github_token_never_follows_archive_redirect_to_storage():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={
                "location": "https://productionresultssa.blob.core.windows.net/artifact?sig=test"})
        return httpx.Response(200, content=b"zip bytes")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = baseline.GitHubAPI(REPOSITORY, "test-private-token", client=client)
        assert api.download_artifact(5) == b"zip bytes"
    assert calls[0].headers["authorization"] == "Bearer test-private-token"
    assert "authorization" not in calls[1].headers


def test_default_stdlib_transport_drops_token_on_signed_artifact_redirect(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 30
            if request.full_url.startswith("https://api.github.com/"):
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {
                    "Location": "https://productionresultssa.blob.core.windows.net/artifact?sig=test"
                }, io.BytesIO())
            return Response(b"archive")

    monkeypatch.setattr(baseline.urllib.request, "build_opener", lambda *handlers: Opener())
    api = baseline.GitHubAPI(REPOSITORY, "test-private-token")
    assert api.download_artifact(5) == b"archive"
    assert calls[0].get_header("Authorization") == "Bearer test-private-token"
    assert calls[1].get_header("Authorization") is None


def test_default_stdlib_transport_bounds_and_redacts_http_errors(monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            return Response(b"x" * 129)

    monkeypatch.setattr(baseline, "MAX_BYTES", 128)
    monkeypatch.setattr(baseline.urllib.request, "build_opener", lambda *handlers: Opener())
    api = baseline.GitHubAPI(REPOSITORY, "test-private-token")
    with pytest.raises(ValueError, match="size bound"):
        api.json("GET", f"/repos/{REPOSITORY}")

    class FailingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "PRIVATE", {}, io.BytesIO(b"SECRET"))

    monkeypatch.setattr(baseline.urllib.request, "build_opener", lambda *handlers: FailingOpener())
    with pytest.raises(ValueError) as error:
        api.json("GET", f"/repos/{REPOSITORY}")
    assert "403" in str(error.value)
    assert "PRIVATE" not in str(error.value) and "SECRET" not in str(error.value)


@pytest.mark.parametrize("target", [
    "https://evil.example/archive", "http://productionresultssa.blob.core.windows.net/a",
    "https://productionresultssa.blob.core.windows.net.evil.example/a",
    "https://user@productionresultssa.blob.core.windows.net/a",
    "https://productionresultssa.blob.core.windows.net:8443/a",
])
def test_artifact_redirect_host_boundary(target):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": target})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = baseline.GitHubAPI(REPOSITORY, "private", client=client)
        with pytest.raises(ValueError):
            api.download_artifact(5)
    assert len(calls) == 1


def test_github_requests_reject_external_or_other_repository_paths():
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("network"))) as client:
        api = baseline.GitHubAPI(REPOSITORY, "private", client=client)
        for path in ["https://evil.example", "//evil.example", "/repos/another/repo/issues"]:
            with pytest.raises(ValueError):
                api.json("GET", path)


def test_bounded_transport_rejects_large_api_and_artifact_bodies(monkeypatch):
    monkeypatch.setattr(baseline, "MAX_BYTES", 128)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 129)
    )) as client:
        api = baseline.GitHubAPI(REPOSITORY, "private", client=client)
        with pytest.raises(ValueError):
            api.json("GET", f"/repos/{REPOSITORY}")


@pytest.mark.parametrize("name", ["../public-baseline.json", "/public-baseline.json", "other.json"])
def test_baseline_zip_cannot_extract_unexpected_names(name):
    with pytest.raises(ValueError):
        baseline.read_baseline_archive(archive({}, name), REVISION, validator=lambda value: value)


def test_baseline_zip_requires_single_bounded_regular_entry(monkeypatch):
    monkeypatch.setattr(baseline, "MAX_BYTES", 256)
    with pytest.raises(ValueError):
        baseline.read_baseline_archive(archive({"padding": "x" * 1000}), REVISION,
                                       validator=lambda value: value)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr("public-baseline.json", "{}")
        output.writestr("private.json", "{}")
    with pytest.raises(ValueError):
        baseline.read_baseline_archive(stream.getvalue(), REVISION, validator=lambda value: value)


def test_baseline_revision_and_validator_are_required():
    document = {"source_revision": REVISION}
    assert baseline.read_baseline_archive(archive(document), REVISION,
        validator=lambda value: value) == document
    with pytest.raises(ValueError):
        baseline.read_baseline_archive(archive(document), "c" * 40, validator=lambda value: value)
    with pytest.raises(ValueError):
        baseline.read_baseline_archive(archive(document), REVISION,
            validator=lambda value: (_ for _ in ()).throw(ValueError("private field")))


def test_fetch_uses_only_known_successful_default_branch_workflow():
    run = {"id": 3, "event": "schedule", "conclusion": "success", "head_branch": "main",
           "path": ".github/workflows/provider-evidence-review.yml", "head_sha": REVISION,
           "head_repository": {"full_name": REPOSITORY}}
    bad = [{**run, "id": 4, "event": "pull_request"},
           {**run, "id": 5, "head_branch": "untrusted"},
           {**run, "id": 6, "head_repository": {"full_name": "evil/repo"}},
           {**run, "id": 7, "path": ".github/workflows/another.yml"}]

    class API:
        repository = REPOSITORY

        def json(self, method, path, payload=None):
            if path == f"/repos/{REPOSITORY}":
                return {"default_branch": "main"}
            if "/workflows/" in path:
                return {"workflow_runs": bad + [run]}
            assert "/runs/3/artifacts" in path
            return {"artifacts": [{"id": 2, "name": baseline.ARTIFACT_NAME,
                                   "size_in_bytes": 200, "expired": False,
                                   "workflow_run": {"id": 3, "head_sha": REVISION}}]}

        def download_artifact(self, artifact_id):
            assert artifact_id == 2
            return archive({"source_revision": REVISION})

    result = baseline.fetch_baseline(API(), current_run_id=10, validator=lambda value: value)
    assert result == {"source_revision": REVISION}


def test_issue_repeat_is_quiet_and_preserves_manual_text():
    api = MemoryAPI()
    issues.reconcile_validated_report(report([finding()]), api)
    assert len(api.mutations) == 1
    api.rows[0]["body"] = "Maintainer notes before\n\n" + api.rows[0]["body"] + "\n\nManual footer"
    api.calls.clear()
    issues.reconcile_validated_report(report([finding()]), api)
    assert not api.mutations
    changed = finding(fingerprint="c" * 64)
    issues.reconcile_validated_report(report([changed]), api)
    assert len(api.mutations) == 1
    assert api.rows[0]["body"].startswith("Maintainer notes before")
    assert api.rows[0]["body"].endswith("Manual footer")


def test_foreign_created_issue_with_spoofed_marker_is_not_modified():
    api = MemoryAPI()
    issues.reconcile_validated_report(report([finding()]), api)
    api.rows[0]["user"]["login"] = "human-reviewer"
    original = copy.deepcopy(api.rows[0])
    api.calls.clear()
    issues.reconcile_validated_report(report([finding()]), api)
    assert api.rows[0] == original
    assert api.mutations[0][0] == "POST"


def test_manually_closed_issue_stays_suppressed_until_evidence_changes():
    api = MemoryAPI()
    issues.reconcile_validated_report(report([finding()]), api)
    api.rows[0].update(state="closed", closed_by={"login": "maintainer"})
    api.calls.clear()
    issues.reconcile_validated_report(report([finding()]), api)
    assert not api.mutations
    issues.reconcile_validated_report(report([finding(fingerprint="d" * 64)]), api)
    assert api.rows[0]["state"] == "open"


def test_absence_and_failed_checks_never_close_pending_review():
    api = MemoryAPI()
    old = {**finding(kind="review"), "code": "price_changed"}
    issues.reconcile_validated_report(report([old]), api)
    api.calls.clear()
    issues.reconcile_validated_report(report(), api)
    assert not api.mutations
    resolution = {key: old[key] for key in ["id", "provider", "kind", "code", "subject", "fingerprint"]}
    mismatched = {**resolution, "fingerprint": "f" * 64}
    issues.reconcile_validated_report(report(resolutions=[mismatched]), api)
    assert not api.mutations
    failed = report(resolutions=[resolution])
    failed["providers"]["groq"]["catalog"]["status"] = "partial"
    issues.reconcile_validated_report(failed, api)
    assert not api.mutations
    issues.reconcile_validated_report(report(resolutions=[resolution]), api)
    assert api.rows[0]["state"] == "closed"


def test_unsafe_upstream_text_cannot_inject_mentions_links_or_commands():
    entry = finding()
    entry.update(summary="@everyone [click](https://evil.example) $(execute)",
                 command="$(execute)", subject="` @person https://evil.example\n<!-- escape -->",
                 source_url="https://evil.example/SECRET")
    api = MemoryAPI()
    issues.reconcile_validated_report(report([entry]), api)
    body = api.rows[0]["body"]
    assert "@everyone" not in body and "@person" not in body
    assert "https://evil.example" not in body and "$(execute)" not in body
    assert "<!-- escape -->" not in body


def test_issue_links_only_reviewed_provider_sources():
    from freellmpool.provider_registry import load_registry
    approved = load_registry()["groq"]["evidence"][0]["url"]
    api = MemoryAPI()
    issues.reconcile_validated_report(report([{**finding(), "source_url": approved}]), api)
    assert approved in api.rows[0]["body"]


def test_workflow_failure_incident_is_static_and_resolves_on_valid_report():
    api = MemoryAPI()
    issues.reconcile_workflow_failure(api)
    api.calls.clear()
    issues.reconcile_workflow_failure(api)
    assert not api.mutations
    issues.reconcile_validated_report(report(), api)
    assert api.rows[0]["state"] == "closed"


def test_incomplete_issue_pagination_cannot_mutate():
    class CrowdedAPI(MemoryAPI):
        def json(self, method, path, payload=None):
            assert method == "GET"
            return [{"number": i} for i in range(100)]
    with pytest.raises(ValueError):
        issues.reconcile_validated_report(report([finding()]), CrowdedAPI())


def test_workflow_is_default_branch_serial_and_separates_tokens():
    content = Path(".github/workflows/provider-evidence-review.yml").read_text()
    workflow = yaml.safe_load(content)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["reset_baseline"]["default"] is False
    assert workflow["concurrency"]["cancel-in-progress"] is False
    collection = workflow["jobs"]["public-evidence"]
    assert "default_branch" in collection["if"]
    assert collection["permissions"].get("issues") != "write"
    checks = next(step for step in collection["steps"] if step.get("id") == "collect")
    restoration = next(step for step in collection["steps"] if "Restore" in step.get("name", ""))
    assert restoration["if"] == "inputs.reset_baseline != true"
    assert "GH_TOKEN" not in checks.get("env", {})
    assert "GITHUB_TOKEN" not in checks.get("env", {})
    assert "secrets." not in json.dumps(checks)
    synchronization = workflow["jobs"]["maintenance-issues"]
    assert synchronization["permissions"]["issues"] == "write"
    assert "always()" in synchronization["if"]
    assert "--workflow-failure" in content
    assert "persist-credentials: false" in content


def actual_public_report():
    from freellmpool.maintenance import build_public_report
    from freellmpool.provider_registry import load_registry
    registry = load_registry()
    snapshot = {"schema": 1, "providers": {}}
    return build_public_report(registry, snapshot, now=NOW, source_revision=REVISION)


def test_real_public_schema_and_archive_round_trip():
    public, stored = actual_public_report()
    assert issues.validate_report(public, REVISION, now=NOW) == public
    assert baseline.read_baseline_archive(archive(stored), REVISION) == stored


@pytest.mark.parametrize("mutation", ["private_field", "private_nested", "wrong_revision", "stale", "future"])
def test_public_entry_validation_rejects_private_or_wrong_evidence(mutation):
    public, _ = actual_public_report()
    if mutation == "private_field":
        public["account"] = {"key": "PRIVATE"}
    elif mutation == "private_nested":
        public["providers"][next(iter(public["providers"]))]["account_id"] = "PRIVATE"
    elif mutation == "wrong_revision":
        public["source_revision"] = "d" * 40
    elif mutation == "stale":
        public["checked_at"] = (NOW - timedelta(days=2)).isoformat()
    else:
        public["checked_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        issues.validate_report(public, REVISION, now=NOW)


def test_cli_rejects_private_input_before_creating_api_transport(tmp_path, monkeypatch):
    public, _ = actual_public_report()
    public["raw_error"] = "SECRET"
    path = tmp_path / "public-report.json"
    path.write_text(json.dumps(public))
    monkeypatch.setattr(issues, "GitHubAPI", lambda *args, **kwargs: pytest.fail("API before validation"))
    assert issues.main(["--report", str(path), "--source-revision", REVISION]) == 1


def test_static_failure_reporter_runs_when_package_installation_is_unavailable():
    env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "PYTHONPATH"}}
    result = subprocess.run([sys.executable, "-S", "-m", "scripts.sync_maintenance_issues",
                             "--workflow-failure"], env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert "failed validation or a GitHub API check" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_source_recovery_needs_exact_fresh_unchanged_evidence():
    api = MemoryAPI()
    entry = {**finding(), "code": "source_check_failed", "subject": "terms"}
    issues.reconcile_validated_report(report([entry]), api)
    resolution = {key: entry[key] for key in ["id", "provider", "kind", "code", "subject", "fingerprint"]}
    recovered = report(resolutions=[resolution])
    api.calls.clear()
    issues.reconcile_validated_report(recovered, api)
    assert not api.mutations
    recovered["providers"]["groq"]["sources"] = [{"id": "terms", "status": "unchanged",
        "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat()}]
    issues.reconcile_validated_report(recovered, api)
    assert api.rows[0]["state"] == "closed"
