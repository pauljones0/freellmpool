"""Maintained routing core: immutable discovery, free admission, atomic quotas.

The legacy Pool remains a low-level compatibility API for explicitly constructed
providers. Normal configuration/CLI/proxy/MCP access uses this core. Existing wire
adapters are reused; they receive a single-attempt, reservation-owning transport.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import tomllib
import wave
from collections import defaultdict
from collections.abc import Awaitable, Callable, Generator, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack, cast

from . import client
from .allowances import AllowanceDenied, AllowanceLedger, Limit, default_allowance_path
from .cache import Cache
from .config import effective_env, finite_float, load_catalog, settings
from .conformance import (
    FEATURE_VISION,
    ConformanceStore,
    default_conformance_path,
    required_features,
)
from .context import estimate_input_tokens
from .errors import (
    AllProvidersExhausted,
    ContextWindowExceeded,
    ProviderHTTPError,
    StructuredOutputError,
)
from .free_policy import admit, credential_fingerprint, fresh, load_accounts, timestamp
from .key_rotation import ROTATE_STATUSES, KeyRotator, cool_delay
from .media import check_image_url, image_input_tokens
from .metrics import Metrics
from .models import EmbedReply, Model, Provider, Reply, TranscribeReply
from .observe import EventHook, emit
from .prefixcache import cached_prompt_tokens as _cached_prompt_tokens
from .provider_registry import reviewed_limit_capacity
from .quota import QuotaStore
from .route_health import RouteHealthStore, default_route_health_path
from .router import Pool, Target, _is_account_quota_exhaustion
from .routing_modes import normalize_routing_mode
from .stats import StatsStore

JSON = dict[str, Any]  # Validated provider/config/protocol schema boundaries.
Modality = Literal["chat", "embedding", "transcription"]
ManagedStream = Generator[JSON | str, None, None]
StreamOpened = tuple[int, Iterable[str]] | tuple[int, JSON, Iterable[str]]
AsyncPost = Callable[[str, dict[str, str], JSON, float], Awaitable[client.HTTPResult]]


class PoolOptions(TypedDict, total=False):
    quota: QuotaStore | None
    cooldown_seconds: float
    embedders: list[Provider] | None
    transcribers: list[Provider] | None
    transcribe_post: client.MultipartPostFn
    cache: Cache | None
    metrics: Metrics | None
    routing: str
    on_event: EventHook | None
    stats_store: StatsStore | None
    route_health: RouteHealthStore | None
    conformance: ConformanceStore | None


class CallOptions(TypedDict, total=False):
    model: str | None
    providers: Iterable[str] | None
    max_tokens: int
    temperature: float
    timeout: float
    tools: list[JSON] | None
    tool_choice: JSON | str | None
    response_format: JSON | str | None
    protocol: str | None
    routing: str | None
    task: str | None
    language: str | None
    redact: bool
    private: bool


class AttemptState(TypedDict, total=False):
    reservation: str
    reserved_amounts: dict[str, float]
    usage: JSON
    active_key: str | None
    active_slot: int


@dataclass(frozen=True)
class Route:
    provider: Provider
    model: str
    metadata: JSON
    policy: JSON
    grant: JSON
    account: JSON
    modality: Modality
    automatic: bool
    limits: tuple[Limit, ...]
    env: dict[str, str] = field(repr=False, compare=False)

    @property
    def name(self) -> str:
        return f"{self.provider.id}/{self.model}"

    @property
    def account_scope(self) -> str:
        return f"{self.provider.id}:{self.account.get('account_ref') or 'primary'}"

    @property
    def model_scope(self) -> str:
        return f"{self.account_scope}:model:{self.model}"


@dataclass(frozen=True)
class Snapshot:
    generation: str
    routes: tuple[Route, ...]
    providers: tuple[JSON, ...]


class _AccountingError(ValueError):
    """Locally authored, credential-free diagnostics safe for the client."""


def _automatic_tool_conflict(provider_id: str, model: str, grant: JSON) -> str:
    # Compound enables server-side tools by default. Its documented allowlist
    # does not establish empty-list semantics, so we cannot promise no tools.
    # https://console.groq.com/docs/compound/built-in-tools
    # GPT-OSS tools require an explicit tools entry and do not share this default.
    if provider_id == "groq" and model in {"groq/compound", "groq/compound-mini"}:
        prohibited = grant.get("prohibited_addons", [])
        if "web_search" in prohibited or "paid_tools" in prohibited:
            return "automatic built-in tools conflict with this grant's excluded add-ons; no verified tool-disable contract"
    return ""


def _stream_usage_reply(route: Route, usage: object) -> Reply:
    """A usage-carrying reply for stream stats (same validation as settle)."""
    counters: dict[str, int] = {}
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
                counters[key] = value
    return Reply(text="", provider_id=route.provider.id, model=route.model, raw={},
                 prompt_tokens=counters.get("prompt_tokens"),
                 completion_tokens=counters.get("completion_tokens"))


class ManagedPool(Pool):
    """Strict-free default runtime; an exact model selection never bypasses policy."""

    managed = True
    _cancelled: threading.Event | None

    def __init__(self, providers: list[Provider] | None = None, *, registry: dict[str, JSON] | None = None,
                 discovery: JSON | None = None, accounts: dict[str, JSON] | None = None,
                 ledger: AllowanceLedger | None = None, env: dict[str, str] | None = None,
                 post: client.PostFn = client.default_post, stream_post: client.StreamPostFn = client.default_stream_post,
                 clock: Callable[[], float] = time.time, **kwargs: Unpack[PoolOptions]) -> None:
        self._base_env = dict(os.environ if env is None else env)
        env = effective_env(self._base_env)
        self._catalog_override = providers
        self._registry_override = registry
        self._discovery_override = discovery
        self._accounts_override = accounts
        self._wall_clock = clock
        self.ledger = ledger or AllowanceLedger(default_allowance_path(env), clock=clock)
        self._snapshot_lock = threading.RLock()
        self._sequence_lock = threading.Lock()
        self._last_used: dict[str, int] = {}
        self._request_sequence = [0]  # shared by asynchronous worker copies
        self._key_rotator = KeyRotator()
        kwargs.setdefault("stats_store", StatsStore())
        kwargs.setdefault("conformance", ConformanceStore(default_conformance_path(env)))
        kwargs.setdefault("route_health", RouteHealthStore(path=default_route_health_path(env)))
        super().__init__(providers or [], env=env, post=post, stream_post=stream_post, **kwargs)
        self.snapshot()

    @classmethod
    def from_default_config(cls, *, env: dict[str, str] | None = None, quota: QuotaStore | None = None,
                            post: client.PostFn = client.default_post, on_event: EventHook | None = None) -> ManagedPool:
        request_env = effective_env(env)
        ttl = finite_float(request_env.get("FREELLMPOOL_CACHE_TTL") or settings(request_env).get("cache_ttl", 0) or 0,
                           0.0, minimum=0.0)
        return cls(env=env, quota=quota, post=post, on_event=on_event,
                   cache=Cache(ttl) if ttl > 0 else None)

    def _operator_rows(self, env: dict[str, str] | None = None) -> dict[tuple[str, str], Provider]:
        if self._catalog_override is not None:
            return {("chat", p.id): p for p in self._catalog_override}
        try:
            path = Path((self.env if env is None else env).get("FREELLMPOOL_CONFIG") or
                        Path.home() / ".config/freellmpool/providers.toml").expanduser()
        except RuntimeError as exc:
            raise ValueError("cannot resolve local catalog path") from exc
        if not path.exists():
            return {}
        from .catalog_validation import _raw_catalog_errors
        from .config import _parse_rows
        if _raw_catalog_errors(path):
            raise ValueError("invalid local catalog restrictions")
        data = tomllib.loads(path.read_text())
        return {(modality, p.id): p for section, modality in
                (("provider", "chat"), ("embedder", "embedding"), ("transcriber", "transcription"))
                for p in _parse_rows(data.get(section, []))}

    def snapshot(self) -> Snapshot:
        """Each caller receives one immutable generation; no network work occurs."""
        from .discovery import load_discovery
        from .provider_registry import load_registry
        request_env = effective_env(self._base_env)
        registry = copy.deepcopy(self._registry_override) if self._registry_override is not None else load_registry(env=request_env)
        discovery = copy.deepcopy(self._discovery_override) if self._discovery_override is not None else load_discovery(request_env)
        accounts = copy.deepcopy(self._accounts_override) if self._accounts_override is not None else load_accounts(request_env)
        try:
            raw = self._catalog_override if self._catalog_override is not None else load_catalog()
            raw_by_id = {p.id: p for p in raw}
            operator = self._operator_rows(request_env)
        except (OSError, ValueError, TypeError, RuntimeError):
            result = Snapshot("invalid-config", (), ({"id": "configuration", "reason": "invalid local restrictions; repair providers.toml", "eligible": 0},))
            with self._snapshot_lock:
                self.providers = []
                self.embedders = []
                self.transcribers = []
                self._current_snapshot = result
            return result
        now = self._wall_clock()
        routes: list[Route] = []
        statuses: list[JSON] = []
        max_age = finite_float(request_env.get("FREELLMPOOL_CATALOG_MAX_AGE_SECONDS", "172800"),
                               172800.0, minimum=60.0, maximum=604800.0)
        for pid, spec in registry.items():
            old = raw_by_id.get(pid)
            key_env = spec.get("credential_env") or (old.key_env if old else None)
            anonymous = spec.get("inference_auth") == "none"
            if anonymous:
                key_env = None
            base = spec["api_base_url"]
            adapter = "cloudflare" if pid == "cloudflare" else "openai"
            if pid == "gemini" and not base.endswith("/openai"):
                base += "/openai"
            provider = Provider(pid, spec.get("display_name", pid), adapter, base, (), key_env=key_env,
                                auth=("none" if anonymous else old.auth if old is not None else ("bearer" if key_env else "none")),
                                key_optional=old.key_optional if old is not None else False,
                                extra_env=old.extra_env if old is not None else ())
            row = discovery.get("providers", {}).get(pid, {})
            checked = timestamp(row.get("checked_at"))
            reason = ""
            if not provider.is_configured(request_env):
                reason = "API key or required account field missing"
            elif row.get("complete") is not True:
                reason = "complete model discovery needed"
            elif checked is None or checked > now or now - checked > max_age:
                reason = "model discovery expired; run freellmpool update"
            account = accounts.get(pid, {})
            provider_routes: list[Route] = []
            exclusions: defaultdict[str, int] = defaultdict(int)
            slot_keys = provider.api_keys(request_env) or (provider.api_key(request_env),)
            slot_refs = [credential_fingerprint(pid, key) for key in slot_keys]
            for metadata in row.get("models", []):
                if not isinstance(metadata, dict) or not isinstance(metadata.get("id"), str):
                    continue
                model_id = metadata["id"]
                for modality in metadata.get("modalities", []):
                    if modality not in {"chat", "embedding", "transcription"}:
                        continue
                    # A route is usable when ANY key slot is admitted, so a dead
                    # slot-1 key never hides a healthy slot 2 from rotation.
                    allowed = None
                    admitted_ref = slot_refs[0]
                    for ref in slot_refs:
                        candidate = admit(spec, metadata, account, modality=modality, now=now,
                                          credential_ref=ref)
                        if allowed is None:
                            allowed = candidate
                        if candidate.allowed:
                            allowed, admitted_ref = candidate, ref
                            break
                    assert allowed is not None  # slot_refs is never empty
                    local = operator.get((modality, pid))
                    restriction = local.model(model_id) if local else None
                    local_reason = ""
                    if local is not None and not local.models:
                        local_reason = "provider disabled by local restrictions"
                    if restriction is not None and not restriction.enabled:
                        local_reason = "model disabled by local restrictions"
                    tool_reason = (_automatic_tool_conflict(pid, model_id, cast(JSON, allowed.grant))
                                   if allowed.allowed else "")
                    if reason or local_reason or tool_reason or not allowed.allowed:
                        exclusions[reason or local_reason or tool_reason or allowed.reason] += 1
                        continue
                    try:
                        limits = self._limits(spec, cast(JSON, allowed.grant), model_id, account,
                                              credential_ref=admitted_ref)
                    except (ValueError, TypeError):
                        exclusions["invalid allowance rule; update/review provider"] += 1
                        continue
                    model_metadata = dict(metadata)
                    model_metadata["catalog_checked_at"] = checked
                    model_metadata["catalog_expires_at"] = cast(float, checked) + max_age
                    if restriction is not None and restriction.context:
                        discovered_context = model_metadata.get("context")
                        model_metadata["context"] = min(restriction.context, discovered_context) if discovered_context else restriction.context
                    route = Route(provider, model_id, model_metadata, spec, cast(JSON, allowed.grant), account,
                                  modality, restriction.auto if restriction else True, limits, request_env)
                    provider_routes.append(route)
            routes.extend(provider_routes)
            statuses.append({"id": pid, "eligible": len(provider_routes), "configured": provider.is_configured(request_env),
                             "checked_at": row.get("checked_at"), "last_attempt_at": row.get("last_attempt_at"),
                             "discovery_status": row.get("status", "not_checked"),
                             "reason": reason or ("ready" if provider_routes else next(iter(exclusions), "no eligible models")),
                             "exclusions": dict(exclusions), "account_tier": account.get("tier", "unknown")})
        generation = hashlib.sha256(json.dumps([discovery.get("generation"), statuses, [
            {"route": r.name, "modality": r.modality, "metadata": r.metadata,
             "policy": r.policy, "account": r.account, "automatic": r.automatic,
             "limits": [asdict(limit) for limit in r.limits],
             "credential_ref": credential_fingerprint(r.provider.id, "\0".join(r.provider.api_keys(r.env)))}
            for r in routes]], sort_keys=True).encode()).hexdigest()[:16]
        result = Snapshot(generation, tuple(routes), tuple(statuses))
        with self._snapshot_lock:
            self.env = request_env
            for attr, modality in (("providers", "chat"), ("embedders", "embedding"), ("transcribers", "transcription")):
                grouped: defaultdict[str, list[Route]] = defaultdict(list)
                for route in routes:
                    if route.modality == modality:
                        grouped[route.provider.id].append(route)
                setattr(self, attr, [replace(values[0].provider, models=tuple(
                    Model(r.model, enabled=True, auto=r.automatic, context=r.metadata.get("context")) for r in values
                )) for values in grouped.values()])
            self._current_snapshot = result
        return result

    def _limits(self, spec: JSON, grant: JSON, model: str, account: JSON,
                *, credential_ref: str | None = None) -> tuple[Limit, ...]:
        account_ref = account.get("account_ref") or "primary"
        root = f"{spec['id']}:{account_ref}"
        rules = []
        account_current = (credential_ref is not None and account.get("credential_ref") == credential_ref
                           and fresh(account.get("verified_at"), account.get("expires_at"), self._wall_clock()))
        account_limits = account.get("limits", [])
        if not isinstance(account_limits, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or not isinstance(row.get("capacity"), (int, float)) or isinstance(row.get("capacity"), bool)
            or not math.isfinite(row["capacity"]) or row["capacity"] < 0
            or not isinstance(row.get("model_ids", []), list)
            or any(not isinstance(model_id, str) for model_id in row.get("model_ids", []))
            for row in account_limits
        ):
            raise _AccountingError("invalid account allowance restriction")
        for rule in spec.get("limits", []):
            if rule.get("grant_ids") and grant["id"] not in rule["grant_ids"]:
                continue
            if rule.get("model_ids") and model not in rule["model_ids"]:
                continue
            self._check_evidence(spec, rule.get("evidence_ids", []))
            scope = rule.get("scope", "account")
            scope_root = f"{spec['id']}:ip" if scope in {"ip", "ip_model"} else root
            prefix = f"{scope_root}:model:{model}" if scope in {"model", "ip_model"} or "model_id" in rule.get("scope_keys", []) else scope_root
            capacity = reviewed_limit_capacity(rule, model)
            if spec["id"] == "openrouter" and rule["id"] == "rpd":
                credit = account.get("lifetime_purchased_credits", 0)
                if isinstance(credit, (int, float)) and not isinstance(credit, bool) and math.isfinite(credit) and credit >= 10 and account_current:
                    capacity = 1000
            if account_current:
                for override in account_limits:
                    if override.get("id") != rule["id"] or (override.get("model_ids") and model not in override["model_ids"]):
                        continue
                    observed_cap = override.get("capacity")
                    if isinstance(observed_cap, (int, float)) and not isinstance(observed_cap, bool) and math.isfinite(observed_cap) and observed_cap >= 0:
                        capacity = observed_cap if capacity is None else min(capacity, observed_cap)
            algorithm = rule.get("algorithm")
            algorithm = {"calendar_day": "day", "calendar_month": "month"}.get(algorithm, algorithm)
            # Unknown reset details use an explicitly conservative rolling ceiling
            # if a duration and cap are known. They are not called provider resets.
            if algorithm == "unknown" and capacity is not None and rule.get("window_seconds"):
                algorithm = "rolling"
            if capacity is None or algorithm == "unknown":
                rules.append(Limit(f"{prefix}:{rule['id']}", rule["metric"], None, algorithm="observed"))
                continue
            anchor = rule.get("anchor")
            if anchor in {"signup_at", "first_use_at"}:
                anchor = account.get(anchor)
            if algorithm in {"day", "month", "anniversary_month"} and not rule.get("timezone"):
                # A longer rolling window enforces a conservative ceiling while
                # leaving the provider's undocumented reset boundary unknown.
                seconds = (25 if algorithm == "day" else 32 * 24) * 3600
                rules.append(Limit(f"{prefix}:{rule['id']}", rule["metric"], capacity, seconds=seconds))
                continue
            rules.append(Limit(f"{prefix}:{rule['id']}", rule["metric"], capacity,
                               seconds=rule.get("window_seconds") or 60, algorithm=algorithm,
                               timezone=rule.get("timezone") or "UTC", anchor=anchor,
                               refill_per_second=rule.get("refill_per_second")))
        # These are labelled operator pacing, not invented provider allowances.
        rules.extend((Limit(f"{root}:operator_pacing", "requests", 10, seconds=60),
                      Limit(f"{root}:inflight", "requests", 1, algorithm="concurrency")))
        return tuple(rules)

    def _check_evidence(self, spec: JSON, references: Iterable[str]) -> None:
        evidence = {row["id"]: row for row in spec.get("evidence", [])}
        for name in references:
            row = evidence.get(name, {})
            if row.get("status") not in {"verified", "observed", "official"} or not fresh(row.get("checked_at"), row.get("expires_at"), self._wall_clock()):
                raise _AccountingError("allowance or cost evidence needs review")

    def _candidates(self, snapshot: Snapshot, modality: Modality, model: str | None,
                    providers: Iterable[str] | None, messages: list[JSON] | None = None,
                    tools: list[JSON] | None = None, response_format: JSON | str | None = None,
                    protocol: str | None = None, probe: bool = False, max_tokens: int = 0,
                    stream: bool = False, private: bool = False) -> list[Route]:
        from . import privacy as privacy_mod

        include = set(providers or [])
        if model in {"auto", "free", "free-coding", "free-fast", "free-quality"}:
            model = None
        routes = [r for r in snapshot.routes if r.modality == modality and
                  (not include or r.provider.id in include) and
                  (r.model == model if model else r.automatic)]
        if private:
            eligible = [r for r in routes
                        if privacy_mod.training_policy(r.provider.id) == "api-no-train"]
            if not eligible and routes:
                raise AllProvidersExhausted(
                    [(r.name, f"excluded by private mode ({privacy_mod.training_policy(r.provider.id)})")
                     for r in routes],
                    client_status=400,
                    client_message=("Private mode admits only api-no-train providers; no eligible "
                                    "route remains. Relax filters or run freellmpool status."))
            routes = eligible
        features: Iterable[str] = required_features(messages, tools=tools, response_format=response_format)
        if stream:
            features = (*features, "streaming")
        # Responses/Anthropic are local tested translations, not an upstream model
        # capability. Tool/vision/JSON behavior is independently verified.
        if features and not probe:
            routes = [r for r in routes if self.conformance is not None and
                      self.conformance.passes(r.provider, r.model, features)]
        estimate = estimate_input_tokens(messages, tools) + max_tokens
        too_small = [r for r in routes if r.metadata.get("context") and estimate > r.metadata["context"]]
        routes = [r for r in routes if r not in too_small]
        if not routes:
            if too_small:
                raise ContextWindowExceeded([(r.name, "context window too small") for r in too_small], est_tokens=estimate)
            if FEATURE_VISION in features and not probe:
                raise AllProvidersExhausted([], client_status=400,
                                            client_message="No vision-verified free route is available for this image request. Run freellmpool verify --features vision.")
            raise AllProvidersExhausted([], client_status=403,
                                        client_message="No current free route matches these filters/capabilities. Run freellmpool setup or verify.")
        with self._sequence_lock:
            ordered = sorted(routes, key=lambda r: (self._last_used.get(r.provider.id, 0), self._last_used.get(r.name, 0)))
        # One model from each provider before siblings avoids a large catalog
        # monopolizing a bounded failover deadline.
        first: list[Route] = []
        later: list[Route] = []
        seen: set[str] = set()
        for route in ordered:
            (later if route.provider.id in seen else first).append(route)
            seen.add(route.provider.id)
        return first + later

    def _cost(self, route: Route, body: object) -> dict[str, float]:
        if isinstance(body, dict):
            image_tokens = 0
            scrubbed: list[object] = []
            for message in body.get("messages", []):
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    scrubbed.append(message)
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") not in {"text", "input_text", "image_url"}:
                        raise _AccountingError("Chat media needs a verified token/cost bound; currently use text, images, and function tools.")
                parts = []
                for block in content:
                    if block.get("type") == "image_url":
                        url = (block.get("image_url") or {}).get("url") or ""
                        check_image_url(url)
                        image_tokens += image_input_tokens(url)
                        parts.append({**block, "image_url": {}})
                    else:
                        parts.append(block)
                scrubbed.append({**message, "content": parts} if isinstance(message, dict) else message)
            output = body.get("max_tokens", body.get("max_completion_tokens", 0)) or 0
            if not isinstance(output, int) or isinstance(output, bool) or output < 0:
                raise ValueError("invalid output token budget")
            inputs = {key: value for key, value in body.items() if key not in {"max_tokens", "max_completion_tokens", "stream"}}
            if image_tokens:
                inputs = {**inputs, "messages": scrubbed}
            input_tokens = len(json.dumps(inputs, ensure_ascii=False).encode("utf-8")) + 32 + image_tokens
        else:
            input_tokens, output = 0, 0
        amounts: dict[str, float] = {"requests": 1, "input_tokens": input_tokens, "output_tokens": output,
                   "total_tokens": input_tokens + output, "tokens": input_tokens + output}
        context = route.metadata.get("context")
        if context and input_tokens + output > context:
            raise ContextWindowExceeded([(route.name, "final payload exceeds context window")], est_tokens=input_tokens + output)
        costs = route.policy.get("model_costs", {}).get(route.model, {})
        self._check_evidence(route.policy, costs.get("evidence_ids", []))
        if "neurons_per_input_token" in costs and "neurons_per_output_token" in costs:
            amounts["neurons"] = math.ceil(Decimal(str(costs["neurons_per_input_token"])) * input_tokens +
                                           Decimal(str(costs["neurons_per_output_token"])) * output)
        elif "neurons_per_input_token" in costs:
            # Input-only rates (e.g. embeddings) have no output tokens to price.
            amounts["neurons"] = math.ceil(Decimal(str(costs["neurons_per_input_token"])) * input_tokens)
        prices = route.metadata.get("pricing", {})
        if "input" in prices and "output" in prices:
            amounts["micro_usd"] = math.ceil((Decimal(str(prices["input"])) * input_tokens +
                                               Decimal(str(prices["output"])) * output) * 1_000_000)
        # Unknown monetary grants are server-hard-stop only; their observed
        # quantity stays unknown rather than translating tokens into dollars.
        for limit in route.limits:
            if limit.unit not in amounts and limit.capacity is None:
                amounts[limit.unit] = 0
        return amounts

    def _actual_cost(self, route: Route, body: object, reserved: Mapping[str, float]) -> dict[str, float]:
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        if not isinstance(usage, dict):
            return {"requests": 1}
        # Reject malformed or unrepresentable counters without refunding the
        # corresponding reservation. Aggregate totals can include server work
        # absent from prompt/completion counts (for example, internal rounds).
        counts = {key: value for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                  if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
                  and 0 <= value <= 2**63 - 1}
        prompt, completion = counts.get("prompt_tokens"), counts.get("completion_tokens")
        # Provider-confirmed cached prefix tokens cost nothing: the provider
        # served them from its prefix cache, so only the delta consumes token
        # allowances. Unconfirmed or malformed claims deduct zero.
        cached = _cached_prompt_tokens(usage, prompt_tokens=prompt)
        billed_prompt = (prompt - cached) if prompt is not None else None
        actual: dict[str, float] = {"requests": 1}
        if billed_prompt is not None:
            actual["input_tokens"] = billed_prompt
        if completion is not None:
            actual["output_tokens"] = completion
        if "total_tokens" in counts:
            total = max(counts["total_tokens"] - cached, (billed_prompt or 0) + (completion or 0))
            actual.update(total_tokens=total, tokens=total)
        elif "total_tokens" not in usage and billed_prompt is not None and completion is not None:
            actual.update(total_tokens=billed_prompt + completion, tokens=billed_prompt + completion)
        else:
            # Incomplete usage cannot justify a refund, but any known part
            # above the estimate must still consume its allowance.
            known_tokens = (billed_prompt or 0) + (completion or 0)
            for unit in ("total_tokens", "tokens"):
                actual[unit] = max(reserved.get(unit, 0), known_tokens)
        if prompt is not None or completion is not None:
            costs = route.policy.get("model_costs", {}).get(route.model, {})
            if "neurons_per_input_token" in costs and "neurons_per_output_token" in costs:
                neurons = math.ceil(Decimal(str(costs["neurons_per_input_token"])) * (prompt or 0) +
                                    Decimal(str(costs["neurons_per_output_token"])) * (completion or 0))
                actual["neurons"] = (neurons if prompt is not None and completion is not None
                                     else max(reserved.get("neurons", 0), neurons))
        return actual

    def _headers(self, route: Route, headers: Mapping[str, object] | None, reservation: str | None = None) -> None:
        normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if route.provider.id == "modelscope":
            for family, rule_id in (("requests", "daily_account"), ("model-requests", "daily_model")):
                try:
                    remaining = float(normalized[f"modelscope-ratelimit-{family}-remaining"])
                    capacity = float(normalized[f"modelscope-ratelimit-{family}-limit"])
                    if not 0 <= remaining <= capacity or not math.isfinite(capacity):
                        continue
                    for limit in route.limits:
                        if limit.key.endswith(f":{rule_id}"):
                            # No official reset header/timezone: retain this
                            # tighter observation for a conservative 25h horizon.
                            self.ledger.observe_remaining(limit.key, remaining,
                                                          reset_at=self._wall_clock() + 25 * 3600,
                                                          reservation=reservation)
                except (KeyError, ValueError, TypeError):
                    continue
        if route.provider.id != "groq":
            return
        for family, rule_id in (("requests", "rpd"), ("tokens", "tpm")):
            raw = normalized.get(f"x-ratelimit-remaining-{family}")
            reset = normalized.get(f"x-ratelimit-reset-{family}")
            if raw is None or reset is None:
                continue
            try:
                remaining = float(raw)
                duration = _duration(reset)
                for limit in route.limits:
                    if limit.key.endswith(f":{rule_id}"):
                        self.ledger.observe_remaining(limit.key, remaining,
                                                      reset_at=self._wall_clock() + duration,
                                                      reservation=reservation)
            except (ValueError, TypeError):
                continue

    def _post_for(self, route: Route, state: AttemptState) -> client.PostFn:
        def post(url: str, headers: dict[str, str], body: JSON, timeout: float) -> client.HTTPResult:
            limits = self._recheck(route, state)
            # Last opportunity to constrain provider-specific paid additions.
            if route.provider.id == "openrouter":
                body = dict(body)
                body["provider"] = {"max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}, "allow_fallbacks": True}
            if any(key in body for key in ("plugins", "web_search_options", "service_tier")):
                raise ValueError("billable built-in tools/service options are excluded")
            if any(tool.get("type") != "function" for tool in body.get("tools", []) if isinstance(tool, dict)):
                raise ValueError("only caller-executed function tools are supported")
            state["reserved_amounts"] = self._cost(route, body)
            state["reservation"] = self.ledger.reserve(limits, state["reserved_amounts"],
                                                      scopes=[route.account_scope, route.model_scope], ttl=timeout + 5)
            # A transport failure cannot prove the server stopped working. Keep
            # its concurrency lease until expiry and its full usage reservation.
            response = (client.default_post(url, headers, body, timeout, max_attempts=1)
                        if self._post is client.default_post else self._post(url, headers, body, timeout))
            self._headers(route, response.headers, state["reservation"])
            self.ledger.settle(state["reservation"], self._actual_cost(route, response.body, state["reserved_amounts"])
                               if response.status == 200 else None)
            return response
        return post

    def _stream_for(self, route: Route, state: AttemptState) -> client.StreamPostFn:
        def stream(url: str, headers: dict[str, str], body: JSON, timeout: float) -> StreamOpened:
            limits = self._recheck(route, state)
            if route.provider.id == "openrouter":
                body = dict(body, provider={"max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}})
            state["reserved_amounts"] = self._cost(route, body)
            state["reservation"] = self.ledger.reserve(limits, state["reserved_amounts"],
                                                      scopes=[route.account_scope, route.model_scope], ttl=timeout + 5)
            opened = self._stream_post(url, headers, body, timeout)
            if len(opened) == 3:
                self._headers(route, opened[1], state["reservation"])
            if opened[0] >= 400:
                self.ledger.settle(state["reservation"])
            lines = opened[-1]
            def observed_lines() -> Generator[str, None, None]:
                try:
                    for line in lines:
                        if isinstance(line, str) and line.startswith("data:") and len(line) <= client._MAX_RESPONSE_BYTES:
                            try:
                                value = json.loads(line[5:].strip())
                                if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                                    state["usage"] = value["usage"]
                            except (ValueError, RecursionError):
                                pass
                        yield line
                finally:
                    getattr(lines, "close", lambda: None)()
            if len(opened) == 3:
                return opened[0], opened[1], observed_lines()
            return opened[0], observed_lines()
        return stream

    def _recheck(self, route: Route, state: AttemptState | None = None) -> tuple[Limit, ...]:
        self._check_cancelled()
        now = self._wall_clock()
        # Bind admission to the key actually being spent, so a rotated slot
        # never borrows slot 1's account limits: a non-primary key simply
        # fails the account-current check and falls back to policy limits.
        active = state.get("active_key") if state else None
        if active is None and (state is None or "active_key" not in state):
            active = route.provider.api_key(route.env)
        result = admit(route.policy, route.metadata, route.account, modality=route.modality, now=now,
                       credential_ref=credential_fingerprint(route.provider.id, active))
        if not result.allowed or now >= route.metadata["catalog_expires_at"]:
            raise ValueError("free eligibility expired before dispatch")
        if conflict := _automatic_tool_conflict(route.provider.id, route.model, cast(JSON, result.grant)):
            raise ValueError(conflict)
        return self._limits(route.policy, route.grant, route.model, route.account,
                            credential_ref=credential_fingerprint(route.provider.id, active))

    def _check_cancelled(self) -> None:
        cancelled: threading.Event | None = getattr(self, "_cancelled", None)
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError()

    def _multipart_for(self, route: Route, state: AttemptState) -> client.MultipartPostFn:
        def multipart(url: str, headers: dict[str, str], files: JSON, data: JSON, call_timeout: float) -> client.HTTPResult:
            limits = self._recheck(route, state)
            amounts = self._cost(route, data)
            if any(limit.unit == "audio_seconds" for limit in limits):
                try:
                    with wave.open(io.BytesIO(files["file"][1]), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                except (wave.Error, EOFError, ZeroDivisionError):
                    raise _AccountingError("Audio allowance requires measurable PCM WAV input.") from None
                minimum = route.policy.get("model_costs", {}).get(route.model, {}).get("minimum_audio_seconds", 0)
                amounts["audio_seconds"] = max(math.ceil(duration), minimum)
            state["reserved_amounts"] = amounts
            state["reservation"] = self.ledger.reserve(limits, amounts,
                                                      scopes=[route.account_scope, route.model_scope], ttl=call_timeout + 5)
            if self._transcribe_post is client.default_multipart_post:
                response = client.default_multipart_post(url, headers, files, data, call_timeout, max_attempts=1)
            else:
                response = self._transcribe_post(url, headers, files, data, call_timeout)
            self._headers(route, response.headers, state["reservation"])
            self.ledger.settle(state["reservation"], self._actual_cost(route, response.body, state["reserved_amounts"])
                               if response.status == 200 else None)
            return response
        return multipart

    def _key_trials_for_route(self, route: Route) -> tuple[list[tuple[int, str | None]], list[int]]:
        """Usable (slot, key) trials in sticky order plus skipped-as-unadmitted slots.

        Keyless routes get one None trial. Slots whose credential is not
        admitted are skipped without spending them; when none qualifies,
        the sticky slot is tried anyway so the failure stays honest.
        """
        keys = route.provider.api_keys(route.env)
        if not keys:
            return [(-1, None)], []
        now = self._wall_clock()
        slots = self._key_rotator.usable_slots(route.provider.id, len(keys), now)
        if not slots:  # every slot cooling: try the sticky slot anyway, let it fail honestly
            slots = [self._key_rotator.usable_slots(route.provider.id, len(keys), float("inf"))[0]]
        trials: list[tuple[int, str | None]] = []
        skipped: list[int] = []
        for slot in slots:
            ref = credential_fingerprint(route.provider.id, keys[slot])
            if admit(route.policy, route.metadata, route.account,
                     modality=route.modality, now=now, credential_ref=ref).allowed:
                trials.append((slot, keys[slot]))
            else:
                skipped.append(slot)
        if not trials:
            slot = slots[0]
            return [(slot, keys[slot])], skipped[1:] if skipped else []
        return trials, skipped

    def _cool_key_slot(self, provider_id: str, slot: int, count: int, exc: ProviderHTTPError) -> None:
        """Cool one slot; ``count`` is the full configured key count (not trials)."""
        now = self._wall_clock()
        self._key_rotator.cool(provider_id, slot, now + cool_delay(exc.status, exc.retry_after))
        self._key_rotator.advance(provider_id, count)

    def _failure(self, route: Route, exc: Exception) -> float | None:
        if isinstance(exc, AllowanceDenied):
            return exc.retry_after
        if isinstance(exc, (ValueError, ContextWindowExceeded)):
            return None
        delay, scope = 15.0, route.model_scope
        if isinstance(exc, ProviderHTTPError):
            if _is_account_quota_exhaustion(exc, route.provider.id):
                delay, scope = max(900, exc.retry_after or 0), route.account_scope
            elif exc.status == 429:
                delay = max(1, exc.retry_after or 60)
                if route.policy.get("rate_limit_scope") != "model" and route.provider.id not in {"groq", "cohere"}:
                    scope = route.account_scope
            elif exc.status in {401, 403}:
                delay, scope = 300, route.account_scope
            elif exc.status == 404:
                delay = 300
        self.ledger.block(scope, self._wall_clock() + delay)
        self.metrics.record_failure(route.name, type(exc).__name__)
        return delay

    def _success(self, route: Route, reply: Reply | EmbedReply | TranscribeReply | None, started: float) -> None:
        self.metrics.record_success(route.name, (time.monotonic() - started) * 1000)
        self.quota.record(route.provider.id, route.model)
        cached = getattr(reply, "cached_prompt_tokens", 0) or 0
        self._bump_stats(requests=1, prompt_tokens=getattr(reply, "prompt_tokens", 0) or 0,
                         completion_tokens=getattr(reply, "completion_tokens", 0) or 0,
                         prefix_cache_hits=1 if cached > 0 else 0,
                         prefix_tokens_avoided=cached)
        with self._sequence_lock:
            self._request_sequence[0] += 1
            self._last_used[route.name] = self._last_used[route.provider.id] = self._request_sequence[0]

    def _run(self, modality: Modality, payload: list[JSON] | list[str] | bytes, *, model: str | None = None,
             providers: Iterable[str] | None = None, max_tokens: int = 1024, temperature: float = 0,
             timeout: float = 90, tools: list[JSON] | None = None, tool_choice: JSON | str | None = None,
             response_format: JSON | str | None = None, protocol: str | None = None,
             probe: bool = False, stream: bool = False, filename: str | None = None,
             language: str | None = None, redact: bool = False, private: bool = False,
             **unused: object) -> Reply | EmbedReply | TranscribeReply | ManagedStream:
        from . import privacy as privacy_mod

        redactions: list[str] = []
        if redact and modality == "chat" and not probe:
            payload, redactions = privacy_mod.redact_messages(cast(list[Any], payload))
        snapshot = self.snapshot()
        candidates = self._candidates(snapshot, modality, model, providers,
                                      cast(list[JSON], payload) if modality == "chat" else None, tools, response_format,
                                      protocol, probe, max_tokens if modality == "chat" else 0, stream=stream,
                                      private=private)
        cache_key: str | None = None
        if modality == "chat" and not stream and not probe and self._cache is not None:
            # Probes are canary traffic: they must always reach the provider so
            # a cached success can never mask an outage from health checks.
            routing = unused.get("routing")
            cache_key = self._cache.make_key(
                payload, model, list(providers) if providers else None, max_tokens,
                temperature, tools, tool_choice,
                normalize_routing_mode(routing if isinstance(routing, str) else None, self.routing),
                response_format=response_format, protocol=protocol,
                task=unused.get("task") if isinstance(unused.get("task"), str) else None,
            )
            hit = self._cache.get(cache_key)
            features = required_features(cast(list[JSON], payload), tools=tools,
                                         response_format=response_format)
            if hit is not None and (not features or self.conformance is None or any(
                    r.provider.id == hit.get("provider_id") and r.model == hit.get("model")
                    for r in candidates)):
                emit(self._on_event, "cache_hit", key=cache_key)
                self._bump_stats(cache_hits=1)
                return Reply(text=hit.get("text", ""), provider_id=hit.get("provider_id", "cache"),
                             model=hit.get("model", "?"), raw={},
                             prompt_tokens=hit.get("prompt_tokens"),
                             completion_tokens=hit.get("completion_tokens"),
                             message=hit.get("message"), cached=True, attempts=0)
            emit(self._on_event, "cache_miss", key=cache_key)
        attempts: list[tuple[str, str]] = []
        deadlines: list[float] = []
        failures: list[Exception] = []
        deadline = time.monotonic() + max(0, timeout)
        try:
            wait_budget = max(0, min(30, float(self.env.get("FREELLMPOOL_WAIT_SECONDS", "5"))))
        except (ValueError, TypeError):
            wait_budget = 5
        waiting = 0.0
        queue = list(candidates)
        if modality == "chat":
            queue = self._prefer_prefix_route(
                queue, cast(list[JSON], payload) if isinstance(payload, list) else None)
        retryable: list[tuple[Route, float]] = []
        while queue:
            self._check_cancelled()
            route = queue.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            state: AttemptState = {}
            started = time.monotonic()
            try:
                trials, skipped_slots = self._key_trials_for_route(route)
                for slot in skipped_slots:
                    attempts.append((route.name, f"key slot {slot + 1} skipped (credential not admitted)"))
                for trial, (slot, key) in enumerate(trials):
                    state["active_key"] = key
                    state["active_slot"] = slot
                    try:
                        if stream:
                            gen = cast(Generator[str, None, None], client.stream_call(route.provider, route.model, cast(list[JSON], payload),
                                                     api_key=key, env=route.env,
                                                     max_tokens=max_tokens, temperature=temperature,
                                                     timeout=min(remaining, 30), stream_post=self._stream_for(route, state)))
                            try:
                                first = next(gen)
                            except BaseException:
                                gen.close()
                                raise
                            return self._committed_stream(route, gen, first, state, started, len(attempts) + 1)
                        reply: Reply | EmbedReply | TranscribeReply
                        if modality == "chat":
                            reply = client.call(route.provider, route.model, cast(list[JSON], payload),
                                                api_key=key, env=route.env,
                                                max_tokens=max_tokens, temperature=temperature, timeout=min(remaining, 30),
                                                tools=tools, tool_choice=tool_choice, response_format=response_format,
                                                enforce_thinking_floor=not probe, post=self._post_for(route, state))
                            if not reply.text.strip() and not (reply.message and reply.message.get("tool_calls")):
                                raise ProviderHTTPError(502, "empty completion", retryable=True)
                        elif modality == "embedding":
                            reply = client.embed(route.provider, route.model, cast(list[str], payload),
                                                 api_key=key, env=route.env,
                                                 timeout=min(remaining, 30), post=self._post_for(route, state))
                        else:
                            reply = client.transcribe(route.provider, route.model, cast(bytes, payload), cast(str, filename),
                                                      api_key=key, env=route.env,
                                                      language=language, response_format=cast(str, response_format or "json"),
                                                      timeout=min(remaining, 30), post=self._multipart_for(route, state))
                        self._check_cancelled()
                        self._success(route, reply, started)
                        if modality == "chat" and isinstance(payload, list):
                            self._remember_prefix_route(cast(list[JSON], payload), route.name)
                        if hasattr(reply, "attempts"):
                            reply.attempts = len(attempts) + 1
                        if redactions and hasattr(reply, "redactions"):
                            reply.redactions = tuple(redactions)
                        if modality == "chat" and isinstance(reply, Reply) and cache_key is not None \
                                and self._cache is not None:
                            self._cache.put(cache_key, {
                                "text": reply.text, "provider_id": reply.provider_id,
                                "model": reply.model, "prompt_tokens": reply.prompt_tokens,
                                "completion_tokens": reply.completion_tokens,
                                "message": reply.message,
                            })
                            emit(self._on_event, "cache_store", key=cache_key, target=route.name)
                        return reply
                    except ProviderHTTPError as key_exc:
                        if slot < 0 or key_exc.status not in ROTATE_STATUSES or trial + 1 >= len(trials):
                            raise
                        self._cool_key_slot(
                            route.provider.id, slot,
                            len(route.provider.api_keys(route.env)), key_exc,
                        )
                        attempts.append((route.name, f"key slot {slot + 1}: HTTP {key_exc.status}"))
            except Exception as exc:
                self._check_cancelled()
                delay = self._failure(route, exc)
                failures.append(exc)
                if delay is not None:
                    wake = time.monotonic() + max(.01, delay)
                    deadlines.append(wake)
                    if isinstance(exc, AllowanceDenied) or (isinstance(exc, ProviderHTTPError)
                            and (exc.status == 429 or (500 <= exc.status <= 599 and exc.retryable))):
                        retryable.append((route, wake))
                # Error types/statuses are enough for diagnostics; never persist
                # an upstream body that may echo credentials or request content.
                detail = (exc.reason if isinstance(exc, AllowanceDenied) else
                          f"HTTP {exc.status}" if isinstance(exc, ProviderHTTPError) else type(exc).__name__)
                if (isinstance(exc, ProviderHTTPError) and state.get("active_slot", -1) >= 0
                        and len(route.provider.api_keys(route.env)) > 1):
                    detail = f"key slot {state['active_slot'] + 1}: {detail}"
                attempts.append((route.name, detail))
            if not queue and retryable:
                wake = min(end for _, end in retryable)
                delay = max(.01, wake - time.monotonic())
                if delay <= wait_budget - waiting and delay + .01 < deadline - time.monotonic():
                    cancelled = getattr(self, "_cancelled", None)
                    if cancelled is None:
                        time.sleep(delay)
                    else:
                        cancelled.wait(delay)
                    waiting += delay
                    self._check_cancelled()
                    queue = [candidate for candidate, end in retryable if end <= time.monotonic() + .001]
                    retryable = [(candidate, end) for candidate, end in retryable if candidate not in queue]
        limited = any(isinstance(exc, AllowanceDenied) or (isinstance(exc, ProviderHTTPError) and exc.status == 429) for exc in failures)
        if failures and all(isinstance(exc, ContextWindowExceeded) for exc in failures):
            raise ContextWindowExceeded(attempts, est_tokens=max(cast(ContextWindowExceeded, exc).est_tokens for exc in failures))
        invalid = failures and all(isinstance(exc, ValueError) for exc in failures)
        error = AllProvidersExhausted(attempts, client_status=429 if limited else 400 if invalid else 503,
                                      client_message=("Free capacity is temporarily exhausted; retry after the reported cooldown."
                                                      if limited else "No free route could serve this request; inspect freellmpool status and verify."))
        if failures and all(isinstance(exc, _AccountingError) for exc in failures):
            error.client_message = str(failures[0])
        future = [end - time.monotonic() for end in deadlines if end > time.monotonic()]
        error.retry_after = min(future) if future else None
        upstream = [exc.status for exc in failures if isinstance(exc, ProviderHTTPError)]
        error.upstream_status = upstream[-1] if upstream else None
        raise error

    def _committed_stream(self, route: Route, gen: Generator[str, None, None], first: str,
                          state: AttemptState, started: float, attempts: int) -> ManagedStream:
        completed = False
        try:
            yield {"provider": route.provider.id, "model": route.model, "attempts": attempts}
            yield first
            yield from gen
            completed = True
            self._success(route, _stream_usage_reply(route, state.get("usage", {})), started)
        except Exception as exc:
            self._failure(route, exc)
            raise
        finally:
            gen.close()
            if completed and state.get("reservation"):
                self.ledger.settle(state["reservation"], self._actual_cost(
                    route, {"usage": state.get("usage", {})}, state["reserved_amounts"]))

    def chat(self, messages: list[JSON], **kwargs: Unpack[CallOptions]) -> Reply:
        reply = cast(Reply, self._run("chat", messages, **kwargs))
        return self._structured_repair(messages, reply, kwargs)

    def _structured_repair(self, messages: list[JSON], reply: Reply,
                           kwargs: CallOptions) -> Reply:
        from .structured import MAX_REPAIRS, check_reply, repair_messages

        response_format = kwargs.get("response_format")
        if response_format is None:
            return reply
        _value, error = check_reply(reply.text, response_format)
        if error is None:
            return reply
        attempts = [(f"{reply.provider_id}/{reply.model}", f"raw reply invalid: {error}")]
        current: Reply = reply
        for _ in range(MAX_REPAIRS):
            fixed = cast(Reply, self._run(
                "chat", repair_messages(list(messages), current.text, error), **kwargs))
            _value, error = check_reply(fixed.text, response_format)
            if error is None:
                fixed.attempts += current.attempts
                return fixed
            attempts.append((f"{fixed.provider_id}/{fixed.model}", f"repair reply invalid: {error}"))
            current = fixed
        raise StructuredOutputError(
            attempts,
            client_message=(f"JSON-mode reply failed validation after max {MAX_REPAIRS} repair "
                            f"turn(s); last error: {error}"))

    async def achat(self, messages: list[JSON], *, apost: AsyncPost | None = None,
                    **kwargs: Unpack[CallOptions]) -> Reply:
        """Reuse the same ledger and snapshot logic from asynchronous clients."""
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        worker_pool = copy.copy(self)
        worker_pool._cancelled = cancelled
        if apost is not None:
            def bridge(url: str, headers: dict[str, str], body: JSON, timeout: float) -> client.HTTPResult:
                async def send() -> client.HTTPResult:
                    return await apost(url, headers, body, timeout)
                pending = asyncio.run_coroutine_threadsafe(send(), loop)
                try:
                    return pending.result(timeout=timeout)
                except BaseException:
                    pending.cancel()
                    raise
            worker_pool._post = bridge
        task = asyncio.create_task(asyncio.to_thread(worker_pool.chat, messages, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # A bounded in-flight request may still finish; it remains accounted.
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise

    def stream_chat(self, messages: list[JSON], **kwargs: Unpack[CallOptions]) -> ManagedStream:
        if kwargs.get("tools") or kwargs.get("tool_choice") is not None or kwargs.get("response_format") is not None:
            raise ValueError("stream_chat returns text deltas; use chat for structured replies or the proxy for tool-call SSE")
        return cast(ManagedStream, self._run("chat", messages, stream=True, **kwargs))

    def embed(self, texts: str | list[str], **kwargs: Unpack[CallOptions]) -> EmbedReply:
        return cast(EmbedReply, self._run("embedding", [texts] if isinstance(texts, str) else list(texts), **kwargs))

    def transcribe(self, audio: bytes, filename: str, **kwargs: Unpack[CallOptions]) -> TranscribeReply:
        return cast(TranscribeReply, self._run("transcription", audio, filename=filename, **kwargs))

    def probe_call(self, provider: Provider, model: str, messages: list[JSON], **kwargs: Any) -> Reply:
        accepted = {k: v for k, v in kwargs.items() if k in {"max_tokens", "temperature", "timeout", "tools", "tool_choice", "response_format"}}
        return cast(Reply, self._run("chat", messages, model=model, providers=[provider.id], probe=True, **accepted))

    def probe_stream(self, provider: Provider, model: str, messages: list[JSON], **kwargs: Any) -> Generator[str, None, None]:
        accepted = {k: v for k, v in kwargs.items() if k in {"max_tokens", "temperature", "timeout"}}
        stream = cast(ManagedStream, self._run("chat", messages, model=model, providers=[provider.id], probe=True, stream=True, **accepted))
        try:
            next(stream)  # routing metadata belongs to the proxy, not the canary
            yield from cast(Iterable[str], stream)
        finally:
            stream.close()

    def rank_targets(self, messages: list[JSON] | None = None, *, providers: Iterable[str] | None = None,
                      model: str | None = None, **kwargs: Any) -> list[Target]:
        candidates = self._candidates(self.snapshot(), "chat", model, providers, messages,
                                      tools=kwargs.get("tools"), response_format=kwargs.get("response_format"),
                                      max_tokens=kwargs.get("max_tokens", 0),
                                      private=bool(kwargs.get("private")))
        return [Target(r.provider, r.model, 0, r.metadata.get("context")) for r in candidates]

    def _all_targets(self, include: Iterable[str] | None = None, model: str | None = None) -> list[Target]:
        try:
            return self.rank_targets(providers=include, model=model)
        except AllProvidersExhausted:
            return []

    def managed_status(self, snapshot: Snapshot | None = None) -> JSON:
        snapshot = self.snapshot() if snapshot is None else snapshot
        limits = {limit.key: limit for route in snapshot.routes for limit in route.limits}
        tool_routes = [r for r in snapshot.routes if r.modality == "chat" and r.automatic
                       and self.conformance is not None
                       and self.conformance.passes(r.provider, r.model, ("tools",))]
        key_depth: dict[str, int] = {}
        for r in snapshot.routes:
            if r.provider.key_env and r.provider.id not in key_depth:
                key_depth[r.provider.id] = len(r.provider.api_keys(r.env))
        return {"schema": 1, "generation": snapshot.generation, "strict_free": True,
                "eligible_routes": len(snapshot.routes), "providers": list(snapshot.providers),
                "tools_ready": len(tool_routes),
                "tools_providers": len({r.provider.id for r in tool_routes}),
                "key_depth": key_depth,
                "allowances": self.ledger.status(limits.values()),
                "note": "Unknown upstream allowances are paced conservatively; external account usage may be unknown."}


def _duration(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value)
        if not parts or "".join(number + unit for number, unit in parts) != value:
            raise ValueError("invalid reset duration") from None
        result = sum(float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit] for number, unit in parts)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid reset duration")
    # Reset headers feed reserve() denials; clamp absurd values so one header
    # cannot wedge an allowance for decades (32d matches the unknown-monthly ceiling).
    return min(result, 32 * 86400)
