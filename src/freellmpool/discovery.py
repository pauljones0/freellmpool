"""Bounded GET-only discovery of reviewed free catalog candidates.

Snapshots contain normalized model facts and provenance, never credentials or
provider response bodies. Failed or partial refreshes cannot renew last-good age.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NamedTuple, ParamSpec, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .config import finite_float
from .free_policy import model_matches_grant, timestamp
from .http_read import (
    _SOURCE_TOTAL_SECONDS,
    ACCEPT_ENCODING,
    abounded_response_bytes,
    bounded_response_bytes,
)
from .provider_registry import evidence_path, load_registry, policy_digest

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Overall wall-clock budget gate for one check_public_sources pass (G26):
# budget gate 120s (worst ~= 166s + glibc), i.e. one in-flight 46s read may
# run past the gate. Checked BETWEEN sources only (fail-fast pre-check
# emitting error records); never preempts in-flight sync DNS/connect/read.
_EVIDENCE_OVERALL_SECONDS = 120
# Wizard single-check bound (G26): check_provider fails over to the deferred
# row when its fetch cannot finish inside this monotonic window.
_WIZARD_CHECK_SECONDS = 60
# First-run discovery budget (G24): absolute wall-clock bound enforced with
# asyncio.wait_for per page. Covers connect/handshake/headers/body under the
# event loop's control; return/exit never wait on a stalled system resolver
# (G26 closed the v5.2 shutdown-lag residual; the stall itself stays OS-time).
DISCOVERY_BUDGET_SECONDS = 40.0
_MIN_ATTEMPT_SECONDS = 5.0
_MIN_PAGE_SECONDS = 3.0
_NOTE_NETWORK_FAILURE = "Catalog network failure; last-good evidence preserved."
# Per-request idle phases preserve budget (fast-fail idle stalls); they are
# per-chunk waits, NOT a total bound. The bound is wait_for(timeout=R).
_IDLE_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
_DEFERRED_SKIP_NOTE = ("Skipped: discovery time budget exhausted (provider not "
                       "attempted); run `freellmpool update` to retry.")
_REFRESH_IN_LOOP = ("freellmpool: refresh_catalog cannot run inside a running event "
                    "loop; await arefresh_catalog instead.")
_CHECK_IN_LOOP = "freellmpool: check_provider cannot run inside a running event loop."
_ACCOUNT_ID = re.compile(r"[a-fA-F0-9]{32}\Z")
# Edge-mitigation refusal markers. Presence of one of these headers on a 403
# means the provider edge refused the listing; no account action applies.
# 401 ignores the header: the status itself reports missing authentication.
_MITIGATION_HEADERS = ("x-vercel-mitigated",)
_BLOCKED_RECHECK = ("A later re-check via `freellmpool update --provider PROVIDER` "
                    "re-verdicts; the verdict may persist. Run `freellmpool status` "
                    "for the reason.")


class DiscoveryBusy(OSError):
    """Another catalog refresh holds the lock; the caller kept last-good data."""


def _deferred_page_note(page: int) -> str:
    return (f"Skipped: discovery time budget exhausted (page {page} not fetched); "
            "run `freellmpool update` to retry.")


def budget_seconds(env: dict[str, str]) -> float:
    """Discovery wall budget from env, clamped so typos cannot wedge first run."""
    return finite_float(env.get("FREELLMPOOL_DISCOVERY_BUDGET_SECONDS"), 40.0,
                        minimum=5.0, maximum=45.0)


def _finite_seconds(value: float | None, name: str) -> float | None:
    """Strict caller-param check: None passes through, anything else must be finite.

    Unlike the env path (typos coerce to the default), explicit caller params
    fail loud: NaN poisons min()/comparisons and inf would silently unbind.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number of seconds")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number of seconds")
    return result


def _ensure_no_running_loop(message: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(message)


def _emit_progress(progress: Callable[..., None] | None, **event: Any) -> None:
    if progress is None:
        return
    try:
        progress(**event)
    except Exception:  # noqa: BLE001 - caller progress bugs must not break refresh
        pass


def safe_print(*args: Any, **kwargs: Any) -> None:
    """print() for human diagnostics; a closed pipe ends output, not the run."""
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        return


_MAIN_BUSY_LINE = ("freellmpool: another catalog refresh is running; scheduled refresh skipped "
                   "(retry later).")


_PID_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_PID_RUN = re.compile(r"[A-Za-z0-9]{32,}")


def sanitize_pid(pid: Any) -> str:
    """Pid for stderr lines: registry ids pass through, hostile bytes collapse.

    Long alphanumeric runs (account-id-shaped) are shortened so a hostile
    registry file cannot smuggle credential-shaped tokens into diagnostics.
    """
    text = pid if isinstance(pid, str) else str(pid)
    text = _PID_SAFE.sub("_", text)
    text = _PID_RUN.sub(lambda match: match.group(0)[:8] + "_", text)
    return text[:64] or "provider"


def progress_provider_line(pid: str, index: int, total: int) -> str:
    return f"freellmpool: discovering {sanitize_pid(pid)} ({index + 1}/{total})..."


def progress_page_line(pid: str, page: int) -> str:
    return f"freellmpool: discovering {sanitize_pid(pid)} page {page}..."


def stderr_progress_printer() -> Callable[..., None]:
    """Format provider/page refresh events as stderr progress lines (G24 order)."""
    def progress(**event: Any) -> None:
        pid = event.get("provider_id", "provider")
        if "page" in event:
            if event["page"] >= 2:
                safe_print(progress_page_line(pid, event["page"]), file=sys.stderr)
        else:
            safe_print(progress_provider_line(pid, event.get("index", 0), event.get("total", 0)),
                       file=sys.stderr)

    return progress


def count_snapshot(snapshot: dict[str, Any]) -> tuple[int, int, int]:
    """(ok, deferred, failed) over snapshot providers; failed is any other status."""
    ok = deferred = failed = 0
    providers = snapshot.get("providers", {})
    if not isinstance(providers, dict):
        return 0, 0, 0
    for row in providers.values():
        if not isinstance(row, dict):
            failed += 1
        elif row.get("status") == "ok":
            ok += 1
        elif row.get("status") == "deferred":
            deferred += 1
        else:
            failed += 1
    return ok, deferred, failed


def discovery_summary_line(snapshot: dict[str, Any], elapsed: float) -> str:
    ok, deferred, failed = count_snapshot(snapshot)
    return (f"freellmpool: discovery: {ok} ok, {deferred} deferred, {failed} failed "
            f"({elapsed:.0f}s)")


def deferred_failed_csv(snapshot: dict[str, Any]) -> str | None:
    """Name non-ok providers when the caller prints no table; None when all ok."""
    providers = snapshot.get("providers", {})
    if not isinstance(providers, dict):
        return None
    names = [sanitize_pid(pid) for pid, row in providers.items()
             if not isinstance(row, dict) or row.get("status") != "ok"]
    if not names:
        return None
    return f"freellmpool: deferred/failed providers: {', '.join(names)}"
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
        for row in result["providers"].values():
            if "fallback_models" in row:
                row["fallback_models"] = _sanitize_fallback_models(row["fallback_models"])
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


def _aclient() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_IDLE_TIMEOUT, follow_redirects=False)


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


class _AttemptContext(NamedTuple):
    url: str
    headers: dict[str, str]
    authenticated: bool
    key: str


class _EarlyResult(Exception):
    """Pre-network phase fully determines the row (unsupported/auth outcomes)."""

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__("pre-network outcome")
        self.row = row


class _DoneEarly(Exception):
    """A page response fully determines the row (auth/rate-limit outcomes)."""

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__("page outcome")
        self.row = row


class _BudgetExhausted(Exception):
    """wait_for fired or a pre-check tripped: defer current row, stop the world."""

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__("discovery budget exhausted")
        self.row = row


def _blank_result(now: str) -> dict[str, Any]:
    return {"status": "error", "last_attempt_at": now,
            "checked_at": None, "complete": False, "models": [], "note": "Catalog check failed."}


def _deferred_row(now: str, note: str) -> dict[str, Any]:
    return {"status": "deferred", "last_attempt_at": now, "checked_at": None,
            "complete": False, "models": [], "note": note}


