#!/usr/bin/env python3
"""Validate a freellmpool drift snapshot against the documented schema (v1).

Usage: python scripts/check_drift_snapshot.py [PATH ...]
Exits nonzero and names every violation found.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

STATUSES = {"pass", "fail", "unsupported", "unavailable"}


def _is_iso_z(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_snapshot(path: str | Path) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return [f"{path}: unreadable ({exc})"]
    if not isinstance(data, dict):
        return [f"{path}: top level must be an object"]
    if data.get("schema") != 1:
        errors.append(f"{path}: schema must be 1")
    if not _is_iso_z(data.get("generated_at")):
        errors.append(f"{path}: generated_at must be an ISO-8601 UTC timestamp")
    if not isinstance(data.get("freellmpool"), str) or not data["freellmpool"]:
        errors.append(f"{path}: freellmpool version string required")
    targets = data.get("targets")
    if not isinstance(targets, dict):
        errors.append(f"{path}: targets must be an object")
        return errors
    for name, feats in targets.items():
        if not isinstance(feats, dict):
            errors.append(f"{path}: {name}: features must be an object")
            continue
        for feature, info in feats.items():
            where = f"{path}: {name} {feature}"
            if not isinstance(info, dict):
                errors.append(f"{where}: entry must be an object")
                continue
            if info.get("status") not in STATUSES:
                errors.append(f"{where}: status must be one of {sorted(STATUSES)}")
            if not _is_iso_z(info.get("verified_at")):
                errors.append(f"{where}: verified_at must be an ISO-8601 UTC timestamp")
            if not isinstance(info.get("classification"), str) or not info["classification"]:
                errors.append(f"{where}: classification string required")
    return errors


def main(argv: list[str]) -> int:
    paths = argv[1:] or ["drift-snapshot.json"]
    errors: list[str] = []
    for path in paths:
        errors.extend(validate_snapshot(path))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} drift-snapshot violation(s)", file=sys.stderr)
        return 1
    print(f"{len(paths)} drift snapshot(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
