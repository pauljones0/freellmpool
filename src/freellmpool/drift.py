"""G10 free-tier drift radar: snapshot conformance evidence, diff it over time.

Snapshots are dated, machine-readable, and free of key material. The live
store (conformance.json) is the source of truth; this module only reads it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SNAPSHOT_PREFIX = "snapshot-"
KEEP_SNAPSHOTS = 30

_PASS = "pass"
_NON_PASS_BASELINE = {"fail", "unsupported", "unavailable"}


def default_drift_dir() -> Path:
    override = os.environ.get("FREELLMPOOL_DRIFT_DIR")
    if override:
        return Path(override)
    # Same rule as config.xdg_config_home (kept inline: no sibling imports).
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    return base / "freellmpool" / "drift"


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def take_snapshot(store_state: dict[str, Any], *, freellmpool_version: str,
                  generated_at: str | None = None) -> dict[str, Any]:
    """Project conformance store state into the public snapshot schema (v1)."""
    targets: dict[str, dict[str, dict[str, Any]]] = {}
    raw = store_state.get("targets") or {}
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            feats = entry.get("features") or {}
            if not isinstance(feats, dict):
                continue
            snap_feats: dict[str, dict[str, Any]] = {}
            for feature, info in feats.items():
                if not isinstance(info, dict):
                    continue
                snap_feats[str(feature)] = {
                    "status": info.get("status"),
                    "verified_at": info.get("verified_at"),
                    "classification": info.get("classification"),
                }
            targets[str(name)] = snap_feats
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at or utcnow(),
        "freellmpool": freellmpool_version,
        "targets": targets,
    }


def write_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return out


def _snapshot_files(drift_dir: Path) -> list[Path]:
    if not drift_dir.is_dir():
        return []
    return sorted(drift_dir.glob(SNAPSHOT_PREFIX + "*.json"))


def load_latest(drift_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    files = _snapshot_files(drift_dir)
    for path in reversed(files):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == SCHEMA_VERSION:
            return path, data
    return None, None


def save_run(snapshot: dict[str, Any], drift_dir: Path | None = None) -> Path:
    """Persist a dated snapshot, pruning to the newest KEEP_SNAPSHOTS well-formed files."""
    target = drift_dir or default_drift_dir()
    stamp = (snapshot.get("generated_at") or utcnow()).replace(":", "").replace("-", "")
    path = write_snapshot(snapshot, target / f"{SNAPSHOT_PREFIX}{stamp}.json")
    for stale in _snapshot_files(target)[: -KEEP_SNAPSHOTS]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


def _classify(before: str | None, after: str | None) -> str | None:
    if before == after:
        return None
    if before is None:
        return None  # first observation has no baseline
    if after is None:
        return "died" if before == _PASS else None  # stale negatives vanishing is cleanup
    if before == _PASS and after in {"fail", "unsupported"}:
        return "died"
    if before in _NON_PASS_BASELINE and after == _PASS:
        return "recovered"
    return "changed"


def classify_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Diff two snapshots into died/recovered/changed entries, stably ordered."""
    old_targets = old.get("targets") or {}
    new_targets = new.get("targets") or {}
    changes: list[dict[str, Any]] = []
    for name in sorted(set(old_targets) | set(new_targets)):
        before_feats = old_targets.get(name) or {}
        after_feats = new_targets.get(name) or {}
        for feature in sorted(set(before_feats) | set(after_feats)):
            before = (before_feats.get(feature) or {}).get("status")
            after = (after_feats.get(feature) or {}).get("status")
            kind = _classify(before, after)
            if kind is None:
                continue
            changes.append({
                "target": name, "feature": feature, "kind": kind,
                "before": before, "after": after,
                "observed_at": new.get("generated_at"),
            })
    return changes


def render_report(changes: list[dict[str, Any]], *, old_at: str | None = None,
                  new_at: str | None = None) -> str:
    if not changes:
        return f"No drift since {old_at or 'baseline'}."
    lines = [f"Drift: {len(changes)} change(s) since {old_at or 'baseline'} (as of {new_at or 'now'}):"]
    for change in changes:
        lines.append(
            f"  [{change['kind']}] {change['target']} {change['feature']}: "
            f"{change['before']} -> {change['after']} (observed {change['observed_at']})"
        )
    return "\n".join(lines)
