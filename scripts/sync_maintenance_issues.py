"""Reconcile validated public maintenance findings into owned GitHub issues.

No report field is evaluated as code, shell, workflow syntax or a command.
Only a bot-created issue with this script's marker may be changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.fetch_maintenance_baseline import DEFAULT_REPOSITORY, SHA, GitHubAPI, bounded_json

BEGIN = "<!-- freellmpool-maintenance:begin -->"
END = "<!-- freellmpool-maintenance:end -->"
MARKER = re.compile(
    r"<!-- freellmpool-maintenance:v1 key=([0-9a-f]{64}) "
    r"fingerprint=([0-9a-f]{64}) state=(open|resolved) -->"
)
MAX_ISSUE_PAGES = 20
BOT_LOGIN = "github-actions[bot]"
WORKFLOW_FINDING = {
    "id": "workflow:public-report", "provider": "workflow", "kind": "incident",
    "code": "public_report_failed", "subject": "provider-evidence-review.yml",
    "fingerprint": hashlib.sha256(b"public-maintenance-workflow-failed-v1").hexdigest(),
}


def _key(finding: dict[str, Any]) -> str:
    identity = [finding.get(field) for field in ("id", "provider", "kind", "code", "subject")]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _detail(value: Any) -> str:
    """Render bounded plain code text, with mentions, URLs and markup removed."""
    text = json.dumps(value, ensure_ascii=True, sort_keys=True) if not isinstance(value, str) else value
    text = re.sub(r"(?i)\b(?:https?|ftp|javascript|data):[^\s]*", "[URL omitted]", text)
    text = re.sub(r"[^A-Za-z0-9 _.,/:+={}\[\]-]", "?", text)
    return text[:400]


def _source_links(finding: dict[str, Any]) -> list[str]:
    from freellmpool.provider_registry import load_registry
    provider = load_registry().get(finding.get("provider"), {})
    known = [entry.get("url") for entry in provider.get("evidence", [])]
    known.append(provider.get("discovery", {}).get("url"))
    candidates = [finding["source_url"]] if "source_url" in finding else known[:3]
    links: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or candidate not in known:
            continue
        parsed = urlsplit(candidate)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not re.search(r"[\s<>`\[\]()\\]", candidate)):
            links.append(candidate)
    return links


def _block(finding: dict[str, Any], revision: str | None, *, resolved: bool = False) -> str:
    fingerprint = finding.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid public finding fingerprint")
    state = "resolved" if resolved else "open"
    lines = [BEGIN,
             f"<!-- freellmpool-maintenance:v1 key={_key(finding)} fingerprint={fingerprint} state={state} -->",
             f"Provider: `{_detail(finding.get('provider'))}`",
             f"Check: `{_detail(finding.get('code'))}`",
             f"Subject: `{_detail(finding.get('subject') or finding.get('id'))}`",
             "", "Status: " + ("Resolved by complete fresh evidence." if resolved else "Review required.")]
    for field in ("before", "after"):
        if field in finding:
            lines.append(f"{field.capitalize()}: `{_detail(finding[field])}`")
    if revision is not None and SHA.fullmatch(revision):
        lines.extend(["", f"Source revision: `{revision}`"])
    if finding.get("provider") == "workflow":
        lines.extend(["", "The public maintenance workflow did not produce a validated report.",
                      "Open Actions → Provider evidence review, inspect the failing step, and rerun the workflow."])
    else:
        lines.extend(["", "Run `freellmpool maintenance --public-only --refresh` from the reviewed checkout.",
                      "Review the public workflow artifacts and the provider's sources in `src/freellmpool/provider_registry.json`.",
                      "Model, pricing and allowance changes require reviewed policy changes; this issue grants no new eligibility."])
        lines.extend("[Reviewed provider source](" + url + ")" for url in _source_links(finding))
    lines.extend(["", "Maintainer notes may be kept outside this managed section.",
                  "Closing this issue suppresses unchanged evidence; a changed finding can reopen it.", END])
    return "\n".join(lines)


def _owned(issue: dict[str, Any], automation_login: str) -> tuple[str, str, str] | None:
    if (issue.get("user", {}).get("login") != automation_login or "pull_request" in issue
            or not isinstance(issue.get("body"), str)):
        return None
    body = issue["body"]
    matches = list(MARKER.finditer(body))
    if not matches:
        return None
    if len(matches) != 1 or body.count(BEGIN) != 1 or body.count(END) != 1:
        raise ValueError("Owned maintenance issue has ambiguous markers")
    if not body.index(BEGIN) < matches[0].start() < body.index(END):
        raise ValueError("Owned maintenance issue marker escaped its managed section")
    return matches[0].group(1), matches[0].group(2), matches[0].group(3)


def _issues(api: GitHubAPI, automation_login: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for page in range(1, MAX_ISSUE_PAGES + 1):
        rows = api.json("GET", f"/repos/{api.repository}/issues?state=all&per_page=100&page={page}")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid issue listing")
        for issue in rows:
            if not isinstance(issue, dict):
                raise ValueError("Invalid issue record")
            marker = _owned(issue, automation_login)
            if marker is not None:
                if marker[0] in result:
                    raise ValueError("Duplicate automation-owned maintenance issues")
                if type(issue.get("number")) is not int or issue["number"] <= 0:
                    raise ValueError("Invalid issue number")
                result[marker[0]] = issue
        if len(rows) < 100:
            return result
    raise ValueError("Issue listing exceeded its bounded pagination")


def _replace_block(body: str, block: str) -> str:
    start, end = body.index(BEGIN), body.index(END) + len(END)
    return body[:start] + block + body[end:]


def _reconcile(findings: list[dict[str, Any]], resolutions: list[dict[str, Any]], api: GitHubAPI,
               revision: str | None, automation_login: str) -> dict[str, int]:
    if not re.fullmatch(r"[A-Za-z0-9_\[\]-]{1,100}", automation_login):
        raise ValueError("Invalid automation identity")
    desired: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = _key(finding)
        _block(finding, revision)  # Validate every mutation before contacting GitHub.
        if key in desired and desired[key]["fingerprint"] != finding["fingerprint"]:
            raise ValueError("Contradictory public findings")
        desired[key] = finding
    for resolution in resolutions:
        _block(resolution, revision, resolved=True)
    existing = _issues(api, automation_login)
    stats = {"created": 0, "updated": 0, "closed": 0, "suppressed": 0, "unchanged": 0}
    prefix = f"/repos/{api.repository}/issues"
    for key, finding in desired.items():
        block = _block(finding, revision)
        previous = existing.get(key)
        if previous is None:
            title = "[maintenance] " + _detail(finding.get("provider")) + ": " + _detail(finding.get("code"))
            api.json("POST", prefix, {"title": title[:160], "body": block})
            stats["created"] += 1
            continue
        # Re-read immediately before updates so human notes added since listing survive.
        path = f"{prefix}/{previous['number']}"
        current = api.json("GET", path)
        if not isinstance(current, dict):
            raise ValueError("Invalid current issue state")
        marker = _owned(current, automation_login)
        if marker is None or marker[0] != key:
            raise ValueError("Maintenance issue ownership changed during synchronization")
        if marker[1] == finding["fingerprint"] and marker[2] == "open":
            stats["suppressed" if current.get("state") == "closed" else "unchanged"] += 1
            continue
        payload = {"body": _replace_block(current["body"], block)}
        if current.get("state") == "closed":
            payload["state"] = "open"
        api.json("PATCH", path, payload)
        stats["updated"] += 1
    for resolution in resolutions:
        key = _key(resolution)
        if key in desired or key not in existing:
            continue
        path = f"{prefix}/{existing[key]['number']}"
        current = api.json("GET", path)
        if not isinstance(current, dict):
            raise ValueError("Invalid current issue state")
        marker = _owned(current, automation_login)
        if (marker is None or marker[0] != key or marker[1] != resolution["fingerprint"]
                or current.get("state") != "open"):
            continue
        block = _block(resolution, revision, resolved=True)
        api.json("PATCH", path, {"body": _replace_block(current["body"], block),
                                 "state": "closed", "state_reason": "completed"})
        stats["closed"] += 1
    return stats


def reconcile_validated_report(report: dict[str, Any], api: GitHubAPI, *,
                                automation_login: str = BOT_LOGIN) -> dict[str, int]:
    """Apply an already schema-validated report; absence is never proof of recovery."""
    findings = report.get("findings", []) + report.get("pending_changes", [])
    resolutions = [row for row in report.get("resolutions", []) if _proves_recovery(row, report)]
    resolutions.append(WORKFLOW_FINDING)
    return _reconcile(findings, resolutions, api, report.get("source_revision"), automation_login)


def _proves_recovery(resolution: dict[str, Any], report: dict[str, Any]) -> bool:
    """A well-shaped resolution alone cannot override failed or expired checks."""
    provider = report.get("providers", {}).get(resolution.get("provider"), {})
    code = resolution.get("code", "")
    evidence: dict[str, Any] = {}
    if code.startswith("catalog_") or code in {"model_added", "model_removed", "price_changed"}:
        evidence = provider.get("catalog", {})
        if evidence.get("status") != "ok" or evidence.get("complete") is not True:
            return False
    elif code.startswith("source_"):
        evidence = next((row for row in provider.get("sources", [])
                         if row.get("id") == resolution.get("subject")), {})
        if evidence.get("status") != "unchanged":
            return False
    else:
        # Limit parser changes need explicit maintainer review; a catalog success
        # cannot establish that a proposed allowance change has been reviewed.
        return False
    try:
        checked = datetime.fromisoformat(evidence["checked_at"])
        expires = datetime.fromisoformat(evidence["expires_at"])
        observed = datetime.fromisoformat(report["checked_at"])
        return (checked.tzinfo is not None and expires.tzinfo is not None
                and observed.tzinfo is not None and expires > observed
                and 0 <= (observed - checked).total_seconds() <= 86400)
    except (KeyError, TypeError, ValueError):
        return False


def reconcile_workflow_failure(api: GitHubAPI, *, automation_login: str = BOT_LOGIN) -> dict[str, int]:
    """A static incident works even when no safe public report can be read."""
    return _reconcile([WORKFLOW_FINDING], [], api, None, automation_login)


def validate_report(document: Any, expected_revision: str, *,
                    now: datetime | None = None) -> dict[str, Any]:
    from freellmpool.maintenance import validate_public_report
    report = validate_public_report(document)
    if not SHA.fullmatch(expected_revision) or report.get("source_revision") != expected_revision:
        raise ValueError("Public report revision does not match the trusted workflow")
    checked = datetime.fromisoformat(report["checked_at"])
    current = now or datetime.now(UTC)
    if checked.tzinfo is None or not 0 <= (current - checked).total_seconds() <= 86400:
        raise ValueError("Public issue evidence is not fresh")
    return dict(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--automation-login", default=BOT_LOGIN)
    parser.add_argument("--source-revision")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--report", type=Path)
    inputs.add_argument("--workflow-failure", action="store_true")
    args = parser.parse_args(argv)
    api: GitHubAPI | None = None
    try:
        # Validate the public input before constructing any privileged API transport.
        report = None
        if args.report is not None:
            with args.report.open("rb") as stream:
                from scripts.fetch_maintenance_baseline import MAX_BYTES
                document = bounded_json(stream.read(MAX_BYTES + 1))
            report = validate_report(document, args.source_revision or "")
        api = GitHubAPI(args.repository, os.environ.get("GH_TOKEN", ""))
        stats = (reconcile_workflow_failure(api, automation_login=args.automation_login) if report is None
                 else reconcile_validated_report(report, api, automation_login=args.automation_login))
        print(json.dumps(stats, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print("Maintenance issue synchronization failed validation or a GitHub API check.")
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    raise SystemExit(main())
