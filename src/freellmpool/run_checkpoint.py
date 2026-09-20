"""G21 run survivor: incremental checkpoints for long fan-out runs.

A checkpoint records per-target results (text or error) as they arrive, so
a run killed by 429s, outages, or SIGKILL can resume with only the missing
labels re-attempted. Replayed answers never touch the pool, so quotas
account exactly the live calls. Checkpoints store answer texts but never
prompts; files are owner-only (0o600) under a 0o700 runs directory.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .panel import PanelAnswer

CHECKPOINT_SCHEMA = 1
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def default_runs_dir() -> Path:
    """Owner-only directory for run checkpoints (XDG-aware)."""
    # Same rule as config.xdg_config_home (kept inline: no sibling imports).
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    runs = base / "freellmpool" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    try:
        runs.chmod(0o700)
    except OSError:
        pass
    return runs


def checkpoint_path(run_id: str, *, runs_dir: Path | None = None) -> Path:
    """Resolve a run id to its checkpoint file; rejects path traversal."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return (runs_dir or default_runs_dir()) / f"{run_id}.json"


def new_run_id() -> str:
    """Generate a filesafe unique run id (UTC stamp + randomness)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(2)}"


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunCheckpoint:
    run_id: str
    kind: str
    max_tokens: int | None
    targets: list[str] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, label: str, text: str | None = None, error: str | None = None,
               *, fresh: bool = True, **extra: Any) -> None:
        """Record one target outcome (thread-safe, in-memory)."""
        with self._lock:
            self.results[label] = {"label": label, "text": text, "error": error,
                                   "fresh": fresh, "at": _utcnow(), **extra}
            if label not in self.targets:
                self.targets.append(label)

    def record_and_save(self, label: str, text: str | None = None,
                        error: str | None = None, *, fresh: bool = True,
                        **extra: Any) -> None:
        """Record one outcome and persist immediately (survives SIGKILL)."""
        self.record(label, text, error, fresh=fresh, **extra)
        self.save()

    @property
    def answered(self) -> dict[str, dict[str, Any]]:
        """Labels with a recorded successful text (replayable without calls)."""
        with self._lock:
            return {label: entry for label, entry in self.results.items()
                    if entry.get("text") is not None and not entry.get("error")}

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"schema": CHECKPOINT_SCHEMA, "run_id": self.run_id, "kind": self.kind,
                    "max_tokens": self.max_tokens, "targets": list(self.targets),
                    "results": {label: dict(entry)
                                for label, entry in self.results.items()}}

    def save(self, path: Path | str | None = None) -> Path:
        """Persist atomically with owner-only permissions."""
        from .client_setup import atomic_write

        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no checkpoint path (pass one or set RunCheckpoint.path)")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        self.path = target
        return target

    @classmethod
    def load(cls, path: Path | str) -> RunCheckpoint:
        """Load a checkpoint; raises FileNotFoundError when absent."""
        target = Path(path)
        data = json.loads(target.read_text())
        if data.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError(f"unsupported checkpoint schema in {target}")
        return cls(run_id=data["run_id"], kind=data.get("kind", "tokenmax"),
                   max_tokens=data.get("max_tokens"),
                   targets=list(data.get("targets", [])),
                   results={label: dict(entry)
                            for label, entry in data.get("results", {}).items()},
                   path=target)


def _pick_label(pick: Any) -> str:
    return f"{pick.provider.id}/{pick.model}"


def resume_plan(checkpoint: RunCheckpoint
                ) -> tuple[list[tuple[str, str]], list[Any]]:
    """Resume the ORIGINAL target set: answered labels replay, failed and
    never-attempted labels re-run. The current bench is deliberately NOT
    re-selected, so a resume never fans out wider than the first attempt.
    """
    from types import SimpleNamespace

    answered = checkpoint.answered
    replay: list[tuple[str, str]] = []
    todo: list[Any] = []
    for label in checkpoint.targets:
        if label in answered:
            replay.append((label, answered[label]["text"]))
        else:
            provider_id, _, model = label.partition("/")
            todo.append(SimpleNamespace(provider=SimpleNamespace(id=provider_id),
                                        model=model or label))
    return replay, todo


def merge_answers(replay: list[tuple[str, str]],
                  fresh: list[tuple[str, str]]) -> list[tuple[str, str, bool]]:
    """Merge replayed and fresh answers with provenance flags (replay first)."""
    return [(label, text, False) for label, text in replay] + [
        (label, text, True) for label, text in fresh]


def _safe_int(value: Any) -> int:
    """Best-effort int from possibly-corrupt checkpoint data; 0 on failure."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def replay_panel_answer(entry: dict[str, Any]) -> PanelAnswer:
    """Rebuild a PanelAnswer from a checkpoint entry, marked replayed."""
    from .panel import PanelAnswer

    return PanelAnswer(
        provider_id=str(entry.get("provider_id", "?")),
        model=str(entry.get("model", "?")),
        label=str(entry.get("label", "?")),
        family=entry.get("family"),
        text=entry.get("text"),
        latency_ms=_safe_int(entry.get("latency_ms", 0)),
        error=entry.get("error"),
        cached=bool(entry.get("cached", False)),
        replayed=True,
    )
