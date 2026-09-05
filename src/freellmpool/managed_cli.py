"""Operational commands for the maintained free gateway."""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .config import effective_env
from .conformance import ConformanceStore
from .managed import ManagedPool
from .router import Target


def cmd_status(args: argparse.Namespace) -> int:
    status = ManagedPool.from_default_config().managed_status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Strict free access: {status['eligible_routes']} eligible routes")
        for row in status["providers"]:
            print(f"  {row['id']:<14} {row['eligible']:>3} routes  {row['reason']}")
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


def cmd_verify(args: argparse.Namespace) -> int:
    from .conformance import run_target_canaries
    from .maintenance import select_verification_targets
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
    features = tuple(item.strip() for item in args.features.split(",") if item.strip())
    rows: list[dict[str, Any]] = []
    for target in selected:
        results = run_target_canaries(target.provider, target.model, env=pool.env,
                                      features=features, timeout=args.timeout,
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


def install_maintenance(unit_dir: Path | None = None) -> list[str]:
    from .client_setup import atomic_write
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
        atomic_write(unit_dir / f"freellmpool-{name}.service", service, mode=0o644)
        atomic_write(unit_dir / f"freellmpool-{name}.timer", timer, mode=0o644)
    return [f"freellmpool-{name}.timer" for name in commands]


def cmd_setup_clients(args: argparse.Namespace) -> int:
    from .client_setup import install_client_setup
    result = install_client_setup()
    timers = install_maintenance()
    if not args.no_start and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "freellmpool.service", *timers], check=True)
    print("Free client profiles installed. Start with opencode-free or hermes-free.")
    print("T3's OpenCode provider uses the free profile when T3 settings are present.")
    print("Gateway: " + result["base_url"])
    return 0


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
    verify.add_argument("--features", default="chat,tools,streaming")
    verify.add_argument("--timeout", type=float, default=30)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)
    status = sub.add_parser("status", help="show free admission and allowance state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    clients = sub.add_parser("setup-clients", help="install free client profiles and local maintenance")
    clients.add_argument("--no-start", action="store_true")
    clients.set_defaults(func=cmd_setup_clients)
