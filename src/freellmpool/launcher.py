"""One-command agent launcher: gateway + harness env + exec.

``freellmpool claude -- <agent args>`` starts the loopback proxy when none
is running (reusing a live one), sets the harness env, and exec-replaces
into the agent so signals propagate naturally. The spawned proxy outlives
the agent by design; reruns reuse it.

``freellmpool agent-start`` (G39) is the validated sibling: POSIX/binary/
port/model checks, managed admission, tool-route evidence, authed proxy
reuse-or-spawn with pidfile attribution, an opencode config for the opencode
harness, a seven-line receipt, then exec. See :func:`agent_start`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifacts import default_data_dir

READY_TIMEOUT = 60.0
# Identity marker our own gateway stamps on every /v1/models row
# (proxy._openai_models_payload sets owned_by="freellmpool"). gateway_ready()
# requires it so a foreign service on a pre-bound local port is never
# mistaken for our gateway.
_GATEWAY_OWNER = "freellmpool"
_MAX_MODELS_BYTES = 1024 * 1024
# Handles of proxies spawned by agent_start, kept for the process lifetime.
# A spawned proxy outlives the agent BY DESIGN; dropping the last handle
# while it runs would warn (ResourceWarning: subprocess still running).
# Rollback waits on failures, so only abandoned-live handles stay here.
_SPAWNED_PROXIES: list[Any] = []


class LauncherError(RuntimeError):
    pass


def _require_posix() -> None:
    """Single choke point for the agent-launch POSIX gate (Gap 5)."""
    if os.name != "posix":
        raise LauncherError(
            "agent launch requires POSIX (Linux or macOS); Windows is not supported"
        )


def claude_env(port: int, model: str, api_key: str = "dummy") -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_MODEL": model,
    }


_OPENCODE_MODEL_NAMES: tuple[tuple[str, str], ...] = (
    ("agent", "Agent — strongest healthy tier"),
    ("spread", "Spread — maximum pool breadth"),
    ("auto", "Auto — proxy default routing"),
    ("fast", "Fast — lowest latency"),
    ("quality", "Quality — capability matched"),
    ("fair", "Fair — provider quota spread"),
)


def opencode_config(
    port: int, model: str, *, authenticated: bool = False, host: str = "127.0.0.1"
) -> dict[str, object]:
    options: dict[str, object] = {"baseURL": f"http://{host}:{port}/v1"}
    if authenticated:
        options["apiKey"] = "{env:FREELLMPOOL_PROXY_KEY}"
        options["headerTimeout"] = 600000
        options["timeout"] = 600000
        options["chunkTimeout"] = 120000
    models: dict[str, object] = {alias: {"name": label} for alias, label in _OPENCODE_MODEL_NAMES}
    if model not in models:
        # Union emission for the opencode top-level model menu (F-7: the
        # proxy only routes freellmpool/<alias>; other entries are menu-only).
        models[model] = {}
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"freellmpool/{model}",
        "provider": {
            "freellmpool": {
                "name": "freellmpool (free pool)",
                "npm": "@ai-sdk/openai-compatible",
                "options": options,
                "models": models,
            }
        },
    }


def write_opencode_config(
    path: str | Path, port: int, model: str, *,
    authenticated: bool = False, host: str = "127.0.0.1",
) -> Path:
    """Write the opencode harness config without following symlinks.

    The payload goes to a same-dir temporary file (O_CREAT|O_EXCL|O_NOFOLLOW,
    mode 0o600) which is fsynced and then moved over the target with
    os.replace — replacing a symlink itself, so a planted link can neither
    redirect the write into a victim file nor survive as the config, and the
    previous config stays byte-intact until the replace.
    """
    path = Path(path)
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                opencode_config(port, model, authenticated=authenticated, host=host),
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                with contextlib.suppress(OSError):
                    os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise LauncherError(f"refusing to write opencode config to {path}: {exc}") from exc
    return path


def _probe_get(url: str, key: str | None) -> tuple[str, int, bytes]:
    """One /v1/models GET: (outcome, code, body), outcome in ok/http-error/transport-error."""
    try:
        if key is not None:
            request: str | urllib.request.Request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {key}"}
            )
        else:
            request = url
        with urllib.request.urlopen(request, timeout=3) as resp:
            code = resp.getcode()
            body = resp.read(_MAX_MODELS_BYTES + 1)
        return ("ok", code, body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_MAX_MODELS_BYTES + 1)
        except Exception:
            body = b""
        with contextlib.suppress(Exception):
            exc.close()
        return ("http-error", exc.code, body)
    except (urllib.error.URLError, OSError, ValueError):
        return ("transport-error", 0, b"")


def _classify_models_body(body: bytes) -> str:
    """marker | empty | nonempty-unmarked | invalid (oversize counts as invalid)."""
    if len(body) > _MAX_MODELS_BYTES:
        return "invalid"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "invalid"
    if not isinstance(payload, dict) or payload.get("object") != "list":
        return "invalid"
    data = payload.get("data")
    if data == []:
        return "empty"
    if not isinstance(data, list) or not data:
        return "invalid"
    if any(isinstance(row, dict) and row.get("owned_by") == _GATEWAY_OWNER for row in data):
        return "marker"
    return "nonempty-unmarked"


def probe_gateway(port: int, key: str | None) -> tuple[bool, str]:
    """One authed probe round: (ok, reason) with reason in ready/auth-mismatch/
    foreign-service/unreachable/no-routes. Round 1 is ``?ready=1``; round 2
    (plain) runs only when round 1 is 200-valid-empty, to discriminate
    ours-empty from foreign."""
    base = f"http://127.0.0.1:{port}/v1/models"
    outcome, code, body = _probe_get(base + "?ready=1", key)
    if outcome == "transport-error":
        return False, "unreachable"
    if outcome == "http-error":
        if code == 401:
            return False, "auth-mismatch"
        if code == 503 and body == b"":
            # Our shed-connections fast-drop: retried, never fail-fast.
            return False, "unreachable"
        return False, "foreign-service"
    if code != 200:
        return False, "foreign-service"
    shape = _classify_models_body(body)
    if shape == "marker":
        return True, "ready"
    if shape != "empty":
        # An unmarked non-empty list cannot be ours-empty: no round 2.
        return False, "foreign-service"
    outcome, code, body = _probe_get(base, key)
    if outcome == "transport-error":
        return False, "unreachable"
    if outcome == "http-error":
        if code == 401:
            return False, "auth-mismatch"
        if code == 503 and body == b"":
            return False, "unreachable"
        return False, "foreign-service"
    if code != 200:
        return False, "foreign-service"
    if _classify_models_body(body) == "marker":
        return False, "no-routes"
    return False, "foreign-service"


def gateway_ready(port: int, key: str | None = None) -> bool:
    """True only when the port answers with OUR gateway identity marker.

    A bare HTTP 200 is not enough: any local pre-bound port would be
    trusted and agent traffic routed to it. Require the freellmpool
    owned_by marker in the /v1/models body, with at least one ready model.
    """
    return probe_gateway(port, key)[0]


def await_proxy_ready(
    port: int, key: str | None, proc: Any, timeout: float = READY_TIMEOUT
) -> str:
    """Poll until ready/terminal/deadline; returns the terminal reason.

    ``proc`` is the live Popen handle on the spawn path, None on the
    no-routes reuse path. Raises LauncherError iff the child exits mid-loop.
    """
    deadline = time.monotonic() + timeout
    last = "unreachable"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise LauncherError(f"proxy exited during startup (code {proc.returncode})")
        ok, reason = probe_gateway(port, key)
        if reason in ("auth-mismatch", "foreign-service"):
            return reason
        if reason == "ready":
            return "ready"
        last = reason
        time.sleep(0.5)
    return last


def _shutdown_child(proc: Any, grace: float = 5.0) -> None:
    """Terminate, wait, kill, reap a spawned proxy child (best effort)."""
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _reap_in_background(proc)


def ensure_proxy(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """Start the loopback proxy unless one is already live. Returns True if spawned."""
    if gateway_ready(port):
        return False
    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool", "proxy", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise LauncherError(f"proxy exited during startup (code {proc.returncode})")
        if gateway_ready(port):
            return True
        time.sleep(0.5)
    _shutdown_child(proc)
    raise LauncherError(f"proxy on port {port} did not become ready within {timeout:.0f}s")


def _reap_in_background(proc: Any) -> None:
    """Keep waiting on an unkillable child in a daemon thread.

    Dropping the Popen handle would strand a zombie (and a ResourceWarning at
    GC); blocking here would hang teardown. The watcher only reaps.
    """

    def _watch() -> None:
        try:
            proc.wait(timeout=120)
        except Exception:  # noqa: BLE001 - last-chance reap; nothing left to do
            pass

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()


def launch(harness: str, agent_args: list[str], *, port: int, model: str) -> None:
    """Ensure the gateway, set harness env, and exec into the agent (no return)."""
    _require_posix()
    if harness not in ("claude", "opencode"):
        raise LauncherError(f"unknown harness: {harness}")
    started = ensure_proxy(port)
    if started:
        print(f"freellmpool: started loopback proxy on 127.0.0.1:{port}", file=sys.stderr)
    binary = shutil.which(harness)
    if binary is None:
        raise LauncherError(
            f"{harness} not found on PATH — install it first "
            f"({'npm i -g @anthropic-ai/claude-code' if harness == 'claude' else 'https://opencode.ai/install'})"
        )
    env = dict(os.environ)
    if harness == "claude":
        env.update(claude_env(port, model))
    else:
        cfg = write_opencode_config(
            default_data_dir() / f"freellmpool-opencode-{port}.json", port, model,
            authenticated=False)
        env["OPENCODE_CONFIG"] = str(cfg)
    os.execvpe(harness, [harness, *agent_args], env)


def resolve_proxy_auth(cli_key: str | None) -> tuple[str | None, str]:
    """Resolve the proxy key: flag > env > settings > none (or-semantics)."""
    if cli_key:
        return cli_key, "flag"
    from .config import settings

    env_key = os.environ.get("FREELLMPOOL_PROXY_KEY")
    if env_key:
        return env_key, "env"
    settings_key = settings().get("proxy_key")
    if settings_key:
        return str(settings_key), "settings"
    return None, "none"


def validate_agent_model(value: str, registry: Mapping[str, Any] | None) -> None:
    """Validate --model: alias, provider/model, or bare passthrough.

    Raises LauncherError (message without the freellmpool: prefix) on empty
    values and unknown providers. A dead registry (None) skips the check.
    """
    from .provider_registry import resolve_provider_ids
    from .routing_modes import PUBLIC_ROUTING_ALIASES

    if value.strip() == "":
        raise LauncherError(
            f"invalid --model '{value}': must be an alias "
            "[auto, agent, spread, fast, quality, fair], provider/model, or a bare name"
        )
    if value.strip().lower() in PUBLIC_ROUTING_ALIASES:
        return
    if "/" in value:
        if registry is None:
            return
        segment = value.split("/", 1)[0]
        _, unknown = resolve_provider_ids([segment], registry, None)
        if unknown:
            known = ", ".join(sorted(registry))
            raise LauncherError(
                f"unknown provider '{unknown[0]}'. Known registry ids: {known} "
                "(model must be an alias [auto, agent, spread, fast, quality, fair], "
                "provider/model, or a bare name)"
            )
        return
    # Bare passthrough (incl the claude-3-5-sonnet default): the proxy judges.
    return


def _cmdline_is_proxy(text: str) -> bool:
    """Stdout-only, case-sensitive cmdline predicate for managed proxies."""
    return "proxy" in text and ("freellmpool" in text or "ffp" in text)


def _read_proc_cmdline(pid: int) -> str | None:
    """Read /proc/<pid>/cmdline: None when /proc is unavailable, "" when dead."""
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return "" if Path("/proc").is_dir() else None
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def verify_proxy_process(pid: int) -> bool | None:
    """Verify a pidfile pid: True = proxy, False = dead/mismatch, None = unverifiable."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, errors="replace", timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raw = _read_proc_cmdline(pid)
        if raw is None:
            return None
        return _cmdline_is_proxy(raw)
    return _cmdline_is_proxy(completed.stdout)


