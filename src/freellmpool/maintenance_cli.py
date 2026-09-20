"""Readable maintenance command and isolated credentialless workflow entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import effective_env
from .discovery import DiscoveryBusy
from .maintenance import (
    _read,
    _write,
    format_report,
    run_maintenance,
    state_directory,
    status_report,
    validate_public_report,
)

_MAINTENANCE_BUSY_LINE = ("freellmpool: another catalog refresh is running; maintenance refresh "
                          "skipped (retry later).")


def _maybe_heal_after_refresh(env: dict[str, str], report: dict[str, Any]) -> str | None:
    """Timer-consented heal hook: AUTOHEAL=1 only, private reports only.

    Returns the heal outcome reason (or None when no heal was attempted)
    so callers can surface io-error as exit 1.
    """
    from .heal import HealStore, autoheal_enabled, default_heal_path, run_heal
    from .managed import ManagedPool

    if not autoheal_enabled(env):
        return None
    pool = ManagedPool.from_default_config(env=env)
    outcome = run_heal(pool, HealStore(default_heal_path(env)), trigger="maintenance")
    print(f"heal: {outcome['reason']} ({outcome['passes']}/{len(outcome['targets'])} "
          f"re-verified, {outcome['probes']} probes)", file=sys.stderr)
    if not outcome["ran"]:
        return str(outcome["reason"])
    for finding in report.get("findings", []):
        if isinstance(finding, dict) and finding.get("code") in {
                "conformance_expired", "conformance_due"}:
            summary = finding.get("summary", "")
            finding["summary"] = (
                f"{summary} (heal: {outcome['passes']}/{len(outcome['targets'])} "
                f"re-verified)")
    return str(outcome["reason"])


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--refresh", action="store_true", help="refresh reviewed policy, catalogs and supported observations; no inference")
    parser.add_argument("--public-only", "--public", dest="public_only", action="store_true", help="credentialless public report; never read private state")
    parser.add_argument("--output", type=Path, help="write the normalized report to this local file")
    parser.add_argument("--baseline", type=Path, help="public baseline artifact to validate and advance")
    parser.add_argument("--source-revision", help="immutable reviewed Git source revision for public workflow provenance")
    parser.add_argument("--json", action="store_true", help="print the normalized report as JSON")


def cmd_maintenance(args: argparse.Namespace) -> int:
    try:
        if args.baseline is not None and not args.public_only:
            raise ValueError("baseline option is public-only")
        env = {} if args.public_only else effective_env()
        if args.refresh:
            report = run_maintenance(env, public_only=args.public_only, baseline_path=args.baseline,
                                     source_revision=args.source_revision)
            if not args.public_only and _maybe_heal_after_refresh(env, report) == "io-error":
                return 1
        elif args.public_only:
            directory = args.baseline.parent if args.baseline else state_directory({}) / "public-maintenance"
            report = validate_public_report(_read(directory / "public-report.json"))
        else:
            report = status_report(env)
        if args.public_only:
            report = validate_public_report(report)
        if args.output:
            _write(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
        return 0
    except DiscoveryBusy:
        print(_MAINTENANCE_BUSY_LINE, file=sys.stderr)
        return 2
    except (OSError, ValueError, httpx.HTTPError):
        print("Maintenance could not produce a valid report. Retry: freellmpool maintenance --refresh", file=sys.stderr)
        return 2


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("maintenance", help="show maintenance attention or refresh evidence without inference")
    _arguments(parser)
    parser.set_defaults(func=cmd_maintenance)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _arguments(parser)
    return cmd_maintenance(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
