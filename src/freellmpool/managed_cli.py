"""Operational commands for the maintained free gateway."""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from .catalog import load_external_catalog
from .config import effective_env, load_catalog
from .conformance import FEATURES, ConformanceStore
from .credential_store import _secret
from .managed import ManagedPool
from .onboarding import save_key_values
from .provider_registry import load_registry, resolve_provider_ids
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


def _unknown_provider_error(unknown: list[str], registry: dict[str, Any]) -> int:
    """Exit-2 usage error for --provider literals nothing knows (G36).

    Single-id bytes match keys-check exactly; the multi-id join is this
    module's own shape. Literals echoed verbatim (keys-check parity).
    """
    known = ", ".join(sorted(registry))
    print(f"freellmpool: unknown provider '{', '.join(unknown)}'. "
          f"Known registry ids: {known}", file=sys.stderr)
    return 2


def _verify_extra_ids() -> dict[str, str]:
    """User-catalog + external ids verify accepts (keys-check parity, G36).

    Never raises: every load is wrapped so validation never tracebacks;
    the snapshot judges config downstream. User-first on intra-extra
    collisions (unobservable: extra-only ids never match routes).
    """
    extra: dict[str, str] = {}
    try:
        for entry in load_catalog():
            extra.setdefault(entry.id.lower(), entry.id)
    except Exception:  # noqa: BLE001 — validation never tracebacks
        pass
    try:
        for item in load_external_catalog():
            extra.setdefault(item.slug.lower(), item.slug)
            extra.setdefault(item.name.lower(), item.slug)
    except Exception:  # noqa: BLE001 — validation never tracebacks
        pass
    return extra


