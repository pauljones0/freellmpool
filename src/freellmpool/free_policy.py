"""Free billing eligibility is independent of discovery and quota accounting."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

_ACCOUNT_FIELDS = frozenset({
    "tier", "account_ref", "verified_at", "expires_at", "evidence_source",
    "no_paid_overage", "signup_at", "first_use_at", "lifetime_purchased_credits",
    "notes", "source_url", "limits", "disabled", "disabled_models", "manual_models",
    "plan", "paid_balance_zero", "automatic_usage_billing",
    "credential_ref",
})


def timestamp(value: object) -> float | None:
    """Naive, malformed, and non-finite dates provide no evidence."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def credential_fingerprint(provider_id: str, key: str | None) -> str:
    """Bind entitlement to a credential without retaining a second key copy."""
    return hashlib.sha256((provider_id + "\0" + (key or "keyless")).encode()).hexdigest()


def fresh(checked: object, expires: object, now: float) -> bool:
    start, end = timestamp(checked), timestamp(expires)
    return start is not None and end is not None and start <= now < end


def default_accounts_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("FREELLMPOOL_ACCOUNTS_FILE") or
                Path.home() / ".config/freellmpool/accounts.json").expanduser()


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item) for item in value)


def _valid_account(account: object) -> bool:
    if not isinstance(account, Mapping):
        return False
    for field in ("disabled_models", "manual_models"):
        if field in account and not _string_list(account[field]):
            return False
    if "disabled" in account and not isinstance(account["disabled"], bool):
        return False
    for field in ("tier", "plan", "account_ref", "credential_ref"):
        if field in account and (not isinstance(account[field], str) or not account[field]):
            return False
    for field in ("paid_balance_zero", "automatic_usage_billing", "no_paid_overage"):
        if account.get(field) is not None and not isinstance(account[field], bool):
            return False
    return True


def load_accounts(env: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    path = default_accounts_path(env)
    try:
        if path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text())
        if value.get("schema") != 1 or not isinstance(value.get("providers"), dict):
            return {}
        return {key: ({k: v for k, v in row.items() if k in _ACCOUNT_FIELDS}
                      if _valid_account(row) else {"disabled": True, "notes": "Invalid account metadata; setup review required."})
                for key, row in value["providers"].items()}
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}


@contextmanager
def _account_lock(path: Path) -> Iterator[None]:
    """Serialize wizard/migration writers; reads see only complete generations."""
    locking: ModuleType | None
    try:
        import fcntl as locking
    except ImportError:  # pragma: no cover - Windows single-writer fallback
        locking = None
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as stream:
        if locking is not None:
            locking.flock(stream, locking.LOCK_EX)
        try:
            yield
        finally:
            if locking is not None:
                locking.flock(stream, locking.LOCK_UN)


