"""Bounded maintenance reports, durable review work, and private local attention.

Building or reading a report does no network work. Public builders whitelist facts
and public validators reject unknown fields; private observations never enter the
public orchestration path. A new baseline does not acknowledge a pending change.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .free_policy import credential_fingerprint, fresh, model_matches_grant, timestamp
from .provider_registry import evidence_renewal_is_current, policy_digest, reviewed_limit_capacity

if TYPE_CHECKING:
    from .conformance import ConformanceStore
    from .router import Target

JSON = dict[str, Any]
_MAX_BYTES = 8_000_000
_MAX_MODELS = 12000
_MAX_FINDINGS = 12000
_ID = re.compile(r"[A-Za-z0-9_./:+~-]{1,256}\Z")
_NAMESPACED_MODEL_ID = re.compile(r"@[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9_./:+~-]+\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REVISION = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\Z")
_NUMBER = re.compile(r"-?\d{1,30}(?:\.\d{1,30})?\Z")
_PRICES = frozenset({"input", "output", "request", "image", "audio", "video", "cached_input",
    "input_cache_reads", "input_cache_writes", "internal_reasoning", "web_search",
    "input_tiers", "output_tiers", "input_cache_read_tiers", "input_cache_write_tiers", "unrecognized_price"})
_STATUSES = frozenset({"ok", "unchanged", "not_checked", "unsupported", "auth_missing", "auth_failed",
    "rate_limited", "partial", "error", "review_required", "check_failed", "expired", "stale",
    "credential_changed", "malformed", "official", "verified", "observed", "disabled", "requires_client_update"})
_MESSAGES = {
    "catalog_failed": "Model catalog check failed; previous evidence has not been renewed.",
    "catalog_stale": "Model discovery is missing or expired.",
    "catalog_due": "Model discovery is approaching expiry.",
    "model_added": "A model was added; review its current grant and capabilities.",
    "model_removed": "A model is no longer in the complete free catalog.",
    "price_changed": "Published model prices changed and require review.",
    "source_changed": "A reviewed policy source changed and requires review.",
    "source_baseline_needed": "This policy source needs an initial reviewed hash baseline.",
    "source_check_failed": "A policy source could not be checked; previous evidence has not been renewed.",
    "source_expired": "Reviewed policy evidence is missing or expired.",
    "source_due": "Reviewed policy evidence is approaching expiry.",
    "limit_changed": "An official limit parser proposes a rule change for review.",
    "limit_source_failed": "The official limit source could not be checked.",
    "limit_source_review": "The official limit table needs a parser or evidence review.",
    "account_expired": "Account confirmation has expired; confirm the current plan and billing conditions.",
    "account_due": "Account confirmation is approaching expiry.",
    "account_verification": "This configured account needs current plan and billing confirmation.",
    "account_check_failed": "The account observation check failed; existing confirmation is unchanged.",
    "account_stale": "Supported account observations are missing or expired.",
    "credential_changed": "The credential changed; confirm the current account before using its allowance.",
    "conformance_expired": "Previously useful protocol proof has expired.",
    "conformance_due": "Useful protocol proof is approaching expiry.",
    "policy_failed": "Reviewed policy delivery failed; the last valid policy remains active.",
    "workflow_disabled": "The public maintenance workflow is disabled; enable its schedule.",
    "workflow_failed": "The latest public maintenance workflow failed; inspect and rerun it.",
    "workflow_overdue": "The latest observed public workflow success is more than 48 hours old.",
}
_PRIVATE_CODES = frozenset({code for code in _MESSAGES if code.startswith(("account_", "conformance_", "policy_", "workflow_"))} | {"credential_changed"})
_REVIEW_CODES = frozenset({"model_added", "model_removed", "price_changed", "source_changed", "source_baseline_needed", "limit_changed", "limit_source_review"})
_FINDING_KEYS = frozenset({"id", "provider", "kind", "code", "status", "subject", "fingerprint", "summary", "command", "before", "after", "source_url"})
_RESOLUTION_KEYS = frozenset({"id", "provider", "kind", "code", "subject", "fingerprint"})


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("maintenance timestamps require a timezone")
    return result.astimezone(UTC)


def _stamp(value: object) -> str | None:
    return str(value) if timestamp(value) is not None else None


def _status(value: object) -> str:
    return str(value) if isinstance(value, str) and value in _STATUSES else "error"


def _model_identity(value: Any) -> bool:
    # Cloudflare's @namespace/model names are model identifiers, not provider
    # IDs or arbitrary report text. Neither form may contain path traversal.
    return (isinstance(value, str) and len(value) <= 256
            and bool(_ID.fullmatch(value) or _NAMESPACED_MODEL_ID.fullmatch(value))
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def _subject_identity(code: str, value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 256:
        return False
    if code in {"model_added", "model_removed", "price_changed"}:
        return _model_identity(value)
    if code == "limit_changed":
        rule, separator, model = value.partition("/")
        return bool(separator and _ID.fullmatch(rule) and _model_identity(model))
    return bool(_ID.fullmatch(value))


def _command(provider: str, code: str) -> str:
    if code.startswith("workflow_"):
        return f"gh workflow {'enable' if code == 'workflow_disabled' else 'run'} provider-evidence-review.yml --repo pauljones0/freellmpool"
    if code.startswith("account_") or code == "credential_changed":
        return f"freellmpool setup --provider {provider}" if code in {"account_due", "account_expired", "account_verification", "credential_changed"} else "freellmpool maintenance --refresh"
    if code.startswith("conformance_"):
        return f"freellmpool verify --provider {provider} --limit 4"
    return "freellmpool maintenance --refresh"


def _finding(provider: str, code: str, subject: str = "provider", *, before: Any = None,
             after: Any = None, source_url: str | None = None) -> JSON:
    identity = f"{provider}:{code}:{subject}"
    digest = hashlib.sha256(json.dumps([identity, before, after], sort_keys=True).encode()).hexdigest()
    row = {"id": f"{provider}:{code}:{hashlib.sha256(subject.encode()).hexdigest()[:16]}",
           "provider": provider, "kind": "review" if code in _REVIEW_CODES else "incident",
           "code": code, "status": "open", "subject": subject, "fingerprint": digest,
           "summary": _MESSAGES[code], "command": _command(provider, code), "before": before, "after": after}
    if source_url is not None:
        row["source_url"] = source_url
    return row


def _resolution(row: JSON) -> JSON:
    return {key: row[key] for key in _RESOLUTION_KEYS}


def _object(value: Any, allowed: set[str] | frozenset[str], required: set[str] | frozenset[str] = frozenset()) -> JSON:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - allowed:
        raise ValueError("invalid maintenance fields")
    return value


def _bounded(value: Any) -> None:
    try:
        if len(json.dumps(value, allow_nan=False)) > _MAX_BYTES:
            raise ValueError("maintenance document is too large")
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid maintenance document") from exc


def _fact(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(_ID.fullmatch(value))
    if type(value) in {int, float}:
        return abs(value) <= 1e18 and math.isfinite(value)
    return isinstance(value, dict) and value.keys() <= _PRICES and all(
        isinstance(amount, str) and _NUMBER.fullmatch(amount) for amount in value.values())


def _public_registry() -> JSON:
    from .provider_registry import load_registry
    return load_registry()


def _urls(registry: JSON, provider: str) -> set[str]:
    spec = registry[provider]
    return {str(row["url"]) for row in spec.get("evidence", [])} | {str(spec.get("discovery", {}).get("url", ""))}


def _source_allowed(registry: JSON, provider: str, value: Any, private_sources: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    approved = _urls(registry, provider)
    if value in approved:
        return True
    if not private_sources:
        return False
    try:
        source = urlsplit(value)
        return (source.scheme == "https" and not source.username and not source.password
                and source.port in (None, 443) and source.hostname in {urlsplit(url).hostname for url in approved})
    except ValueError:
        return False


def _validate_finding(row: Any, registry: JSON, *, resolution: bool = False, private_sources: bool = False) -> None:
    keys = _RESOLUTION_KEYS if resolution else _FINDING_KEYS
    required = _RESOLUTION_KEYS if resolution else _FINDING_KEYS - {"source_url"}
    value = _object(row, keys, required)
    pid, code, subject = value["provider"], value["code"], value["subject"]
    if (not isinstance(pid, str) or pid not in registry or not isinstance(code, str)
            or code not in _MESSAGES or code in _PRIVATE_CODES
            or not _subject_identity(code, subject)
            or not isinstance(value["fingerprint"], str) or not _HASH.fullmatch(value["fingerprint"])):
        raise ValueError("invalid public finding")
    expected = _finding(pid, code, subject, before=value.get("before"), after=value.get("after"))
    if value["id"] != expected["id"] or value["kind"] != expected["kind"]:
        raise ValueError("finding identity mismatch")
    if resolution:
        return
    if (value["status"] != "open" or value["fingerprint"] != expected["fingerprint"]
            or value["summary"] != expected["summary"] or value["command"] != expected["command"]
            or not _fact(value["before"]) or not _fact(value["after"])
            or ("source_url" in value and not _source_allowed(registry, pid, value["source_url"], private_sources))):
        raise ValueError("invalid public finding content")


def _validate_common(value: Any, keys: set[str]) -> JSON:
    row = _object(value, keys, keys)
    _bounded(row)
    if (type(row["schema"]) is not int or row["schema"] != 1 or row["visibility"] != "public"
            or not _stamp(row["checked_at"]) or not isinstance(row["providers"], dict)
            or (row["source_revision"] is not None and (not isinstance(row["source_revision"], str)
                or not _REVISION.fullmatch(row["source_revision"])))):
        raise ValueError("invalid public maintenance envelope")
    return row


def _validate_baseline(value: Any, registry: JSON, *, private_sources: bool = False) -> JSON:
    row = _validate_common(value, {"schema", "visibility", "checked_at", "source_revision", "providers", "pending_changes", "incidents", "proposals"})
    for pid, provider in row["providers"].items():
        if pid not in registry:
            raise ValueError("unreviewed provider in baseline")
        state = _object(provider, {"checked_at", "models"}, {"checked_at", "models"})
        if not _stamp(state["checked_at"]) or not isinstance(state["models"], dict) or len(state["models"]) > _MAX_MODELS:
            raise ValueError("invalid baseline catalog")
        for model, prices in state["models"].items():
            if not _model_identity(model) or not isinstance(prices, dict) or not _fact(prices):
                raise ValueError("invalid baseline model")
    for key, kind in (("pending_changes", "review"), ("incidents", "incident")):
        if not isinstance(row[key], list) or len(row[key]) > _MAX_FINDINGS:
            raise ValueError("invalid baseline findings")
        identities: set[str] = set()
        for finding in row[key]:
            _validate_finding(finding, registry, private_sources=private_sources)
            if finding["kind"] != kind or finding["id"] in identities:
                raise ValueError("invalid baseline finding kind or duplicate")
            identities.add(finding["id"])
    if not isinstance(row["proposals"], list) or len(row["proposals"]) > _MAX_FINDINGS:
        raise ValueError("invalid baseline proposals")
    for proposal in row["proposals"]:
        _validate_proposal(proposal, registry, private_sources=private_sources)
    return copy.deepcopy(row)


def validate_public_baseline(value: Any) -> JSON:
    """Reject unbounded/unrecognized artifact content before reconciliation."""
    return _validate_baseline(value, _public_registry())


def _validate_private_baseline(value: JSON) -> JSON:
    # Reviewed policy delivery permits same-origin evidence path updates. Private
    # baselines can retain those URLs; this permission never reaches public export.
    if value.get("visibility") != "private":
        raise ValueError("invalid private baseline")
    result = _validate_baseline({**value, "visibility": "public"}, _public_registry(), private_sources=True)
    result["visibility"] = "private"
    return result


def _validate_proposal(row: Any, registry: JSON, *, private_sources: bool = False) -> None:
    fields = {"provider", "rule_id", "model_id", "metric", "window_seconds", "old_capacity", "new_capacity", "source_url", "source_sha256"}
    value = _object(row, fields, fields)
    pid = value["provider"]
    if (not isinstance(pid, str) or pid not in registry or not _source_allowed(registry, pid, value["source_url"], private_sources)
            or not isinstance(value["source_sha256"], str) or not _HASH.fullmatch(value["source_sha256"])):
        raise ValueError("invalid proposal provenance")
    for key in ("rule_id", "model_id", "metric"):
        valid = (_model_identity(value[key]) if key == "model_id" else
                 isinstance(value[key], str) and bool(_ID.fullmatch(value[key])))
        if not valid:
            raise ValueError("invalid proposal identity")
    if any(not _fact(value[key]) or isinstance(value[key], (str, dict, bool)) for key in ("window_seconds", "old_capacity", "new_capacity")):
        raise ValueError("invalid proposed capacity")


def validate_public_report(value: Any) -> JSON:
    """Independent public-only schema gate used before GitHub receives a report."""
    row = _validate_common(value, {"schema", "visibility", "checked_at", "source_revision", "baseline_status", "providers", "findings", "resolutions", "proposals"})
    registry = _public_registry()
    if not isinstance(row["baseline_status"], str) or row["baseline_status"] not in {"ok", "missing", "invalid"}:
        raise ValueError("invalid baseline status")
    for pid, provider in row["providers"].items():
        if pid not in registry:
            raise ValueError("unreviewed report provider")
        value = _object(provider, {"catalog", "sources", "coverage"}, {"catalog", "sources", "coverage"})
        if not isinstance(value["coverage"], str) or value["coverage"] not in {"public", "unsupported"}:
            raise ValueError("private catalog coverage")
        cat = _object(value["catalog"], {"status", "checked_at", "last_attempt_at", "expires_at", "complete", "model_count"},
                      {"status", "checked_at", "last_attempt_at", "expires_at", "complete", "model_count"})
        if (not isinstance(cat["status"], str) or cat["status"] not in _STATUSES or type(cat["complete"]) is not bool
                or type(cat["model_count"]) is not int or not 0 <= cat["model_count"] <= _MAX_MODELS
                or any(cat[key] is not None and not _stamp(cat[key]) for key in ("checked_at", "last_attempt_at", "expires_at"))):
            raise ValueError("invalid catalog summary")
        if not isinstance(value["sources"], list) or len(value["sources"]) > 64:
            raise ValueError("invalid source summary")
        for source in value["sources"]:
            source = _object(source, {"id", "status", "checked_at", "expires_at", "last_attempt_at"}, {"id", "status", "checked_at", "expires_at"})
            if (not isinstance(source["id"], str) or source["id"] not in {item["id"] for item in registry[pid].get("evidence", [])}
                    or not isinstance(source["status"], str) or source["status"] not in _STATUSES
                    or any(source.get(key) is not None and not _stamp(source[key]) for key in ("checked_at", "expires_at", "last_attempt_at"))):
                raise ValueError("invalid public source")
    for key in ("findings", "resolutions", "proposals"):
        if not isinstance(row[key], list) or len(row[key]) > _MAX_FINDINGS:
            raise ValueError("invalid public list")
        identities: set[str] = set()
        for item in row[key]:
            if key == "proposals":
                _validate_proposal(item, registry)
            else:
                _validate_finding(item, registry, resolution=key == "resolutions")
                if item["id"] in identities:
                    raise ValueError("duplicate public finding identity")
                identities.add(item["id"])
    return copy.deepcopy(row)


def _models(row: JSON) -> JSON:
    result: JSON = {}
    models = row.get("models", [])
    if not isinstance(models, list) or len(models) > _MAX_MODELS:
        raise ValueError("invalid maintenance catalog")
    for model in models:
        if (not isinstance(model, dict) or not _model_identity(model.get("id")) or model["id"] in result):
            raise ValueError("invalid maintenance model identity")
        prices = model.get("pricing", {})
        if not isinstance(prices, dict) or not _fact(prices):
            raise ValueError("invalid maintenance price")
        result[model["id"]] = dict(prices)
    return result


def _deadline(provider: str, prefix: str, expires: Any, now: datetime, subject: str = "provider") -> JSON | None:
    end = timestamp(expires)
    if end is None or end <= now.timestamp():
        return _finding(provider, f"{prefix}_expired" if prefix != "catalog" else "catalog_stale", subject)
    if end - now.timestamp() <= 86400:
        return _finding(provider, f"{prefix}_due", subject)
    return None


def _catalog_summary(row: JSON) -> JSON:
    checked = timestamp(row.get("checked_at"))
    return {"status": _status(row.get("status", "not_checked")), "complete": row.get("complete") is True,
            "checked_at": _stamp(row.get("checked_at")), "last_attempt_at": _stamp(row.get("last_attempt_at")),
            "expires_at": datetime.fromtimestamp(checked + 172800, UTC).isoformat() if checked is not None else None,
            "model_count": len(row.get("models", [])) if isinstance(row.get("models", []), list) else 0}


def _renewed_source(spec: JSON, source: JSON, overlay: JSON, now: datetime) -> JSON:
    """Use renewal dates only when they remain bound to the active reviewed policy."""
    if not evidence_renewal_is_current(source, overlay, policy_digest(spec), now.timestamp()):
        return source
    return overlay


def build_public_report(registry: JSON, catalog: JSON, *, evidence: JSON | None = None,
                        proposals: JSON | None = None, baseline: JSON | None = None,
                        acknowledged: Iterable[str] = (), now: datetime | None = None,
                        source_revision: str | None = None, _private: bool = False) -> tuple[JSON, JSON]:
    """Reconcile complete public facts; returning to the original value is recovery.

    Explicit acknowledgements are trusted caller inputs, never provider responses.
    `_private` is internal: its result must not pass the public export boundary.
    """
    current = _now(now)
    old = ((_validate_private_baseline(baseline) if _private else validate_public_baseline(baseline))
           if baseline is not None else {"providers": {}, "pending_changes": [], "incidents": [], "proposals": []})
    report: JSON = {"schema": 1, "visibility": "public", "checked_at": current.isoformat(), "source_revision": source_revision,
                    "baseline_status": "ok" if baseline is not None else "missing", "providers": {}, "findings": [], "resolutions": [], "proposals": []}
    next_base: JSON = {"schema": 1, "visibility": "public", "checked_at": current.isoformat(), "source_revision": source_revision,
                       "providers": copy.deepcopy(old["providers"]), "pending_changes": [], "incidents": [], "proposals": []}
    pending = {row["id"]: copy.deepcopy(row) for row in old["pending_changes"]}
    incidents: dict[str, JSON] = {}
    fresh_catalogs: set[str] = set()
    fresh_sources: set[tuple[str, str]] = set()
    fresh_limits: set[str] = set()
    retained_proposals = {(row["provider"], row["rule_id"], row["model_id"]): row for row in old["proposals"]}
    acknowledged_set = set(acknowledged)

    def add(finding: JSON) -> None:
        destination = pending if finding["kind"] == "review" else incidents
        previous = destination.get(finding["id"])
        if previous is not None and finding["kind"] == "review":
            if finding["after"] == previous["before"]:
                return  # Resolution must retain the original outstanding fingerprint.
            # Preserve the original pre-change value across subsequent changes.
            finding = _finding(finding["provider"], finding["code"], finding["subject"], before=previous["before"], after=finding["after"], source_url=finding.get("source_url"))
        destination[finding["id"]] = finding

    for pid, spec in registry.items():
        row = catalog.get("providers", {}).get(pid, {})
        accessible = _private or (spec.get("discovery", {}).get("supports_public") is True and row.get("catalog_access") in {None, "public"})
        if not accessible:
            row = {"status": "unsupported"}
        summary = _catalog_summary(row)
        report["providers"][pid] = {"catalog": summary, "sources": [], "coverage": "public" if accessible else "unsupported"}
        valid = accessible and row.get("status") == "ok" and row.get("complete") is True and summary["checked_at"] is not None
        valid = valid and 0 <= current.timestamp() - (timestamp(summary["checked_at"]) or 0) < 172800
        models: JSON = {}
        if valid:
            try:
                models = _models(row)
            except ValueError:
                valid = False
                summary["status"] = "partial"
        if accessible:
            if summary["status"] not in {"ok", "auth_missing", "unsupported", "not_checked"}:
                add(_finding(pid, "catalog_failed"))
            expiry = _deadline(pid, "catalog", summary["expires_at"], current)
            if expiry is not None:
                add(expiry)
        if valid:
            fresh_catalogs.add(pid)
            prior = old["providers"].get(pid)
            if prior is not None:
                before_models = prior["models"]
                for model in sorted(set(before_models) | set(models)):
                    if any(item["provider"] == pid and item["subject"] == model
                           and item["code"] in {"model_added", "model_removed"}
                           for item in pending.values()):
                        continue  # The existing addition/removal owns this lifecycle.
                    if model not in before_models:
                        add(_finding(pid, "model_added", model, before=False, after=True))
                    elif model not in models:
                        add(_finding(pid, "model_removed", model, before=True, after=False))
                    elif before_models[model] != models[model]:
                        add(_finding(pid, "price_changed", model, before=before_models[model], after=models[model]))
            next_base["providers"][pid] = {"checked_at": summary["checked_at"], "models": models}
        for source in spec.get("evidence", []):
            sid = source["id"]
            overlay = (evidence or {}).get("providers", {}).get(pid, {}).get(sid, {})
            state = _status(overlay.get("last_status", overlay.get("status", "not_checked")))
            verified = _renewed_source(spec, source, overlay, current)
            if state == "unchanged" and verified is source:
                state = "not_checked"
            checked = _stamp(verified.get("checked_at"))
            expires = _stamp(verified.get("expires_at"))
            report["providers"][pid]["sources"].append({"id": sid, "status": state, "checked_at": checked, "expires_at": expires,
                "last_attempt_at": _stamp(overlay.get("last_attempt_at", overlay.get("checked_at")))})
            if state == "review_required":
                baseline_hash = source.get("source_hash", {})
                has_baseline = (isinstance(baseline_hash.get("sha256"), str) and _HASH.fullmatch(baseline_hash["sha256"])
                    and baseline_hash.get("algorithm") in {"visible_text_v1", "raw_body_v1", "modelscope_article_v1", "discourse_first_post_v1"})
                add(_finding(pid, "source_changed" if has_baseline else "source_baseline_needed", sid,
                    before=baseline_hash.get("sha256") if has_baseline else None,
                    after=overlay.get("last_observed_sha256", overlay.get("sha256")), source_url=source["url"]))
            elif state in {"check_failed", "error"}:
                add(_finding(pid, "source_check_failed", sid, source_url=source["url"]))
            elif state == "unchanged" and checked and expires and (timestamp(checked) or 0) <= current.timestamp() < (timestamp(expires) or 0):
                fresh_sources.add((pid, sid))
            expiry = _deadline(pid, "source", expires, current, sid)
            if expiry is not None:
                add(expiry)

    for pid, row in (proposals or {}).get("providers", {}).items():
        if pid not in registry:
            continue
        if row.get("status") in {"error", "review_required"}:
            add(_finding(pid, "limit_source_failed" if row["status"] == "error" else "limit_source_review"))
        if row.get("status") == "ok":
            fresh_limits.add(pid)
        for raw in row.get("proposals", []):
            proposal = {key: raw.get(key) for key in ("rule_id", "model_id", "metric", "window_seconds", "old_capacity", "new_capacity")}
            proposal.update(provider=pid, source_url=row.get("source_url"), source_sha256=row.get("source_sha256"))
            try:
                _validate_proposal(proposal, registry)
            except (ValueError, TypeError):
                fresh_limits.discard(pid)
                add(_finding(pid, "limit_source_review"))
                continue
            retained_proposals[(pid, proposal["rule_id"], proposal["model_id"])] = proposal
            if proposal["old_capacity"] != proposal["new_capacity"]:
                add(_finding(pid, "limit_changed", f"{proposal['rule_id']}/{proposal['model_id']}",
                    before=proposal["old_capacity"], after=proposal["new_capacity"], source_url=proposal["source_url"]))

    for identity, finding in list(pending.items()):
        pid, subject, code = finding["provider"], finding["subject"], finding["code"]
        reversed_change = False
        if pid in fresh_catalogs and code in {"model_added", "model_removed", "price_changed"}:
            current_models = next_base["providers"][pid]["models"]
            observed = current_models.get(subject) if code == "price_changed" else subject in current_models
            reversed_change = observed == finding["before"]
        elif code in {"source_changed", "source_baseline_needed"}:
            reversed_change = (pid, subject) in fresh_sources
        elif pid in fresh_limits and code == "limit_source_review":
            reversed_change = True
        elif pid in fresh_limits and code == "limit_changed":
            rule_id, _, model_id = subject.partition("/")
            rule: JSON = next((rule for rule in registry[pid].get("limits", []) if rule["id"] == rule_id), {})
            reviewed = rule.get("model_capacities", {}).get(model_id, rule.get("capacity"))
            latest = next((item for item in (proposals or {}).get("providers", {}).get(pid, {}).get("proposals", [])
                           if item.get("rule_id") == rule_id and item.get("model_id") == model_id), None)
            observed = latest.get("new_capacity") if latest is not None else reviewed
            reversed_change = observed == finding["before"] or (reviewed == finding["after"] and observed == reviewed)
        if reversed_change or finding["fingerprint"] in acknowledged_set:
            report["resolutions"].append(_resolution(finding))
            del pending[identity]
    for finding in old["incidents"]:
        pid, code = finding["provider"], finding["code"]
        recovered = (code.startswith("catalog_") and pid in fresh_catalogs) or (
            code.startswith("source_") and (pid, finding["subject"]) in fresh_sources) or (code == "limit_source_failed" and pid in fresh_limits)
        if finding["id"] not in incidents and recovered:
            report["resolutions"].append(_resolution(finding))
        elif finding["id"] not in incidents:
            incidents[finding["id"]] = finding
    next_base["pending_changes"] = sorted(pending.values(), key=lambda row: row["id"])
    next_base["incidents"] = sorted(incidents.values(), key=lambda row: row["id"])
    report["findings"] = sorted([*pending.values(), *incidents.values()], key=lambda row: row["id"])
    report["proposals"] = [row for row in retained_proposals.values() if any(
        finding["provider"] == row["provider"] and finding["subject"] == f"{row['rule_id']}/{row['model_id']}"
        and finding["code"] == "limit_changed" for finding in pending.values())]
    next_base["proposals"] = report["proposals"]
    if not _private:
        validate_public_report(report)
        validate_public_baseline(next_base)
    else:
        report["visibility"] = next_base["visibility"] = "private"
    return report, next_base


def _account_requirements_met(grant: JSON, account: JSON, credential_ref: str, now: float) -> bool:
    """Mirror admission's account checks without renewing or exposing evidence."""
    if grant.get("requires_account_evidence"):
        tiers = grant.get("required_account_tier")
        tiers = [tiers] if isinstance(tiers, str) else tiers
        if (not fresh(account.get("verified_at"), account.get("expires_at"), now)
                or not isinstance(tiers, list) or not tiers
                or any(not isinstance(tier, str) or not tier for tier in tiers)
                or account.get("tier") not in tiers or account.get("credential_ref") != credential_ref):
            return False
    conditions = grant.get("required_account_conditions", {})
    if not isinstance(conditions, Mapping):
        return False
    if conditions:
        if not fresh(account.get("verified_at"), account.get("expires_at"), now):
            return False
        for key, expected in conditions.items():
            value = account.get(key, account.get("tier") if key == "plan" else None)
            if not (value is expected if isinstance(expected, bool) else value == expected):
                return False
    return True