def _prepare_attempt(provider: dict[str, Any], env: dict[str, str], result: dict[str, Any],
                     *, public_only: bool = False) -> _AttemptContext:
    """Pre-network phase; raises _EarlyResult for unsupported/auth outcomes."""
    spec = provider["discovery"]
    url = spec.get("url")
    if not url:
        raise _EarlyResult({**result, "status": "unsupported", "note": "No supported listing endpoint; no key judgment made."})
    key_name = provider.get("credential_env")
    key = env.get(key_name, "") if key_name else ""
    public = spec["supports_public"]
    if (public_only and not public) or (not key and spec["auth"] != "none" and not public):
        raise _EarlyResult({**result, "status": "auth_missing", "note": "A private listing credential is required."})
    if "{account_id}" in url:
        account_id = env.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not _ACCOUNT_ID.fullmatch(account_id):
            raise _EarlyResult({**result, "status": "auth_missing", "note": "A valid Cloudflare account ID is required."})
        url = url.replace("{account_id}", account_id)
    if not _same_origin(url, url):
        raise _EarlyResult({**result, "status": "unsupported", "note": "Unsupported catalog URL."})
    headers = {"Accept": "application/json", "Accept-Encoding": ACCEPT_ENCODING}
    # Public listing checks deliberately omit credentials. Inference entitlement
    # and actual key validity remain separate even when an API ignores bad keys.
    authenticated = bool(key and not public_only and not public and spec["auth"] != "none")
    if authenticated:
        if spec["auth"] == "x-goog-api-key":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    return _AttemptContext(url=_url_with(url, **spec.get("params", {})), headers=headers,
                           authenticated=authenticated, key=key)


_FALLBACK_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")


def _sanitize_fallback_models(value: Any) -> list[str]:
    """Fail-closed fallback-id filter: malformed entries are dropped."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value
            if isinstance(entry, str) and _FALLBACK_ID.fullmatch(entry)][:64]


def _grant_fallback_models(provider: dict[str, Any]) -> list[str]:
    """Reviewed-policy model names usable as fallback candidates.

    Verbatim selector.models entries from verified grants (any kind, any
    selector kind); grant exclusions and provider-level blocks are honored.
    Callers sanitize via _sanitize_fallback_models: entries are never used
    raw. The names do not establish availability, account access, or
    conformance.
    """
    grants = provider.get("grants")
    if not isinstance(grants, list):
        return []
    blocked = provider.get("blocked_models", [])
    blocked = blocked if isinstance(blocked, list) else []
    names: list[str] = []
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("status") != "verified":
            continue
        selector = grant.get("model_selector")
        if not isinstance(selector, dict):
            continue
        configured = selector.get("models", [])
        excluded = selector.get("exclude", [])
        if not isinstance(configured, list) or not isinstance(excluded, list):
            continue
        for name in configured:
            if name not in excluded and name not in blocked and name not in names:
                names.append(name)
    return names


def _classify_denied(status: int, headers: Any, *, key_env: str | None, key_present: bool,
                     authenticated: bool, supports_public: bool) -> tuple[str, str]:
    """Verdict for a 401/403 listing refusal: (status, note).

    Edge-mitigation headers and keyless-403 refusals are provider-side
    refusals (blocked). Every 403 is denied (permission scope, account
    verification, or edge policy — a 403 never proves a key bad; RFC 9110
    15.5.4). The auth_failed fallback is 401-only and authentication-only.
    """
    mitigated = status == 403 and any(headers.get(marker) is not None
                                      for marker in _MITIGATION_HEADERS)
    if mitigated:
        return ("blocked", f"HTTP {status}: provider edge refused the listing "
                           "[Vercel mitigation observed]; no account action applies. " + _BLOCKED_RECHECK)
    if status == 403 and supports_public and not authenticated:
        if key_present:
            return ("blocked", "HTTP 403: keyless public listing refused (no edge-mitigation "
                               "header observed); keyed listing not attempted. " + _BLOCKED_RECHECK)
        return ("blocked", "HTTP 403: keyless public listing refused (no edge-mitigation "
                           "header observed); no account action applies. " + _BLOCKED_RECHECK)
    if status == 401 and supports_public and not authenticated:
        if key_env is None:
            return ("auth_failed", "HTTP 401: keyless public listing refused; the provider now "
                                   "requires authentication and no credential applies to this provider.")
        if key_present:
            return ("auth_failed", f"HTTP 401: keyless public listing refused; the provider now "
                                   f"requires authentication via {key_env} (keyed listing not "
                                   "attempted). Run `freellmpool status` for the reason.")
        return ("auth_failed", f"HTTP 401: keyless public listing refused; the provider now "
                               f"requires authentication via {key_env}. Add the credential, "
                               "then run `freellmpool update` to retry.")
    if status == 403 and authenticated:
        # 018: the credential authenticated but the listing is forbidden
        # (permission scope or account verification). Never auth_failed:
        # a 403 does not establish an invalid credential.
        return ("denied", "HTTP 403: listing denied for this credential (often permission "
                           "scope or account verification); key NOT proven bad; listing did not "
                           "establish entitlement.")
    if status == 403:
        # H2: choke-point invariant — no 403 may reach the auth_failed
        # fallback below, even on paths _prepare_attempt cannot produce
        # today (unauthenticated private listing). A 403 never proves a key.
        return ("denied", "HTTP 403: listing denied for this request (permission scope, "
                           "account verification, or edge policy); no key proven bad; listing "
                           "did not establish entitlement.")
    # M1: fallback is 401-only and authentication-only; permission verdicts
    # are denied's territory since 018 (Cloudflare account-ID nuance is
    # added by consumers that know the provider: keys check, wizard).
    return ("auth_failed", f"HTTP {status}: authentication failed; the credential did not "
                           "authenticate; listing did not establish entitlement.")


async def _afetch_page(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                       timeout: httpx.Timeout, result: dict[str, Any], *,
                       key_env: str | None, key_present: bool, authenticated: bool,
                       supports_public: bool) -> Any:
    async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        if response.status_code in {401, 403}:
            status, note = _classify_denied(response.status_code, response.headers,
                                            key_env=key_env, key_present=key_present,
                                            authenticated=authenticated,
                                            supports_public=supports_public)
            raise _DoneEarly({**result, "status": status, "note": note})
        if response.status_code == 429:
            raise _DoneEarly({**result, "status": "rate_limited", "note": "Listing rate limited; prior evidence age is unchanged."})
        if 300 <= response.status_code < 400:
            raise ValueError("Catalog redirects are not followed")
        response.raise_for_status()
        return json.loads(await abounded_response_bytes(response, _MAX_RESPONSE_BYTES),
                          object_pairs_hook=_unique_object)


def _timeout_was_capped_by_deadline(error: httpx.TimeoutException,
                                    remaining: float) -> bool:
    """Return whether the absolute budget supplied this phase's timeout."""
    phase_limits = (
        (httpx.ConnectTimeout, 5.0),
        (httpx.ReadTimeout, 10.0),
        (httpx.WriteTimeout, 5.0),
        (httpx.PoolTimeout, 2.0),
    )
    return any(isinstance(error, kind) and remaining <= limit
               for kind, limit in phase_limits)