def cmd_status(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from .heal import HealStore, default_heal_path, gate_open

    pool = ManagedPool.from_default_config()
    status = pool.managed_status()
    # Offer-only: status reads heal state but never probes.
    view = HealStore(default_heal_path(getattr(pool, "env", None))).view()
    ready = status.get("tools_ready", 0)
    thin = isinstance(ready, int) and ready < TOOLS_BENCH_MINIMUM
    blocked = gate_open(view, datetime.now(UTC))
    status["heal_available"] = bool(thin and blocked is None)
    status["heal_cooldown_until"] = view["cooldown_until"]
    status["last_heal"] = view["last_heal"]
    status["heal_probes_today"] = view["probes_today"]
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Strict free access: {status['eligible_routes']} eligible routes")
        for row in status["providers"]:
            warned = f"; WARNING: {row['warning']}" if row.get("warning") else ""
            print(f"  {row['id']:<14} {row['eligible']:>3} routes  {row['reason']}{warned}")
        depth = status.get("key_depth") or {}
        multi = sorted(pid for pid, n in depth.items() if isinstance(n, int) and n > 1)
        if multi:
            print(f"Multi-key rotation: {', '.join(f'{pid}={depth[pid]} keys' for pid in multi)}")
        warning = tools_bench_warning(status)
        if warning:
            print(f"\n{warning}")
        if thin and status.get("chat_routes") == 0:
            # G35: no healable routes at all — prescribing --heal would
            # dead-end (exit 3 "run update"). Name both tools; the
            # per-row reasons above disambiguate. Takes precedence
            # over the gate lines: update/setup aren't heal-gated.
            print(f"Bench thin with no healable routes "
                  f"({ready} fresh, need {TOOLS_BENCH_MINIMUM}); run "
                  f"freellmpool update, or freellmpool setup to connect access")
        elif thin and blocked is None:
            print(f"Bench thin: run freellmpool verify --heal "
                  f"({ready} fresh, need {TOOLS_BENCH_MINIMUM})")
        elif thin and blocked == "cooldown":
            print(f"Heal on cooldown until {view['cooldown_until']}")
        elif thin:
            print("Heal budget exhausted for today")
        last = view["last_heal"]
        if isinstance(last, dict):
            print(f"Last heal: {last.get('at')} ({last.get('passes', 0)} passes, "
                  f"{last.get('probes', 0)} probes, via {last.get('trigger')})")
        print(f"Heal probes today: {view['probes_today']}")
        print("\nInspect enforced budgets and unknown limits: freellmpool status --json")
    return 0


_UPDATE_FOOTER_OK = ("Discovery updated. Pricing, account eligibility, and protocol evidence "
                     "remain separate checks.")
_UPDATE_BUSY_LINE = ("freellmpool: another catalog refresh is running; showing last-good catalog "
                     "without refreshing (retry `freellmpool update`).")
_UPDATE_BUSY_RENEW_SKIP = "freellmpool: evidence renewal skipped (catalog refresh is busy)."


def _read_last_good(path: Path) -> dict[str, Any]:
    from .discovery import _load

    data = _load(path)
    data["providers"] = {pid: row for pid, row in data["providers"].items() if isinstance(row, dict)}
    return data


_FALLBACK_RENDER_SAFE = re.compile(r"[^A-Za-z0-9_.:/-]+")


def _render_fallback_id(value: Any) -> str | None:
    """Slash-preserving single-line id for fallback display (defense in depth)."""
    if not isinstance(value, str):
        return None
    return _FALLBACK_RENDER_SAFE.sub("_", value)[:256] or None


def _fallback_line(row: dict[str, Any]) -> str | None:
    """Bounded per-row fallback line for blocked rows; None when nothing to show."""
    if not isinstance(row, dict) or row.get("status") != "blocked":
        return None
    names = row.get("fallback_models", [])
    if not isinstance(names, list):
        return None
    rendered = [text for text in (_render_fallback_id(name) for name in names) if text]
    if not rendered:
        return None
    suffix = f" +{len(rendered) - 10} more" if len(rendered) > 10 else ""
    return ("    reviewed fallback candidates (availability unverified): "
            + ", ".join(rendered[:10]) + suffix)


def cmd_update(args: argparse.Namespace) -> int:
    from .discovery import (
        DiscoveryBusy,
        budget_seconds,
        count_snapshot,
        default_discovery_path,
        discovery_summary_line,
        refresh_catalog,
        refresh_evidence,
        safe_print,
        sanitize_pid,
        stderr_progress_printer,
    )
    env = {} if args.public_only else effective_env()
    if args.provider:
        # G36: validate FIRST — an unknown literal exits 2 instead of
        # dying in _prepare_refresh. Registry mirrors refresh's
        # own load (overlay-included); canonical ids feed refresh,
        # evidence renewal, and the display filter below.
        registry = load_registry() if args.public_only else load_registry(env)
        canonical, unknown = resolve_provider_ids(args.provider, registry)
        if unknown:
            return _unknown_provider_error(unknown, registry)
        args.provider = canonical
    path = default_discovery_path(env).with_name("public-discovery.json") if args.public_only else None
    start = time.monotonic()
    try:
        result = refresh_catalog(env, provider_ids=args.provider, public_only=args.public_only, path=path,
                                 deadline=start + budget_seconds(env), progress=stderr_progress_printer())
        refreshed = True
    except DiscoveryBusy:
        safe_print(_UPDATE_BUSY_LINE, file=sys.stderr)
        if getattr(args, "renew_evidence", False):
            safe_print(_UPDATE_BUSY_RENEW_SKIP, file=sys.stderr)
        result = _read_last_good(path or default_discovery_path(env))
        refreshed = False
    if getattr(args, "renew_evidence", False) and refreshed:
        refresh_evidence(env, provider_ids=args.provider, public_only=args.public_only)
    rows = result.get("providers", {})
    shown = {pid: row for pid, row in rows.items() if not args.provider or pid in args.provider}
    safe_print(discovery_summary_line({"providers": shown}, time.monotonic() - start), file=sys.stderr)
    for pid, row in shown.items():
        models = row.get("models", [])
        safe_print(f"{sanitize_pid(pid):<14} {row.get('status', 'unknown'):<16} {len(models) if isinstance(models, list) else 0:>4} catalog routes")
        line = _fallback_line(row) if isinstance(row, dict) else None
        if line is not None:
            safe_print(line)
    ok, deferred, failed = count_snapshot({"providers": shown})
    # Never print "updated" unless this run refreshed every requested provider to ok.
    if refreshed and failed == 0 and deferred == 0:
        safe_print(_UPDATE_FOOTER_OK)
    else:
        blocked = any(isinstance(row, dict) and row.get("status") == "blocked"
                      for row in shown.values())
        denied = any(isinstance(row, dict) and row.get("status") == "denied"
                     for row in shown.values())
        retryable = deferred > 0 or any(not isinstance(row, dict) or row.get("status") not in {"ok", "deferred", "blocked", "denied"}
                                        for row in shown.values())
        trailer = ("run `freellmpool update` to retry. Pricing, account eligibility, and protocol "
                   "evidence remain separate checks.")
        if denied and not blocked and not retryable:
            # 019: denied rows need out-of-band scope verification first; a
            # blind-retry trailer would misdirect.
            trailer = ("denied listings need scope/account verification with the provider, then re-verdict on "
                       "a later `freellmpool update --provider PROVIDER` re-check, verdict may persist. "
                       "Pricing, account eligibility, and protocol evidence remain separate checks.")
        elif blocked and denied and not retryable:
            trailer = ("blocked/denied listings re-verdict on a later `freellmpool update --provider PROVIDER` "
                       "re-check (verify scope/account for denied rows first), verdict may persist. "
                       "Pricing, account eligibility, and protocol evidence remain separate checks.")
        elif blocked and not retryable:
            trailer = ("blocked listings re-verdict on a later `freellmpool update --provider PROVIDER` "
                       "re-check, verdict may persist. Pricing, account eligibility, and protocol "
                       "evidence remain separate checks.")
        elif denied and not blocked:
            trailer = ("run `freellmpool update` to retry, but denied listings need scope/account verification "
                       "with the provider, then re-verdict on a later re-check and the verdict may persist. "
                       "Pricing, account eligibility, and protocol evidence remain separate checks.")
        elif blocked and denied:
            trailer = ("run `freellmpool update` to retry, but blocked/denied listings re-verdict on a later "
                       "re-check (verify scope/account for denied rows first) and the verdict may persist. "
                       "Pricing, account eligibility, and protocol evidence remain separate checks.")
        elif blocked:
            trailer = ("run `freellmpool update` to retry, but blocked listings re-verdict on a later "
                       "re-check and the verdict may persist. Pricing, account eligibility, and protocol "
                       "evidence remain separate checks.")
        safe_print(f"Discovery incomplete: {ok} ok, {deferred} deferred, {failed} failed; {trailer}")
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
    from .heal import HealStore, autoheal_enabled, default_heal_path, run_heal
    from .maintenance import select_verification_targets
    try:
        features = tuple(_verification_features(args.features).split(","))
        timeout = _verification_timeout(args.timeout)
    except argparse.ArgumentTypeError as exc:
        print(f"freellmpool verify: {exc}", file=sys.stderr)
        return 2
    if args.provider:
        # G36: validate before pool load and heal — a bad literal exits 2
        # instead of burning a heal run or misdirecting to exit 3. Universe
        # is registry ∪ user-catalog ∪ external (keys-check acceptance
        # parity); extra-only ids pass through and exit 3 as today. A dead
        # registry skips validation; the tolerant downstream judges.
        try:
            registry = load_registry(effective_env())
        except Exception:  # noqa: BLE001 — validation never tracebacks
            registry = None
        if registry is not None:
            canonical, unknown = resolve_provider_ids(
                args.provider, registry, _verify_extra_ids())
            if unknown:
                return _unknown_provider_error(unknown, registry)
            args.provider = canonical
    pool = ManagedPool.from_default_config()
    ready = pool.managed_status().get("tools_ready", 0)
    thin = isinstance(ready, int) and ready < TOOLS_BENCH_MINIMUM
    heal_flag = bool(getattr(args, "heal", False))
    # Review fix 8: consent, budget, and paths all read pool.env
    # (effective env), never bare os.environ.
    healing = thin and (heal_flag or autoheal_enabled(pool.env))
    healed_empty = False
    if healing:
        outcome = run_heal(pool, HealStore(default_heal_path(pool.env)),
                           trigger="verify", limit=args.limit, timeout=timeout)
        if outcome["reason"] == "io-error":
            return 1
        healed_empty = outcome["reason"] == "empty"
    # ManagedPool always installs a store, unlike the optional legacy base.
    conformance = cast(ConformanceStore, pool.conformance)
    snapshot = pool.snapshot()
    # G35: heal ignores --provider, so the offer matches the action it
    # advertises iff UNFILTERED chat+automatic routes exist. The offer
    # moved below routes (selection still post-heal: evidence flow
    # intact); with no healable routes the line-255 message carries it.
    healable = any(r.modality == "chat" and r.automatic for r in snapshot.routes)
    if thin and not healing and healable:
        print(f"Bench thin: run freellmpool verify --heal "
              f"({ready} fresh, need {TOOLS_BENCH_MINIMUM})", file=sys.stderr)
    routes = [r for r in snapshot.routes if r.modality == "chat" and r.automatic
              and (not args.provider or r.provider.id in args.provider)]
    targets = [Target(r.provider, r.model, 0, r.metadata.get("context")) for r in routes]
    selected = select_verification_targets(targets, conformance, max_targets=args.limit)
    if not selected:
        if not healable and not healed_empty:
            print("No verifiable routes: run freellmpool update, or "
                  "freellmpool setup to connect access.")
        elif healable:
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
    # G36: without --probe, --provider is a documented-ignore (see its help
    # text): a snapshot diff has no probes to filter, so the flag is neither
    # validated nor applied.
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
    notes = ""
    if stats.get("truncated"):
        notes += " [INCOMPLETE: collection capped — re-index a smaller folder]"
    if stats.get("skipped_oversize"):
        notes += f" [{stats['skipped_oversize']} oversize file(s) skipped]"
    print(f"Indexed {stats['chunks']} chunks from {stats['files']} file(s) "
          f"(embeddings: {stats['model']}){notes}")
    return 0


def cmd_rag_leaderboard(args: argparse.Namespace) -> int:
    from . import embed_leaderboard as board_mod
    from .managed import ManagedPool

    pool = ManagedPool.from_default_config()
    providers = args.providers.split(",") if args.providers else None
    scores = board_mod.run_leaderboard(pool, providers=providers, k=args.k)
    print(board_mod.render_table(scores, args.k))
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
                          "prefix_cache_hits": stats.get("prefix_cache_hits", 0),
                          "prefix_tokens_avoided": stats.get("prefix_tokens_avoided", 0),
                          "baseline": BASELINE_LABEL, "would_have_cost_usd": round(avoided, 4),
                          "paid_usd": 0}, indent=2))
        return 0
    print(f"Lifetime free usage: {stats.get('requests', 0)} requests, "
          f"{prompt + completion:,} tokens ({stats.get('cache_hits', 0)} cache hits, "
          f"{stats.get('prefix_cache_hits', 0)} prefix-cache hits, "
          f"{stats.get('prefix_tokens_avoided', 0):,} prefix tokens avoided)")
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


