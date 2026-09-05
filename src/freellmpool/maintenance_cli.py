"""Readable maintenance command and isolated credentialless workflow entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .config import effective_env
from .maintenance import (
    _read,
    _write,
    format_report,
    run_maintenance,
    state_directory,
    status_report,
    validate_public_report,
)


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