async def _afetch_attempt(provider: dict[str, Any], context: _AttemptContext,
                          result: dict[str, Any], *, deadline: float | None,
                          progress: Callable[..., None] | None = None,
                          cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    spec = provider["discovery"]
    now = result["last_attempt_at"]
    origin = context.url
    url = context.url
    seen: set[str] = set()
    collected: dict[str, dict[str, Any]] = {}
    total_raw = 0
    page = 0
    try:
        async with _aclient() as client:
            for _ in range(min(int(spec.get("max_pages", 100)), 100)):
                if url in seen or not _same_origin(url, origin):
                    raise ValueError("Unsafe or repeated pagination URL")
                seen.add(url)
                page += 1
                _emit_progress(progress, provider_id=provider["id"], page=page)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining < _MIN_PAGE_SECONDS:
                        raise _BudgetExhausted(_deferred_row(now, _deferred_page_note(page)))
                    # PINNED clamp. httpcore's connect phase covers getaddrinfo, so a
                    # resolver stall fails here (error + network note) when
                    # remaining > 5. When a phase timeout is capped to the same
                    # remaining budget, normalize its race with wait_for to the
                    # deferred budget verdict. Either path fails fast and loop
                    # shutdown abandons the executor thread instead of lagging it
                    # (G26 closed the v5.2 residual).
                    request_timeout = httpx.Timeout(connect=min(5.0, remaining), read=min(10.0, remaining),
                                                    write=min(5.0, remaining), pool=min(2.0, remaining))
                    try:
                        body = await asyncio.wait_for(
                            _afetch_page(client, url, context.headers, request_timeout, result,
                                         key_env=provider.get("credential_env"),
                                         key_present=bool(context.key),
                                         authenticated=context.authenticated,
                                         supports_public=spec["supports_public"]),
                            timeout=remaining)
                    except httpx.TimeoutException as error:
                        if not _timeout_was_capped_by_deadline(error, remaining):
                            raise
                        raise _BudgetExhausted(
                            _deferred_row(now, _deferred_page_note(page))) from None
                    except TimeoutError:
                        raise _BudgetExhausted(_deferred_row(now, _deferred_page_note(page))) from None
                else:
                    body = await _afetch_page(client, url, context.headers, _IDLE_TIMEOUT, result,
                                              key_env=provider.get("credential_env"),
                                              key_present=bool(context.key),
                                              authenticated=context.authenticated,
                                              supports_public=spec["supports_public"])
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
                    if context.key and context.key in encoded:
                        raise ValueError("Provider reflected a credential")
                    for candidate in _reviewed_policy_candidates(provider):
                        # Even a paid/unsupported listing row takes precedence over
                        # reviewed policy. Only wholly omitted identities are added.
                        collected.setdefault(candidate["id"], candidate)
                    free_models = free_catalog_models(provider, list(collected.values()))
                    note = ("Listing checked for reviewed free candidates; account free eligibility and capabilities remain unverified."
                            if context.authenticated else "Public listing checked for reviewed free candidates; API key validity, account free eligibility and capabilities remain unverified.")
                    if any(model["metadata"].get("unlisted") is True for model in free_models):
                        note += " Some candidates are unlisted and come from reviewed policy; their availability remains unverified."
                    return {**result, "status": "ok", "checked_at": now, "complete": True,
                            "models": free_models, "source_url": spec["url"],
                            "catalog_access": "authenticated" if context.authenticated else "public",
                            "catalog_ttl_seconds": spec.get("catalog_ttl_seconds", 86400),
                            "note": note}
                url = next_url
            raise ValueError("Catalog exceeded pagination budget")
    except _DoneEarly as done:
        if done.row["status"] != "blocked":
            # G32: Cloudflare 401 disambiguation runs only for callers that
            # pass a probe cache (keys-check loop, wizard check); every other
            # caller keeps byte-identical legacy behavior with zero extra I/O.
            if (done.row["status"] == "auth_failed"
                    and provider.get("id") == "cloudflare"
                    and cf_probe_cache is not None):
                await _maybe_cf_probe(context, deadline, cf_probe_cache)
            return done.row
        row = dict(done.row)
        row["catalog_ttl_seconds"] = spec.get("catalog_ttl_seconds", 86400)
        if spec["supports_public"]:
            row["catalog_access"] = "public"
        row["fallback_models"] = _sanitize_fallback_models(_grant_fallback_models(provider))
        return row
    except (ValueError, TypeError, KeyError, OverflowError):
        return {**result, "status": "partial", "note": "Incomplete, empty or malformed catalog; last-good evidence preserved."}
    except httpx.HTTPStatusError as error:
        return {**result, "status": "error", "note": f"Catalog HTTP {error.response.status_code}; last-good evidence preserved."}
    except httpx.HTTPError:
        return {**result, "status": "error", "note": _NOTE_NETWORK_FAILURE}


async def _aattempt(provider: dict[str, Any], env: dict[str, str], *, public_only: bool = False,
                  deadline: float | None = None,
                  progress: Callable[..., None] | None = None,
                  cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    now = _now()
    result = _blank_result(now)
    try:
        context = _prepare_attempt(provider, env, result, public_only=public_only)
    except _EarlyResult as early:
        return early.row
    return await _afetch_attempt(provider, context, result, deadline=deadline,
                                 progress=progress, cf_probe_cache=cf_probe_cache)


_T = TypeVar("_T")
_P = ParamSpec("_P")

_Queued = tuple["Future[Any]", "Callable[..., Any]", "tuple[Any, ...]", "dict[str, Any]"]


class _DaemonExecutor(ThreadPoolExecutor):
    """Per-call daemon-thread pool whose teardown never joins hung workers.

    Each _run_sync owns one instance (no singleton), so max outstanding
    work is calls-in-window x workers and one call's hung resolver thread
    can never stall another call's teardown. Workers are daemon threads
    that are never registered for interpreter-exit joining: shutdown with
    wait=False abandons in-flight items (their futures never complete)
    and the process may exit while they are still blocked.

    The ThreadPoolExecutor base is forced: loop.set_default_executor
    isinstance-gates on it. This __init__ deliberately never calls
    super().__init__(), so no eager spawn and no exit-join registration
    ever happen; proven by the exit-timing test plus the grep guard.
    Never use this pool as a context manager: the inherited __exit__
    joins with wait=True, contradicting the no-join contract.
    """

    def __init__(self, max_workers: int | None = None,
                 thread_name_prefix: str = "freellmpool-resolver-",
                 initializer: Callable[..., Any] | None = None,
                 initargs: tuple[Any, ...] = ()) -> None:
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._prefix = thread_name_prefix
        self._initializer = initializer
        self._initargs = initargs
        self._queue: queue.Queue[_Queued | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._shutdown = False
        self._drop = False
        self._lock = threading.Lock()

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future[_T] = Future()
            self._queue.put((future, fn, args, kwargs))
            if len(self._workers) < self._max_workers:
                worker = threading.Thread(target=self._worker,
                                          name=f"{self._prefix}{len(self._workers)}",
                                          daemon=True)
                self._workers.append(worker)
                worker.start()
            return future

    def _worker(self) -> None:
        # Initializer exceptions kill the worker; callers must not raise.
        if self._initializer is not None:
            self._initializer(*self._initargs)
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as error:
                    with self._lock:
                        if not self._drop:
                            future.set_exception(error)
                else:
                    with self._lock:
                        if not self._drop:
                            future.set_result(result)
                # Else result-drop: the shutdown-flag check and set_result /
                # set_exception run under this single lock, so a worker
                # either completes on the open loop or drops; dropped
                # futures never complete and no callback ever fires.
            finally:
                self._queue.task_done()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if not wait:
                self._drop = True
        if cancel_futures:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not None:
                        item[0].cancel()
                finally:
                    self._queue.task_done()
        if wait:
            self._queue.join()
        with self._lock:
            threads = list(self._workers)
        for _ in threads:
            self._queue.put(None)
        if wait:
            for thread in threads:
                thread.join()


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return
    # Documented assumption: fetch tasks never shield or suppress
    # CancelledError (no shields in fetch code; wait_for/httpx propagate
    # cancellation), matching asyncio.run without a bounded re-drive.
    for task in pending:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """asyncio.run twin with a per-call daemon resolver pool.

    No future escapes this call (pinned invariant): in-flight executor
    work is result-dropped at teardown, so never-completing dropped
    futures are safe. Teardown never blocks on threads on any path.
    """
    loop = asyncio.new_event_loop()
    executor = _DaemonExecutor()
    try:
        loop.set_default_executor(executor)
        asyncio.set_event_loop(loop)
        task = loop.create_task(coro)
        try:
            return loop.run_until_complete(task)
        except KeyboardInterrupt:
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
            # A done task is never re-driven: _run_until_complete_cb
            # declines to stop the loop for KI/SystemExit outcomes
            # (issue #22429), so re-driving a KI-completed future
            # would idle in select() forever.
            raise
    finally:
        try:
            _cancel_all_tasks(loop)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                finally:
                    try:
                        loop.close()
                    finally:
                        asyncio.set_event_loop(None)


def _attempt(provider: dict[str, Any], env: dict[str, str], *, public_only: bool = False,
             cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    now = _now()
    result = _blank_result(now)
    try:
        context = _prepare_attempt(provider, env, result, public_only=public_only)
    except _EarlyResult as early:
        return early.row
    # Pre-network outcomes above never touch the loop. check_provider is
    # sync-only: fail loudly instead of a cryptic _run_sync error.
    _ensure_no_running_loop(_CHECK_IN_LOOP)
    try:
        return _run_sync(_afetch_attempt(
            provider, context, result,
            deadline=time.monotonic() + _WIZARD_CHECK_SECONDS,
            cf_probe_cache=cf_probe_cache))
    except _BudgetExhausted as exhausted:
        return exhausted.row


def check_provider(provider_id: str, env: dict[str, str],
                   cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    """GET-only wizard check. Does not write snapshots or authorize inference.

    Bounded by _WIZARD_CHECK_SECONDS: a fetch that cannot finish in time
    returns the deferred row. Bounded callers (bootstrap, update, setup,
    maintenance, main) pass a deadline to refresh_catalog instead.
    """
    provider = load_registry(env).get(provider_id)
    if provider is None:
        return {"status": "unsupported", "complete": False, "model_count": 0,
                "checked_at": None, "last_attempt_at": _now(), "note": "Provider is not in the reviewed registry."}
    result = _attempt(provider, env, cf_probe_cache=cf_probe_cache)
    return {key: value for key, value in result.items() if key != "models"} | {"model_count": len(result["models"])}


# --- G29 `keys check`: per-slot key validation (GET-only, zero inference) ---

KEYS_CHECK_UNSUPPORTED_NOTE = "listing check does not authenticate; no key judgment"
KEYS_CHECK_TIMEOUT_NOTE = "overall budget expired; retry narrowed"
KEYS_CHECK_OK_NOTE = "key accepted on the listing endpoint"
KEYS_CHECK_PARTIAL_NOTE = ("listing partially read; key NOT proven "
                           "(redirect or malformed catalog); retry")
KEYS_CHECK_AUTH_FAILED_NOTE = "key rejected — replace/re-verify"
KEYS_CHECK_RATE_LIMITED_NOTE = "429: back off and retry"
KEYS_CHECK_BLOCKED_NOTE = "edge refused listing; key NOT judged; no account action"
KEYS_CHECK_DENIED_NOTE = ("listing denied: often permission scope or account "
                          "verification; key not proven bad")
KEYS_CHECK_DEFERRED_NOTE = "per-call bound expired; retry"
KEYS_CHECK_ERROR_NOTE = "transport/HTTP failure; retry"
KEYS_CHECK_ACCOUNT_ID_FIX = "set a valid CLOUDFLARE_ACCOUNT_ID"
KEYS_CHECK_REGISTRY_FIX = "freellmpool update --renew-evidence"

# --- G32 Cloudflare token-verify disambiguation (spike v2.1) ---
# A Cloudflare listing 401 jointly authenticates (token, account ID). Two
# verify probes disambiguate: A (account endpoint) tests the pair jointly,
# B (user endpoint) catches user tokens. Notes are static text only: probe
# URLs carry the account-ID value and are never rendered (SCOPE#5).

CF_VERIFY_USER_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"
CF_VERIFY_ACCOUNT_URL = ("https://api.cloudflare.com/client/v4/accounts/"
                         "{account_id}/tokens/verify")
CF_VERIFY_PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
# Minimum remaining check-deadline to spend one probe GET (_MIN_PAGE_SECONDS
# is the 3.0 analogy; a verify GET is a single small round-trip).
_MIN_PROBE_SECONDS = 2.0
# Parse-back of the substituted account ID from the listing URL (FEAS T2):
# _AttemptContext carries no account field, and substitution at
# _prepare_attempt guarantees the /accounts/{32hex}/ shape on this path.
_CF_VERIFY_ACCOUNT_RE = re.compile(r"/accounts/([a-fA-F0-9]{32})(?:/|\Z)")

KEYS_CHECK_CF_H1_NOTE = ("HTTP 401 does not isolate a bad token from a wrong "
                         "CLOUDFLARE_ACCOUNT_ID; key NOT proven bad")
KEYS_CHECK_CF_PAIR_OK_NOTE = ("pair verified at the account verify endpoint; "
                              "listing refused (scope or account verification)")
KEYS_CHECK_CF_WRONG_ACCOUNT_NOTE = ("token valid at the user endpoint but rejected for "
                                    "this account: re-verify CLOUDFLARE_ACCOUNT_ID "
                                    "(or grant the token account access)")
KEYS_CHECK_CF_TOKEN_DEAD_NOTE = ("Cloudflare account and user verifiers both reject "
                                 "this token (both agree: dead); replace the key")
KEYS_CHECK_CF_TOKEN_EXPIRED_NOTE = ("Cloudflare token is expired (verify reports "
                                    "status=expired); replace the key")
KEYS_CHECK_CF_RETRY_SUFFIX = " (verify endpoint rate-limited; retry later)"


def _cf_token_hash(token: str) -> str:
    """Non-reversible memo identity for a token (COMP gap 1).

    Raw token values never enter memo keys, notes, URLs, or logs; the
    truncated hash is computed only for memo lookup, never persisted or
    rendered.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _classify_verify_response(status_code: int, body: bytes) -> str:
    """Classify one verify-endpoint answer (v2.1 §3 table; total).

    HTTP status drives; bodies parse defensively (success bool + status
    field, never message strings). Returns ok/expired/auth/rate_limited/
    ambiguous — unknown signals fail closed toward ambiguous.
    """
    if status_code == 401:
        return "auth"
    if status_code == 429:
        return "rate_limited"
    if status_code == 200:
        try:
            payload = json.loads(body)
        except ValueError:
            return "ambiguous"
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return "ambiguous"
        result = payload.get("result")
        status = result.get("status") if isinstance(result, dict) else None
        if status == "active":
            return "ok"
        if status == "expired":
            return "expired"
    return "ambiguous"


async def _afetch_verify_probe(url: str, token: str, remaining: float | None) -> str:
    """One verify GET; transport/timeout failures classify as ambiguous."""
    if remaining is None:
        timeout = CF_VERIFY_PROBE_TIMEOUT
    else:
        timeout = httpx.Timeout(connect=min(5.0, remaining), read=min(10.0, remaining),
                                write=min(5.0, remaining), pool=min(2.0, remaining))
    headers = {"Accept": "application/json", "Accept-Encoding": ACCEPT_ENCODING,
               "Authorization": f"Bearer {token}"}
    try:
        async with _aclient() as client:
            if remaining is None:
                response = await client.get(url, headers=headers, timeout=timeout)
            else:
                response = await asyncio.wait_for(
                    client.get(url, headers=headers, timeout=timeout),
                    timeout=remaining)
            # Non-streaming get() returns after the (timeout-bounded) body
            # is fully received, so this read is a size check, not I/O:
            # no deadline overrun is possible here (adversarial-4).
            body = await abounded_response_bytes(response, _MAX_RESPONSE_BYTES)
    except (httpx.HTTPError, TimeoutError, OSError):
        return "ambiguous"
    return _classify_verify_response(response.status_code, body)


async def _aprobe_inner(token: str, account_id: str, ident: str,
                        deadline: float | None, cache: dict[str, str]) -> str:
    """A-then-maybe-B probe flow (v2.1 §3 rules 1-7); memoizes a:/b: entries."""
    akey, bkey = f"a:{ident}:{account_id}", f"b:{ident}"
    probe_a = cache.get(akey)
    if probe_a is None:
        # One clock read per probe: the same remaining gates the spend and
        # bounds the GET (scripted-clock test pins the two-call sequence).
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining < _MIN_PROBE_SECONDS:
            probe_a = "skipped"
        else:
            probe_a = await _afetch_verify_probe(
                CF_VERIFY_ACCOUNT_URL.replace("{account_id}", account_id),
                token, remaining)
        cache[akey] = probe_a
    if probe_a == "ok":
        return "pair_ok"
    if probe_a == "expired":
        return "token_expired"
    if probe_a == "rate_limited":
        return "inconclusive_retry"
    if probe_a != "auth":
        return "inconclusive"
    probe_b = cache.get(bkey)
    if probe_b is None:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining < _MIN_PROBE_SECONDS:
            probe_b = "skipped"
        else:
            probe_b = await _afetch_verify_probe(CF_VERIFY_USER_URL, token, remaining)
        cache[bkey] = probe_b
    if probe_b == "ok":
        return "wrong_account"
    if probe_b == "auth":
        return "token_dead"
    if probe_b == "expired":
        return "token_expired"
    if probe_b == "rate_limited":
        return "inconclusive_retry"
    return "inconclusive"


async def _aprobe_cloudflare_token(token: str, account_id: str, *,
                                   deadline: float | None,
                                   cache: dict[str, str]) -> str:
    """Run Cloudflare verify probes; return the outcome token (v2.1 §3-§4).

    Rule 0: a memoized outcome replays with zero new calls and no budget
    check. Never raises: an unexpected Exception records `inconclusive`
    instead of breaking the check path (SCOPE blocker 3); BaseException
    (CancelledError/KeyboardInterrupt) still propagates.
    """
    okey = f"outcome:{_cf_token_hash(token)}:{account_id}"
    cached = cache.get(okey)
    if cached is not None:
        return cached
    try:
        outcome = await _aprobe_inner(token, account_id, _cf_token_hash(token),
                                      deadline, cache)
    except Exception:
        outcome = "inconclusive"
    cache[okey] = outcome
    return outcome


async def _maybe_cf_probe(context: _AttemptContext, deadline: float | None,
                          cache: dict[str, str]) -> None:
    """Probe hook for the CF-401 branch; records the outcome in cache."""
    match = _CF_VERIFY_ACCOUNT_RE.search(context.url)
    token, account_id = context.key, match.group(1) if match else ""
    if not token or not account_id:
        cache[f"outcome:{_cf_token_hash(token)}:{account_id}"] = "inconclusive"
        return
    await _aprobe_cloudflare_token(token, account_id, deadline=deadline, cache=cache)


def cf_probe_outcome(cache: dict[str, str]) -> str | None:
    """Outcome token from a single-check probe cache (wizard channel).

    Single-check caches (one fresh dict per wizard check) hold at most one
    outcome: entry; multi-slot keys-check caches are read per (token,
    account) by check_provider_slot instead.
    """
    for key, value in cache.items():
        if key.startswith("outcome:"):
            return value
    return None


def is_listing_checkable(provider: Mapping[str, Any]) -> bool:
    """True when a listing check would authenticate, and so judge the key.

    Computed from registry fields (credential + url + non-none auth +
    private listing), never a hardcoded id list, so registry drift re-scopes
    automatically; the keys-check tripwire test pins the reviewed set.
    """
    spec = provider.get("discovery")
    if not isinstance(spec, Mapping):
        return False
    return (bool(provider.get("credential_env"))
            and bool(spec.get("url"))
            and spec.get("auth") not in (None, "none")
            and spec.get("supports_public") is False)


def slot_env_var(key_env: str, slot: int) -> str:
    """Env var holding a key slot: slot 1 is the bare var, N > 1 is ``VAR_N``."""
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9:
        raise ValueError(f"slot must be 1-9 (got {slot!r})")
    return key_env if slot == 1 else f"{key_env}_{slot}"


def keys_check_retry_fix(provider_id: str, slot: int) -> str:
    """Narrowed re-check command for inconclusive rows."""
    return f"retry: freellmpool keys check --provider {provider_id} --slot {slot}"


def keys_check_replace_fix(provider_id: str, slot: int) -> str:
    """Replace + re-verify command for a proven-dead key."""
    return (f"replace: freellmpool keys add {provider_id} --slot {slot}; "
            f"re-verify: freellmpool setup --provider {provider_id}")


def keys_check_scope_fix(provider_id: str, slot: int) -> str:
    """Out-of-band scope check + narrowed re-check for a denied listing (018)."""
    return (f"scope: verify model-listing permission / account verification "
            f"for {provider_id}, then: freellmpool keys check "
            f"--provider {provider_id} --slot {slot}")


def keys_check_missing_note(slot: int, env_var: str) -> str:
    """Static note for an unconfigured slot (names are never secrets)."""
    # 018/F8: unconfigured covers absent AND blank (both fail the truthiness gate).
    return f"slot {slot} unconfigured ({env_var} not set or blank)"


def _keys_check_cf_h1_fix(provider_id: str, slot: int) -> str:
    """Account-ID-first scope fix for an inconclusive Cloudflare 401 (H1)."""
    return ("scope: re-verify CLOUDFLARE_ACCOUNT_ID for cloudflare (a wrong "
            "account ID fails auth with a valid token), then: freellmpool keys "
            f"check --provider {provider_id} --slot {slot}")


def _map_cf_probe_row(base: dict[str, Any], provider_id: str, slot: int,
                      cf_probe: str | None) -> dict[str, Any]:
    """Map a Cloudflare 401 onto a verdict via the probe outcome (G32, pure).

    `cf_probe` is the v2.1 §4 outcome token (None when no probe cache was
    passed). None, `inconclusive`, and unknown tokens fail closed to the
    byte-identical H1 row — never auth_failed, never replace-key.
    """
    if cf_probe == "pair_ok":
        return {**base, "verdict": "denied", "note": KEYS_CHECK_CF_PAIR_OK_NOTE,
                "fix": keys_check_scope_fix(provider_id, slot)}
    if cf_probe == "wrong_account":
        return {**base, "verdict": "config_error",
                "note": KEYS_CHECK_CF_WRONG_ACCOUNT_NOTE,
                "fix": KEYS_CHECK_ACCOUNT_ID_FIX}
    if cf_probe == "token_dead":
        return {**base, "verdict": "auth_failed", "note": KEYS_CHECK_CF_TOKEN_DEAD_NOTE,
                "fix": keys_check_replace_fix(provider_id, slot)}
    if cf_probe == "token_expired":
        return {**base, "verdict": "auth_failed",
                "note": KEYS_CHECK_CF_TOKEN_EXPIRED_NOTE,
                "fix": keys_check_replace_fix(provider_id, slot)}
    if cf_probe == "inconclusive_retry":
        return {**base, "verdict": "denied",
                "note": KEYS_CHECK_CF_H1_NOTE + KEYS_CHECK_CF_RETRY_SUFFIX,
                "fix": _keys_check_cf_h1_fix(provider_id, slot)}
    # H1: Cloudflare jointly authenticates (token, account ID), so a 401
    # cannot isolate a bad token from a wrong account ID. The verdict judges
    # the KEY: denied with an account-ID scope fix — never auth_failed,
    # never replace-key. The raw endpoint signal stays in status.
    return {**base, "verdict": "denied", "note": KEYS_CHECK_CF_H1_NOTE,
            "fix": _keys_check_cf_h1_fix(provider_id, slot)}


def _map_slot_row(provider_id: str, slot: int, env_var: str,
                  raw: dict[str, Any], cf_probe: str | None = None) -> dict[str, Any]:
    """Map one raw listing row onto a keys-check verdict row (total mapping)."""
    status = raw.get("status")
    base: dict[str, Any] = {"provider": provider_id, "slot": slot, "env_var": env_var,
                            "status": status, "catalog_access": raw.get("catalog_access"),
                            "attempted": True}
    if status == "ok":
        return {**base, "verdict": "ok", "note": KEYS_CHECK_OK_NOTE, "fix": None}
    if status == "partial":
        return {**base, "verdict": "partial", "note": KEYS_CHECK_PARTIAL_NOTE,
                "fix": keys_check_retry_fix(provider_id, slot)}
    if status == "auth_failed":
        if provider_id == "cloudflare":
            return _map_cf_probe_row(base, provider_id, slot, cf_probe)
        return {**base, "verdict": "auth_failed", "note": KEYS_CHECK_AUTH_FAILED_NOTE,
                "fix": keys_check_replace_fix(provider_id, slot)}
    if status == "rate_limited":
        return {**base, "verdict": "rate_limited", "note": KEYS_CHECK_RATE_LIMITED_NOTE,
                "fix": keys_check_retry_fix(provider_id, slot)}
    if status == "blocked":
        return {**base, "verdict": "blocked", "note": KEYS_CHECK_BLOCKED_NOTE,
                "fix": keys_check_retry_fix(provider_id, slot)}
    if status == "denied":
        # 018: an authenticated 403 proves nothing about the key; the fix is
        # out-of-band scope verification, never replace-key, never retry.
        return {**base, "verdict": "denied", "note": KEYS_CHECK_DENIED_NOTE,
                "fix": keys_check_scope_fix(provider_id, slot)}
    if status == "deferred":
        return {**base, "verdict": "deferred", "note": KEYS_CHECK_DEFERRED_NOTE,
                "fix": keys_check_retry_fix(provider_id, slot)}
    if status == "error":
        return {**base, "verdict": "error", "note": KEYS_CHECK_ERROR_NOTE,
                "fix": keys_check_retry_fix(provider_id, slot)}
    if status == "auth_missing":
        # Only reachable on a configured slot via a bad CLOUDFLARE_ACCOUNT_ID:
        # every other auth_missing path needs a falsy key, excluded above.
        return {**base, "verdict": "config_error",
                "note": (f"CLOUDFLARE_ACCOUNT_ID is missing or invalid; slot {slot} "
                         f"({env_var}) cannot authenticate"),
                "fix": KEYS_CHECK_ACCOUNT_ID_FIX}
    if status == "unsupported":
        # 018/F5: reachable only when _prepare_attempt refuses pre-network
        # (missing endpoint or non-same-origin URL from a corrupt bundle):
        # no network ran, so attempted is False and checked must exclude it.
        base["attempted"] = False
        return {**base, "verdict": "unsupported",
                "note": KEYS_CHECK_UNSUPPORTED_NOTE, "fix": None}
    raise ValueError(f"unmapped discovery status: {status!r}")


def check_provider_slot(provider_id: str, env: dict[str, str], slot: int,
                        cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate one key slot over the GET-only listing path. Zero inference.

    Slot 1 reads the bare credential var, slot N > 1 reads ``VAR_N``. Slots
    whose check would not authenticate yield ``unsupported`` without touching
    the network; unconfigured (absent or blank) slots yield ``missing``. The
    row carries ``catalog_access`` for internal auditing; CLI output must
    project the whitelisted fields only and never render it.
    """
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9:
        raise ValueError(f"slot must be 1-9 (got {slot!r})")
    provider = load_registry(env).get(provider_id)
    if provider is None:
        return {"provider": provider_id, "slot": slot, "env_var": None,
                "verdict": "unsupported", "status": None,
                "note": "Provider is not in the reviewed registry.", "fix": None,
                "catalog_access": None, "attempted": False}
    key_env = provider.get("credential_env")
    if not is_listing_checkable(provider):
        return {"provider": provider_id, "slot": slot,
                "env_var": (slot_env_var(key_env, slot)
                            if isinstance(key_env, str) and key_env else None),
                "verdict": "unsupported", "status": None,
                "note": KEYS_CHECK_UNSUPPORTED_NOTE, "fix": None,
                "catalog_access": None, "attempted": False}
    if not isinstance(key_env, str) or not key_env:
        raise ValueError(f"checkable provider {provider_id!r} has no credential env")
    env_var = slot_env_var(key_env, slot)
    if not env.get(env_var):
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "missing", "status": None,
                "note": keys_check_missing_note(slot, env_var), "fix": None,
                "catalog_access": None, "attempted": False}
    slot_env = dict(env)
    slot_env[key_env] = env[env_var]
    raw = check_provider(provider_id, slot_env, cf_probe_cache=cf_probe_cache)
    cf_probe: str | None = None
    if (cf_probe_cache is not None and provider_id == "cloudflare"
            and raw.get("status") == "auth_failed"):
        token = slot_env.get(key_env, "")
        account_id = slot_env.get("CLOUDFLARE_ACCOUNT_ID", "")
        cf_probe = cf_probe_cache.get(f"outcome:{_cf_token_hash(token)}:{account_id}")
    return _map_slot_row(provider_id, slot, env_var, raw, cf_probe=cf_probe)


# --- G30 `--canary`: opt-in inference canary for uncheckable providers ---

CANARY_TARGETS: dict[str, str] = {
    "openrouter": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
    "vercel": "poolside/laguna-s-2.1-free",
    "aion": "aion-labs/aion-2.0",
}
CANARY_MAX_TOKENS = 16
CANARY_TIMEOUT_SECONDS = 20.0
CANARY_PROMPT = "Reply with exactly OK."
KEYS_CHECK_CANARY_DENIED_NOTE = ("canary refused for this credential (permission scope or "
                                 "account access); key NOT proven bad")
KEYS_CHECK_CANARY_BILLING_NOTE = ("canary refused: billing or no free route; key NOT proven bad")
KEYS_CHECK_CANARY_REDIRECT_NOTE = "canary redirected; key NOT judged"
KEYS_CHECK_CANARY_CONSENT = ("freellmpool: --canary spends ONE inference call (max_tokens=16) per "
                             "listed slot on a free model; every dispatched attempt is recorded "
                             "in quota, including failures.")


def is_canary_eligible(provider_id: str, record: Mapping[str, Any] | None, local: Any | None,
                       slot: int, env: Mapping[str, str]) -> bool:
    """True when a canary may judge this slot (G30 eligibility rules 1-6).

    `record` is the reviewed-registry entry (None for registry-external
    providers); `local` is the catalog Provider (adapter/key_optional live
    there, not in the registry). The pinned set lives in CANARY_TARGETS;
    grant terms (paid_overage_possible False + no required account
    conditions) are pinned by test, not evaluated at runtime.
    """
    if record is None or provider_id not in CANARY_TARGETS:
        return False
    key_env = record.get("credential_env")
    if not isinstance(key_env, str) or not key_env:
        return False
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9:
        return False
    if not env.get(slot_env_var(key_env, slot)):
        return False
    if is_listing_checkable(record):
        return False
    if record.get("inference_auth") == "none":
        return False
    if local is None or getattr(local, "key_optional", False) is True:
        return False
    return getattr(local, "adapter", None) == "openai"


def keys_check_canary_retry_fix(provider_id: str, slot: int) -> str:
    """Narrowed re-canary command (the fix must reproduce --canary)."""
    return (f"retry: freellmpool keys check --provider {provider_id} --slot {slot} --canary")


def keys_check_canary_scope_fix(provider_id: str, slot: int) -> str:
    """Out-of-band permission check + narrowed re-canary for a refused canary."""
    return (f"scope: verify model/inference permission / account access "
            f"for {provider_id}, then: freellmpool keys check "
            f"--provider {provider_id} --slot {slot} --canary")


def _canary_default_post() -> Any:
    """Single-shot network POST for canaries (no retries, no backoff sleep)."""
    from functools import partial

    from . import client

    return partial(client.default_post, max_attempts=1)


def _map_canary_http(provider_id: str, slot: int, env_var: str, model: str,
                     status: int, headers: Any) -> dict[str, Any]:
    """Map one raw canary HTTP outcome onto a verdict row (total mapping)."""
    base: dict[str, Any] = {"provider": provider_id, "slot": slot, "env_var": env_var,
                            "catalog_access": None, "attempted": True,
                            "via": "canary", "canary_model": model}
    retry = keys_check_canary_retry_fix(provider_id, slot)
    if 200 <= status <= 299:
        return {**base, "status": "ok", "verdict": "ok",
                "note": f"key accepted on {model} canary", "fix": None}
    if status == 401:
        return {**base, "status": "auth_failed", "verdict": "auth_failed",
                "note": KEYS_CHECK_AUTH_FAILED_NOTE,
                "fix": keys_check_replace_fix(provider_id, slot)}
    if status == 403:
        mitigated = headers is not None and any(
            headers.get(marker) is not None for marker in _MITIGATION_HEADERS)
        if mitigated:
            return {**base, "status": "blocked", "verdict": "blocked",
                    "note": KEYS_CHECK_BLOCKED_NOTE, "fix": retry}
        return {**base, "status": "denied", "verdict": "denied",
                "note": KEYS_CHECK_CANARY_DENIED_NOTE,
                "fix": keys_check_canary_scope_fix(provider_id, slot)}
    if status == 402:
        return {**base, "status": "denied", "verdict": "denied",
                "note": KEYS_CHECK_CANARY_BILLING_NOTE,
                "fix": keys_check_canary_scope_fix(provider_id, slot)}
    if status == 404:
        return {**base, "status": "error", "verdict": "error",
                "note": f"canary target {model} not found; catalog drift suspected",
                "fix": retry}
    if status in (408, 504):
        return {**base, "status": "deferred", "verdict": "deferred",
                "note": KEYS_CHECK_DEFERRED_NOTE, "fix": retry}
    if status == 429:
        return {**base, "status": "rate_limited", "verdict": "rate_limited",
                "note": KEYS_CHECK_RATE_LIMITED_NOTE, "fix": retry}
    if 300 <= status <= 399:
        return {**base, "status": "error", "verdict": "error",
                "note": KEYS_CHECK_CANARY_REDIRECT_NOTE, "fix": retry}
    if 400 <= status <= 499:
        return {**base, "status": "error", "verdict": "error",
                "note": f"canary malformed/unsupported by target ({status}); key NOT judged",
                "fix": retry}
    return {**base, "status": "error", "verdict": "error",
            "note": KEYS_CHECK_ERROR_NOTE, "fix": retry}


def check_canary_slot(provider_id: str, env: dict[str, str], slot: int,
                      *, post: Any = None) -> dict[str, Any]:
    """Judge one key slot with a single-shot inference canary (G30).

    Exactly one POST to the pinned free target (max_tokens=16, thinking
    floor disabled, no retries). Attempt-based quota: every dispatched
    attempt records, including failures; connect-phase failures
    (ConnectError/ConnectTimeout/PoolTimeout — httpx cannot split
    DNS/refused/TLS) record nothing. The AllowanceLedger is never
    touched. Notes are static strings; exception text is never rendered.
    """
    from . import client
    from .errors import ProviderHTTPError
    from .models import Provider
    from .quota import QuotaStore

    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9:
        raise ValueError(f"slot must be 1-9 (got {slot!r})")
    provider = load_registry(env).get(provider_id)
    model = CANARY_TARGETS.get(provider_id)
    key_env = provider.get("credential_env") if provider else None
    if provider is None or model is None or not key_env:
        return {"provider": provider_id, "slot": slot, "env_var": None,
                "verdict": "unsupported", "status": None,
                "note": KEYS_CHECK_UNSUPPORTED_NOTE, "fix": None,
                "catalog_access": None, "attempted": False}
    env_var = slot_env_var(key_env, slot)
    if not env.get(env_var):
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "missing", "status": None,
                "note": keys_check_missing_note(slot, env_var), "fix": None,
                "catalog_access": None, "attempted": False}
    key = env[env_var]
    slot_env = dict(env)
    slot_env[key_env] = key
    target = Provider(provider_id, provider.get("display_name", provider_id),
                      "openai", provider["api_base_url"], (), key_env=key_env,
                      auth="bearer", key_optional=False, extra_env=())
    captured: dict[str, Any] = {}
    inner = post or _canary_default_post()

    def spy(url: str, headers: dict[str, str], body: dict[str, Any],
            timeout: float) -> Any:
        result = inner(url, headers, body, timeout)
        captured["result"] = result
        return result

    quota = QuotaStore()

    def charge() -> None:
        quota.record(provider_id, model)

    def mapped(status: int | None, headers: Any, fallback: int) -> dict[str, Any]:
        # Raw captured status is authoritative (a synthesized 502 on an
        # empty-choices 2xx must not misjudge an accepted key); the
        # exception status is the fallback when nothing was captured.
        return _map_canary_http(provider_id, slot, env_var, model,
                                status if status is not None else fallback,
                                headers)

    try:
        client.call(target, model, [{"role": "user", "content": CANARY_PROMPT}],
                    api_key=key, env=slot_env, max_tokens=CANARY_MAX_TOKENS,
                    temperature=0, timeout=CANARY_TIMEOUT_SECONDS,
                    enforce_thinking_floor=False, post=spy)
    except ProviderHTTPError as exc:
        charge()
        result = captured.get("result")
        return mapped(getattr(result, "status", None),
                      getattr(result, "headers", None), exc.status)
    except httpx.TimeoutException as exc:
        # ReadTimeout dispatched (charge); connect-phase timeouts did not.
        if not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
            charge()
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "deferred", "status": "deferred",
                "note": KEYS_CHECK_DEFERRED_NOTE,
                "fix": keys_check_canary_retry_fix(provider_id, slot),
                "catalog_access": None, "attempted": True,
                "via": "canary", "canary_model": model}
    except httpx.ConnectError:
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "error", "status": "error",
                "note": KEYS_CHECK_ERROR_NOTE,
                "fix": keys_check_canary_retry_fix(provider_id, slot),
                "catalog_access": None, "attempted": True,
                "via": "canary", "canary_model": model}
    except Exception:  # noqa: BLE001 - static note; never render str(exc)
        charge()
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "error", "status": "error",
                "note": KEYS_CHECK_ERROR_NOTE,
                "fix": keys_check_canary_retry_fix(provider_id, slot),
                "catalog_access": None, "attempted": True,
                "via": "canary", "canary_model": model}
    charge()
    result = captured.get("result")
    if result is None:  # pragma: no cover - defensive; openai adapter always posts
        return {"provider": provider_id, "slot": slot, "env_var": env_var,
                "verdict": "error", "status": "error",
                "note": KEYS_CHECK_ERROR_NOTE,
                "fix": keys_check_canary_retry_fix(provider_id, slot),
                "catalog_access": None, "attempted": True,
                "via": "canary", "canary_model": model}
    return mapped(result.status, result.headers, result.status)


