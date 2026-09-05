from __future__ import annotations

from dataclasses import dataclass

from .benchmark import benchmark
from .router import Pool


@dataclass(frozen=True)
class HealthRow:
    target: str
    status: str
    latency_ms: float | None
    note: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run_healthcheck(
    pool: Pool, *, model: str | None = None, providers=None, timeout: float = 20.0
) -> list[HealthRow]:
    # Reasoning models can consume the first few dozen tokens internally before
    # emitting visible text. A single-word health probe still terminates early on
    # ordinary models, while 64 avoids false "empty completion" failures.
    # Free Z.ai Flash health probes can disable reasoning altogether, avoiding
    # hidden-token truncation and latency while retaining the strict 64 budget.
    rows = benchmark(
        pool, model=model, providers=providers, timeout=timeout, max_tokens=64,
        disable_probe_thinking=True,
    )
    out: list[HealthRow] = []
    for row in rows:
        if row.ok:
            note = f"{row.tokens} tok" if row.tokens else "responded"
            out.append(HealthRow(row.target, "ok", row.latency_ms, note))
        else:
            error = row.error or "failed"
            note = _short_note(error)
            # Classify the full response before truncating its display note:
            # providers can put billing details after a long JSON prefix.
            status = "skipped" if row.skipped else _failure_status(error)
            out.append(HealthRow(row.target, status, None, note))
    return out


def render_health_table(rows: list[HealthRow]) -> str:
    if not rows:
        return "No configured providers to check."
    width = max(len(row.target) for row in rows)
    status_width = max(12, *(len(row.status) for row in rows))
    lines = [f"  {'provider/model':<{width}}  {'status':<{status_width}}  {'latency':>9}  note"]
    for row in rows:
        latency = f"{row.latency_ms:,.0f} ms" if row.latency_ms is not None else "-"
        lines.append(
            f"  {row.target:<{width}}  {row.status:<{status_width}}  {latency:>9}  {_short_note(row.note)}"
        )
    ok = sum(1 for row in rows if row.ok)
    lines.append(f"\n  {ok}/{len(rows)} providers ok")
    return "\n".join(lines)


def _short_note(value: str) -> str:
    lines = (value or "").splitlines()
    return lines[0][:80] if lines else ""


def _failure_status(error: str) -> str:
    lowered = error.lower()
    if lowered.startswith("http 402:") or (
        lowered.startswith("http 429:")
        and any(
            marker in lowered
            for marker in ("insufficient balance", "no resource package", "insufficient credits")
        )
    ):
        return "billing_blocked"
    return "rate_limited" if lowered.startswith("http 429:") else "fail"
