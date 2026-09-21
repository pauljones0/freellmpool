"""Reviewed provider policy. Discovery and credentials cannot change these facts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

REGISTRY_PATH = Path(__file__).with_name("provider_registry.json")


def reviewed_limit_capacity(rule: Mapping[str, Any], model_id: str) -> int | float | None:
    """Resolve a validated nominal rule consistently for routing and reporting."""
    capacity = rule.get("model_capacities", {}).get(model_id, rule.get("capacity"))
    if capacity is None:
        capacity = rule.get("maximum_documented")
    return cast(int | float | None, capacity)


def resolve_provider_ids(ids: Iterable[str], registry: Mapping[str, Any],
                         extra: Mapping[str, str] | None = None
                         ) -> tuple[list[str], list[str]]:
    """Split --provider literals into (canonical, unknown).

    Strip + lower match (keys-check parity); empty-after-strip is UNKNOWN in
    every entry (uniform; keys-check's falsy→all rule is not mirrored for
    list forms). Unknowns dedupe on the normalized key echoing the FIRST
    verbatim occurrence; canonicals dedupe order-stable. `extra` maps
    normalized keys to canonical values and is consulted after the registry.
    """
    canonical: list[str] = []
    unknown: list[str] = []
    seen_canonical: set[str] = set()
    seen_unknown: set[str] = set()
    table = {pid.lower(): pid for pid in registry}
    if extra:
        for key, value in extra.items():
            table.setdefault(key, value)
    for literal in ids:
        key = literal.strip().lower()
        hit = table.get(key) if key else None
        if hit is None:
            if key not in seen_unknown:
                seen_unknown.add(key)
                unknown.append(literal)
        elif hit not in seen_canonical:
            seen_canonical.add(hit)
            canonical.append(hit)
    return canonical, unknown


def evidence_path(env: Mapping[str, str]) -> Path:
    """Private renewal state is distinct from model and account discovery."""
    if env.get("FREELLMPOOL_EVIDENCE_FILE"):
        return Path(env["FREELLMPOOL_EVIDENCE_FILE"]).expanduser()
    root = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return root / "freellmpool" / "evidence.json"


def policy_digest(provider: Mapping[str, Any]) -> str:
    """Bind renewals to all reviewed policy fields, not only a source URL."""
    return hashlib.sha256(json.dumps(dict(provider), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_renewal_is_current(
    source: Mapping[str, Any], update: Mapping[str, Any], digest: str, now: float,
) -> bool:
    """Authenticate renewal provenance and age for both admission and reports."""
    baseline = source.get("source_hash", {})
    if (not isinstance(baseline, dict) or update.get("status") != "unchanged"
            or update.get("policy_sha256") != digest
            or baseline.get("algorithm") not in {"visible_text_v1", "raw_body_v1", "modelscope_article_v1", "discourse_first_post_v1"}
            or not isinstance(baseline.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", baseline["sha256"]) is None
            or update.get("hash_algorithm") != baseline.get("algorithm")
            or update.get("sha256") != baseline["sha256"]
            or update.get("url") != source.get("url")):
        return False
    try:
        checked = datetime.fromisoformat(update["checked_at"])
        expires = datetime.fromisoformat(update["expires_at"])
        start, end = checked.timestamp(), expires.timestamp()
        return (checked.tzinfo is not None and expires.tzinfo is not None
                and start <= now < end and 0 < end - start <= 7 * 86400)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False


def _apply_renewals(registry: dict[str, dict[str, Any]], env: Mapping[str, str]) -> None:
    try:
        path = evidence_path(env)
        if path.stat().st_size > 2_000_000:
            return
        document = json.loads(path.read_text())
        if document.get("schema") != 1 or not isinstance(document.get("providers"), dict):
            return
        now = time.time()
        for provider_id, provider in registry.items():
            digest = policy_digest(provider)
            updates = document["providers"].get(provider_id, {})
            if not isinstance(updates, dict):
                continue
            for evidence in provider.get("evidence", []):
                update = updates.get(evidence.get("id"), {})
                if not isinstance(update, dict) or not evidence_renewal_is_current(evidence, update, digest, now):
                    continue
                # Dates are the only mutable fields. Never import policy,
                # quota, model, account or capability fields from this file.
                evidence.update(checked_at=update["checked_at"], expires_at=update["expires_at"])
    except (OSError, ValueError, AttributeError, RecursionError):
        return


def _require_loopback_test_bases(result: dict[str, dict[str, Any]]) -> None:
    """Fail closed unless every test-registry api_base_url is loopback-only."""
    for provider_id, provider in result.items():
        base = provider.get("api_base_url")
        if not base:
            continue
        if not isinstance(base, str):
            raise ValueError(f"test registry provider {provider_id!r} has an invalid api_base_url")
        host = urlsplit(base).hostname or ""
        if host.casefold().rstrip(".") == "localhost":
            continue
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(f"test registry provider {provider_id!r} is not loopback-only")


def load_registry(env: Mapping[str, str] | None = None, *, renew_evidence: bool = True) -> dict[str, dict[str, Any]]:
    """Read reviewed rules; private callers can use a validated policy bundle.

    TEST-ONLY SEAM (G39 hermetic acceptance): when FREELLMPOOL_TEST_REGISTRY_PATH
    is set, the JSON document there replaces the packaged registry (policy
    overlay skipped) after the same schema validation plus a loopback check.
    Never set this in production; no shipped code path sets it.
    """
    test_path = env.get("FREELLMPOOL_TEST_REGISTRY_PATH") if env is not None else None
    if not test_path:
        test_path = os.environ.get("FREELLMPOOL_TEST_REGISTRY_PATH")
    if test_path:
        document = json.loads(Path(test_path).expanduser().read_text(encoding="utf-8"))
    else:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if env is not None:
            from .policy_updates import load_policy_document
            document = load_policy_document(env, document)
    if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("providers"), list):
        raise ValueError("Unsupported provider registry schema")
    result: dict[str, dict[str, Any]] = {}
    tombstones = {entry["id"] for entry in document.get("tombstones", [])}
    for provider in document["providers"]:
        if not isinstance(provider, dict):
            raise ValueError("Invalid provider registry record")
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id or provider_id in result:
            raise ValueError("Invalid or duplicate provider registry ID")
        if provider_id in tombstones:
            raise ValueError("A removed provider cannot re-enter the registry")
        if "inference_auth" in provider and provider["inference_auth"] not in ("none", "bearer"):
            raise ValueError("Unsupported provider inference authentication policy")
        result[provider_id] = provider
    if test_path:
        _require_loopback_test_bases(result)
    if env is not None and renew_evidence:
        _apply_renewals(result, env)
    return result
