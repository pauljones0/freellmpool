"""Bounded private account reads, independent of free-billing attestations.

These adapters report telemetry only. They never renew accounts.json, establish
historical purchases, or convert a balance into requests remaining.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import httpx

from .free_policy import credential_fingerprint
from .http_read import ACCEPT_ENCODING, bounded_response_bytes
from .provider_registry import load_registry

JSON = dict[str, Any]
_MAX_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_TTL = timedelta(days=1)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class _Endpoint:
    url: str
    method: str
    key_env: str
    header: str = "Authorization"


# Independent from provider-configurable inference URLs. No redirects or URL
# overrides can move these credentials to a different destination.
_ENDPOINTS = {
    "openrouter": _Endpoint("https://openrouter.ai/api/v1/key", "GET", "OPENROUTER_API_KEY"),
    "ollama": _Endpoint("https://ollama.com/api/me", "POST", "OLLAMA_API_KEY"),
    "vercel": _Endpoint("https://ai-gateway.vercel.sh/v1/credits", "GET", "AI_GATEWAY_API_KEY"),
    "mistral": _Endpoint("https://api.mistral.ai/v1/admin/rate-limit", "GET", "MISTRAL_ADMIN_API_KEY", "x-api-key"),
}
_OLLAMA_USAGE = _Endpoint("https://ollama.com/api/usage", "GET", "OLLAMA_API_KEY")
_UNSUPPORTED = {
    "llm7": "No reviewed account endpoint; anonymous IP limits use local accounting.",
    "ovh": "No reviewed account endpoint; anonymous IP/model limits use local accounting.",
    "kilo": "No reviewed account allowance endpoint; catalog prices do not establish remaining quota.",
    "opencode": "No reviewed account allowance endpoint; anonymous usage outside this gateway is unknown.",
    "groq": "Remaining RPD/TPM are observed from normal response headers; account metrics API requires Enterprise access.",
    "aion": "No reviewed account quota endpoint; documented capacities and local accounting remain separate.",
    "modelscope": "Remaining request counts are observed from normal response headers; account reset time remains unverified.",
    "nvidia": "No reviewed account quota endpoint; a successful model listing does not reveal account limits.",
    "gemini": "Quota configuration requires Google OAuth, serviceusage.quotas.get and verified project binding; an API key alone is insufficient.",
    "cloudflare": "Restricted billing usage requires verified access, Workers AI neuron mapping and reporting freshness before quota import.",
    "cohere": "No reviewed remaining account-call endpoint; local monthly accounting cannot see external usage.",
    "zhipu": "No reviewed quota endpoint for this inference service; coding-plan limits must not be substituted.",
}
_NOTES = {
    "not_checked": "Account telemetry has not been checked.",
    "ok": "Account telemetry observed; billing attestations and routing eligibility are unchanged.",
    "stale": "Account telemetry expired; run maintenance refresh.",
    "auth_missing": "The account observation credential is not configured.",
    "auth_failed": "Account observation authentication or permission failed; check the configured credential.",
    "rate_limited": "Account observation was rate limited; last-good evidence age is unchanged.",
    "error": "Account observation network or HTTP failure; last-good evidence age is unchanged.",
    "malformed": "Account observation schema was incomplete, conflicting or unsupported; last-good evidence age is unchanged.",
    "credential_changed": "The configured credential changed; previous account telemetry is no longer applicable.",
}
# fact -> (kind, unit, scope, optional window). Values are validated separately.
_SPECS: dict[str, dict[str, tuple[str, str, str, str | None]]] = {
    "openrouter": {
        "key.is_free_tier": ("flag", "boolean", "key", None),
        "key.include_byok_in_limit": ("flag", "boolean", "key", None),
        "key.limit": ("monetary_limit", "USD", "key", None),
        "key.limit_remaining": ("monetary_balance", "USD", "key", None),
        "key.limit_reset": ("reset_schedule", "schedule", "key", None),
        "key.expires_at": ("timestamp", "iso8601", "key", None),
        **{f"key.{prefix}usage{suffix}": ("monetary_usage", "USD", "key", window)
           for prefix in ("", "byok_")
           for suffix, window in (("", None), ("_daily", "day"), ("_weekly", "week"), ("_monthly", "month"))},
    },
    "ollama": {
        "account.plan": ("plan", "plan", "account", None),
        "account.reported_usage.monthly": ("reported_usage", "unknown", "account", "month"),
        "model.request_count.monthly": ("consumed", "requests", "account_model", "month"),
    },
    # The SDK calls these credits but its endpoint schema does not label a
    # currency or distinguish purchased credits from the recurring free grant.
    "vercel": {
        "team.balance": ("monetary_balance", "gateway_credits", "team", None),
        "team.total_used": ("monetary_usage", "gateway_credits", "team", None),
    },
    "mistral": {
        "organization.requests_per_second": ("capacity", "requests", "organization", "second"),
        "model.tokens_per_minute": ("capacity", "total_tokens", "organization_model", "minute"),
        "model.tokens_per_month": ("capacity", "total_tokens", "organization_model", "month"),
    },
}
_PLANS = frozenset({"free", "starter", "unknown"})


def _now() -> datetime:
    return datetime.now(UTC)


def default_observations_path(env: Mapping[str, str]) -> Path:
    if env.get("FREELLMPOOL_OBSERVATIONS_FILE"):
        return Path(env["FREELLMPOOL_OBSERVATIONS_FILE"]).expanduser()
    root = Path(env.get("XDG_STATE_HOME") or Path(env.get("HOME") or Path.home()) / ".local/state")
    return root / "freellmpool" / "account-observations.json"


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False)


def _unique(pairs: list[tuple[str, Any]]) -> JSON:
    result: JSON = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _date(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp lacks timezone")
    return parsed


def _amount(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or len(str(value)) > 64:
        raise ValueError("Invalid monetary value")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (number and abs(number.adjusted()) > 18):
            raise ValueError("Invalid monetary value")
        # Bound exponents even for zero to prevent oversized decimal formatting.
        if abs(int(number.as_tuple().exponent)) > 36:
            raise ValueError("Unsupported precision")
        return format(number, "f")
    except InvalidOperation as error:
        raise ValueError("Invalid monetary value") from error


def _capacity(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 10**15:
        raise ValueError("Invalid capacity")
    return int(value)


def _binding(provider_id: str, env: Mapping[str, str]) -> str | None:
    endpoint = _ENDPOINTS.get(provider_id)
    key = env.get(endpoint.key_env, "") if endpoint else ""
    if not key:
        return None
    if provider_id == "mistral":
        key += "\0" + env.get("MISTRAL_API_KEY", "")
    return credential_fingerprint(provider_id, key)


def _base(provider_id: str) -> JSON:
    supported = provider_id in _ENDPOINTS
    return {
        "status": "not_checked" if supported else "unsupported",
        "coverage": ("requires_admin_credential" if provider_id == "mistral" else
                     "supported" if supported else "unsupported"),
        "checked_at": None, "expires_at": None, "last_attempt_at": None,
        "credential_ref": None, "observations": [],
        "note": _NOTES["not_checked"] if supported else _UNSUPPORTED.get(
            provider_id, "No reviewed account observation adapter is available."),
    }


def _source_url(provider_id: str, fact: str) -> str:
    if provider_id == "ollama" and fact in {"account.reported_usage.monthly", "model.request_count.monthly"}:
        return _OLLAMA_USAGE.url
    return _ENDPOINTS[provider_id].url


def _observation(provider_id: str, fact: str, value: Any, now: datetime,
                 *, model_id: str | None = None) -> JSON:
    kind, unit, scope, window = _SPECS[provider_id][fact]
    result = {"fact": fact, "kind": kind, "value": value, "unit": unit, "scope": scope,
              "source_url": _source_url(provider_id, fact), "checked_at": now.isoformat(),
              "expires_at": (now + _TTL).isoformat()}
    if window is not None:
        result["window"] = window
    if model_id is not None:
        result["model_id"] = model_id
    return result


def _normalize(provider_id: str, body: Any, now: datetime) -> list[JSON]:
    if not isinstance(body, dict):
        raise ValueError("Account document is not an object")
    result = []
    if provider_id == "openrouter":
        data = body.get("data")
        required = {"is_free_tier", "limit", "limit_remaining", "limit_reset", "usage"}
        if not isinstance(data, dict) or not required <= set(data) or type(data.get("is_free_tier")) is not bool:
            raise ValueError("Missing key context")
        for fact, (kind, _, _, _) in _SPECS[provider_id].items():
            key = fact.removeprefix("key.")
            if key not in data:
                continue
            value = data[key]
            if kind.startswith("monetary"):
                value = _amount(value, nullable=key in {"limit", "limit_remaining"})
            elif kind == "flag" and type(value) is not bool:
                raise ValueError("Invalid key flag")
            elif kind == "reset_schedule" and value not in (None, "daily", "weekly", "monthly"):
                raise ValueError("Unknown key budget schedule")
            elif kind == "timestamp" and value is not None:
                value = _date(value).isoformat()
            result.append(_observation(provider_id, fact, value, now))
        values = {row["fact"]: row["value"] for row in result}
        limit, remaining = values.get("key.limit"), values.get("key.limit_remaining")
        if limit is not None and remaining is not None and Decimal(remaining) > Decimal(limit):
            raise ValueError("Conflicting monetary limits")
    elif provider_id == "ollama":
        # The live cloud service emits ID/Plan; the first-party Go client uses
        # id/plan tags and Go's case-insensitive JSON decoding accepts both.
        # Reject simultaneous aliases instead of choosing a possibly stale one.
        if ("id" in body and "ID" in body) or ("plan" in body and "Plan" in body):
            raise ValueError("Ambiguous account field aliases")
        identifier = body.get("id", body.get("ID"))
        if not isinstance(identifier, str) or len(identifier) > 36:
            raise ValueError("Missing account identity")
        UUID(identifier)  # Validate identity evidence, then discard the raw ID.
        plan = body.get("plan", body.get("Plan", "unknown"))
        if not isinstance(plan, str) or len(plan) > 100:
            raise ValueError("Invalid plan")
        normalized = plan.lower() if plan.lower() in _PLANS else "unknown"
        result.append(_observation(provider_id, "account.plan", normalized, now))
    elif provider_id == "vercel":
        for key in ("balance", "total_used"):
            if not isinstance(body.get(key), str):
                raise ValueError("Missing team credit value")
            result.append(_observation(provider_id, f"team.{key}", _amount(body[key]), now))
    elif provider_id == "mistral":
        result.append(_observation(provider_id, "organization.requests_per_second",
                                   _capacity(body.get("requests_per_second")), now))
        models = body.get("tokens_limits_by_model")
        if not isinstance(models, dict) or len(models) > 1000:
            raise ValueError("Unsupported per-model limits schema")
        for model_id, limits in models.items():
            if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id) or not isinstance(limits, dict):
                raise ValueError("Invalid model limit")
            for key in ("tokens_per_minute", "tokens_per_month"):
                result.append(_observation(provider_id, f"model.{key}", _capacity(limits.get(key)),
                                           now, model_id=model_id))
    return result


def _normalize_ollama_usage(body: Any, now: datetime) -> list[JSON]:
    if not isinstance(body, dict) or not isinstance(body.get("limits"), dict):
        raise ValueError("Missing usage limits")
    monthly = body["limits"].get("monthly")
    if not isinstance(monthly, dict) or type(monthly.get("usage")) not in (int, float):
        raise ValueError("Unsupported monthly usage schema")
    models = monthly.get("models")
    if not isinstance(models, list) or len(models) > 1000:
        raise ValueError("Unsupported model usage schema")
    # Preserve the provider's number without inferring a percentage, currency,
    # allowance or reset. The bounded decimal formatter does not assign units.
    result = [_observation("ollama", "account.reported_usage.monthly", _amount(monthly["usage"]), now)]
    seen = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("Invalid model usage")
        name = model.get("name")
        if not isinstance(name, str) or not _MODEL_ID.fullmatch(name) or name in seen:
            raise ValueError("Invalid or duplicate model usage identifier")
        seen.add(name)
        result.append(_observation("ollama", "model.request_count.monthly",
                                   _capacity(model.get("request_count")), now, model_id=name))
    # activity.cost and activity.period are intentionally unrelated to these
    # facts: neither proves a free balance or a quota reset timestamp.
    return result


def _valid_observations(provider_id: str, record: JSON) -> bool:
    rows = record.get("observations")
    if not isinstance(rows, list) or len(rows) > (1002 if provider_id == "ollama" else 2001):
        return False
    seen = set()
    for row in rows:
        required = {"fact", "kind", "value", "unit", "scope", "source_url", "checked_at", "expires_at"}
        if not isinstance(row, dict) or not required <= set(row) or set(row) - {
            "fact", "kind", "value", "unit", "scope", "source_url", "checked_at", "expires_at", "window", "model_id",
        }:
            return False
        fact = row.get("fact")
        if not isinstance(fact, str) or fact not in _SPECS.get(provider_id, {}):
            return False
        model = row.get("model_id")
        if model is not None and (not isinstance(model, str) or not _MODEL_ID.fullmatch(model)):
            return False
        if (fact, model) in seen:
            return False
        seen.add((fact, model))
        kind, unit, scope, window = _SPECS[provider_id][fact]
        if ((row.get("kind"), row.get("unit"), row.get("scope"), row.get("window")) != (kind, unit, scope, window)
                or row.get("source_url") != _source_url(provider_id, fact)
                or row.get("checked_at") != record["checked_at"] or row.get("expires_at") != record["expires_at"]
                or (scope in {"organization_model", "account_model"}) != (model is not None)):
            return False
        value = row.get("value")
        try:
            if kind.startswith("monetary"):
                if _amount(value, nullable=fact in {"key.limit", "key.limit_remaining"}) != value:
                    return False
            elif kind == "reported_usage" and _amount(value) != value:
                return False
            elif kind in {"capacity", "consumed"}:
                _capacity(value)
            elif kind == "flag" and type(value) is not bool:
                return False
            elif kind == "plan" and value not in _PLANS:
                return False
            elif kind == "reset_schedule" and value not in (None, "daily", "weekly", "monthly"):
                return False
            elif kind == "timestamp" and value is not None:
                _date(value)
        except (ValueError, TypeError):
            return False
    # Older identity-only snapshots cannot claim the new paired read succeeded.
    return not (provider_id == "ollama" and rows and not {
        ("account.plan", None), ("account.reported_usage.monthly", None),
    } <= seen)


def _read(env: Mapping[str, str], provider_ids: Sequence[str], now: datetime) -> JSON:
    result: JSON = {"schema": 1, "updated_at": None, "providers": {pid: _base(pid) for pid in provider_ids}}
    path = default_observations_path(env)
    try:
        if path.stat().st_size > _MAX_SNAPSHOT_BYTES:
            return result
        document = json.loads(path.read_text(), object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if (not isinstance(document, dict) or set(document) != {"schema", "updated_at", "providers"}
                or document["schema"] != 1 or not isinstance(document["providers"], dict)):
            return result
        if document["updated_at"] is not None:
            if _date(document["updated_at"]) > now:
                return result
            result["updated_at"] = document["updated_at"]
        for pid in provider_ids:
            row = document["providers"].get(pid)
            base = result["providers"][pid]
            if not isinstance(row, dict) or set(row) != set(base):
                continue
            if (row["coverage"] != base["coverage"] or row["status"] not in {*_NOTES, "unsupported"}
                    or (row["credential_ref"] is not None and
                        (not isinstance(row["credential_ref"], str) or not _HASH.fullmatch(row["credential_ref"])))):
                continue
            if (row["status"] == "unsupported") != (pid not in _ENDPOINTS):
                continue
            if row["last_attempt_at"] is not None and _date(row["last_attempt_at"]) > now:
                continue
            if row["checked_at"] is None:
                if row["expires_at"] is not None or row["observations"] or row["status"] in {"ok", "stale"}:
                    continue
            elif not (row["credential_ref"] is not None and row["observations"] and
                      _date(row["checked_at"]) <= now and timedelta(0) <
                      _date(row["expires_at"]) - _date(row["checked_at"]) <= _TTL):
                continue
            if not _valid_observations(pid, row):
                continue
            # Reconstruct all text from reviewed code; a cache cannot forward
            # private/untrusted notes into the maintenance report.
            clean = {**row, "note": _NOTES.get(row["status"], base["note"])}
            if row["credential_ref"] is not None and row["credential_ref"] != _binding(pid, env):
                clean = {**_base(pid), "status": "credential_changed", "note": _NOTES["credential_changed"]}
            elif row["status"] == "ok" and row["expires_at"] is not None and _date(row["expires_at"]) <= now:
                clean.update(status="stale", note=_NOTES["stale"])
            result["providers"][pid] = clean
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # Corrupt storage never supplies a capacity or a freshness assertion.
        return {"schema": 1, "updated_at": None, "providers": {pid: _base(pid) for pid in provider_ids}}
    return result


def load_observations(env: Mapping[str, str]) -> JSON:
    """Read bounded, credential-bound telemetry without networking or writes."""
    return _read(env, list(load_registry()), _now())


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    locking: ModuleType | None
    try:
        import fcntl as locking
    except ImportError:  # pragma: no cover - Windows single-writer fallback
        locking = None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        if locking is not None:
            locking.flock(stream, locking.LOCK_EX)
        try:
            yield
        finally:
            if locking is not None:
                locking.flock(stream, locking.LOCK_UN)


def _write(path: Path, snapshot: JSON) -> None:
    payload = json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode()) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("Observation snapshot exceeded storage budget")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".observations-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_endpoint(client: httpx.Client, endpoint: _Endpoint, headers: dict[str, str]) -> tuple[str, Any]:
    with client.stream(endpoint.method, endpoint.url, headers={**headers, "Accept-Encoding": ACCEPT_ENCODING}) as response:
        if response.status_code in (401, 403):
            return "auth_failed", None
        if response.status_code == 429:
            return "rate_limited", None
        if response.status_code != 200:
            return "error", None
        data = bounded_response_bytes(response, _MAX_BYTES)
        return "ok", json.loads(data, object_pairs_hook=_unique, parse_constant=_invalid_constant)


def _attempt(provider_id: str, env: Mapping[str, str], now: datetime) -> JSON:
    result = {**_base(provider_id), "last_attempt_at": now.isoformat()}
    endpoint = _ENDPOINTS.get(provider_id)
    if endpoint is None:
        return result
    key = env.get(endpoint.key_env, "")
    result["credential_ref"] = _binding(provider_id, env)
    status = "auth_missing" if not key else "error"
    if key:
        if not key.isascii() or len(key) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            status = "auth_failed"
        else:
            headers = {"Accept": "application/json", endpoint.header:
                       key if endpoint.header == "x-api-key" else f"Bearer {key}"}
            try:
                with _client() as client:
                    status, body = _read_endpoint(client, endpoint, headers)
                    if status == "ok":
                        rows = _normalize(provider_id, body, now)
                        if provider_id == "ollama":
                            status, usage = _read_endpoint(client, _OLLAMA_USAGE, headers)
                            if status == "ok":
                                rows.extend(_normalize_ollama_usage(usage, now))
                        encoded = json.dumps(rows)
                        secrets = [env.get(item.key_env, "") for item in _ENDPOINTS.values()]
                        secrets.append(env.get("MISTRAL_API_KEY", ""))
                        if any(secret and secret in encoded for secret in secrets):
                            raise ValueError("Reflected credential")
                        if status == "ok":
                            result.update(checked_at=now.isoformat(), expires_at=(now + _TTL).isoformat(), observations=rows)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                status = "malformed"
            except httpx.HTTPError:
                status = "error"
    result.update(status=status, note=_NOTES[status])
    return result


def refresh_accounts(env: Mapping[str, str], provider_ids: Sequence[str] | None = None) -> JSON:
    """Persist private reads without modifying entitlement, quota, or registry.

    Supported reads are fixed here, including Ollama's semantically read-only
    POST. A changed credential invalidates prior observations even if its new
    request fails. Failures for the same key preserve last-good evidence age.
    """
    providers = list(load_registry())
    requested = providers if provider_ids is None else list(dict.fromkeys(provider_ids))
    if any(pid not in providers for pid in requested):
        raise ValueError("Provider is not in the reviewed registry")
    path = default_observations_path(env)
    with _lock(path):
        now = _now()
        snapshot = _read(env, providers, now)
        for pid in requested:
            previous = snapshot["providers"][pid]
            captured_env = dict(env)
            record = _attempt(pid, captured_env, now)
            if _binding(pid, captured_env) != _binding(pid, env):
                record = {**_base(pid), "status": "credential_changed", "last_attempt_at": now.isoformat(),
                          "note": _NOTES["credential_changed"]}
            elif (record["status"] != "ok" and record["credential_ref"] is not None and
                  record["credential_ref"] == previous["credential_ref"]):
                for field in ("checked_at", "expires_at", "observations"):
                    record[field] = previous[field]
            snapshot["providers"][pid] = record
        # A key can change after its own request, while another adapter runs.
        # Invalidate both persisted and returned observations before publishing.
        for pid, record in snapshot["providers"].items():
            if record["credential_ref"] is not None and record["credential_ref"] != _binding(pid, env):
                snapshot["providers"][pid] = {
                    **_base(pid), "status": "credential_changed", "last_attempt_at": record["last_attempt_at"],
                    "note": _NOTES["credential_changed"],
                }
        snapshot["updated_at"] = now.isoformat()
        _write(path, snapshot)
        return snapshot


def quota_restrictions(snapshot: Mapping[str, Any], provider_id: str, account: Mapping[str, Any], *,
                       credential_ref: str, now: float | None = None) -> list[JSON]:
    """No current account adapter proves scope AND reset for routing imports.

    In particular Mistral's Admin response cannot bind its organization to an
    inference key. Return no restrictions until a reviewed adapter proves that
    relationship. Existing runtime Groq/ModelScope header accounting is separate.
    """
    return []