def save_account(provider_id: str, details: Mapping[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Persist operator/API entitlement evidence, never a provider credential."""
    path = default_accounts_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _account_lock(path):
        providers = load_accounts(env)
        providers[provider_id] = {**providers.get(provider_id, {}),
                                  **{key: value for key, value in details.items() if key in _ACCOUNT_FIELDS}}
        payload = {"schema": 1, "providers": providers}
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".accounts-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    grant: dict[str, Any] | None = None


def _price_values(model: Mapping[str, Any]) -> dict[str, Decimal] | None:
    raw = model.get("pricing")
    if not isinstance(raw, Mapping):
        return None
    values = {}
    for key, value in raw.items():
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not amount.is_finite() or amount < 0:
            return None
        values[key] = amount
    return values


def _matches(selector: object, model: Mapping[str, Any]) -> bool:
    if not isinstance(selector, Mapping):
        return False
    name = model.get("id", "")
    if not isinstance(name, str) or not name:
        return False
    if not _string_list(selector.get("exclude", [])) or not _string_list(selector.get("models", [])):
        return False
    if name in selector.get("exclude", []):
        return False
    kind = selector.get("kind")
    if kind == "all":
        return True
    if kind == "allowlist":
        return name in selector.get("models", [])
    if kind == "free_suffix":
        suffix = selector.get("suffix", ":free")
        return isinstance(suffix, str) and bool(suffix) and (name.endswith(suffix) or name in selector.get("models", []))
    if kind == "zero_price":
        return True  # price evidence is checked independently below
    return False


def _zero_price_reason(grant: Mapping[str, Any], model: Mapping[str, Any]) -> str | None:
    prices = _price_values(model)
    selector = grant.get("model_selector", {})
    if prices == {} and isinstance(selector, Mapping) and selector.get("kind") == "allowlist":
        prices = _price_values({"pricing": grant.get("pricing")})
    if prices is None or any(value != 0 for value in prices.values()):
        return "model has paid or invalid price evidence"
    if not {"input", "output"} <= prices.keys():
        return "zero input/output pricing not verified"
    return None


def model_matches_grant(grant: Mapping[str, Any], model: Mapping[str, Any]) -> bool:
    """Match a reviewed hard-free catalog candidate, never account entitlement.

    Catalogs remain observable when account or source evidence expires. Runtime
    admission separately checks current evidence, credentials and billing terms.
    """
    if (grant.get("status") not in ("verified", "conditional")
            or grant.get("kind") not in ("zero_price", "recurring_quota", "recurring_credit")
            or grant.get("hard_free_boundary") is not True
            or not _matches(grant.get("model_selector"), model)):
        return False
    allowed, modalities = grant.get("allowed_modalities"), model.get("modalities")
    if (not isinstance(allowed, list) or not isinstance(modalities, list)
            or not _string_list(allowed) or not _string_list(modalities)
            or not set(allowed) & set(modalities)):
        return False
    return grant.get("kind") != "zero_price" or _zero_price_reason(grant, model) is None


def admit(
    provider: Mapping[str, Any], model: Mapping[str, Any], account: Mapping[str, Any] | None = None, *,
    modality: str = "chat", now: float | None = None, credential_ref: str | None = None,
) -> Admission:
    """Every route, including explicit pins, needs one current eligible grant."""
    now = time.time() if now is None else now
    account = {} if account is None else account
    if not _valid_account(account):
        return Admission(False, "invalid account metadata; setup review required")
    if account.get("disabled") is True:
        return Admission(False, "provider disabled")
    if model.get("id") in account.get("disabled_models", []):
        return Admission(False, "model disabled by operator")
    if not _string_list(provider.get("blocked_models", [])):
        return Admission(False, "invalid provider exclusions")
    if model.get("id") in provider.get("blocked_models", []):
        return Admission(False, "model requires paid access")
    rows = provider.get("evidence", [])
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) or not isinstance(row.get("id"), str) for row in rows):
        return Admission(False, "invalid provider evidence")
    evidence = {row["id"]: row for row in rows}
    if len(evidence) != len(rows):
        return Admission(False, "duplicate provider evidence identifiers")
    reasons = []
    grants = provider.get("grants", [])
    if not isinstance(grants, list):
        return Admission(False, "invalid provider grants")
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if grant.get("status") not in ("verified", "conditional"):
            reasons.append("grant excluded or unverified")
            continue
        if grant.get("kind") not in ("zero_price", "recurring_quota", "recurring_credit"):
            reasons.append("trial, paid, or unknown access")
            continue
        if not _string_list(grant.get("allowed_modalities", [])) or modality not in grant.get("allowed_modalities", []):
            reasons.append("modality not covered by free grant")
            continue
        references = grant.get("evidence_ids", [])
        if not references or not _string_list(references) or any(
            ref not in evidence or evidence[ref].get("status") not in ("verified", "observed", "official")
            or not fresh(evidence[ref].get("checked_at"), evidence[ref].get("expires_at"), now)
            for ref in references
        ):
            reasons.append("free policy evidence missing or expired")
            continue
        if not _matches(grant.get("model_selector"), model):
            reasons.append("model not covered by free grant")
            continue
        if grant.get("paid_overage_possible", True) and grant.get("hard_free_boundary") is not True:
            reasons.append("provider-enforced no-charge boundary not established")
            continue
        if grant.get("requires_account_evidence"):
            tiers = grant.get("required_account_tier")
            tiers = [tiers] if isinstance(tiers, str) else tiers
            if not fresh(account.get("verified_at"), account.get("expires_at"), now):
                reasons.append("free account tier needs verification")
                continue
            if not tiers or not _string_list(tiers) or account.get("tier") not in tiers:
                reasons.append("account tier not covered by free grant")
                continue
            if credential_ref is not None and account.get("credential_ref") != credential_ref:
                reasons.append("this credential needs its own free account verification")
                continue
        conditions = grant.get("required_account_conditions", {})
        if not isinstance(conditions, Mapping):
            reasons.append("invalid account billing conditions")
            continue
        if conditions:
            valid = fresh(account.get("verified_at"), account.get("expires_at"), now)
            for key, expected in conditions.items():
                value = account.get(key, account.get("tier") if key == "plan" else None)
                valid = valid and (value is expected if isinstance(expected, bool) else value == expected)
            if not valid:
                reasons.append("free account billing conditions need verification")
                continue
        if grant.get("kind") == "zero_price":
            price_reason = _zero_price_reason(grant, model)
            if price_reason is not None:
                reasons.append(price_reason)
                continue
        return Admission(True, "recurring free access verified", grant)
    return Admission(False, "; ".join(dict.fromkeys(reasons)) or "no reviewed free grant")
