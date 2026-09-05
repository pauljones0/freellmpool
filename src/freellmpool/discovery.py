"""Bounded GET-only discovery of reviewed free catalog candidates.

Snapshots contain normalized model facts and provenance, never credentials or
provider response bodies. Failed or partial refreshes cannot renew last-good age.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .free_policy import model_matches_grant, timestamp
from .provider_registry import evidence_path, load_registry, policy_digest

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_ACCOUNT_ID = re.compile(r"[a-fA-F0-9]{32}\Z")
_PRICE_ALIASES = {"prompt": "input", "completion": "output",
                  "input_cache_read": "input_cache_reads", "input_cache_write": "input_cache_writes"}
_PRICE_KEYS = {"input", "output", "request", "image", "audio", "video", "cached_input",
               "input_cache_reads", "input_cache_writes", "internal_reasoning", "web_search"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_discovery_path(env: dict[str, str]) -> Path:
    """Honor the explicit discovery path, then standard XDG state location."""
    if env.get("FREELLMPOOL_DISCOVERY_FILE"):
        return Path(env["FREELLMPOOL_DISCOVERY_FILE"]).expanduser()
    state = Path(env.get("XDG_STATE_HOME") or Path(env.get("HOME") or Path.home()) / ".local/state")
    return state / "freellmpool" / "discovery.json"


def _empty_snapshot() -> dict[str, Any]:
    return {"schema": 1, "generation": "empty", "updated_at": None, "providers": {}}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Ambiguous repeated JSON fields cannot replace prices or identities."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _valid_cached_model(row: Any) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        return False
    if "modalities" in row and (not isinstance(row["modalities"], list)
            or any(not isinstance(value, str) or not value for value in row["modalities"])):
        return False
    for field in ("context", "max_output"):
        value = row.get(field)
        if value is not None and (type(value) is not int or value <= 0):
            return False
    if any(field in row and not isinstance(row[field], dict) for field in ("metadata", "pricing")):
        return False
    return True


def _load(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(result, dict) or result.get("schema") != 1
                or not isinstance(result.get("providers"), dict)):
            return _empty_snapshot()
        if any(not isinstance(v, dict) or not isinstance(v.get("models", []), list)
               or any(not _valid_cached_model(row) for row in v.get("models", []))
               or len({row["id"] for row in v.get("models", [])}) != len(v.get("models", []))
               for v in result["providers"].values()):
            return _empty_snapshot()
        return result
    except (OSError, ValueError, TypeError):
        return _empty_snapshot()


def load_discovery(env: dict[str, str]) -> dict[str, Any]:
    return _free_snapshot(_load(default_discovery_path(env)), load_registry(env))


def free_catalog_models(provider: dict[str, Any], models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project listing facts through reviewed free rules without account claims."""
    grants, blocked = provider.get("grants", []), provider.get("blocked_models", [])
    if (not isinstance(grants, list) or not isinstance(blocked, list)
            or any(not isinstance(value, str) for value in blocked)):
        return []
    policy_candidates = {row["id"]: row for row in _reviewed_policy_candidates(provider)}

    def current_policy_candidate(model: dict[str, Any]) -> bool:
        metadata = model.get("metadata", {})
        if not isinstance(metadata, dict) or metadata.get("listing_source") != "reviewed_policy":
            return True
        current = policy_candidates.get(model.get("id"))
        return (current is not None and model.get("pricing") == {}
                and metadata.get("grant_id") == current["metadata"]["grant_id"])

    return [model for model in models if model.get("id") not in blocked
            and current_policy_candidate(model)
            and any(isinstance(grant, dict) and model_matches_grant(grant, model) for grant in grants)]


