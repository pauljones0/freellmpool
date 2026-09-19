"""G18 live free-tier status page: probe → snapshot → Pages files.

The publisher runs a live healthcheck, projects it into a fixed public
schema (targets, statuses, latencies, notes — key material cannot appear
by construction), redacts + scans the output as a backstop, and writes
``free-tier-status.html`` plus ``status-history.json`` into the docs dir.
Every page carries its ``generated_at`` stamp and a staleness note.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .healthcheck import HealthRow, run_healthcheck
from .privacy import redact_text
from .router import Pool

STATUS_SCHEMA = 1
STATUS_PAGE_NAME = "free-tier-status.html"
STATUS_HISTORY_NAME = "status-history.json"
HISTORY_LIMIT = 12

_SECRET_PATTERNS = (
    re.compile(r"sk-(?:live|proj)-[A-Za-z0-9]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"xox[bap]-[A-Za-z0-9-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{10,}"),
    re.compile(r"(?i)\bapi[_-]?key\b\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
)

_STATUS_CLASS = {
    "ok": "ok",
    "rate_limited": "warn",
    "skipped": "muted",
}


def collect_live_rows(pool: Pool, *, model: str | None = None,
                      providers: list[str] | None = None,
                      timeout: float = 20.0) -> list[HealthRow]:
    """Probe configured providers with a tiny live request each."""
    return run_healthcheck(pool, model=model, providers=providers, timeout=timeout)


def build_snapshot(rows: list[HealthRow], *, generated_at: str, version: str) -> dict[str, Any]:
    """Project health rows into the fixed public snapshot schema (v1)."""
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": generated_at,
        "freellmpool": version,
        "rows": [
            {"target": row.target, "status": row.status,
             "latency_ms": row.latency_ms, "note": row.note}
            for row in rows
        ],
    }


def append_history(history: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Append a snapshot, keeping the newest HISTORY_LIMIT entries."""
    return [*history, snapshot][-HISTORY_LIMIT:]


