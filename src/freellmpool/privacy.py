"""G16 privacy: data-policy labels, pre-flight redaction, strict routing.

Two independent defenses:

1. Redaction (``redact=True``) scrubs secrets/PII from prompts before any
   provider sees them. Regex-based and best-effort by design — it is
   defense in depth, NOT a leak-proof guarantee.
2. Private routing (``private=True``) admits only ``api-no-train``
   providers and refuses otherwise. This is the guarantee: unknown and
   logging providers are excluded fail-closed.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, cast

POLICIES_PATH = Path(__file__).with_name("data_policies.json")
PROVIDER_REGISTRY_PATH = Path(__file__).with_name("provider_registry.json")

TRAINING_LABELS = ("trains-by-default", "api-no-train", "unknown")
RETENTION_LABELS = ("unknown", "none (local)", "abuse-logging only")


@functools.lru_cache(maxsize=1)
def load_policies() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(POLICIES_PATH.read_text(encoding="utf-8")))


def training_policy(provider_id: str) -> str:
    """Training label for a provider; unknown ids fail closed to ``unknown``."""
    try:
        label = load_policies().get("policies", {}).get(provider_id, {}).get("training")
    except (OSError, ValueError):
        return "unknown"
    return label if label in TRAINING_LABELS else "unknown"


def policy_entry(provider_id: str) -> dict[str, Any] | None:
    try:
        entry = load_policies().get("policies", {}).get(provider_id)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def validate_policies() -> list[str]:
    """Validate the data-policy file; every registry provider must be labeled."""
    errors: list[str] = []
    try:
        document = json.loads(POLICIES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"data_policies.json unreadable: {exc}"]
    if document.get("schema") != 1:
        errors.append("schema must be 1")
    policies = document.get("policies")
    if not isinstance(policies, dict):
        return errors + ["policies must be an object"]
    try:
        registry = json.loads(PROVIDER_REGISTRY_PATH.read_text(encoding="utf-8"))
        registry_ids = {p["id"] for p in registry["providers"]}
    except (OSError, ValueError, KeyError, TypeError):
        registry_ids = set()
    for missing in sorted(registry_ids - set(policies)):
        errors.append(f"{missing}: missing data-policy label")
    for pid, entry in policies.items():
        where = f"policies.{pid}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry must be an object")
            continue
        if entry.get("training") not in TRAINING_LABELS:
            errors.append(f"{where}: training must be one of {list(TRAINING_LABELS)}")
        if entry.get("retention") not in RETENTION_LABELS:
            errors.append(f"{where}: retention must be one of {list(RETENTION_LABELS)}")
        try:
            date.fromisoformat(str(entry.get("as_of")))
        except ValueError:
            errors.append(f"{where}: as_of must be an ISO date")
        source = entry.get("source")
        if source is not None and not (isinstance(source, str) and source.startswith("https://")):
            errors.append(f"{where}: source must be an https URL or null")
    return errors


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if i % 2 == 1:
            value *= 2
            value -= 9 if value > 9 else 0
        total += value
    return total % 10 == 0


def _card_replace(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 16 and _luhn_valid(digits):
        return "[REDACTED_CARD]"
    return match.group(0)


# Order matters: block/assignment shapes first, bare tokens after.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL)),
    ("SECRET", re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|auth[_-]?token|client[_-]?secret|key)\b[\"']?\s*([:=])\s*[\"']?([^\s\"'`;,}&]{8,})")),
    ("BEARER", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")),
    ("API_KEY", re.compile(r"\b(?:sk-or-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|gsk_[A-Za-z0-9]{8,}|nvapi-[A-Za-z0-9_-]{8,}|csk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9]{8,})")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]

_SECRET_VALUE = re.compile(r"[\"']?([^\s\"'`;,}&]{8,})")


def _secret_replace(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]"


def redact_text(text: str) -> tuple[str, list[str]]:
    """Scrub secrets/PII; return (scrubbed, sorted kinds hit). Best-effort, never a guarantee."""
    hits: list[str] = []
    for kind, pattern in _PATTERNS:
        if kind == "SECRET":
            text, count = pattern.subn(_secret_replace, text)
        else:
            text, count = pattern.subn(f"[REDACTED_{kind}]", text)
        if count:
            hits.append(kind)
    card_pattern = re.compile(r"\b(?:\d[ -]?){13,16}\b")
    text, card_count = card_pattern.subn(_card_replace, text)
    if card_count and "[REDACTED_CARD]" in text:
        hits.append("CARD")
    return text, sorted(hits)


def _scrub_value(value: Any, hits: set[str]) -> Any:
    """Recursively scrub every string in a message (content, tool calls, outputs)."""
    if isinstance(value, str):
        new_text, found = redact_text(value)
        hits.update(found)
        return new_text
    if isinstance(value, dict):
        return {key: _scrub_value(item, hits) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, hits) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, hits) for item in value)
    return value


def redact_messages(messages: list[Any]) -> tuple[list[Any], list[str]]:
    """Redact every string in chat messages (content, blocks, tool calls); report kinds hit."""
    hits: set[str] = set()
    scrubbed: list[Any] = [_scrub_value(message, hits) for message in messages]
    return scrubbed, sorted(hits)
