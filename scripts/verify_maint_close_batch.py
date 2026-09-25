"""Fingerprint-gated verdicts for a reviewed-issue close batch.

Compares each batch target's reviewed fingerprint against a fresh public
maintenance report. A target whose finding still fires with a different
fingerprint carries changed evidence and must stay open: closing it would
suppress that evidence, and the automation would only reopen it on the next
run. Mirrors scripts/sync_maintenance_issues.py semantics: closing suppresses
unchanged evidence; a changed finding reopens; only a resolution with the
original fingerprint plus complete fresh evidence proves recovery.

Exit codes: 0 when every target is closable (unchanged or resolved),
3 when any target re-fired or needs review, 1 on invalid input.
With --require-complete, 1 also covers partial reports: no issue may close
on evidence that was not freshly observed in the same report.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.sync_maintenance_issues import _proves_recovery

_HASH = re.compile(r"[0-9a-f]{64}")
_CLOSABLE = {"unchanged", "resolved"}
_CHECK_FAILURES = {"catalog_failed", "source_check_failed", "limit_source_failed"}
_ERROR_STATUSES = {"check_failed", "error"}
_FRESH_WINDOW_SECONDS = 172800.0  # 48h, mirrors the catalog validity window.


def load_batch(path: str | Path) -> dict[str, Any]:
    """Read and validate a close-batch spec built for one maintenance review."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid batch file: {path}") from exc
    if not isinstance(document, dict):
        raise ValueError("batch must be a JSON object")
    targets = document.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("batch needs a non-empty targets list")
    seen: set[tuple[str, str, str]] = set()
    for entry in targets:
        if not isinstance(entry, dict):
            raise ValueError("batch target must be an object")
        issue = entry.get("issue")
        triple = (entry.get("provider"), entry.get("code"), entry.get("subject"))
        fingerprint = entry.get("reviewed_fingerprint")
        if (type(issue) is not int or issue <= 0
                or any(not isinstance(part, str) or not part for part in triple)
                or not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)):
            raise ValueError(f"invalid batch target: {entry!r:.200}")
        key = (str(triple[0]), str(triple[1]), str(triple[2]))
        if key in seen:
            raise ValueError(f"duplicate batch target: {key}")
        seen.add(key)
    return {"batch": document.get("batch"), "review": document.get("review"),
            "targets": targets}