def assert_no_key_material(text: str) -> None:
    """Fail closed if ``text`` matches known secret shapes."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError("refusing to publish: output contains key material")


def _counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    ok = sum(1 for row in rows if row.get("status") == "ok")
    return ok, len(rows)


def render_status_html(snapshot: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """Render the full public status page for one snapshot + history."""
    generated_at = str(snapshot.get("generated_at", "unknown"))
    rows = [r for r in snapshot.get("rows", []) if isinstance(r, dict)]
    ok, total = _counts(rows)
    if rows:
        body_rows = "\n".join(
            "<tr><td>{target}</td><td><span class=\"pill {cls}\">{status}</span></td>"
            "<td class=\"num\">{latency}</td><td>{note}</td></tr>".format(
                target=html.escape(str(r.get("target", "?"))),
                cls=_STATUS_CLASS.get(str(r.get("status", "")), "bad"),
                status=html.escape(str(r.get("status", "?"))),
                latency=(f"{r['latency_ms']:,.0f} ms"
                         if isinstance(r.get("latency_ms"), (int, float)) else "-"),
                note=html.escape(str(r.get("note", ""))),
            )
            for r in rows
        )
        table = (f"<p><strong>{ok}/{total}</strong> providers responding.</p>\n"
                 "<table>\n<tr><th>Provider/model</th><th>Status</th>"
                 "<th>Latency</th><th>Note</th></tr>\n"
                 f"{body_rows}\n</table>")
    else:
        table = ("<p><strong>0/0</strong> — No configured providers responded to this "
                 "snapshot's probes. The pool had nothing to check; this is an empty "
                 "reading, not a clean bill of health.</p>")
    history_rows = "\n".join(
        "<tr><td>{at}</td><td class=\"num\">{ok}/{total}</td></tr>".format(
            at=html.escape(str(entry.get("generated_at", "?"))),
            ok=_counts([r for r in entry.get("rows", []) if isinstance(r, dict)])[0],
            total=_counts([r for r in entry.get("rows", []) if isinstance(r, dict)])[1],
        )
        for entry in reversed(history)
    )
    history_table = (f"<table>\n<tr><th>Snapshot (UTC)</th><th>Ok/total</th></tr>\n"
                     f"{history_rows}\n</table>" if history_rows else
                     "<p>No earlier snapshots yet.</p>")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Are the free LLM tiers working right now? (live status)</title>
<meta name="description" content="Live-observed free LLM tier status: which providers answer probes right now, observed latency, snapshot history, and staleness labeling.">
<link rel="canonical" href="https://0xzr.github.io/freellmpool/{STATUS_PAGE_NAME}">
<meta property="og:title" content="Are the free LLM tiers working right now?">
<meta property="og:description" content="Live probe results across free LLM providers, with snapshot history and staleness labeling.">
<meta property="og:type" content="article">
<style>
 :root{{--bg:#0b0e14;--fg:#e6e6e6;--mut:#8a93a2;--card:#141925;--bd:#232a39;--ac:#6ea8ff;
 --good:#3fb950;--warn:#d29922;--bad:#f85149}}
 *{{box-sizing:border-box}}
 body{{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--fg);line-height:1.65}}
 .wrap{{max-width:780px;margin:0 auto;padding:40px 20px 80px}}
 h1{{font-size:27px;margin:0 0 6px;line-height:1.25}}
 h2{{font-size:20px;margin:36px 0 10px;border-bottom:1px solid var(--bd);padding-bottom:6px}}
 .tag{{color:var(--mut);font-size:14px}}
 a{{color:var(--ac);text-decoration:none}}a:hover{{text-decoration:underline}}
 code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
 code{{background:#1b2230;padding:1px 5px;border-radius:4px;font-size:.92em}}
 .lead{{font-size:17px}}
 .note{{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:10px 14px;font-size:14px;margin:14px 0}}
 table{{width:100%;border-collapse:collapse;margin:8px 0;font-size:14px;background:var(--card);border:1px solid var(--bd);border-radius:8px;overflow:hidden}}
 th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--bd)}}
 th{{color:var(--mut);font-weight:600}}
 td.num{{font-variant-numeric:tabular-nums;white-space:nowrap}}
 .pill{{display:inline-block;padding:1px 10px;border-radius:999px;font-size:13px;font-weight:600}}
 .pill.ok{{background:#12331f;color:#7ee787}}
 .pill.warn{{background:#3a2c10;color:#f0b429}}
 .pill.bad{{background:#3d1a1d;color:#ff9d97}}
 .pill.muted{{background:#232a39;color:#8a93a2}}
 .meta{{color:var(--mut);font-size:13px;margin-top:34px;border-top:1px solid var(--bd);padding-top:14px}}
</style>
<meta property="og:url" content="https://0xzr.github.io/freellmpool/{STATUS_PAGE_NAME}">
<meta name="twitter:card" content="summary_large_image">
<meta property="og:image" content="https://0xzr.github.io/freellmpool/assets/social-preview.png">
<meta name="twitter:image" content="https://0xzr.github.io/freellmpool/assets/social-preview.png">
</head>
<body>
<div class="wrap">

<p class="tag"><a href="https://0xzr.github.io/freellmpool/">freellmpool</a> &rsaquo; status</p>
<h1>Are the free tiers working right now?</h1>

<p class="lead"><strong>Live probe results across free LLM providers, refreshed on a
schedule.</strong> Each snapshot below is a real tiny request sent to every configured
provider — not a guess from docs or dashboards.</p>

<div class="note" id="stale">Snapshot taken <code>{html.escape(generated_at)}</code>
<span id="age"></span> — treat snapshots older than 12 hours as stale. Snapshots refresh
on a 6-hour schedule; intraday outages between snapshots will not appear here.</div>

<h2>Latest snapshot</h2>
{table}

<h2>History</h2>
{history_table}
<p class="tag">Machine-readable: <a href="{STATUS_HISTORY_NAME}">{STATUS_HISTORY_NAME}</a>
(schema v{STATUS_SCHEMA}).</p>

<p class="meta">Part of <a href="https://github.com/0xzr/freellmpool">freellmpool</a> (MIT, free, open
source). Observed by this project's own health probes; your keys and quotas may differ.</p>

</div>
<script>
(function() {{
  var el = document.getElementById("age");
  var taken = Date.parse("{html.escape(generated_at)}");
  if (!el || isNaN(taken)) return;
  var mins = Math.max(0, Math.round((Date.now() - taken) / 60000));
  var label = mins < 90 ? mins + " min ago" : Math.round(mins / 60) + " h ago";
  el.textContent = "(" + label + ")";
}})();
</script>
</body>
</html>
"""


