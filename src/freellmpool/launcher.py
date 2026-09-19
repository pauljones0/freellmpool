"""One-command agent launcher: gateway + harness env + exec.

``freellmpool claude -- <agent args>`` starts the loopback proxy when none
is running (reusing a live one), sets the harness env, and exec-replaces
into the agent so signals propagate naturally. The spawned proxy outlives
the agent by design; reruns reuse it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

READY_TIMEOUT = 60.0


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
    path = Path(path)
    path.write_text(json.dumps(opencode_config(port, model), indent=2) + "\n")
    return path


def gateway_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as resp:
            return bool(resp.getcode() == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_proxy(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """Start the loopback proxy unless one is already live. Returns True if spawned."""
    if gateway_ready(port):
        return False
    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool", "proxy", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise LauncherError(f"proxy exited during startup (code {proc.returncode})")
        if gateway_ready(port):
            return True
        time.sleep(0.5)
    proc.terminate()
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
            Path(tempfile.gettempdir()) / f"freellmpool-opencode-{port}.json", port, model)
        env["OPENCODE_CONFIG"] = str(cfg)
    os.execvpe(harness, [harness, *agent_args], env)