def _account_attention(pid: str, spec: JSON, grants: list[JSON], account: JSON, catalog: JSON,
                       env: Mapping[str, str], now: datetime) -> JSON | None:
    checked = timestamp(catalog.get("checked_at"))
    complete = (catalog.get("status") == "ok" and catalog.get("complete") is True
                and checked is not None and 0 <= now.timestamp() - checked < 172800)
    if complete:
        # An unrelated grant cannot excuse missing conditions for the actual
        # candidates. A complete empty catalog is different from an unavailable one.
        models = [row for row in catalog.get("models", [])
                  if row.get("id") not in account.get("disabled_models", [])
                  and row.get("id") not in spec.get("blocked_models", [])]
        grants = [grant for grant in grants if any(model_matches_grant(grant, model) for model in models)]
    if not grants:
        return None
    credential_ref = credential_fingerprint(pid, env.get(spec.get("credential_env", "")))
    matching = [grant for grant in grants if _account_requirements_met(grant, account, credential_ref, now.timestamp())]
    if any(not grant.get("requires_account_evidence") and not grant.get("required_account_conditions") for grant in matching):
        return None
    if matching:
        return _deadline(pid, "account", account.get("expires_at"), now)
    expires = timestamp(account.get("expires_at"))
    if expires is not None and expires <= now.timestamp():
        return _finding(pid, "account_expired")
    return _finding(pid, "account_verification")