def proxy_tools_ready_count(port: int, key: str | None) -> int:
    """Best-effort tools-verified row count via a fresh authed unfiltered GET."""
    try:
        if key is not None:
            request: str | urllib.request.Request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        else:
            request = f"http://127.0.0.1:{port}/v1/models"
        with urllib.request.urlopen(request, timeout=3) as resp:
            if resp.getcode() != 200:
                return 0
            raw = resp.read(_MAX_MODELS_BYTES + 1)
    except Exception:
        return 0
    if len(raw) > _MAX_MODELS_BYTES:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return 0
    count = 0
    for row in payload["data"]:
        features = row.get("verified_features") if isinstance(row, dict) else None
        if isinstance(features, list) and "tools" in features:
            count += 1
    return count


def _read_last_4k(path: str) -> str:
    """Tail mechanics: last 4096 bytes of the child-stderr tempfile, replaced."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _verified_pidfile_pid(pidfile: str) -> int | None:
    """Pidfile pid iff the record verifies as a live freellmpool proxy."""
    try:
        payload = json.loads(Path(pidfile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    return pid if verify_proxy_process(pid) is True else None


def _unlink_stale_own_pidfile(own_pidfile: str, child_pid: int) -> None:
    """Drop a dead-occupant record on spawn-path first ready (never ours)."""
    try:
        payload = json.loads(Path(own_pidfile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pid = payload.get("pid") if isinstance(payload, dict) else None
    if pid == child_pid:
        return
    with contextlib.suppress(OSError):
        Path(own_pidfile).unlink()


def _exit_terminal_reason(port: int, reason: str, key: str | None, source: str) -> int:
    """Print the auth-mismatch/foreign-service terminal literal; returns 3."""
    if reason == "auth-mismatch":
        if key is not None:
            print(
                f"freellmpool: auth mismatch on port {port}: the {source} key was "
                "rejected (401); fix the key and re-run",
                file=sys.stderr,
            )
        else:
            print(
                f"freellmpool: proxy on port {port} requires a key (protected); "
                "pass --api-key or set FREELLMPOOL_PROXY_KEY",
                file=sys.stderr,
            )
    else:
        print(
            f"freellmpool: port {port} serves a non-freellmpool service (foreign); "
            "free the port or pick another with --port",
            file=sys.stderr,
        )
    return 3


def agent_start(args: argparse.Namespace) -> int:
    """One-command agent start: stages S0-S7, then exec (exit 2 usage, 3 operational).

    S0 validates (POSIX, binary, port, registry probe, model grammar); S1
    judges managed admission read-only; S2 gathers tool-route evidence; S3
    resolves the proxy key; S4 reuses or spawns the loopback proxy; S5 writes
    the opencode config (opencode only); S6 prints the seven-line receipt; S7
    exec-replaces into the harness. Rollback reaps a spawned child plus the
    own pidfile and stderr tempfile on S4-deadline/S5/S7 failures and on
    KeyboardInterrupt during S4-S7; reused proxies are never touched.
    """
    harness = args.harness
    # S0 validate (side-effect-free; numeric order; S0 fully before any spawn).
    try:
        _require_posix()
    except LauncherError as exc:
        print(f"freellmpool: {exc}", file=sys.stderr)
        return 2
    if shutil.which(harness) is None:
        hint = (
            "npm i -g @anthropic-ai/claude-code"
            if harness == "claude"
            else "https://opencode.ai/install"
        )
        print(
            f"freellmpool: {harness} not found on PATH — install it first ({hint})",
            file=sys.stderr,
        )
        return 2
    port = args.port
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        print(f"freellmpool: invalid --port {port}: must be 1-65535", file=sys.stderr)
        return 2
    from .config import effective_env
    from .provider_registry import load_registry

    skip_model_registry_check = False
    registry: Mapping[str, Any] | None = None
    try:
        registry = load_registry(effective_env())
    except Exception:
        skip_model_registry_check = True
    model = args.model or ("agent" if harness == "opencode" else "claude-3-5-sonnet")
    if not skip_model_registry_check:
        try:
            validate_agent_model(model, registry)
        except LauncherError as exc:
            print(f"freellmpool: {exc}", file=sys.stderr)
            return 2
    # S1 preflight (read-only: one pool, then managed_status()).
    from .managed import ManagedPool

    if effective_env().get("FREELLMPOOL_LEGACY_ROUTER") == "1":
        print(
            "freellmpool: agent-start requires the managed router "
            "(unset FREELLMPOOL_LEGACY_ROUTER)",
            file=sys.stderr,
        )
        return 3
    try:
        status: Any = ManagedPool.from_default_config().managed_status()
    except Exception:
        print(
            "freellmpool: provider registry unreadable — cannot judge routes "
            "(reinstall or clear the policy bundle)",
            file=sys.stderr,
        )
        return 3
    if status.get("generation") == "invalid-config":
        print("freellmpool: invalid local restrictions; repair providers.toml", file=sys.stderr)
        return 3
    if not status.get("eligible_routes"):
        print(
            "freellmpool: no eligible routes — run freellmpool status, then "
            "freellmpool update or freellmpool setup as directed",
            file=sys.stderr,
        )
        return 3
    allowances = status.get("allowances") or []
    if allowances and all(row.get("remaining") == 0 for row in allowances):
        print(
            "freellmpool: all allowances exhausted — wait for reset (see freellmpool quota)",
            file=sys.stderr,
        )
        return 3
    # S2 evidence: skip when tool routes exist, else run real verify in-process.
    tools_n = status.get("tools_ready", 0)
    if tools_n > 0:
        from .managed_cli import tools_bench_warning

        warning = tools_bench_warning(status)
        if warning:
            print(warning, file=sys.stderr)
    else:
        from .managed_cli import cmd_verify

        verify_args = argparse.Namespace(
            provider=None, limit=args.verify_limit, features="tools",
            timeout=args.verify_timeout, json=False,
        )
        verify_rc = cmd_verify(verify_args)
        reread: Any = ManagedPool.from_default_config().managed_status()
        tools_n = reread.get("tools_ready", 0)
        if verify_rc != 0 and not tools_n:
            print(
                "freellmpool: verification found no tool-capable route "
                "(see verify output above); run the named command, or wait and re-run",
                file=sys.stderr,
            )
            return 3
    # S3 auth.
    key, source = resolve_proxy_auth(args.api_key)
    # S4-S7 with rollback of everything spawned by me.
    proc: Any = None
    err_path: str | None = None
    spawned_by_me = False
    actually_keyed = False
    own_pidfile = str(default_data_dir() / f"proxy-{port}.pid")
    deadline_timeout = READY_TIMEOUT

    def rollback() -> None:
        if proc is not None:
            _shutdown_child(proc)
        if spawned_by_me:
            with contextlib.suppress(OSError):
                Path(own_pidfile).unlink()
        if err_path is not None:
            with contextlib.suppress(OSError):
                Path(err_path).unlink()

    try:
        # S4 proxy: probe, then the decision table.
        _, reason = probe_gateway(port, key)
        if reason in ("auth-mismatch", "foreign-service"):
            return _exit_terminal_reason(port, reason, key, source)
        if reason == "ready":
            if key is not None:
                _, discriminant = probe_gateway(port, None)
                actually_keyed = discriminant == "auth-mismatch"
        elif reason == "unreachable":
            key, source = resolve_proxy_auth(args.api_key)
            actually_keyed = key is not None
            child_env = dict(os.environ)
            if key is not None:
                child_env["FREELLMPOOL_PROXY_KEY"] = key
            child_env["FREELLMPOOL_MANAGED_PIDFILE"] = own_pidfile
            try:
                err_handle = tempfile.NamedTemporaryFile(
                    prefix="freellmpool-proxy-err-", delete=False
                )
            except OSError as exc:
                print(
                    f"freellmpool: cannot start proxy on port {port}: {exc}",
                    file=sys.stderr,
                )
                return 3
            err_path = err_handle.name
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "freellmpool", "proxy",
                     "--host", "127.0.0.1", "--port", str(port)],
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=err_handle,
                    start_new_session=True,
                    close_fds=True,
                    cwd=None,
                )
            except OSError as exc:
                err_handle.close()
                with contextlib.suppress(OSError):
                    Path(err_path).unlink()
                err_path = None
                print(
                    f"freellmpool: cannot start proxy on port {port}: {exc}",
                    file=sys.stderr,
                )
                return 3
            except BaseException:
                err_handle.close()
                with contextlib.suppress(OSError):
                    Path(err_path).unlink()
                err_path = None
                raise
            err_handle.close()
            spawned_by_me = True
            _SPAWNED_PROXIES.append(proc)
            try:
                reason = await_proxy_ready(port, key, proc, timeout=deadline_timeout)
            except LauncherError as exc:
                tail = _read_last_4k(err_path or "")
                rollback()
                print(f"freellmpool: {exc}", file=sys.stderr)
                print(f"freellmpool: proxy stderr (last 4KB): {tail}", file=sys.stderr)
                return 3
            if reason == "ready":
                assert err_path is not None
                with contextlib.suppress(OSError):
                    Path(err_path).unlink()
                err_path = None
                _unlink_stale_own_pidfile(own_pidfile, proc.pid)
            else:
                rollback()
                if reason in ("auth-mismatch", "foreign-service"):
                    return _exit_terminal_reason(port, reason, key, source)
                if reason == "no-routes":
                    print(
                        f"freellmpool: proxy on port {port} has no ready routes "
                        f"(see freellmpool status); gave up after {deadline_timeout:.0f}s",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"freellmpool: proxy on port {port} did not become ready "
                        f"within {deadline_timeout:.0f}s",
                        file=sys.stderr,
                    )
                return 3
        else:  # no-routes: loop WITHOUT spawn until the deadline.
            reason = await_proxy_ready(port, key, None, timeout=deadline_timeout)
            if reason == "ready":
                if key is not None:
                    _, discriminant = probe_gateway(port, None)
                    actually_keyed = discriminant == "auth-mismatch"
            elif reason in ("auth-mismatch", "foreign-service"):
                return _exit_terminal_reason(port, reason, key, source)
            elif reason == "no-routes":
                print(
                    f"freellmpool: proxy on port {port} has no ready routes "
                    f"(see freellmpool status); gave up after {deadline_timeout:.0f}s",
                    file=sys.stderr,
                )
                return 3
            else:
                print(
                    f"freellmpool: proxy on port {port} did not become ready "
                    f"within {deadline_timeout:.0f}s",
                    file=sys.stderr,
                )
                return 3
        # S5 config: opencode only — claude skips config entirely.
        config_path: Path | None = None
        if harness == "opencode":
            config_path = default_data_dir() / f"freellmpool-opencode-{port}.json"
            try:
                write_opencode_config(
                    config_path, port, model, authenticated=(key is not None)
                )
            except LauncherError as exc:
                rollback()
                print(f"freellmpool: {exc}", file=sys.stderr)
                return 3
        # S6 receipt: exactly seven lines, pre-exec, no secrets.
        auth_field = f"protected:{source}" if actually_keyed else "keyless"
        if spawned_by_me:
            proxy_field = f"spawned (pid {proc.pid})"
        else:
            reused_pid = _verified_pidfile_pid(own_pidfile)
            proxy_field = f"reused (pid {reused_pid if reused_pid is not None else 'unknown'})"
        config_field = str(config_path) if config_path is not None else "none (claude uses env)"
        print(f"freellmpool: endpoint http://127.0.0.1:{port}/v1", file=sys.stderr)
        print(f"freellmpool: harness {harness}", file=sys.stderr)
        print(f"freellmpool: model {model}", file=sys.stderr)
        print(f"freellmpool: auth {auth_field}", file=sys.stderr)
        print(f"freellmpool: tools_ready {tools_n}", file=sys.stderr)
        print(f"freellmpool: proxy {proxy_field}", file=sys.stderr)
        print(f"freellmpool: config {config_field}", file=sys.stderr)
        # S7 exec: argv[0] is the harness name; PATH resolves at exec.
        agent_args = list(args.agent_args or [])
        if agent_args[:1] == ["--"]:
            agent_args.pop(0)
        env = dict(os.environ)
        if harness == "claude":
            env.update(claude_env(port, model, api_key=(key or "dummy")))
        else:
            assert config_path is not None
            env["OPENCODE_CONFIG"] = str(config_path)
            if key is not None:
                env["FREELLMPOOL_PROXY_KEY"] = key
        try:
            os.execvpe(harness, [harness, *agent_args], env)
        except OSError as exc:
            rollback()
            print(f"freellmpool: cannot exec {harness}: {exc}", file=sys.stderr)
            return 3
        return 0  # unreachable: exec replaces the process on success
    except KeyboardInterrupt:
        rollback()
        raise
