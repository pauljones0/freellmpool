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
        print("No supported coding client was found. Install OpenCode or Hermes, then run freellmpool setup-clients.")
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
    clients = sub.add_parser("setup-clients", help="install free client profiles and local maintenance")
    clients.add_argument("--no-start", action="store_true")
    clients.set_defaults(func=cmd_setup_clients)
