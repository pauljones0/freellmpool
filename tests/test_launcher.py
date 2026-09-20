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
    ours = {"object": "list", "data": [{"id": "agent", "object": "model", "owned_by": "freellmpool"}]}

    def fake_urlopen(*a, **k):
        calls.append(a)
        return _models_response(ours)

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
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

    ours = {"object": "list", "data": [{"id": "agent", "object": "model", "owned_by": "freellmpool"}]}

    def urlopen_or_ready(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise urllib.error.URLError("down")
        return _models_response(ours)

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

        def wait(self, timeout=None):
            return 0

        def kill(self):
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
    monkeypatch.setenv("FREELLMPOOL_DATA_DIR", str(tmp_path))
    execed = {}
    monkeypatch.setattr(launcher.os, "execvpe",
                         lambda prog, args, env: execed.update(prog=prog, args=args, env=env))
    launcher.launch("opencode", ["run", "hi"], port=18091, model="agent")
    assert execed["prog"] == "opencode"
    cfg_path = execed["env"]["OPENCODE_CONFIG"]
    assert cfg_path.startswith(str(tmp_path))
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


def _models_response(payload: object):
    import json as _json

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

        def read(self, n=-1):
            body = (_json.dumps(payload) if not isinstance(payload, bytes) else payload)
            raw = body.encode() if isinstance(body, str) else body
            return raw if n is None or n < 0 else raw[:n]

    return FakeResponse()


def test_gateway_ready_rejects_foreign_200_body(monkeypatch):
    """A local pre-bound port answering 200 must not be trusted as our gateway."""
    foreign = {"object": "list", "data": [{"id": "x", "object": "model", "owned_by": "someone-else"}]}
    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                         lambda *a, **k: _models_response(foreign))
    assert launcher.gateway_ready(port=18091) is False
    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                         lambda *a, **k: _models_response(b"<html>not json</html>"))
    assert launcher.gateway_ready(port=18091) is False


def test_gateway_ready_accepts_freellmpool_marker(monkeypatch):
    ours = {"object": "list", "data": [{"id": "agent", "object": "model", "owned_by": "freellmpool"}]}
    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                         lambda *a, **k: _models_response(ours))
    assert launcher.gateway_ready(port=18091) is True


def test_ensure_proxy_timeout_kills_leaked_child(monkeypatch):
    import subprocess as _subprocess
    import urllib.error

    monkeypatch.setattr(
        launcher.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    calls: list[str] = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            calls.append("wait")
            raise _subprocess.TimeoutExpired(cmd="proxy", timeout=timeout)

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    with pytest.raises(launcher.LauncherError, match="did not become ready"):
        launcher.ensure_proxy(port=18091, timeout=0.01)
    assert calls[0] == "terminate"
    assert "wait" in calls and "kill" in calls


def test_ensure_proxy_detaches_child_from_terminal(monkeypatch):
    import urllib.error

    seen: dict = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(
        launcher.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    with pytest.raises(launcher.LauncherError, match="did not become ready"):
        launcher.ensure_proxy(port=18091, timeout=0.01)
    assert seen.get("start_new_session") is True
    assert seen.get("stdin") == launcher.subprocess.DEVNULL


def test_write_opencode_config_neutralizes_precreated_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    link = tmp_path / "oc.json"
    link.symlink_to(victim)
    out = launcher.write_opencode_config(link, port=18091, model="agent")
    assert victim.read_text() == "do not touch"
    assert out.is_file() and not out.is_symlink()
    assert json.loads(out.read_text())["model"] == "freellmpool/agent"
    assert (out.stat().st_mode & 0o777) == 0o600


def test_write_opencode_config_rerun_replaces_regular_file(tmp_path):
    path = tmp_path / "oc.json"
    launcher.write_opencode_config(path, port=1, model="a")
    launcher.write_opencode_config(path, port=2, model="b")
    assert json.loads(path.read_text())["model"] == "freellmpool/b"


def test_launch_opencode_config_lives_in_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ensure_proxy", lambda port: False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("FREELLMPOOL_DATA_DIR", str(tmp_path))
    execed = {}
    monkeypatch.setattr(launcher.os, "execvpe",
                         lambda prog, args, env: execed.update(prog=prog, args=args, env=env))
    launcher.launch("opencode", ["run", "hi"], port=18091, model="agent")
    assert execed["env"]["OPENCODE_CONFIG"].startswith(str(tmp_path))


def test_ensure_proxy_unkillable_child_reaped_in_background(monkeypatch):
    import subprocess
    import time as _time

    waits = {"n": 0}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            waits["n"] += 1
            raise subprocess.TimeoutExpired("p", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    with pytest.raises(launcher.LauncherError, match="did not become ready"):
        launcher.ensure_proxy(port=18091, timeout=0.01)
    deadline = _time.monotonic() + 5
    while waits["n"] < 3 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert waits["n"] >= 3, "a background reaper must keep waiting on the child"
