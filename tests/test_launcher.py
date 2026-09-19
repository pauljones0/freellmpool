"""G8: one-command agent launcher (claude exec + opencode config path)."""

from __future__ import annotations

import json

import pytest

from freellmpool import launcher


def test_claude_env_points_at_loopback_gateway():
    env = launcher.claude_env(port=18091, model="claude-3-5-sonnet")
    assert env == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18091",
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_MODEL": "claude-3-5-sonnet",
    }


def test_opencode_config_shape(tmp_path):
    path = launcher.write_opencode_config(tmp_path / "oc.json", port=18091, model="agent")
    cfg = json.loads(path.read_text())
    assert cfg["model"] == "freellmpool/agent"
    opts = cfg["provider"]["freellmpool"]["options"]
    assert opts["baseURL"] == "http://127.0.0.1:18091/v1"
    assert "agent" in cfg["provider"]["freellmpool"]["models"]


def test_ensure_proxy_reuses_running_gateway(monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or FakeResponse())
    assert launcher.ensure_proxy(port=18091) is False
    assert calls and "18091" in calls[0][0]


def test_ensure_proxy_spawns_and_waits(monkeypatch, tmp_path):
    import urllib.error

    attempts = []
    spawned = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        return FakeProc()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

    def urlopen_or_ready(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise urllib.error.URLError("down")
        return FakeResponse()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", urlopen_or_ready)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher.ensure_proxy(port=18091) is True
    assert "proxy" in spawned["cmd"] and "18091" in spawned["cmd"]


def test_ensure_proxy_timeout_is_honest(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        launcher.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    with pytest.raises(launcher.LauncherError, match="did not become ready"):
        launcher.ensure_proxy(port=18091, timeout=0.01)


def test_launch_claude_execs_with_env(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ensure_proxy", lambda port: False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}")
    execed = {}
    monkeypatch.setattr(launcher.os, "execvpe",
                         lambda prog, args, env: execed.update(prog=prog, args=args, env=env))
    launcher.launch("claude", ["-p", "hi"], port=18091, model="claude-3-5-sonnet")
    assert execed["prog"] == "claude"
    assert execed["args"] == ["claude", "-p", "hi"]
    assert execed["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18091"


def test_launch_opencode_writes_config_and_execs(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ensure_proxy", lambda port: False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(launcher.tempfile, "gettempdir", lambda: str(tmp_path))
    execed = {}
    monkeypatch.setattr(launcher.os, "execvpe",
                         lambda prog, args, env: execed.update(prog=prog, args=args, env=env))
    launcher.launch("opencode", ["run", "hi"], port=18091, model="agent")
    assert execed["prog"] == "opencode"
    cfg_path = execed["env"]["OPENCODE_CONFIG"]
    with open(cfg_path, encoding="utf-8") as fh:
        assert json.load(fh)["model"] == "freellmpool/agent"


def test_cli_passes_harness_and_agent_args(monkeypatch):
    from freellmpool import cli as cli_mod

    seen = {}

    def fake_launch(harness, agent_args, *, port, model):
        seen.update(harness=harness, agent_args=agent_args, port=port, model=model)

    monkeypatch.setattr("freellmpool.launcher.launch", fake_launch)
    assert cli_mod.main(["claude", "--port", "18091", "--", "-p", "hi"]) == 0
    assert seen == {"harness": "claude", "agent_args": ["-p", "hi"],
                    "port": 18091, "model": "claude-3-5-sonnet"}
    assert cli_mod.main(["claude", "--harness", "opencode", "--", "run", "hi"]) == 0
    assert seen["harness"] == "opencode" and seen["model"] == "agent"


def test_launch_missing_binary_is_honest(monkeypatch):
    monkeypatch.setattr(launcher, "ensure_proxy", lambda port: False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    with pytest.raises(launcher.LauncherError, match="not found on PATH"):
        launcher.launch("claude", [], port=18091, model="auto")