def _index(rows: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index report rows by identity triple; finding ids derive from it."""
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not isinstance(rows, list):
        raise ValueError("report findings must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("report row must be an object")
        triple = (row.get("provider"), row.get("code"), row.get("subject"))
        if any(not isinstance(part, str) for part in triple):
            continue
        key = (str(triple[0]), str(triple[1]), str(triple[2]))
        if key in indexed and indexed[key].get("fingerprint") != row.get("fingerprint"):
            raise ValueError(f"contradictory report rows for {key}")
        indexed[key] = row
    return indexed


def verdicts(spec: dict[str, Any], report: dict[str, Any]) -> dict[int, str]:
    """Return one verdict per batch target: refired, unchanged, resolved, needs-review."""
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    findings = _index(list(report.get("findings", [])) + list(report.get("pending_changes", [])))
    resolutions = _index(report.get("resolutions", []))
    result: dict[int, str] = {}
    for entry in spec["targets"]:
        key = (str(entry["provider"]), str(entry["code"]), str(entry["subject"]))
        reviewed = str(entry["reviewed_fingerprint"])
        live = findings.get(key)
        if live is not None:
            fingerprint = live.get("fingerprint")
            if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
                raise ValueError(f"report finding for {key} lacks a valid fingerprint")
            result[int(entry["issue"])] = "unchanged" if fingerprint == reviewed else "refired"
            continue
        resolution = resolutions.get(key)
        if (resolution is not None and resolution.get("fingerprint") == reviewed
                and _proves_recovery(resolution, report)):
            result[int(entry["issue"])] = "resolved"
        else:
            result[int(entry["issue"])] = "needs-review"
    return result


def _stamp_moment(value: Any) -> datetime | None:
    """Parse a report timestamp; naive or unparseable stamps are not evidence."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _fresh(moment: datetime | None, observed: datetime) -> bool:
    if moment is None:
        return False
    delta = (observed - moment).total_seconds()
    return 0 <= delta < _FRESH_WINDOW_SECONDS


def _provider_section(report: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = report.get("providers", {})
    section = providers.get(provider, {}) if isinstance(providers, dict) else {}
    return section if isinstance(section, dict) else {}


def _stale_backing(report: dict[str, Any], observed: datetime, entry: dict[str, Any]) -> list[str]:
    """Reasons an unchanged verdict rests on evidence this report did not observe.

    Carried findings persist across runs even when their check was skipped, so
    an unchanged fingerprint alone cannot prove a recheck happened. Resolved
    verdicts already prove recovery through fresh windows and need no extra
    rule; limit rows can never close here because limit changes always need
    explicit maintainer review.
    """
    provider = str(entry["provider"])
    code = str(entry["code"])
    subject = str(entry["subject"])
    label = f"#{entry['issue']} {provider}:{code}:{subject}"
    section = _provider_section(report, provider)
    if code in {"model_added", "model_removed", "price_changed"} or code.startswith("catalog_"):
        catalog = section.get("catalog", {})
        checked = _stamp_moment(catalog.get("checked_at")) if isinstance(catalog, dict) else None
        if not (isinstance(catalog, dict) and catalog.get("status") == "ok"
                and catalog.get("complete") is True and _fresh(checked, observed)):
            return [f"{label}: closable verdict needs a fresh complete catalog check"]
        return []
    if code in {"source_changed", "source_baseline_needed"}:
        sources = section.get("sources", [])
        row = next((item for item in sources
                    if isinstance(item, dict) and item.get("id") == subject), None)
        attempt = _stamp_moment(row.get("last_attempt_at")) if row is not None else None
        if row is None or row.get("status") != "review_required" or not _fresh(attempt, observed):
            return [f"{label}: closable verdict needs a freshly observed live difference"]
        return []
    if code in {"source_due", "source_expired"}:
        sources = section.get("sources", [])
        row = next((item for item in sources
                    if isinstance(item, dict) and item.get("id") == subject), None)
        attempt = _stamp_moment(row.get("last_attempt_at")) if row is not None else None
        window = (row is not None and _stamp_moment(row.get("checked_at")) is not None
                  and _stamp_moment(row.get("expires_at")) is not None)
        if not window or not _fresh(attempt, observed):
            return [f"{label}: closable verdict needs a freshly observed evidence window"]
        return []
    return [f"{label}: closable verdict has no fresh-evidence rule, refusing to close"]


def completeness_defects(report: dict[str, Any], spec: dict[str, Any] | None = None, *,
                         expected_providers: Collection[str] = ()) -> list[str]:
    """Reasons a public report is partial; empty means complete.

    Global checks cover the chained baseline, failed or errored checks, and
    expected provider sections. With a batch spec, every closable (unchanged)
    verdict must additionally rest on a backing check this report observed:
    verdicts that hold (refired, needs-review) close nothing and need no
    backing rule.
    """
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    defects: list[str] = []
    if report.get("baseline_status") != "ok":
        defects.append(f"baseline_status={report.get('baseline_status')!r}: "
                       "closable verdicts need a chained baseline")
    rows: list[Any] = []
    for key in ("findings", "pending_changes"):
        values = report.get(key, [])
        rows.extend(values if isinstance(values, list) else [])
    for row in rows:
        if isinstance(row, dict) and row.get("code") in _CHECK_FAILURES:
            defects.append(f"{row.get('provider')}:{row.get('code')}:{row.get('subject')}: "
                           "check failed, previous evidence not renewed")
    providers = report.get("providers", {})
    sections = providers if isinstance(providers, dict) else {}
    for pid, entry in sections.items():
        if not isinstance(entry, dict):
            continue
        catalog = entry.get("catalog", {})
        if isinstance(catalog, dict) and catalog.get("status") in _ERROR_STATUSES:
            defects.append(f"{pid}: catalog status {catalog.get('status')!r}")
        sources = entry.get("sources", [])
        if isinstance(sources, list):
            for source in sources:
                if (isinstance(source, dict)
                        and (source.get("status") in _ERROR_STATUSES or source.get("error"))):
                    defects.append(f"{pid} source {source.get('id')}: "
                                   f"status {source.get('status')!r}")
    for wanted in expected_providers:
        if wanted not in sections:
            defects.append(f"provider {wanted} missing from report")
    observed = _stamp_moment(report.get("checked_at"))
    if observed is None:
        defects.append("checked_at is missing or not a timezone-aware timestamp")
    if spec is not None and observed is not None:
        outcome = verdicts(spec, report)
        targets = {int(entry["issue"]): entry for entry in spec["targets"]}
        for issue in sorted(outcome):
            if outcome[issue] == "unchanged":
                defects.extend(_stale_backing(report, observed, targets[issue]))
    return defects


def require_complete(report: dict[str, Any], spec: dict[str, Any] | None = None, *,
                     expected_providers: Collection[str] = ()) -> None:
    """Reject partial evidence for closing; raises ValueError listing defects."""
    defects = completeness_defects(report, spec, expected_providers=expected_providers)
    if defects:
        raise ValueError(f"partial public report: {'; '.join(defects)}")


def main(argv: list[str] | None = None) -> int:
    """Compare a batch spec against a public report; 0 closable, 3 holds, 1 invalid."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--require-complete", action="store_true",
                        help="refuse closable verdicts unless the report is complete")
    parser.add_argument("--expect-provider", action="append", default=[],
                        help="provider section the report must contain (repeatable)")
    args = parser.parse_args(argv)
    try:
        spec = load_batch(args.batch)
        report = json.loads(args.report.read_text(encoding="utf-8"))
        if args.require_complete:
            require_complete(report, spec, expected_providers=args.expect_provider)
        outcome = verdicts(spec, report)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"verify_maint_close_batch: invalid input ({exc})\n")
    holds = sorted(issue for issue, verdict in outcome.items() if verdict not in _CLOSABLE)
    if args.format == "json":
        print(json.dumps({"batch": spec.get("batch"), "holds": holds,
                          "verdicts": {str(issue): outcome[issue] for issue in sorted(outcome)}},
                         sort_keys=True))
    else:
        for issue in sorted(outcome):
            print(f"#{issue}: {outcome[issue]}")
        print(f"closable={len(outcome) - len(holds)} holds={len(holds)}")
    return 0 if not holds else 3


if __name__ == "__main__":
    raise SystemExit(main())
