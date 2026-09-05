"""Unauthenticated, bounded public GitHub workflow health cached locally."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from .free_policy import timestamp
from .maintenance import _now, _read, _write, state_directory

REPOSITORY = "pauljones0/freellmpool"
WORKFLOW = "provider-evidence-review.yml"
_URL = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}"
_STATUSES = {"ok", "pending", "not_checked", "unknown", "disabled", "failed", "overdue"}
_MAX_BYTES = 128_000
_OVERDUE_SECONDS = 2 * 86400  # Daily schedule plus one day of scheduler grace.
JSON = dict[str, Any]


def _client() -> httpx.Client:
    return httpx.Client(timeout=8, follow_redirects=False, trust_env=False)


def _empty() -> JSON:
    return {"schema": 1, "repository": REPOSITORY, "status": "not_checked", "checked_at": None,
            "last_attempt_at": None, "last_success_at": None}


def load_workflow_status(env: Mapping[str, str], *, now: datetime | None = None) -> JSON:
    """Read sanitized cached observation; no HTTP calls and no filesystem writes."""
    raw = _read(state_directory(env) / "workflow-health.json")
    result = _empty()
    if raw.get("schema") == 1 and raw.get("repository") == REPOSITORY:
        if isinstance(raw.get("status"), str) and raw["status"] in _STATUSES:
            result["status"] = raw["status"]
        for key in ("checked_at", "last_attempt_at", "last_success_at"):
            if timestamp(raw.get(key)) is not None:
                result[key] = raw[key]
    last_success = timestamp(result["last_success_at"])
    if result["status"] in {"ok", "pending"} and last_success is not None and _now(now).timestamp() - last_success > _OVERDUE_SECONDS:
        result["status"] = "overdue"
    return result


def _get(client: httpx.Client, url: str, *, params: dict[str, str | int] | None = None) -> JSON:
    with client.stream("GET", url, params=params, headers={"Accept": "application/vnd.github+json"}) as response:
        if response.status_code != 200:
            raise ValueError("Workflow observation unavailable")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_BYTES:
                raise ValueError("Workflow response exceeds bound")
        import json
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Invalid workflow observation")
        return result


def refresh_workflow(env: Mapping[str, str], *, now: datetime | None = None) -> JSON:
    """Read known public endpoints without any GitHub or provider credential."""
    current = _now(now)
    result = load_workflow_status(env, now=current)
    result["last_attempt_at"] = current.isoformat()
    try:
        with _client() as client:
            workflow = _get(client, _URL)
            state = workflow.get("state")
            if state in ("disabled_inactivity", "disabled_manually", "disabled_fork"):
                result["status"] = "disabled"
            elif state == "active":
                body = _get(client, _URL + "/runs", params={"branch": "main", "per_page": 1})
                runs = body.get("workflow_runs")
                if not isinstance(runs, list) or len(runs) > 1:
                    raise ValueError("Invalid workflow run list")
                if not runs:
                    result["status"] = "not_checked"
                else:
                    run = runs[0]
                    if not isinstance(run, dict) or run.get("head_branch") != "main":
                        raise ValueError("Unexpected workflow branch")
                    when = timestamp(run.get("updated_at"))
                    if when is None or when > current.timestamp():
                        raise ValueError("Invalid workflow run time")
                    if run.get("status") == "completed":
                        if run.get("conclusion") == "success":
                            result["last_success_at"] = run["updated_at"]
                            result["status"] = "overdue" if current.timestamp() - when > _OVERDUE_SECONDS else "ok"
                        elif run.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"):
                            result["status"] = "failed"
                        else:
                            raise ValueError("Unknown workflow outcome")
                    elif run.get("status") in ("queued", "in_progress", "waiting", "pending", "requested"):
                        result["status"] = "pending"
                    else:
                        raise ValueError("Unknown workflow run state")
            else:
                raise ValueError("Unknown workflow state")
        result["checked_at"] = current.isoformat()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        result["status"] = "unknown"
    _write(state_directory(env) / "workflow-health.json", result)
    return result
