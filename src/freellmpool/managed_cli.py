"""Operational commands for the maintained free gateway."""
from __future__ import annotations

import argparse
import json
import math
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .config import effective_env
from .conformance import FEATURES, ConformanceStore
from .managed import ManagedPool
from .router import Target

TOOLS_BENCH_MINIMUM = 3


def tools_bench_warning(status: dict[str, Any]) -> str | None:
    """Warn when the fresh tools bench is too thin for agent traffic.

    Claude Code sends tools on every request; with fewer than
    TOOLS_BENCH_MINIMUM fresh tool-verified routes, one exhausted provider
    strands whole sessions behind 429s.
    """
    ready = status.get("tools_ready")
    if not isinstance(ready, int) or ready >= TOOLS_BENCH_MINIMUM:
        return None
    return (f"WARNING: only {ready} tool-capable route(s) with fresh evidence "
            f"(need {TOOLS_BENCH_MINIMUM}); agent tool calls may 429 — "
            f"run: freellmpool verify --features tools")


def cmd_status(args: argparse.Namespace) -> int:
    status = ManagedPool.from_default_config().managed_status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Strict free access: {status['eligible_routes']} eligible routes")
        for row in status["providers"]:
            print(f"  {row['id']:<14} {row['eligible']:>3} routes  {row['reason']}")
        depth = status.get("key_depth") or {}
        multi = sorted(pid for pid, n in depth.items() if isinstance(n, int) and n > 1)
        if multi:
            print(f"Multi-key rotation: {', '.join(f'{pid}={depth[pid]} keys' for pid in multi)}")
        warning = tools_bench_warning(status)
        if warning:
            print(f"\n{warning}")
        print("\nInspect enforced budgets and unknown limits: freellmpool status --json")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from .discovery import default_discovery_path, refresh_catalog, refresh_evidence
    env = {} if args.public_only else effective_env()
    path = default_discovery_path(env).with_name("public-discovery.json") if args.public_only else None
    result = refresh_catalog(env, provider_ids=args.provider, public_only=args.public_only, path=path)
    if getattr(args, "renew_evidence", False):
        refresh_evidence(env, provider_ids=args.provider, public_only=args.public_only)
    for pid, row in result.get("providers", {}).items():
        if not args.provider or pid in args.provider:
            print(f"{pid:<14} {row.get('status', 'unknown'):<16} {len(row.get('models', [])):>4} catalog routes")
    print("Discovery updated. Pricing, account eligibility, and protocol evidence remain separate checks.")
    return 0


def _verification_features(value: str) -> str:
    selected = [item.strip() for item in value.split(",") if item.strip()]
    if not selected:
        raise argparse.ArgumentTypeError("choose at least one verification feature")
    if len(selected) != len(set(selected)) or set(selected) - set(FEATURES):
        raise argparse.ArgumentTypeError("choose unique verification features from: " + ", ".join(FEATURES))
    return ",".join(selected)


def _verification_timeout(value: str | float) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("verification timeout must be a positive finite number") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("verification timeout must be a positive finite number")
    return seconds


