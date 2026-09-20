"""One-command agent launcher: gateway + harness env + exec.

``freellmpool claude -- <agent args>`` starts the loopback proxy when none
is running (reusing a live one), sets the harness env, and exec-replaces
into the agent so signals propagate naturally. The spawned proxy outlives
the agent by design; reruns reuse it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .artifacts import default_data_dir

READY_TIMEOUT = 60.0
# Identity marker our own gateway stamps on every /v1/models row
# (proxy._openai_models_payload sets owned_by="freellmpool"). gateway_ready()
# requires it so a foreign service on a pre-bound local port is never
# mistaken for our gateway.
_GATEWAY_OWNER = "freellmpool"
_MAX_MODELS_BYTES = 1024 * 1024


class LauncherError(RuntimeError):
    pass


def claude_env(port: int, model: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_MODEL": model,
    }


def opencode_config(port: int, model: str) -> dict[str, object]:
    base = f"http://127.0.0.1:{port}/v1"
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"freellmpool/{model}",
        "provider": {
            "freellmpool": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "freellmpool (free pool)",
                "options": {"baseURL": base},
                "models": {model: {}, "agent": {}, "auto": {}},
            }
        },
    }


def write_opencode_config(path: str | Path, port: int, model: str) -> Path:
    """Write the opencode harness config without following symlinks.

    A pre-existing path is unlinked first (unlinking a symlink removes the
    link, never its target), then the file is created fresh with
    O_CREAT|O_EXCL|O_NOFOLLOW and mode 0o600, so a planted symlink can
    neither redirect the write into a victim file nor survive as the config.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(opencode_config(port, model), indent=2) + "\n").encode("utf-8")
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LauncherError(f"refusing to write opencode config to {path}: {exc}") from exc
    try:
        if hasattr(os, "fchmod"):
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(payload)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
    return path


def _is_gateway_payload(raw: bytes) -> bool:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("object") != "list":
        return False
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return False
    return any(
        isinstance(row, dict) and row.get("owned_by") == _GATEWAY_OWNER for row in data
    )


def gateway_ready(port: int) -> bool:
    """True only when the port answers with OUR gateway identity marker.

    A bare HTTP 200 is not enough: any local pre-bound port would be
    trusted and agent traffic routed to it. Require the freellmpool
    owned_by marker in the /v1/models body."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as resp:
            if resp.getcode() != 200:
                return False
            raw = resp.read(_MAX_MODELS_BYTES + 1)
            if len(raw) > _MAX_MODELS_BYTES:
                return False
            return _is_gateway_payload(raw)
    except (urllib.error.URLError, OSError, ValueError):
        return False


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
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    raise LauncherError(f"proxy on port {port} did not become ready within {timeout:.0f}s")


def launch(harness: str, agent_args: list[str], *, port: int, model: str) -> None:
    """Ensure the gateway, set harness env, and exec into the agent (no return)."""
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
            default_data_dir() / f"freellmpool-opencode-{port}.json", port, model)
        env["OPENCODE_CONFIG"] = str(cfg)
    os.execvpe(harness, [harness, *agent_args], env)