def _limit_coverage(spec: JSON, catalog: JSON) -> list[JSON]:
    """Describe reviewed capacities, independently of private account overrides.

    A model map is only complete for the models actually assessed. Missing
    catalogs must not turn an empty set into a claim of known allowances.
    """
    try:
        model_ids = set(_models(catalog))
    except ValueError:
        # The report's catalog diagnostics own malformed input. Quota coverage
        # must not hide those diagnostics by raising while rendering them.
        catalog = {}
        model_ids = set()
    models = {model["id"]: model for model in catalog.get("models", [])}
    rows = []
    for rule in spec.get("limits", []):
        applicable = model_ids
        if rule.get("model_ids"):
            applicable = applicable.intersection(rule["model_ids"])
        if rule.get("grant_ids"):
            grants = [grant for grant in spec.get("grants", []) if grant["id"] in rule["grant_ids"]]
            applicable = {model for model in applicable if any(model_matches_grant(grant, models[model]) for grant in grants)}
        unknown = sorted(model for model in applicable if reviewed_limit_capacity(rule, model) is None)
        if applicable:
            status = "partial" if unknown and len(unknown) < len(applicable) else "unknown" if unknown else "known"
        elif model_ids:
            status = "not_applicable"
        elif reviewed_limit_capacity(rule, "") is not None:
            status = "known"
        else:
            status = "unassessed" if rule.get("model_capacities") else "unknown"
        rows.append({"id": rule["id"], "status": status, "unknown_models": unknown})
    return rows