def cmd_verify(args: argparse.Namespace) -> int:
    from .conformance import run_target_canaries
    from .maintenance import select_verification_targets
    try:
        features = tuple(_verification_features(args.features).split(","))
        timeout = _verification_timeout(args.timeout)
    except argparse.ArgumentTypeError as exc:
        print(f"freellmpool verify: {exc}", file=sys.stderr)
        return 2
    pool = ManagedPool.from_default_config()
    # ManagedPool always installs a store, unlike the optional legacy base.
    conformance = cast(ConformanceStore, pool.conformance)
    routes = [r for r in pool.snapshot().routes if r.modality == "chat" and r.automatic
              and (not args.provider or r.provider.id in args.provider)]
    targets = [Target(r.provider, r.model, 0, r.metadata.get("context")) for r in routes]
    selected = select_verification_targets(targets, conformance, max_targets=args.limit)
    if not selected:
        print("No current free route is ready to verify. Run freellmpool status or setup.")
        return 3
    rows: list[dict[str, Any]] = []
    for target in selected:
        results = run_target_canaries(target.provider, target.model, env=pool.env,
                                      features=features, timeout=timeout,
                                      call_fn=pool.probe_call, stream_fn=pool.probe_stream)
        for feature, result in results.items():
            conformance.record(target.provider, target.model, feature,
                                    status=result["status"], classification=result["classification"])
        rows.append({"target": target.name, "features": results})
        if not args.json:
            print(target.name + ": " + ", ".join(f"{name}={value['status']}" for name, value in results.items()))
    if args.json:
        print(json.dumps(rows, indent=2))
    pool.flush()
    return 0 if any(all(value["status"] == "pass" for value in row["features"].values()) for row in rows) else 3


def cmd_drift(args: argparse.Namespace) -> int:
    from . import __version__
    from . import drift as drift_mod

    if getattr(args, "probe", False):
        probe_args = argparse.Namespace(provider=getattr(args, "provider", None),
                                        limit=getattr(args, "limit", 8),
                                        features=getattr(args, "features", "chat,tools,streaming"),
                                        timeout=getattr(args, "timeout", 30), json=False)
        rc = cmd_verify(probe_args)
        if rc not in (0, 3):
            return rc
    pool = ManagedPool.from_default_config()
    conformance = cast(ConformanceStore, pool.conformance)
    snapshot = drift_mod.take_snapshot(conformance.snapshot(), freellmpool_version=__version__)
    drift_dir = drift_mod.default_drift_dir()
    _prev_path, previous = drift_mod.load_latest(drift_dir)
    saved = drift_mod.save_run(snapshot, drift_dir)
    emit = getattr(args, "emit", None)
    if emit:
        drift_mod.write_snapshot(snapshot, emit)
    if previous is None:
        targets = len(snapshot["targets"])
        print(f"Drift baseline recorded ({targets} target(s)) -> {saved}")
        return 0
    changes = drift_mod.classify_changes(previous, snapshot)
    old_at = previous.get("generated_at")
    new_at = snapshot.get("generated_at")
    if getattr(args, "json", False):
        print(json.dumps({"old": old_at, "new": new_at, "changes": changes}, indent=2))
    else:
        print(drift_mod.render_report(changes, old_at=old_at, new_at=new_at))
        print(f"snapshot: {saved}")
    return 0


def cmd_rag_index(args: argparse.Namespace) -> int:
    from . import rag as rag_mod

    pool = ManagedPool.from_default_config()
    try:
        stats = rag_mod.index_folder(pool, args.store or rag_mod.default_rag_path(), args.path,
                                     embed_model=args.embed_model)
    except (ValueError, OSError) as exc:
        print(f"freellmpool rag index: {exc}", file=sys.stderr)
        return 2
    print(f"Indexed {stats['chunks']} chunks from {stats['files']} file(s) "
          f"(embeddings: {stats['model']})")
    return 0


def cmd_rag_ask(args: argparse.Namespace) -> int:
    from . import rag as rag_mod

    pool = ManagedPool.from_default_config()
    try:
        result = rag_mod.ask_question(pool, args.store or rag_mod.default_rag_path(), args.question,
                                      k=args.k, model=args.model, providers=args.provider)
    except ValueError as exc:
        print(f"freellmpool rag ask: {exc}", file=sys.stderr)
        return 2
    print(result["answer"])
    print(f"\nSources ({result['provider']}/{result['model']}):")
    for i, cite in enumerate(result["citations"], 1):
        print(f"  [{i}] {cite['path']} (chunk {cite['chunk']}, score {cite['score']})")
    return 0