def _atomic_write(path: Path, result: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1  # the handle owns the fd from here on
        with handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prepare_refresh(env: dict[str, str], provider_ids: list[str] | None,
                     public_only: bool, path: Path | str | None
                     ) -> tuple[Path, dict[str, dict[str, Any]], list[str]]:
    default_path = default_discovery_path(env)
    if public_only:
        default_path = default_path.with_name("public-discovery.json")
    destination = Path(path) if path is not None else default_path
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = load_registry() if public_only else load_registry(env)
    requested = list(dict.fromkeys(provider_ids if provider_ids is not None else registry))
    if any(provider_id not in registry for provider_id in requested):
        raise ValueError("Requested provider is absent or removed from the reviewed registry")
    return destination, registry, requested


async def _acquire_lock(lock_path: Path, deadline: float | None) -> Any:
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock = os.fdopen(descriptor, "a+")
    try:
        if deadline is None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: fcntl.flock(lock, fcntl.LOCK_EX))
            return lock
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise DiscoveryBusy("another catalog refresh is running") from None
                await asyncio.sleep(0.05)
    except BaseException:
        lock.close()
        raise


async def _arefresh_impl(env: dict[str, str], registry: dict[str, dict[str, Any]],
                         requested: list[str], public_only: bool, destination: Path, *,
                         deadline: float | None,
                         progress: Callable[..., None] | None,
                         cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    lock_path = destination.with_suffix(destination.suffix + ".lock")
    lock = await _acquire_lock(lock_path, deadline)
    with lock:
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
        rows: dict[str, dict[str, Any]] = {}
        total = len(requested)
        for index, provider_id in enumerate(requested):
            if deadline is not None and deadline - time.monotonic() < _MIN_ATTEMPT_SECONDS:
                rows[provider_id] = _deferred_row(_now(), _DEFERRED_SKIP_NOTE)
                for rest in requested[index + 1:]:
                    rows[rest] = _deferred_row(_now(), _DEFERRED_SKIP_NOTE)
                break
            _emit_progress(progress, provider_id=provider_id, index=index, total=total)
            try:
                rows[provider_id] = await _aattempt(
                    registry[provider_id], env, public_only=public_only, deadline=deadline,
                    progress=progress, cf_probe_cache=cf_probe_cache)
            except _BudgetExhausted as exhausted:
                rows[provider_id] = exhausted.row
                for rest in requested[index + 1:]:
                    rows[rest] = _deferred_row(_now(), _DEFERRED_SKIP_NOTE)
                break
        for provider_id in requested:
            attempted = rows[provider_id]
            previous = snapshot["providers"].get(provider_id)
            if attempted["status"] == "blocked" and previous:
                # Blocked is a durable refusal verdict, not transient like
                # deferred: stale models must not serve under it. The display
                # is fallback candidates, so the merge fails closed.
                merged = dict(previous)
                merged.update({key: attempted[key] for key in ("status", "last_attempt_at", "note")})
                merged.update(complete=False, models=[], checked_at=None)
                merged["fallback_models"] = _sanitize_fallback_models(
                    attempted.get("fallback_models", []))
                snapshot["providers"][provider_id] = merged
            elif attempted["status"] != "ok" and previous:
                # CTO-4: preserved rows (including preserved-deferred) keep
                # complete + models and serve while fresh; only the attempt
                # facts are overwritten. Fresh-deferred keeps complete False.
                preserved = dict(previous)
                preserved.update({key: attempted[key] for key in ("status", "last_attempt_at", "note")})
                # A stale fallback list must not outlive a fresh verdict.
                preserved.pop("fallback_models", None)
                snapshot["providers"][provider_id] = preserved
            else:
                snapshot["providers"][provider_id] = attempted
        snapshot.update(schema=1, generation=uuid.uuid4().hex, updated_at=_now())
        _atomic_write(destination, snapshot)
        return snapshot


def refresh_catalog(env: dict[str, str], provider_ids: list[str] | None = None,
                    public_only: bool = False, path: Path | str | None = None, *,
                    deadline: float | None = None,
                    progress: Callable[..., None] | None = None,
                    cf_probe_cache: dict[str, str] | None = None) -> dict[str, Any]:
    """Refresh requested catalogs and atomically reconcile against last-good data.

    A lock serializes writers (including separate scheduled/CLI processes). The
    snapshot contains a complete last-good generation, never a half-written page.

    With ``deadline`` (monotonic seconds), network I/O is bounded: providers
    that cannot start or finish in time are marked ``deferred`` and the single
    locked write still happens. Without it, legacy blocking-lock semantics
    apply. Async consumers inside a running loop must await arefresh_catalog.
    """
    _ensure_no_running_loop(_REFRESH_IN_LOOP)
    destination, registry, requested = _prepare_refresh(env, provider_ids, public_only, path)
    return _run_sync(_arefresh_impl(env, registry, requested, public_only, destination,
                                      deadline=deadline, progress=progress,
                                      cf_probe_cache=cf_probe_cache))


async def arefresh_catalog(env: dict[str, str], provider_ids: list[str] | None = None,
                           public_only: bool = False, path: Path | str | None = None, *,
                           deadline: float | None = None,
                           time_budget_seconds: float | None = None,
                           progress: Callable[..., None] | None = None) -> dict[str, Any]:
    """Bounded async twin of refresh_catalog for consumers inside a running loop.

    Server callers should pass `deadline` (absolute monotonic seconds) or
    `time_budget_seconds` (relative budget) to control the bound; when both
    are omitted the sync default policy (budget_seconds(env), 40s unless
    tuned) applies. The effective bound is the earliest of the candidates,
    so a stricter caller value always wins. There is no implicit unbounded
    mode: past/zero/negative inputs fast-defer all providers, and
    non-finite inputs raise ValueError.

    Timeout degrades to deferred rows over preserved last-good; a second
    concurrent writer gets DiscoveryBusy (probe with a small budget to fail
    fast) and can retry. These are RETURN bounds only: this call never
    touches the caller's loop or default executor, and shutdown behavior
    belongs to the caller's loop.
    """
    deadline = _finite_seconds(deadline, "deadline")
    time_budget_seconds = _finite_seconds(time_budget_seconds, "time_budget_seconds")
    now = time.monotonic()
    candidates = [c for c in (deadline, None if time_budget_seconds is None else now + time_budget_seconds)
                  if c is not None]
    if not candidates:
        candidates = [now + budget_seconds(env)]
    effective = min(candidates)
    destination, registry, requested = _prepare_refresh(env, provider_ids, public_only, path)
    return await _arefresh_impl(env, registry, requested, public_only, destination,
                                deadline=effective, progress=progress)


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


_VOLATILE_PATTERNS = (
    # ISO datetimes and build stamps: docs rebuilds must not read as policy drift.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<datetime>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    (re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
                r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
                re.IGNORECASE), "<date>"),
    (re.compile(r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
                r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}\b", re.IGNORECASE), "<date>"),
    # Clock times with an explicit zone marker; bare numbers are never touched.
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:UTC|GMT|[ECMP][SD]T)\b", re.IGNORECASE), "<time>"),
    # Relative activity stamps ("3 hours ago", "just now").
    (re.compile(r"\b\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b", re.IGNORECASE), "<ago>"),
    (re.compile(r"\bjust now\b", re.IGNORECASE), "<ago>"),
)