def build_private_report(registry: JSON, catalog: JSON, *, accounts: JSON | None = None,
                         observations: JSON | None = None, conformance: JSON | None = None,
                         policy: JSON | None = None, workflow: JSON | None = None, env: Mapping[str, str] | None = None,
                         now: datetime | None = None, **kwargs: Any) -> JSON:
    """Summarize local evidence without retaining keys, balances or raw errors."""
    current = _now(now)
    report, _ = build_public_report(registry, catalog, now=current, _private=True, **kwargs)
    report["visibility"] = "private"
    source_env = env or {}
    for pid, spec in registry.items():
        provider = report["providers"][pid]
        account = (accounts or {}).get(pid, {})
        observation = (observations or {}).get("providers", {}).get(pid, {})
        status = _status(observation.get("status", "unsupported"))
        coverage = observation.get("coverage", "unsupported")
        coverage = coverage if coverage in {"supported", "unsupported", "requires_admin_credential"} else "unsupported"
        provider["account_observation"] = {"status": status, "coverage": coverage,
            "checked_at": _stamp(observation.get("checked_at")), "expires_at": _stamp(observation.get("expires_at")),
            "last_attempt_at": _stamp(observation.get("last_attempt_at"))}
        provider["account_confirmation"] = {"checked_at": _stamp(account.get("verified_at")), "expires_at": _stamp(account.get("expires_at"))}
        provider["limit_coverage"] = _limit_coverage(spec, catalog.get("providers", {}).get(pid, {}))
        provider["unknown_limits"] = sum(row["status"] in {"unknown", "partial", "unassessed"} for row in provider["limit_coverage"])
        grants = spec.get("grants", [])
        available_grants = [grant for grant in grants if grant.get("status") in {None, "verified", "conditional"}]
        configured = (bool(source_env.get(spec.get("credential_env", ""))) or spec.get("inference_auth") == "none")
        configured = configured and account.get("disabled") is not True and (not grants or bool(available_grants))
        if not configured:
            report["findings"] = [row for row in report["findings"] if row["provider"] != pid]
        if configured:
            finding = _account_attention(pid, spec, available_grants, account,
                                         catalog.get("providers", {}).get(pid, {}), source_env, current)
            if finding is not None:
                report["findings"].append(finding)
        if configured and status in {"auth_failed", "error", "malformed", "rate_limited", "credential_changed"}:
            report["findings"].append(_finding(pid, "credential_changed" if status == "credential_changed" else "account_check_failed"))
        elif configured and coverage == "supported" and status in {"stale", "not_checked"}:
            report["findings"].append(_finding(pid, "account_stale"))
        deadlines = []
        for target, proof in (conformance or {}).get("targets", {}).items():
            if not target.startswith(pid + "/"):
                continue
            features = proof.get("features", {})
            model = target.removeprefix(pid + "/")
            base = spec.get("api_base_url", "")
            if pid == "gemini" and not base.endswith("/openai"):
                base += "/openai"
            adapter = "cloudflare" if pid == "cloudflare" else "openai"
            expected = hashlib.sha256("\0".join((pid, adapter, base, model)).encode()).hexdigest()
            current_models = {row.get("id") for row in catalog.get("providers", {}).get(pid, {}).get("models", [])}
            if (proof.get("fingerprint") != expected or model not in current_models
                    or features.get("tools", {}).get("probe_version") != 2):
                continue
            stamps = [timestamp(features.get(feature, {}).get("verified_at")) for feature in ("chat", "tools", "streaming")]
            if all(features.get(feature, {}).get("status") == "pass" for feature in ("chat", "tools", "streaming")) and all(stamp is not None for stamp in stamps):
                deadlines.append(min(float(stamp) for stamp in stamps if stamp is not None) + 7 * 86400)
        useful_until = max(deadlines) if deadlines else None
        provider["conformance"] = {"useful_targets": len(deadlines), "expires_at": datetime.fromtimestamp(useful_until, UTC).isoformat() if useful_until is not None else None}
        if configured and useful_until is not None:
            finding = _deadline(pid, "conformance", provider["conformance"]["expires_at"], current)
            if finding is not None:
                report["findings"].append(finding)
    policy_row = policy or {}
    report["policy_channel"] = {"status": _status(policy_row.get("last_status", policy_row.get("status", "not_checked"))),
        "checked_at": _stamp(policy_row.get("checked_at")), "last_attempt_at": _stamp(policy_row.get("last_attempt_at")),
        "revision": policy_row.get("revision") if type(policy_row.get("revision")) is int else None}
    if report["policy_channel"]["status"] in {"error", "requires_client_update"}:
        report["findings"].append(_finding("maintenance", "policy_failed"))
    workflow_row = workflow or {}
    workflow_status = workflow_row.get("status", "not_checked")
    if workflow_status not in ("ok", "pending", "not_checked", "unknown", "disabled", "failed", "overdue"):
        workflow_status = "unknown"
    report["public_workflow"] = {"status": workflow_status, "checked_at": _stamp(workflow_row.get("checked_at")),
        "last_attempt_at": _stamp(workflow_row.get("last_attempt_at")), "last_success_at": _stamp(workflow_row.get("last_success_at"))}
    if workflow_status in {"disabled", "failed", "overdue"}:
        report["findings"].append(_finding("maintenance", f"workflow_{workflow_status}"))
    report["findings"].sort(key=lambda row: (row["provider"], row["code"], row["id"]))
    return report


