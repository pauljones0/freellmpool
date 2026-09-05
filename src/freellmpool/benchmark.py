"""Measure each configured provider with a tiny prompt and report a table.

Backs ``freellmpool benchmark``. It probes one model per
provider (the first enabled one, or a pinned ``model``) — so it measures raw
provider latency, and records the results into the given pool's
:class:`~freellmpool.metrics.Metrics`. In a long-running process (a library
embedding, or a proxy that calls ``benchmark(pool)`` on its own pool) that warms
``routing="fast"``; the one-shot ``freellmpool benchmark`` CLI exits afterward, so
there it only serves as a latency report.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import client as _client
from .catalog import discover_openai_models
from .errors import AllProvidersExhausted, ProviderHTTPError
from .router import Pool

_PROMPT = "Reply with the single word: ok"
_ZAI_FREE_THINKING_MODELS = frozenset(("glm-4.5-flash", "glm-4.7-flash", "glm-4.6v-flash"))


@dataclass
class BenchRow:
    target: str
    ok: bool
    latency_ms: float | None
    tokens: int | None
    error: str | None
    skipped: bool = False


def _pick_model(provider, model: str | None) -> str | None:
    if model:
        m = provider.model(model)
        return m.name if m else None
    for m in provider.models:
        if m.enabled:
            return m.name
    return None


def _pick_model_for_health(
    provider, model: str | None, pool: Pool, timeout: float
) -> tuple[str | None, str | None]:
    """Pick a configured probe model, using /models as advisory discovery.

    Discovery can omit valid routes or list paid and disabled ones. Default
    probes must remain within enabled configuration. An explicit configured pin
    is intentional and is allowed even when disabled or absent from discovery.
    """
    if model:
        picked = provider.model(model)
        if picked:
            return picked.name, None
    elif _pick_model(provider, None) is None:
        return None, "no enabled configured models; probe skipped"

    discovered: set[str] | None = None
    if provider.adapter == "openai":
        try:
            discovered = set(
                discover_openai_models(
                    provider.base_url,
                    api_key=provider.api_key(pool.env),
                    timeout=min(timeout, 10.0),
                )
            )
        except ValueError:
            discovered = None

    if model:
        base_model = model.rsplit(":", 1)[0]
        if discovered is not None and model not in discovered and base_model not in discovered:
            return None, f"model not listed by /models: {model}"
        return model, None

    if discovered:
        for m in provider.models:
            # OpenAI-compatible aggregators can expose base model
            # ids from /models while accepting a :provider routing suffix on calls.
            if m.enabled and (
                m.name in discovered or m.name.rsplit(":", 1)[0] in discovered
            ):
                return m.name, None

    return _pick_model(provider, model), None


def benchmark(
    pool: Pool,
    *,
    model: str | None = None,
    providers=None,
    prompt: str = _PROMPT,
    max_tokens: int = 16,
    timeout: float = 30.0,
    workers: int = 8,
    disable_probe_thinking: bool = False,
) -> list[BenchRow]:
    """Time one call per configured provider, concurrently. Returns rows sorted
    fastest-first (successes), then failures.

    Health checks can disable reasoning for the Z.ai free Flash models that
    support it. Ordinary benchmarks retain the models' normal behavior.
    """
    include = {p.strip() for p in providers} if providers else None
    managed = getattr(pool, "managed", False)
    skipped_rows = []
    if managed:
        snapshot = pool.snapshot()
        eligible_chat = {route.provider.id for route in snapshot.routes if route.modality == "chat"}
        for row in snapshot.providers:
            if row["id"] not in eligible_chat and ((include and row["id"] in include) or
                                                    (include is None and row.get("configured"))):
                reason = row["reason"] if row["reason"] != "ready" else "no eligible free chat model"
                skipped_rows.append(BenchRow(row["id"], False, None, None, reason, skipped=True))
    candidates = [p for p in pool.providers if include is None or p.id in include]

    def run(provider) -> BenchRow | None:
        # Model selection (incl. the /models discovery probe) runs INSIDE the worker so it's
        # parallelized across the pool — not serialized before the threadpool, which would add
        # up to N*min(timeout,10s) of preflight latency on slow endpoints.
        if managed:
            # Discovery has already passed freshness and price admission. Never
            # probe an unlisted/disabled pin via the legacy advisory GET path.
            mname = _pick_model(provider, model)
            skip_note = None if mname else "no eligible free model; run freellmpool setup"
        else:
            mname, skip_note = _pick_model_for_health(provider, model, pool, timeout)
        if skip_note:
            target = f"{provider.id}/{model}" if model else provider.id
            return BenchRow(target, False, None, None, skip_note, skipped=True)
        if not mname:
            return None  # nothing probeable (no catalog model and no discovery) — omit, as before
        bounded_zai_probe = (
            disable_probe_thinking
            and provider.id == "zhipu"
            and provider.adapter == "openai"
            and mname in _ZAI_FREE_THINKING_MODELS
        )
        post = pool._post
        if bounded_zai_probe:
            # Keep this request's override local: concurrent ordinary requests
            # and the caller's injected transport must remain unchanged.
            def post(url, headers, body, timeout):
                return pool._post(
                    url, headers, {**body, "thinking": {"type": "disabled"}}, timeout
                )

        key = f"{provider.id}/{mname}"
        started = time.monotonic()
        try:
            call_fn = pool.probe_call if managed else _client.call
            reply = call_fn(
                provider,
                mname,
                [{"role": "user", "content": prompt}],
                api_key=provider.api_key(pool.env),
                env=pool.env,
                max_tokens=max(512, max_tokens) if managed else max_tokens,
                temperature=0.0,
                timeout=timeout,
                post=post,
                enforce_thinking_floor=not bounded_zai_probe,
            )
        except AllProvidersExhausted as exc:
            status = exc.upstream_status or exc.client_status or 503
            error = f"HTTP {status}: {exc.client_message or 'no eligible free capacity'}"
            if not managed:
                pool.metrics.record_failure(key, error)
            return BenchRow(key, False, None, None, error)
        except ProviderHTTPError as exc:
            if not managed:
                pool.metrics.record_failure(key, str(exc))
            return BenchRow(key, False, None, None, str(exc))
        except Exception as exc:  # noqa: BLE001 — report it, don't abort the sweep
            if not managed:
                pool.metrics.record_failure(key, f"{type(exc).__name__}: {exc}")
            return BenchRow(key, False, None, None, f"{type(exc).__name__}: {exc}")
        elapsed = (time.monotonic() - started) * 1000.0
        if reply.text:
            if not managed:
                pool.metrics.record_success(key, elapsed)
            return BenchRow(key, True, elapsed, reply.completion_tokens, None)
        if not managed:
            pool.metrics.record_failure(key, "empty completion")
        return BenchRow(key, False, None, None, "empty completion")

    if not candidates:
        return skipped_rows
    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as ex:
        rows = [r for r in ex.map(run, candidates) if r is not None] + skipped_rows
    rows.sort(key=lambda r: (not r.ok, r.latency_ms if r.latency_ms is not None else 1e18))
    return rows


def render_table(rows: list[BenchRow]) -> str:
    """Format benchmark rows as a fixed-width table."""
    if not rows:
        return "No configured providers to benchmark (set an API key first)."
    width = max(len(r.target) for r in rows)
    lines = [f"  {'provider/model':<{width}}  {'status':<6}  {'latency':>9}  note"]
    for r in rows:
        if r.ok:
            lat = f"{r.latency_ms:,.0f} ms" if r.latency_ms is not None else "-"
            note = f"{r.tokens} tok" if r.tokens else ""
            lines.append(f"  {r.target:<{width}}  {'ok':<6}  {lat:>9}  {note}")
        else:
            err_lines = (r.error or "").splitlines()
            note = err_lines[0][:60] if err_lines else ""
            status = "SKIP" if r.skipped else "FAIL"
            lines.append(f"  {r.target:<{width}}  {status:<6}  {'-':>9}  {note}")
    ok = sum(1 for r in rows if r.ok)
    lines.append(f"\n  {ok}/{len(rows)} providers responded")
    return "\n".join(lines)