def install_maintenance(unit_dir: Path | None = None) -> list[str]:
    from .client_setup import atomic_write_public
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    commands = {
        "update": [sys.executable, "-m", "freellmpool", "maintenance", "--refresh"],
        "review": [sys.executable, "-m", "freellmpool", "maintenance", "--public-only", "--refresh"],
        "verify": [sys.executable, "-m", "freellmpool", "verify", "--limit", "4", "--timeout", "20"],
    }
    for name, command in commands.items():
        service = ("[Unit]\nDescription=Maintain free provider evidence\nAfter=network-online.target\n"
                   "\n[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\n"
                   "Environment=FREELLMPOOL_WAIT_SECONDS=0\nSuccessExitStatus=3\nExecStart="
                   + shlex.join(command).replace("%", "%%") + "\nTimeoutStartSec=15min\n")
        schedule = {"update": "daily\nOnBootSec=5min", "review": "Sun *-*-* 10:00:00", "verify": "*-*-* 11:00:00"}[name]
        timer = ("[Unit]\nDescription=Scheduled free provider maintenance\n\n[Timer]\nOnCalendar="
                 + schedule + "\nRandomizedDelaySec=10min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        atomic_write_public(unit_dir / f"freellmpool-{name}.service", service)
        atomic_write_public(unit_dir / f"freellmpool-{name}.timer", timer)
    return [f"freellmpool-{name}.timer" for name in commands]


def cmd_receipt(args: argparse.Namespace) -> int:
    from .savings import BASELINE_LABEL, usd_saved

    stats = ManagedPool.from_default_config().lifetime_stats()
    prompt = stats.get("prompt_tokens") or 0
    completion = stats.get("completion_tokens") or 0
    avoided = usd_saved(prompt, completion)
    if args.json:
        print(json.dumps({"requests": stats.get("requests", 0),
                          "prompt_tokens": prompt, "completion_tokens": completion,
                          "cache_hits": stats.get("cache_hits", 0),
                          "baseline": BASELINE_LABEL, "would_have_cost_usd": round(avoided, 4),
                          "paid_usd": 0}, indent=2))
        return 0
    print(f"Lifetime free usage: {stats.get('requests', 0)} requests, "
          f"{prompt + completion:,} tokens ({stats.get('cache_hits', 0)} cache hits)")
    print(f"Would have cost ~${avoided:,.2f} at {BASELINE_LABEL} — you paid $0.")
    return 0


def cmd_setup_clients(args: argparse.Namespace) -> int:
    from .client_setup import install_client_setup
    result = install_client_setup()
    timers = install_maintenance()
    manager = shutil.which("systemctl")
    start = ["systemctl", "--user", "enable", "--now", "freellmpool.service", *timers]
    started, failed = False, False
    if not args.no_start and manager:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, timeout=30)
            subprocess.run(start, check=True, capture_output=True, timeout=30)
            started = True
        except (OSError, subprocess.SubprocessError):
            failed = True
    if result["wrappers"]:
        print("Installed client launchers:")
        for wrapper in result["wrappers"]:
            print("  " + shlex.quote(wrapper))
        if any(Path(wrapper).name == "opencode-free" for wrapper in result["wrappers"]):
            print("T3's OpenCode provider uses the free profile when T3 settings are present.")
    else:
        print("No supported coding client was found. Install OpenCode, Hermes, or Claude Code, then run freellmpool setup-clients.")
    if started:
        print("Gateway service and maintenance timer starts requested.")
    else:
        print("Gateway startup was not confirmed; some services may have started." if failed else
              "Gateway files prepared; the gateway was not started by this command.")
        print("To run the gateway in this terminal:")
        print("Run: " + shlex.join([sys.executable, "-m", "freellmpool.client_setup", "service", "--root", result["root"]]))
        if manager:
            print("To start the user services: systemctl --user daemon-reload")
            print(shlex.join(start))
        print("Until scheduled maintenance is running, refresh checks with: freellmpool maintenance --refresh")
    print("Gateway: " + result["base_url"])
    return 1 if failed else 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .discovery import refresh_catalog
    from .onboarding import run_onboarding
    def check(pid: str, env: dict[str, str]) -> dict[str, Any]:
        result = refresh_catalog(env, provider_ids=[pid])
        return cast(dict[str, Any], result.get("providers", {}).get(pid, {"status": "error"}))
    result = run_onboarding(provider=args.provider, resume=args.resume, check=check,
                            eligibility=lambda pid: any(r.provider.id == pid for r in ManagedPool.from_default_config().snapshot().routes))
    if result == 0 and not args.no_clients:
        return cmd_setup_clients(argparse.Namespace(no_start=args.no_start))
    return result


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from .maintenance_cli import add_commands as add_maintenance
    add_maintenance(sub)
    setup = sub.add_parser("setup", help="guided, private, resumable free-access setup")
    setup.add_argument("--provider")
    setup.add_argument("--resume", action="store_true", default=True)
    setup.add_argument("--no-clients", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    setup.set_defaults(func=cmd_setup)
    update = sub.add_parser("update", help="refresh model APIs without inference")
    update.add_argument("--provider", action="append")
    update.add_argument("--public-only", action="store_true")
    update.add_argument("--renew-evidence", action="store_true", help="also recheck unchanged reviewed policy sources")
    update.set_defaults(func=cmd_update)
    verify = sub.add_parser("verify", help="bounded free-only chat/tool/stream checks")
    verify.add_argument("--provider", action="append")
    verify.add_argument("--limit", type=int, choices=range(1, 33), default=4)
    verify.add_argument("--features", type=_verification_features, default="chat,tools,streaming")
    verify.add_argument("--timeout", type=_verification_timeout, default=30)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)
    status = sub.add_parser("status", help="show free admission and allowance state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    receipt = sub.add_parser("receipt", help="show lifetime free usage and cost avoided")
    receipt.add_argument("--json", action="store_true")
    receipt.set_defaults(func=cmd_receipt)
    clients = sub.add_parser("setup-clients", help="install free client profiles and local maintenance")
    clients.add_argument("--no-start", action="store_true")
    clients.set_defaults(func=cmd_setup_clients)
    drift = sub.add_parser("drift", help="diff verify evidence vs previous snapshot")
    drift.add_argument("--probe", action="store_true",
                       help="run bounded verify probes before diffing")
    drift.add_argument("--provider", action="append")
    drift.add_argument("--limit", type=int, choices=range(1, 33), default=8)
    drift.add_argument("--features", type=_verification_features, default="chat,tools,streaming")
    drift.add_argument("--timeout", type=_verification_timeout, default=30)
    drift.add_argument("--json", action="store_true")
    drift.add_argument("--emit", help="also write the machine-readable snapshot here")
    drift.set_defaults(func=cmd_drift)
    rag = sub.add_parser("rag", help="index a folder and ask questions over it ($0)")
    rag_sub = rag.add_subparsers(dest="rag_command", required=True)
    rag_index = rag_sub.add_parser("index", help="embed a folder into the local vector store")
    rag_index.add_argument("path", help="folder of text/markdown files to index")
    rag_index.add_argument("--store", help="vector store file (default: ~/.config/freellmpool/rag.sqlite3)")
    rag_index.add_argument("--embed-model", help="embedding model (default: automatic free route)")
    rag_index.set_defaults(func=cmd_rag_index)
    rag_ask = rag_sub.add_parser("ask", help="answer a question from the local vector store")
    rag_ask.add_argument("question", help="question to answer from indexed sources")
    rag_ask.add_argument("--store", help="vector store file (default: ~/.config/freellmpool/rag.sqlite3)")
    rag_ask.add_argument("--k", type=int, default=4, help="sources to retrieve (default: 4)")
    rag_ask.add_argument("--model", help="chat model (default: automatic free route)")
    rag_ask.add_argument("--provider", action="append", help="limit chat to provider(s)")
    rag_ask.set_defaults(func=cmd_rag_ask)