def _policy_sources(provider: dict[str, Any], grant: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Preserve reviewed source dates; a listing never renews price evidence."""
    rows, refs = provider.get("evidence"), grant.get("evidence_ids")
    if (not isinstance(rows, list) or not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows)):
        return None
    evidence = {row["id"]: row for row in rows}
    if len(evidence) != len(rows):
        return None
    sources = []
    for ref in refs:
        row = evidence.get(ref, {})
        url = row.get("url")
        start, end = timestamp(row.get("checked_at")), timestamp(row.get("expires_at"))
        if (row.get("status") not in ("verified", "official", "observed")
                or not isinstance(url, str) or not _same_origin(url, url)
                or start is None or end is None or end <= start):
            return None
        # Expired evidence remains observable, but admit() independently rejects it.
        sources.append({key: row[key] for key in ("id", "url", "checked_at", "expires_at")})
    return sources


def _reviewed_policy_candidates(provider: dict[str, Any]) -> list[dict[str, Any]]:
    """Exact documented chat candidates for explicitly configured listing omissions.

    Empty model pricing deliberately binds admission to the current allowlist
    grant's fixed prices instead of copying zeros into an independently aged cache.
    These rows do not establish availability, account access, or conformance.
    """
    spec, grants = provider.get("discovery"), provider.get("grants")
    configured = spec.get("supplement_from_reviewed_grants") if isinstance(spec, dict) else None
    if (not isinstance(configured, list) or not configured or not isinstance(grants, list)
            or any(not isinstance(ref, str) or not ref for ref in configured)
            or len(set(configured)) != len(configured)):
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for grant in grants:
        if (not isinstance(grant, dict) or grant.get("id") not in configured
                or grant.get("status") != "verified" or grant.get("kind") != "zero_price"
                or grant.get("paid_overage_possible") is not False):
            continue
        selector = grant.get("model_selector")
        names = selector.get("models") if isinstance(selector, dict) and selector.get("kind") == "allowlist" else None
        if (not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not name or any(ord(c) < 32 for c in name) for name in names)):
            continue
        sources = _policy_sources(provider, grant)
        if sources is None:
            continue
        for name in names:
            model: dict[str, Any] = {"id": name, "context": None, "modalities": ["chat"],
                "pricing": {}, "is_free": None, "supports_tools": None, "stream": None,
                "upstream_provider": None, "metadata": {"listing_source": "reviewed_policy",
                    "unlisted": True, "availability": "unverified", "grant_id": grant["id"],
                    "policy_evidence": copy.deepcopy(sources), "modalities_inferred": True}}
            if model_matches_grant(grant, model):
                candidates.setdefault(name, model)
    return list(candidates.values())


def _free_snapshot(snapshot: dict[str, Any], registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {**snapshot, "providers": {provider_id: {**row,
        "models": free_catalog_models(registry[provider_id], row.get("models", []))}
        for provider_id, row in snapshot["providers"].items() if provider_id in registry}}


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20, connect=10), follow_redirects=False)


def _decimal(value: Any, divisor: int = 1) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value)) / divisor
        if not number.is_finite() or number < 0:
            return None
        return format(number, "f")
    except (InvalidOperation, ValueError):
        return None


def _prices(raw: Any, *, per_million: bool = False) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    prices = {}
    for key, value in raw.items():
        key = _PRICE_ALIASES.get(key, key)
        if key in prices:
            raise ValueError("Duplicate advertised price dimension")
        if key in {"unit", "currency"}:
            continue  # metadata, not a price dimension
        if key == "discount" and _decimal(value) == "0":
            continue  # Kilo explicitly reports no discount, not an added fee.
        if key in {"input_tiers", "output_tiers", "input_cache_read_tiers", "input_cache_write_tiers"}:
            maximum = _tier_price(value)
            prices[key] = maximum if maximum is not None else "-1"
            continue
        if key in _PRICE_KEYS:
            # OpenRouter/Kilo use -1 for dynamic router pricing. Preserve the
            # unknown marker so the strict price gate rejects it, while the
            # rest of the complete catalog can still be refreshed.
            if key in {"input", "output"} and str(value) == "-1":
                prices[key] = "-1"
                continue
            token_price = key in {"input", "output", "cached_input", "input_cache_reads",
                                  "input_cache_writes", "internal_reasoning"}
            amount = _decimal(value, 1000000 if per_million and token_price else 1)
            if amount is None:
                raise ValueError("Malformed advertised price")
            prices[key] = amount
        else:
            # New fees and unknown dimensions cannot quietly disappear from
            # zero-price admission. A reviewed parser update must explain them.
            prices["unrecognized_price"] = "-1"
    return prices


def _tier_price(value: Any) -> str | None:
    """An all-zero tier schedule is free only when it covers every length."""
    if not isinstance(value, list) or not value:
        return None
    next_minimum: int | None = 0
    maximum = Decimal(0)
    for tier in value:
        if not isinstance(tier, dict) or set(tier) - {"min", "max", "cost"}:
            return None
        low, high = tier.get("min"), tier.get("max")
        if (not isinstance(low, int) or isinstance(low, bool) or low != next_minimum
                or (high is not None and (not isinstance(high, int) or isinstance(high, bool) or high <= low))):
            return None
        cost = _decimal(tier.get("cost"))
        if cost is None:
            return None
        maximum = max(maximum, Decimal(cost))
        next_minimum = high
    return format(maximum, "f") if next_minimum is None else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _modalities(provider_id: str, row: dict[str, Any]) -> tuple[list[str], bool]:
    if provider_id == "gemini":
        methods = row.get("supportedGenerationMethods", [])
        result = []
        if "generateContent" in methods:
            result.append("chat")
        if "embedContent" in methods or "batchEmbedContents" in methods:
            result.append("embedding")
        return result, False
    if provider_id == "cohere":
        endpoints = row.get("endpoints", [])
        return [kind for name, kind in (("chat", "chat"), ("embed", "embedding"),
                                        ("rerank", "rerank")) if name in endpoints], False
    task = row.get("task", {})
    task_name = task.get("name", "") if isinstance(task, dict) else str(task)
    kind = str(row.get("model_type") or row.get("type") or task_name).lower()
    explicit = {"chat": "chat", "language": "chat", "text generation": "chat",
                "text-generation": "chat", "embedding": "embedding", "embeddings": "embedding",
                "text embeddings": "embedding", "automatic speech recognition": "transcription",
                "speech-recognition": "transcription", "transcription": "transcription",
                "text-to-image": "image", "image": "image", "text-to-speech": "speech",
                "reranker": "rerank", "rerank": "rerank", "video": "video"}
    if kind in explicit:
        return [explicit[kind]], False
    modalities = row.get("architecture", row.get("modalities", {}))
    if isinstance(modalities, dict):
        output = modalities.get("output_modalities", modalities.get("output", []))
        if output:
            mapped = {"text": "chat", "image": "image", "audio": "speech", "video": "video"}
            return list(dict.fromkeys(mapped[o] for o in output if o in mapped)), False
    # Generic /models frequently omits task information. This is a candidate
    # hint only; it never substitutes for protocol/capability conformance.
    name = str(row.get("id") or row.get("name") or "").lower()
    if any(word in name for word in ("embed", "bge-", "e5-")):
        return ["embedding"], True
    if "rerank" in name:
        return ["rerank"], True
    if "whisper" in name:
        return ["transcription"], True
    if any(word in name for word in ("stable-diffusion", "flux-", "flux.", "dall-e")):
        return ["image"], True
    if provider_id == "cloudflare" and task_name:
        return [], False
    return ["chat"], True


def _raw_rows(provider_id: str, body: dict[str, Any]) -> list[Any]:
    key = "models" if provider_id in {"gemini", "cohere", "ollama", "aion"} else "data"
    if provider_id == "cloudflare":
        if body.get("success") is False:
            raise ValueError("Catalog API reported failure")
        key = "result" if "result" in body else "data"
    rows = body.get(key)
    if not isinstance(rows, list):
        raise ValueError("Catalog response has no model array")
    return rows


def normalize_models(provider_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize listing facts, preserving unknown prices and upstream identity.

    Pricing is USD/token (or USD/request/image for non-token fields). A price
    advertised for paid usage does not cancel a separate anonymous/free grant.
    """
    models = []
    for raw in _raw_rows(provider_id, body):
        if not isinstance(raw, dict):
            raise ValueError("Malformed model record")
        row = dict(raw)
        name_key = "name" if provider_id in {"gemini", "cohere", "ollama", "cloudflare"} else "id"
        model_id = row.get(name_key) or row.get("id")
        if not isinstance(model_id, str) or not model_id or any(ord(c) < 32 for c in model_id):
            raise ValueError("Malformed model ID")
        if provider_id == "gemini":
            model_id = model_id.removeprefix("models/")
        context = row.get("context_length") or row.get("context_window") or row.get("inputTokenLimit") or row.get("max_model_len") or row.get("max_context_length")
        if isinstance(context, dict):
            context = context.get("tokens")
        modalities, inferred = _modalities(provider_id, row)
        params = row.get("supported_parameters", [])
        capabilities: dict[str, Any] = {}
        if isinstance(row.get("capabilities"), dict):
            capabilities = row["capabilities"]
        tools = row.get("supports_tools", row.get("tools_calling", capabilities.get("function_calling", capabilities.get("tools"))))
        if tools is None and isinstance(params, list) and params:
            tools = "tools" in params
        price_raw = row.get("pricing", {})
        per_million = provider_id == "llm7" and isinstance(price_raw, dict) and price_raw.get("unit") == "1M tokens"
        prices = _prices(price_raw, per_million=per_million)
        metadata = {key: row[key] for key in ("tier", "usage_based_only", "model_type", "schema_endpoints", "is_deprecated", "supportedGenerationMethods", "endpoints") if key in row}
        metadata["modalities_inferred"] = inferred
        metadata["pricing_unknown"] = any(Decimal(price) < 0 for price in prices.values())
        model: dict[str, Any] = {"id": model_id, "context": _positive_int(context),
            "modalities": modalities, "pricing": prices, "is_free": None,
            "supports_tools": tools if isinstance(tools, bool) else None,
            "stream": row.get("stream") if isinstance(row.get("stream"), bool) else None,
            "upstream_provider": None, "metadata": metadata}
        if "input" in prices and "output" in prices:
            model["is_free"] = all(Decimal(p) == 0 for p in prices.values())
        models.append(model)
    return models


def _url_with(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({key: str(value) for key, value in params.items()})
    return urlunsplit(parts._replace(query=urlencode(query)))


def _same_origin(url: str, origin: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(origin)
        return (a.scheme == b.scheme == "https" and a.hostname == b.hostname
                and a.port == b.port and not a.username and not a.password
                and not a.fragment and not any(ord(c) < 32 for c in url))
    except ValueError:
        return False


def _next_url(provider: dict[str, Any], url: str, body: dict[str, Any], count: int) -> str | None:
    pagination = provider["discovery"]["pagination"]
    if pagination == "gemini" and body.get("nextPageToken"):
        return _url_with(url, pageToken=body["nextPageToken"])
    if pagination == "cohere" and body.get("next_page_token"):
        return _url_with(url, page_token=body["next_page_token"])
    links = body.get("links")
    if isinstance(links, dict) and links.get("next"):
        next_link = links["next"]
        if not isinstance(next_link, str):
            raise ValueError("Malformed next-page link")
        return urljoin(url, next_link)
    if pagination == "cloudflare":
        info = body.get("result_info") or {}
        if not isinstance(info, dict):
            raise ValueError("Malformed pagination metadata")
        params = dict(parse_qsl(urlsplit(url).query))
        page = int(params.get("page", 1))
        size = int(params.get("per_page", 100))
        total_pages = info.get("total_pages")
        total_count = info.get("total_count")
        more = (page < total_pages if isinstance(total_pages, int)
                else page * size < total_count if isinstance(total_count, int)
                else count >= size)
        if more:
            return _url_with(url, page=page + 1)
    return None


def _attempt(provider: dict[str, Any], env: dict[str, str], *, public_only: bool = False) -> dict[str, Any]:
    now = _now()
    result: dict[str, Any] = {"status": "error", "last_attempt_at": now,
        "checked_at": None, "complete": False, "models": [], "note": "Catalog check failed."}
    spec = provider["discovery"]
    url = spec.get("url")
    if not url:
        return {**result, "status": "unsupported", "note": "No supported listing endpoint; no key judgment made."}
    key_name = provider.get("credential_env")
    key = env.get(key_name, "") if key_name else ""
    public = spec["supports_public"]
    if (public_only and not public) or (not key and spec["auth"] != "none" and not public):
        return {**result, "status": "auth_missing", "note": "A private listing credential is required."}
    if "{account_id}" in url:
        account_id = env.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not _ACCOUNT_ID.fullmatch(account_id):
            return {**result, "status": "auth_missing", "note": "A valid Cloudflare account ID is required."}
        url = url.replace("{account_id}", account_id)
    if not _same_origin(url, url):
        return {**result, "status": "unsupported", "note": "Unsupported catalog URL."}
    headers = {"Accept": "application/json"}
    # Public listing checks deliberately omit credentials. Inference entitlement
    # and actual key validity remain separate even when an API ignores bad keys.
    authenticated = bool(key and not public_only and not public and spec["auth"] != "none")
    if authenticated:
        if spec["auth"] == "x-goog-api-key":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    url = _url_with(url, **spec.get("params", {}))
    origin = url
    seen: set[str] = set()
    collected: dict[str, dict[str, Any]] = {}
    total_raw = 0
    try:
        with _client() as client:
            for _ in range(min(int(spec.get("max_pages", 100)), 100)):
                if url in seen or not _same_origin(url, origin):
                    raise ValueError("Unsafe or repeated pagination URL")
                seen.add(url)
                response = client.get(url, headers=headers)
                if response.status_code in {401, 403}:
                    return {**result, "status": "auth_failed", "note": f"HTTP {response.status_code}: authentication, permissions or account verification failed; listing did not establish entitlement."}
                if response.status_code == 429:
                    return {**result, "status": "rate_limited", "note": "Listing rate limited; prior evidence age is unchanged."}
                if 300 <= response.status_code < 400:
                    raise ValueError("Catalog redirects are not followed")
                response.raise_for_status()
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("Catalog response is too large")
                body = response.json(object_pairs_hook=_unique_object)
                if not isinstance(body, dict):
                    raise ValueError("Malformed catalog document")
                raw_rows = _raw_rows(provider["id"], body)
                total_raw += len(raw_rows)
                models = normalize_models(provider["id"], body)
                for model in models:
                    if model["id"] in collected:
                        raise ValueError("Duplicate model records cannot establish a complete inventory")
                    collected[model["id"]] = model
                next_url = _next_url(provider, url, body, len(raw_rows))
                if next_url is None:
                    expected = body.get("total_count")
                    if isinstance(expected, int) and total_raw < expected:
                        raise ValueError("Incomplete catalog count")
                    if not collected:
                        raise ValueError("Empty catalog cannot replace evidence")
                    encoded = json.dumps(list(collected.values()))
                    if key and key in encoded:
                        raise ValueError("Provider reflected a credential")
                    for candidate in _reviewed_policy_candidates(provider):
                        # Even a paid/unsupported listing row takes precedence over
                        # reviewed policy. Only wholly omitted identities are added.
                        collected.setdefault(candidate["id"], candidate)
                    free_models = free_catalog_models(provider, list(collected.values()))
                    note = ("Listing checked for reviewed free candidates; account free eligibility and capabilities remain unverified."
                            if authenticated else "Public listing checked for reviewed free candidates; API key validity, account free eligibility and capabilities remain unverified.")
                    if any(model["metadata"].get("unlisted") is True for model in free_models):
                        note += " Some candidates are unlisted and come from reviewed policy; their availability remains unverified."
                    return {**result, "status": "ok", "checked_at": now, "complete": True,
                            "models": free_models, "source_url": spec["url"],
                            "catalog_access": "authenticated" if authenticated else "public",
                            "catalog_ttl_seconds": spec.get("catalog_ttl_seconds", 86400),
                            "note": note}
                url = next_url
            raise ValueError("Catalog exceeded pagination budget")
    except (ValueError, TypeError, KeyError, OverflowError):
        return {**result, "status": "partial", "note": "Incomplete, empty or malformed catalog; last-good evidence preserved."}
    except httpx.HTTPStatusError as error:
        return {**result, "status": "error", "note": f"Catalog HTTP {error.response.status_code}; last-good evidence preserved."}
    except httpx.HTTPError:
        return {**result, "status": "error", "note": "Catalog network failure; last-good evidence preserved."}


def check_provider(provider_id: str, env: dict[str, str]) -> dict[str, Any]:
    """GET-only wizard check. Does not write snapshots or authorize inference."""
    provider = load_registry(env).get(provider_id)
    if provider is None:
        return {"status": "unsupported", "complete": False, "model_count": 0,
                "checked_at": None, "last_attempt_at": _now(), "note": "Provider is not in the reviewed registry."}
    result = _attempt(provider, env)
    return {key: value for key, value in result.items() if key != "models"} | {"model_count": len(result["models"])}


def _atomic_write(path: Path, result: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh_catalog(env: dict[str, str], provider_ids: list[str] | None = None,
                    public_only: bool = False, path: Path | str | None = None) -> dict[str, Any]:
    """Refresh requested catalogs and atomically reconcile against last-good data.

    A lock serializes writers (including separate scheduled/CLI processes). The
    snapshot contains a complete last-good generation, never a half-written page.
    """
    default_path = default_discovery_path(env)
    if public_only:
        default_path = default_path.with_name("public-discovery.json")
    destination = Path(path) if path is not None else default_path
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = load_registry() if public_only else load_registry(env)
    requested = list(dict.fromkeys(provider_ids if provider_ids is not None else registry))
    if any(provider_id not in registry for provider_id in requested):
        raise ValueError("Requested provider is absent or removed from the reviewed registry")
    lock_path = destination.with_suffix(destination.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snapshot = _free_snapshot(_load(destination), registry)
        if public_only:
            # Public artifacts may be built in a directory that previously held
            # private state. Never carry authenticated or unclassified rows into
            # a credentialless result, including after an auth_missing outcome.
            snapshot = {**_empty_snapshot(), "providers": {
                key: value for key, value in snapshot["providers"].items()
                if key in registry and registry[key]["discovery"]["supports_public"]
                and value.get("catalog_access") == "public"}}
        # Removed providers cannot survive in an old snapshot.
        snapshot["providers"] = {key: value for key, value in snapshot["providers"].items() if key in registry}
        for provider_id in requested:
            attempted = _attempt(registry[provider_id], env, public_only=public_only)
            previous = snapshot["providers"].get(provider_id)
            if attempted["status"] != "ok" and previous:
                preserved = dict(previous)
                preserved.update({key: attempted[key] for key in ("status", "last_attempt_at", "note")})
                snapshot["providers"][provider_id] = preserved
            else:
                snapshot["providers"][provider_id] = attempted
        snapshot.update(schema=1, generation=uuid.uuid4().hex, updated_at=_now())
        _atomic_write(destination, snapshot)
        return snapshot


class _SourceText(HTMLParser):
    """Ignore executable/style payloads and retain all published page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


class _DiscoursePost(_SourceText):
    """The first published post is policy; likes and related topics are not."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.complete = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and (self.depth or (not self.complete and values.get("itemprop") == "text"
                                           and "post" in (values.get("class") or "").split())):
            self.depth += 1
        if self.depth:
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            super().handle_endtag(tag)
            if tag == "div":
                self.depth -= 1
                self.complete = self.depth == 0

    def handle_data(self, data: str) -> None:
        if self.depth:
            super().handle_data(data)


def source_digest(content: bytes, content_type: str, algorithm: str = "visible_text_v1") -> str:
    """Stable, versioned hash: all document text, with whitespace normalized.

    JavaScript-only pages with no substantive text must not receive a reviewed
    baseline. A changed visible price, limit, or restriction changes this hash.
    """
    decoded = content.decode("utf-8", errors="strict")
    if algorithm == "raw_body_v1":
        return hashlib.sha256(content).hexdigest()
    if algorithm == "discourse_first_post_v1":
        post = _DiscoursePost()
        post.feed(decoded)
        text = " ".join(" ".join(post.parts).split())
        if not post.complete or not text:
            raise ValueError("Missing or incomplete policy post")
        return hashlib.sha256(text.encode()).hexdigest()
    if algorithm == "modelscope_article_v1":
        marker = re.search(r"window\.__detail_data__\s*=\s*", decoded)
        if marker is None:
            raise ValueError("Missing embedded article")
        serialized, _ = json.JSONDecoder().raw_decode(decoded[marker.end():])
        document = json.loads(serialized)
        articles = document.get("Articles")
        if not isinstance(articles, list) or len(articles) != 1 or not isinstance(articles[0], dict):
            raise ValueError("Ambiguous embedded article")
        article = articles[0]
        if not isinstance(article.get("Content"), str) or not article["Content"]:
            raise ValueError("Empty embedded article")
        # All article content and publication status matter; viewer counts,
        # likes and avatars do not change the documented allowance.
        fields = {key: article.get(key) for key in ("Title", "TitleEn", "Content", "ContentEn", "Status")}
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    if algorithm != "visible_text_v1":
        raise ValueError("Unknown source digest algorithm")
    if "html" in content_type.lower():
        parser = _SourceText()
        parser.feed(decoded)
        decoded = " ".join(parser.parts)
    normalized = " ".join(decoded.split())
    if not normalized:
        raise ValueError("Empty policy source")
    return hashlib.sha256(normalized.encode()).hexdigest()


def check_public_sources(provider_ids: list[str] | None = None, *,
                         registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Hash reviewed public source pages without updating any policy evidence.

    This produces a review artifact, never executable allowances. No account
    credentials, authorization headers or environment configuration are read.
    """
    registry = load_registry() if registry is None else registry
    requested = provider_ids if provider_ids is not None else list(registry)
    urls: dict[str, list[str]] = {}
    for provider_id in requested:
        if provider_id not in registry:
            raise ValueError("Unknown provider")
        for evidence in registry[provider_id]["evidence"]:
            urls.setdefault(evidence["url"], []).append(provider_id)
    records = []
    with _client() as client:
        for url, providers in urls.items():
            record: dict[str, Any] = {"url": url, "providers": sorted(set(providers)),
                                      "checked_at": _now(), "status": "error", "sha256": None}
            try:
                if not _same_origin(url, url):
                    raise ValueError("Unsupported evidence URL")
                response = client.get(url, headers={"Accept": "text/html, application/json"}, timeout=8)
                record["http_status"] = response.status_code
                if response.status_code == 200 and len(response.content) <= _MAX_RESPONSE_BYTES:
                    record["status"] = "ok"
                    record["sha256"] = source_digest(response.content, response.headers.get("content-type", ""))
                    record["raw_sha256"] = hashlib.sha256(response.content).hexdigest()
                    record["hash_algorithm"] = "visible_text_v1"
                    if urlsplit(url).hostname in {"modelscope.ai", "www.modelscope.ai"} and "/learn/" in url:
                        record["article_sha256"] = source_digest(response.content, "text/html", "modelscope_article_v1")
                    if url == "https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317":
                        record["post_sha256"] = source_digest(response.content, "text/html", "discourse_first_post_v1")
            except (httpx.HTTPError, ValueError):
                pass
            records.append(record)
    return {"schema": 1, "checked_at": _now(), "sources": records,
            "note": "A source change requires human review; this check never renews policy, pricing or account evidence."}


def refresh_evidence(env: dict[str, str], provider_ids: list[str] | None = None,
                     *, path: Path | None = None, public_only: bool = False) -> dict[str, Any]:
    """Renew only content-identical, reviewed policy for at most seven days.

    This separate public GET operation does not touch account entitlement,
    pricing/model snapshots, capabilities, or packaged grant/limit values.
    A new or changed source requires review and a new packaged baseline.
    """
    registry = load_registry() if public_only else load_registry(env, renew_evidence=False)
    checked = check_public_sources(provider_ids, registry=registry)
    sources = {row["url"]: row for row in checked["sources"]}
    providers = {}
    for provider_id in provider_ids if provider_ids is not None else registry:
        provider = registry[provider_id]
        rows = {}
        for evidence in provider.get("evidence", []):
            baseline = evidence.get("source_hash", {})
            source = sources[evidence["url"]]
            algorithm = baseline.get("algorithm")
            field = {"raw_body_v1": "raw_sha256", "modelscope_article_v1": "article_sha256", "discourse_first_post_v1": "post_sha256"}.get(algorithm, "sha256")
            observed_hash = source.get(field)
            status = "review_required"
            if source["status"] != "ok":
                status = "check_failed"
            elif (algorithm in {"visible_text_v1", "raw_body_v1", "modelscope_article_v1", "discourse_first_post_v1"}
                  and baseline.get("sha256") == observed_hash):
                status = "unchanged"
            observed = datetime.fromisoformat(source["checked_at"])
            rows[evidence["id"]] = {"url": evidence["url"], "status": status,
                "sha256": observed_hash, "hash_algorithm": algorithm,
                "policy_sha256": policy_digest(provider),
                "checked_at": source["checked_at"], "expires_at": (observed + timedelta(days=7)).isoformat()}
        providers[provider_id] = rows
    result = {"schema": 1, "checked_at": checked["checked_at"], "providers": providers}
    default_path = evidence_path(env).with_name("public-evidence.json") if public_only else evidence_path(env)
    destination = path if path is not None else default_path
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(destination) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = json.loads(destination.read_text())
            if previous.get("schema") == 1 and isinstance(previous.get("providers"), dict):
                for provider_id, rows in providers.items():
                    old_rows = previous["providers"].get(provider_id, {})
                    if not isinstance(old_rows, dict):
                        continue
                    for evidence_id, row in list(rows.items()):
                        old = old_rows.get(evidence_id, {})
                        if (not isinstance(old, dict) or old.get("status") != "unchanged"
                                or any(old.get(key) != row.get(key) for key in ("url", "policy_sha256", "hash_algorithm"))):
                            continue
                        # A failed or changed page cannot extend the last good
                        # verification. Its existing expiry remains visible.
                        if row["status"] != "unchanged":
                            rows[evidence_id] = {**old, "last_status": row["status"],
                                "last_attempt_at": row["checked_at"], "last_observed_sha256": row["sha256"]}
                if provider_ids is not None:
                    result["providers"] = {**previous["providers"], **providers}
        except (OSError, ValueError, AttributeError):
            pass
        _atomic_write(destination, result)
    return result


def main(argv: list[str] | None = None) -> int:
    """Scheduled maintenance entry point; every upstream request is GET only."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-only", action="store_true", help="Do not load or send credentials")
    parser.add_argument("--provider", action="append", dest="providers")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-sources", type=Path, help="Write public source change hashes here")
    parser.add_argument("--renew-evidence", action="store_true", help="Renew unchanged reviewed public policy separately")
    args = parser.parse_args(argv)
    if args.public_only:
        env: dict[str, str] = {}
    else:
        from .config import effective_env

        env = effective_env()
    result = refresh_catalog(env, args.providers, public_only=args.public_only, path=args.output)
    print(json.dumps({"generation": result["generation"], "providers": {
        key: {"status": row["status"], "models": len(row["models"]),
              "checked_at": row.get("checked_at")}
        for key, row in result["providers"].items()}}, sort_keys=True))
    if args.check_sources:
        args.check_sources.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(args.check_sources, check_public_sources(args.providers))
    if args.renew_evidence:
        renewed = refresh_evidence(env, args.providers, public_only=args.public_only)
        print(json.dumps({"evidence": {provider: {key: row.get("last_status", row["status"]) for key, row in rows.items()}
                                      for provider, rows in renewed["providers"].items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