def _cmd_setup_stdin(args: argparse.Namespace) -> int:
    """Save one piped key without the interactive wizard (G36).

    Mirrors onboarding.main's --stdin semantics on the real CLI: provider
    required, literal validated, TTY refused (read() would echo the key),
    failures exit 2 without echoing the value, success prints the next
    step and returns 0 without running setup-clients. Never raises.
    """
    if args.provider is None:
        print("freellmpool: key input requires --provider", file=sys.stderr)
        return 2
    try:
        registry = load_registry(effective_env())
    except Exception:  # noqa: BLE001 — validation never tracebacks
        print("freellmpool: provider registry is unavailable", file=sys.stderr)
        return 2
    canonical, unknown = resolve_provider_ids([args.provider], registry)
    if unknown:
        return _unknown_provider_error(unknown, registry)
    record = registry[canonical[0]]
    if not record.get("credential_env"):
        print("freellmpool: this provider has no supported credential field",
              file=sys.stderr)
        return 2
    try:
        tty = sys.stdin.isatty()
    except (OSError, ValueError):
        print("freellmpool: could not read key from standard input", file=sys.stderr)
        return 2
    if tty:
        print("freellmpool: --stdin reads a piped key; from a terminal use "
              "interactive setup instead", file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read(16385)
    except UnicodeDecodeError:
        print("freellmpool: could not decode key from standard input "
              "(expected UTF-8 text)", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("freellmpool: could not read key from standard input", file=sys.stderr)
        return 2
    if len(raw) > 16384:
        # _secret strips before capping, which would accept a padded 16385-char
        # transmission; exactness here diverges 2 lines from the frozen entry.
        print("freellmpool: enter one non-empty credential line without "
              "control characters", file=sys.stderr)
        return 2
    try:
        value = _secret(raw)
    except ValueError as exc:
        print(f"freellmpool: {exc}", file=sys.stderr)
        return 2
    try:
        save_key_values({record["credential_env"]: value})
    except (ValueError, OSError) as exc:
        print(f"freellmpool: {exc}", file=sys.stderr)
        return 2
    print("Credential saved privately. Verify account and permissions with: "
          f"freellmpool setup --provider {canonical[0]}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .discovery import (
        DiscoveryBusy,
        budget_seconds,
        cf_probe_outcome,
        refresh_catalog,
        stderr_progress_printer,
    )
    from .onboarding import run_onboarding
    if getattr(args, "stdin", False):
        return _cmd_setup_stdin(args)
    def check(pid: str, env: dict[str, str]) -> dict[str, Any]:
        # CTO-2: setup is interactive (user-paced), so each check arms its own
        # full budget. A shared wall deadline would expire while the user reads.
        try:
            # G32: a fresh probe cache per check; the outcome rides back on
            # the in-memory row only (the snapshot was already written).
            probes: dict[str, str] = {}
            result = refresh_catalog(env, provider_ids=[pid],
                                     deadline=time.monotonic() + budget_seconds(env),
                                     progress=stderr_progress_printer(),
                                     cf_probe_cache=probes)
        except DiscoveryBusy:
            return {"status": "error",
                    "note": "Another catalog refresh is running; retry this provider later."}
        row = cast(dict[str, Any], result.get("providers", {}).get(pid, {"status": "error"}))
        outcome = cf_probe_outcome(probes)
        if outcome is not None:
            row["cloudflare_probe"] = outcome
        return row
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
    setup.add_argument("--stdin", action="store_true",
                       help="read one key privately from standard input "
                            "(requires --provider; other setup flags are ignored)")
    setup.add_argument("--resume", action="store_true", default=True,
                       help="resume saved setup progress (default): pass over already-checked and skipped providers")
    setup.add_argument("--no-resume", action="store_false", dest="resume",
                       help="re-check every provider, ignoring saved progress")
    setup.add_argument("--no-clients", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    setup.set_defaults(func=cmd_setup)
    update = sub.add_parser("update", help="refresh model APIs without inference")
    update.add_argument("--provider", action="append")
    update.add_argument("--public-only", action="store_true")
    update.add_argument("--renew-evidence", action="store_true", help="also recheck unchanged reviewed policy sources")
    update.set_defaults(func=cmd_update)
    verify = sub.add_parser("verify", help="bounded free-only chat/tool/stream checks",
                            epilog="exit 0: at least one target fully verified; "
                                   "exit 1: heal state/evidence I/O failure; "
                                   "exit 2: usage error; "
                                   "exit 3: nothing verified (no targets or no full pass)")
    verify.add_argument("--provider", action="append")
    verify.add_argument("--limit", type=int, choices=range(1, 33), default=4)
    verify.add_argument("--features", type=_verification_features, default="chat,tools,streaming")
    verify.add_argument("--timeout", type=_verification_timeout, default=30)
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--heal", action="store_true",
                        help="when the tool bench is thin, re-probe a bounded set first")
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
    drift.add_argument("--provider", action="append",
                       help="limit probes to provider(s); only used with --probe")
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
    rag_index.add_argument("--embed-model", help="embedding model (default: leaderboard winner)")
    rag_index.set_defaults(func=cmd_rag_index)
    rag_board = rag_sub.add_parser("leaderboard", help="rank free embedding routes on a fixed retrieval fixture")
    rag_board.add_argument("-p", "--providers", help="comma-separated provider ids to rank")
    rag_board.add_argument("--k", type=int, default=3, help="recall cutoff (default: 3)")
    rag_board.set_defaults(func=cmd_rag_leaderboard)
    rag_ask = rag_sub.add_parser("ask", help="answer a question from the local vector store")
    rag_ask.add_argument("question", help="question to answer from indexed sources")
    rag_ask.add_argument("--store", help="vector store file (default: ~/.config/freellmpool/rag.sqlite3)")
    rag_ask.add_argument("--k", type=int, default=4, help="sources to retrieve (default: 4)")
    rag_ask.add_argument("--model", help="chat model (default: automatic free route)")
    rag_ask.add_argument("--provider", action="append", help="limit chat to provider(s)")
    rag_ask.set_defaults(func=cmd_rag_ask)