def state_directory(env: Mapping[str, str]) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "freellmpool"


def _read(path: Path) -> JSON:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _write(path: Path, value: Any, *, plain: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(str(value) if plain else json.dumps(value, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def format_report(report: JSON) -> str:
    """One readable next-action view; no JSON or journal inspection required."""
    findings = report.get("findings", [])
    lines = [f"Maintenance: {len(findings)} item(s) need attention."]
    for row in findings:
        lines.extend([f"- {row['provider']}: {row['summary']}", f"  Run: {row['command']}"])
    if not findings:
        lines.append("No actionable maintenance findings. Unsupported account facts remain unknown.")
    workflow = report.get("public_workflow", {})
    if workflow:
        lines.append(f"Public workflow: {workflow['status']}; last observed success: {workflow.get('last_success_at') or 'unknown'}.")
    unknown = sum(row.get("unknown_limits", 0) for row in report.get("providers", {}).values())
    if unknown:
        lines.append(f"Quota rules with unknown nominal capacities: {unknown}; these are not unlimited allowances.")
        for pid, provider in report.get("providers", {}).items():
            gaps = [f"{row['id']} ({row['status']})" for row in provider.get("limit_coverage", [])
                    if row["status"] in {"unknown", "partial", "unassessed"}]
            if gaps:
                lines.append(f"  {pid}: {', '.join(gaps)}")
    missing = [pid for pid, provider in report.get("providers", {}).items() if provider.get("limit_coverage") == []]
    if missing:
        lines.append(f"No reviewed quota rules: {', '.join(missing)}; limits remain unknown.")
    lines.append("Recheck: freellmpool maintenance --refresh")
    return "\n".join(lines)


def _notify(title: str, body: str) -> None:
    command = shutil.which("notify-send")
    if command:
        try:
            subprocess.run([command, "--", title, body], check=False, timeout=5, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            pass


def emit_attention(report: JSON, env: Mapping[str, str], *, notifier: Callable[[str, str], Any] | None = None) -> None:
    """Notify once per active fingerprint; always retain a private readable file."""
    if report.get("visibility") != "private":
        raise ValueError("attention is local and private")
    root = state_directory(env)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "maintenance-attention.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = _read(root / "maintenance-notifications.json")
        seen = set(prior.get("fingerprints", []))
        current = {row["fingerprint"] for row in report.get("findings", [])}
        _write(root / "maintenance-attention.txt", format_report(report), plain=True)
        if current - seen:
            notify = notifier or _notify
            try:
                notify("freellmpool maintenance", f"{len(current)} item(s) need attention. Run: freellmpool maintenance")
            except (OSError, RuntimeError):
                pass  # A persistent human-readable fallback is already present.
        _write(root / "maintenance-notifications.json", {"schema": 1, "fingerprints": sorted(current)})


def _baseline(path: Path, *, private: bool = False) -> JSON | None:
    if not path.exists():
        return None
    value = _read(path)
    if private:
        _validate_private_baseline(value)
    else:
        validate_public_baseline(value)
    return value


def status_report(env: dict[str, str], *, now: datetime | None = None) -> JSON:
    """Read local evidence only; never starts maintenance or creates state files."""
    from .account_observations import load_observations
    from .conformance import ConformanceStore, default_conformance_path
    from .discovery import load_discovery
    from .free_policy import load_accounts
    from .policy_updates import load_policy_status
    from .provider_registry import evidence_path, load_registry
    from .workflow_health import load_workflow_status
    root = state_directory(env)
    invalid = False
    try:
        prior = _baseline(root / "maintenance-baseline.json", private=True)
    except ValueError:
        prior, invalid = None, True
    report = build_private_report(load_registry(env=env, renew_evidence=False), load_discovery(env),
        accounts=load_accounts(env), observations=load_observations(env),
        conformance=ConformanceStore(default_conformance_path(env)).snapshot(),
        evidence=_read(evidence_path(env)), policy=load_policy_status(env), workflow=load_workflow_status(env, now=now),
        proposals=_read(root / "maintenance-proposals.json"), baseline=prior, now=now, env=env)
    if invalid:
        report["baseline_status"] = "invalid"
    return report


def _services(public_only: bool) -> dict[str, Callable[..., JSON]]:
    from .discovery import refresh_catalog, refresh_evidence
    from .limit_sources import collect_proposals
    result: dict[str, Callable[..., JSON]] = {"catalog": refresh_catalog, "evidence": refresh_evidence, "proposals": collect_proposals}
    if not public_only:
        from .account_observations import refresh_accounts
        from .policy_updates import refresh_policy
        from .workflow_health import refresh_workflow
        result.update(accounts=refresh_accounts, policy=refresh_policy, workflow=refresh_workflow)
    return result


def run_maintenance(env: dict[str, str], *, public_only: bool = False, baseline_path: Path | None = None,
                    source_revision: str | None = None, now: datetime | None = None,
                    refreshers: Mapping[str, Callable[..., JSON]] | None = None,
                    notifier: Callable[[str, str], Any] | None = None) -> JSON:
    """Refresh explicit read-only sources and persist one serialized generation.

    Public operation ignores the supplied private environment entirely. Exceptions
    preventing a trustworthy public report propagate to the workflow incident step.
    """
    from .conformance import ConformanceStore, default_conformance_path
    from .free_policy import load_accounts
    from .provider_registry import load_registry
    services = refreshers if refreshers is not None else _services(public_only)
    effective = {} if public_only else dict(env)
    root = baseline_path.parent if baseline_path is not None else state_directory(effective) / ("public-maintenance" if public_only else "")
    baseline_file = baseline_path or root / ("public-baseline.json" if public_only else "maintenance-baseline.json")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(baseline_file) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = _baseline(baseline_file, private=not public_only)
        policy: JSON = {}
        observations: JSON = {}
        workflow: JSON = {}
        if not public_only:
            policy = services["policy"](effective)
        registry = _public_registry() if public_only else load_registry(env=effective, renew_evidence=False)
        catalog = services["catalog"](effective, public_only=public_only,
            **({"path": root / "public-discovery.json"} if public_only else {}))
        evidence = services["evidence"](effective, **({"path": root / "public-evidence.json", "public_only": True} if public_only else {}))
        # Keep original policy fields for renewal digest validation in the report.
        if not public_only:
            registry = load_registry(env=effective, renew_evidence=False)
            observations = services["accounts"](effective)
            if "workflow" in services:
                workflow = services["workflow"](effective)
        proposals = services["proposals"](registry)
        report, next_base = build_public_report(registry, catalog, evidence=evidence, proposals=proposals,
            baseline=prior, now=now, source_revision=source_revision, _private=not public_only)
        if not public_only:
            report = build_private_report(registry, catalog, evidence=evidence, proposals=proposals,
                baseline=prior, accounts=load_accounts(effective), observations=observations,
                conformance=ConformanceStore(default_conformance_path(effective)).snapshot(),
                policy=policy, workflow=workflow, env=effective, now=now, source_revision=source_revision)
            _write(root / "maintenance-proposals.json", proposals)
        _write(baseline_file, next_base)
        _write(root / ("public-report.json" if public_only else "maintenance-report.json"), report)
        if not public_only:
            emit_attention(report, effective, notifier=notifier)
        return report


def select_verification_targets(targets: Iterable[Target], store: ConformanceStore, max_targets: int) -> list[Target]:
    """Keep useful exact-target proof renewable while reserving one scouting slot."""
    from .conformance import rotating_targets, target_fingerprint
    if type(max_targets) is not int or max_targets < 0:
        raise ValueError("invalid verification budget")
    unique = list({target.name: target for target in targets}.values())
    if not max_targets:
        return []
    snapshot = store.snapshot()
    def previously_useful(target: Target) -> bool:
        row = snapshot.get("targets", {}).get(target.name, {})
        features = row.get("features", {})
        return bool(row.get("fingerprint") == target_fingerprint(target.provider, target.model)
            and features.get("tools", {}).get("probe_version") == 2
            and all(features.get(feature, {}).get("status") == "pass" for feature in ("chat", "tools", "streaming")))
    useful = [target for target in unique if previously_useful(target)]
    useful_names = {target.name for target in useful}
    scouts = [target for target in unique if target.name not in useful_names]
    def oldest(target: Target) -> str:
        features = snapshot["targets"][target.name]["features"]
        return min((str(features[feature]["verified_at"]) for feature in ("chat", "tools", "streaming")), default="")
    ordered = sorted(useful, key=lambda target: (oldest(target), target.name))
    retained: list[Target] = []
    providers: set[str] = set()
    for target in ordered:
        if target.provider.id not in providers:
            retained.append(target)
            providers.add(target.provider.id)
    retained.extend(target for target in ordered if target not in retained)
    keep = max_targets - (1 if scouts and max_targets > 1 else 0)
    selected = retained[:keep]
    selected.extend(rotating_targets(scouts, store, max_targets - len(selected)))
    return selected[:max_targets]