def _normalize_volatile_text(text: str) -> str:
    """Mask volatile stamps so rebuilds and clocks do not read as policy drift.

    Only timestamp-shaped spans are masked; every other word still feeds the
    hash, so a changed price, limit, or restriction changes the digest.
    """
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def source_digest(content: bytes, content_type: str, algorithm: str = "visible_text_v1") -> str:
    """Stable, versioned hash: all document text, with whitespace normalized.

    JavaScript-only pages with no substantive text must not receive a reviewed
    baseline. A changed visible price, limit, or restriction changes this hash.
    visible_text_v2 additionally masks volatile build/clock stamps; v1 vectors
    are pinned by test and must never change.
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
    if algorithm not in {"visible_text_v1", "visible_text_v2"}:
        raise ValueError("Unknown source digest algorithm")
    if "html" in content_type.lower():
        parser = _SourceText()
        parser.feed(decoded)
        decoded = " ".join(parser.parts)
    normalized = " ".join(decoded.split())
    if algorithm == "visible_text_v2":
        normalized = _normalize_volatile_text(normalized)
    if not normalized:
        raise ValueError("Empty policy source")
    return hashlib.sha256(normalized.encode()).hexdigest()


def check_public_sources(provider_ids: list[str] | None = None, *,
                         registry: dict[str, dict[str, Any]] | None = None,
                         time_budget_seconds: float | None = _EVIDENCE_OVERALL_SECONDS) -> dict[str, Any]:
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
    overall = None if time_budget_seconds is None else time.monotonic() + time_budget_seconds
    records = []
    with _client() as client:
        # Per-read ceiling 46s Python-phase + glibc (8 conn + 30 body + 8
        # read; per-request timeout=8, default clients only) x N=30 distinct
        # URLs; between-URL budget gate 120s fail-fasts the rest of the pass.
        for url, providers in urls.items():
            record: dict[str, Any] = {"url": url, "providers": sorted(set(providers)),
                                      "checked_at": _now(), "status": "error", "sha256": None}
            now = time.monotonic()
            if overall is not None and now >= overall:
                records.append(record)
                continue
            try:
                if not _same_origin(url, url):
                    raise ValueError("Unsupported evidence URL")
                with client.stream("GET", url, headers={"Accept": "text/html, application/json", "Accept-Encoding": ACCEPT_ENCODING}, timeout=8) as response:
                    record["http_status"] = response.status_code
                    if response.status_code == 200:
                        content = bounded_response_bytes(response, _MAX_RESPONSE_BYTES,
                                                         deadline=now + _SOURCE_TOTAL_SECONDS)
                        record["status"] = "ok"
                        record["sha256"] = source_digest(content, response.headers.get("content-type", ""))
                        record["raw_sha256"] = hashlib.sha256(content).hexdigest()
                        record["text_v2_sha256"] = source_digest(content, response.headers.get("content-type", ""),
                                                                "visible_text_v2")
                        record["hash_algorithm"] = "visible_text_v1"
                        if urlsplit(url).hostname in {"modelscope.ai", "www.modelscope.ai"} and "/posts/" in url:
                            record["article_sha256"] = source_digest(content, "text/html", "modelscope_article_v1")
                        if url == "https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317":
                            record["post_sha256"] = source_digest(content, "text/html", "discourse_first_post_v1")
            except (httpx.HTTPError, ValueError):
                pass
            records.append(record)
    return {"schema": 1, "checked_at": _now(), "sources": records,
            "note": "A source change requires human review; this check never renews policy, pricing or account evidence."}


def refresh_evidence(env: dict[str, str], provider_ids: list[str] | None = None,
                     *, path: Path | None = None, public_only: bool = False,
                     time_budget_seconds: float | None = _EVIDENCE_OVERALL_SECONDS) -> dict[str, Any]:
    """Renew only content-identical, reviewed policy for at most seven days.

    This separate public GET operation does not touch account entitlement,
    pricing/model snapshots, capabilities, or packaged grant/limit values.
    A new or changed source requires review and a new packaged baseline.
    """
    registry = load_registry() if public_only else load_registry(env, renew_evidence=False)
    checked = check_public_sources(provider_ids, registry=registry, time_budget_seconds=time_budget_seconds)
    sources = {row["url"]: row for row in checked["sources"]}
    providers = {}
    for provider_id in provider_ids if provider_ids is not None else registry:
        provider = registry[provider_id]
        rows = {}
        for evidence in provider.get("evidence", []):
            baseline = evidence.get("source_hash", {})
            source = sources[evidence["url"]]
            algorithm = baseline.get("algorithm")
            field = {"raw_body_v1": "raw_sha256", "modelscope_article_v1": "article_sha256",
                       "discourse_first_post_v1": "post_sha256", "visible_text_v2": "text_v2_sha256"}.get(algorithm, "sha256")
            observed_hash = source.get(field)
            status = "review_required"
            if source["status"] != "ok":
                status = "check_failed"
            elif (algorithm in {"visible_text_v1", "visible_text_v2", "raw_body_v1", "modelscope_article_v1",
                                "discourse_first_post_v1"}
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
    if args.providers:
        # G37: strict registry validation (mirrors cmd_update incl. the
        # public_only-aware load). One check covers refresh, --check-sources,
        # and --renew-evidence; skips the pre-check mkdir and all writes.
        from .managed_cli import _unknown_provider_error
        from .provider_registry import resolve_provider_ids
        try:
            registry = load_registry() if args.public_only else load_registry(env)
        except Exception:  # noqa: BLE001 — validation never tracebacks
            print("freellmpool: provider registry is unavailable", file=sys.stderr)
            return 2
        canonical, unknown = resolve_provider_ids(args.providers, registry)
        if unknown:
            return _unknown_provider_error(unknown, registry)
        args.providers = canonical
    start = time.monotonic()
    try:
        result = refresh_catalog(env, args.providers, public_only=args.public_only, path=args.output,
                                 deadline=start + budget_seconds(env),
                                 progress=stderr_progress_printer())
    except DiscoveryBusy:
        safe_print(_MAIN_BUSY_LINE, file=sys.stderr)
        return 2
    safe_print(discovery_summary_line(result, time.monotonic() - start), file=sys.stderr)
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