def _load_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data
            if isinstance(entry, dict) and entry.get("schema") == STATUS_SCHEMA
            and isinstance(entry.get("generated_at"), str)]


def publish_status(docs_dir: str | Path, rows: list[HealthRow], *,
                   generated_at: str, version: str) -> tuple[Path, Path]:
    """Write the status page + history into ``docs_dir``; fail closed on secrets."""
    docs = Path(docs_dir)
    docs.mkdir(parents=True, exist_ok=True)
    safe_rows = [
        HealthRow(row.target, row.status, row.latency_ms, redact_text(row.note)[0])
        for row in rows
    ]
    snapshot = build_snapshot(safe_rows, generated_at=generated_at, version=version)
    history = append_history(_load_history(docs / STATUS_HISTORY_NAME), snapshot)
    page_text = render_status_html(snapshot, history)
    history_text = json.dumps(history, indent=2, sort_keys=True) + "\n"
    assert_no_key_material(page_text)
    assert_no_key_material(history_text)
    page_path = docs / STATUS_PAGE_NAME
    history_path = docs / STATUS_HISTORY_NAME
    page_path.write_text(page_text)
    history_path.write_text(history_text)
    errors = validate_published(docs)
    if errors:
        raise ValueError(f"published status shape invalid: {errors}")
    return page_path, history_path


def validate_published(docs_dir: str | Path) -> list[str]:
    """Return shape/secret errors for the published status files (empty = valid)."""
    docs = Path(docs_dir)
    page_path = docs / STATUS_PAGE_NAME
    history_path = docs / STATUS_HISTORY_NAME
    errors: list[str] = []
    if not page_path.is_file():
        errors.append(f"{STATUS_PAGE_NAME}: file is missing")
    if not history_path.is_file():
        errors.append(f"{STATUS_HISTORY_NAME}: file is missing")
        return errors
    try:
        page_text = page_path.read_text() if page_path.is_file() else ""
        history_text = history_path.read_text()
    except OSError as exc:
        return [f"status files unreadable: {exc}"]
    try:
        assert_no_key_material(page_text)
        assert_no_key_material(history_text)
    except ValueError as exc:
        errors.append(f"status files: {exc}")
    try:
        history = json.loads(history_text)
    except ValueError:
        return [*errors, f"{STATUS_HISTORY_NAME}: not valid JSON"]
    if not isinstance(history, list) or not history:
        return [*errors, f"{STATUS_HISTORY_NAME}: expected a non-empty list"]
    for entry in history:
        if not isinstance(entry, dict) or entry.get("schema") != STATUS_SCHEMA:
            errors.append(f"{STATUS_HISTORY_NAME}: entry has wrong schema: {entry!r}"[:160])
        if not isinstance(entry, dict) or not entry.get("generated_at"):
            errors.append(f"{STATUS_HISTORY_NAME}: entry lacks generated_at")
    latest = history[-1] if isinstance(history[-1], dict) else {}
    stamp = latest.get("generated_at", "")
    if stamp and stamp not in page_text:
        errors.append(f"{STATUS_PAGE_NAME}: missing latest generated_at {stamp}")
    return errors
