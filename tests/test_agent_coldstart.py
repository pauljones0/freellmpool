"""G39 hermetic acceptance (P-53..P-55): installed-wheel cold start.

Four cases (cold/fast x keyless/protected) against a loopback fake provider
and a real spawned proxy child. No provider or network calls; the socket
guard pins the parent to loopback and the fixture-URL scan covers the child.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from freellmpool import cli as cli_mod
from freellmpool import launcher

ROOT = Path(__file__).resolve().parents[1]
_HERMETIC_KEY = "hermetic-test-key-1"

CANARY_CHOICE = {
    "id": "chatcmpl-hermetic-canary-1", "object": "chat.completion", "created": 1,
    "model": "fake-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call_canary_1", "type": "function",
                                "function": {"name": "record_number",
                                             "arguments": '{"number": 7}'}}]},
                "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}
CANARY_FINAL = {
    "id": "chatcmpl-hermetic-canary-2", "object": "chat.completion", "created": 1,
    "model": "fake-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}
EXE_CHOICE = {
    "id": "chatcmpl-hermetic-exe-1", "object": "chat.completion", "created": 1,
    "model": "fake-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "get_number", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}
EXE_FINAL = {
    "id": "chatcmpl-hermetic-exe-2", "object": "chat.completion", "created": 1,
    "model": "fake-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "tool-observed"},
                "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}


def _first_tool_call_id(body: dict) -> str | None:
    return next(
        (
            c.get("id")
            for m in body.get("messages", [])
            if isinstance(m, dict)
            for c in (m.get("tool_calls") or [])
            if isinstance(c, dict)
        ),
        None,
    )


def _branch(body: dict) -> tuple[int, dict]:
    tools = body.get("tools", [])
    try:
        names = {t["function"]["name"] for t in tools}
    except (KeyError, TypeError, AttributeError):
        return 400, {"error": "malformed tools"}
    if "record_number" in names:
        branch = "canary"
    elif "get_number" in names:
        branch = "exe"
    elif tools:
        return 400, {"error": "unknown tools"}
    else:
        call_id = _first_tool_call_id(body)
        if call_id == "call_canary_1":
            branch = "canary"
        elif call_id == "call_1":
            branch = "exe"
        else:
            return 400, {"error": "unknown tools"}
    has_tool = any(
        isinstance(m, dict) and m.get("role") == "tool" for m in body.get("messages", [])
    )
    if branch == "canary":
        return 200, CANARY_FINAL if has_tool else CANARY_CHOICE
    return 200, EXE_FINAL if has_tool else EXE_CHOICE


class _FakeProvider:
    """Held-socket loopback provider: request-aware canary/exe branching."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # type: ignore[no-untyped-def]
                pass

            def do_POST(self):  # type: ignore[no-untyped-def]
                if urlsplit(self.path).path != "/v1/chat/completions":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode())
                outer.log.append((self.path, body, self.client_address[0]))
                code, payload = _branch(body)
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def provider_factory():
    providers: list[_FakeProvider] = []

    def make() -> _FakeProvider:
        provider = _FakeProvider()
        providers.append(provider)
        return provider

    yield make
    for provider in providers:
        provider.close()


@pytest.fixture(autouse=True)
def _wheel_assert():
    # In the wheel job the INSTALLED artifact must serve: probe a clean child
    # (no PYTHONPATH, neutral cwd) and require a site-packages __file__.
    if os.environ.get("FREELLMPOOL_WHEEL_ASSERT") == "1":
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        completed = subprocess.run(
            [sys.executable, "-c", "import freellmpool; print(freellmpool.__file__)"],
            capture_output=True, text=True, timeout=60, cwd="/tmp", env=env,
        )
        assert completed.returncode == 0, completed.stderr
        assert "site-packages" in completed.stdout, completed.stdout


@pytest.fixture
def socket_guard(monkeypatch):
    allowed = {"127.0.0.1", "::1", "localhost"}
    original = socket.create_connection

    def guarded(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, (tuple, list)) else address
        if str(host) not in allowed:
            raise AssertionError("non-loopback egress")
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)
    return original


class _ExecCapture(Exception):
    def __init__(self, argv: list[str], env: dict[str, str]):
        super().__init__("exec captured")
        self.argv = argv
        self.env = env


_FAKE_EXE = '''#!/usr/bin/env python3
"""Hermetic fake opencode exe (G39 S8): fixed tool round trip through the proxy."""
import json
import os
import sys
import urllib.request

config_text = open(os.environ["OPENCODE_CONFIG"], encoding="utf-8").read()
base = json.loads(config_text)["provider"]["freellmpool"]["options"]["baseURL"]
with open(os.environ["HERMETIC_CAPTURE"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "argv": sys.argv[1:],
        "config": config_text,
        "key_present": bool(os.environ.get("FREELLMPOOL_PROXY_KEY")),
    }) + "\\n")

headers = {"Content-Type": "application/json"}
proxy_key = os.environ.get("FREELLMPOOL_PROXY_KEY")
if proxy_key:
    headers["Authorization"] = f"Bearer {proxy_key}"


def post(payload):
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


user_message = {"role": "user", "content": "call get_number once"}
tool_spec = [{"type": "function", "function": {
    "name": "get_number", "description": "Return the deterministic hermetic number.",
    "parameters": {"type": "object", "properties": {}}}}]
reply1 = post({"model": "freellmpool/agent", "messages": [user_message],
               "tools": tool_spec, "stream": False})
assistant_message = reply1["choices"][0]["message"]
assert assistant_message["tool_calls"][0]["id"] == "call_1", assistant_message
reply2 = post({"model": "freellmpool/agent",
               "messages": [user_message, assistant_message,
                            {"role": "tool", "tool_call_id": "call_1", "content": "7"}],
               "tools": tool_spec, "stream": False})
assert reply2["choices"][0]["message"]["content"] == "tool-observed", reply2
print("AGENT_OK")
'''


def _setup_hermetic(monkeypatch, tmp_path: Path, provider: _FakeProvider, seed: str) -> None:
    """Setup order (0)-(4): provider held, fixtures, env closeout, seed, assert."""
    base_time = datetime.now(UTC)
    checked = base_time.isoformat()
    expires = (base_time + timedelta(days=6)).isoformat()
    registry = {
        "schema": 1,
        "providers": [{
            "id": "hermetic-fake", "display_name": "Hermetic Fake",
            "api_base_url": f"http://127.0.0.1:{provider.port}/v1",
            "credential_env": None, "inference_auth": "none",
            "discovery": {"parser": "openai"},
            "evidence": [{"id": "price", "checked_at": checked, "expires_at": expires,
                          "status": "verified"}],
            "grants": [{
                "id": "free", "kind": "zero_price", "status": "verified",
                "evidence_ids": ["price"],
                "model_selector": {"kind": "allowlist", "models": ["fake-model"]},
                "paid_overage_possible": False, "hard_free_boundary": True,
                "requires_account_evidence": False, "allowed_modalities": ["chat"],
            }],
            "limits": [],
        }],
    }
    (tmp_path / "registry.json").write_text(json.dumps(registry))
    discovery = {
        "schema": 1, "generation": "hermetic",
        "providers": {"hermetic-fake": {
            "checked_at": checked, "status": "ok", "complete": True,
            "models": [{"id": "fake-model", "modalities": ["chat"], "context": 8192,
                        "pricing": {"input": "0", "output": "0"}}],
        }},
    }
    (tmp_path / "discovery.json").write_text(json.dumps(discovery))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("FREELLMPOOL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FREELLMPOOL_TEST_REGISTRY_PATH", str(tmp_path / "registry.json"))
    for name, filename in {
        "FREELLMPOOL_CONFIG_FILE": "config.toml",
        "FREELLMPOOL_CONFIG": "providers.toml",
        "FREELLMPOOL_KEYS_PATH": "keys.toml",
        "FREELLMPOOL_QUOTA_PATH": "quota.json",
        "FREELLMPOOL_STATS_PATH": "stats.json",
        "FREELLMPOOL_CACHE_PATH": "cache.db",
        "FREELLMPOOL_HEALTH_FILE": "route_health.json",
        "FREELLMPOOL_CONFORMANCE_FILE": "conformance.json",
        "FREELLMPOOL_DISCOVERY_FILE": "discovery.json",
        "FREELLMPOOL_ACCOUNTS_FILE": "accounts.json",
        "FREELLMPOOL_ALLOWANCE_FILE": "allowances.sqlite3",
        "FREELLMPOOL_EVIDENCE_FILE": "provider-evidence.json",
        "FREELLMPOOL_SETUP_STATE_PATH": "setup-progress.json",
        "FREELLMPOOL_CAPABILITY_FILE": "capability_scores.json",
        "FREELLMPOOL_TASK_EVIDENCE_FILE": "task_evidence.json",
        "FREELLMPOOL_EXTERNAL_CATALOG_PATH": "provider_catalog.json",
        "FREELLMPOOL_JOBS_PATH": "jobs.jsonl",
        "FREELLMPOOL_RUN_RECORDS_PATH": "run_records.jsonl",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / filename))
    # CONFIG precedence: this nonexistent assignment wins (no user catalog needed).
    assert not (tmp_path / "providers.toml").exists()
    monkeypatch.setenv("FREELLMPOOL_HEAL_PATH", str(tmp_path / "heal.json"))
    monkeypatch.setenv("FREELLMPOOL_HEAL_TICKS_PATH", str(tmp_path / "heal_ticks.json"))
    monkeypatch.setenv("FREELLMPOOL_POLICY_BUNDLE_FILE", str(tmp_path / "policy_bundle.json"))
    monkeypatch.setenv("FREELLMPOOL_POLICY_STATUS_FILE", str(tmp_path / "policy_status.json"))
    monkeypatch.setenv("FREELLMPOOL_RAG_FILE", str(tmp_path / "rag.sqlite3"))
    monkeypatch.setenv("FREELLMPOOL_OBSERVATIONS_FILE", str(tmp_path / "observations.json"))
    monkeypatch.delenv("FREELLMPOOL_REPORT_DIR", raising=False)
    monkeypatch.delenv("FREELLMPOOL_REPORTS_DIR", raising=False)
    monkeypatch.delenv("FREELLMPOOL_LEGACY_ROUTER", raising=False)
    monkeypatch.delenv("FREELLMPOOL_PROXY_KEY", raising=False)
    monkeypatch.setenv("FREELLMPOOL_NO_AUTO_DISCOVERY", "1")
    if os.environ.get("FREELLMPOOL_WHEEL_ASSERT") != "1":
        monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    if seed == "fast":
        from freellmpool.conformance import ConformanceStore
        from freellmpool.models import Provider

        ConformanceStore(tmp_path / "conformance.json").record(
            Provider(id="hermetic-fake", label="Hermetic Fake", adapter="openai",
                     base_url=f"http://127.0.0.1:{provider.port}/v1", models=(),
                     key_env=None, auth="none"),
            "fake-model", "tools", status="pass", classification="ok")
    from freellmpool.managed import ManagedPool

    snapshot = ManagedPool.from_default_config().snapshot()
    assert [route.name for route in snapshot.routes] == ["hermetic-fake/fake-model"]


def _assert_loopback_url(raw_url: str) -> None:
    host = urlsplit(raw_url).hostname or ""

    def ip_loopback(value: str) -> bool:
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    assert host.casefold().rstrip(".") == "localhost" or ip_loopback(host), raw_url


def _receipt_lines(err: str) -> list[str]:
    return [
        line for line in err.splitlines()
        if re.match(r"^freellmpool: (endpoint|harness|model|auth|tools_ready|proxy|config) ", line)
    ]


@pytest.mark.parametrize("seed", ["cold", "fast"])
@pytest.mark.parametrize("auth", ["keyless", "protected"])
def test_installed_wheel_cold_start(monkeypatch, tmp_path, capfd, socket_guard,
                                    provider_factory, seed, auth):
    provider = provider_factory()
    _setup_hermetic(monkeypatch, tmp_path, provider, seed)
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir()
    exe = tmp_bin / "opencode"
    exe.write_text(_FAKE_EXE)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMETIC_CAPTURE", str(tmp_path / "capture.jsonl"))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    gateway_port = sock.getsockname()[1]
    sock.close()

    def capture_exec(prog, argv, env):  # type: ignore[no-untyped-def]
        raise _ExecCapture(list(argv), dict(env))

    monkeypatch.setattr(launcher.os, "execvpe", capture_exec)
    argv = ["agent-start", "--port", str(gateway_port)]
    if auth == "protected":
        argv += ["--api-key", _HERMETIC_KEY]
    argv += ["opencode", "--", "run", "-m", "freellmpool/agent",
            "Reply with exactly: AGENT_OK"]
    try:
        cli_mod.main(argv)
    except _ExecCapture as cap:
        captured = cap
    else:
        raise AssertionError("agent-start returned instead of exec'ing")
    phase1 = capfd.readouterr()
    # P-53: receipt + no-secret scan.
    expected_auth = "protected:flag" if auth == "protected" else "keyless"
    receipt = _receipt_lines(phase1.err)
    assert receipt == [
        f"freellmpool: endpoint http://127.0.0.1:{gateway_port}/v1",
        "freellmpool: harness opencode",
        "freellmpool: model agent",
        f"freellmpool: auth {expected_auth}",
        "freellmpool: tools_ready 1",
        receipt[5],
        f"freellmpool: config {tmp_path / 'data'}/freellmpool-opencode-{gateway_port}.json",
    ]
    assert re.fullmatch(r"freellmpool: proxy spawned \(pid \d+\)", receipt[5])
    spawned_pid = int(re.search(r"\(pid (\d+)\)", receipt[5]).group(1))  # type: ignore[union-attr]

    def reap_proxy() -> None:
        # The test process is the proxy's parent: reap on death so stop's
        # kill-0 dead-check observes the exit instead of a zombie.
        try:
            os.waitpid(spawned_pid, 0)
        except (ChildProcessError, OSError):
            pass

    threading.Thread(target=reap_proxy, daemon=True).start()
    if auth == "protected":
        assert _HERMETIC_KEY not in phase1.err
    if seed == "cold":
        assert "tools=pass" in phase1.out  # real in-process verify ran
    else:
        assert "tools=pass" not in phase1.out  # S2 skipped...
        assert "WARNING: only 1 tool-capable route(s)" in phase1.err  # ...with warning

    # Phase 2: run the captured exec for real.
    assert captured.argv[0] == "opencode"
    result = subprocess.run(
        captured.argv, env=captured.env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "AGENT_OK" in result.stdout
    invocations = (tmp_path / "capture.jsonl").read_text().strip().splitlines()
    assert len(invocations) == 1
    record = json.loads(invocations[0])
    assert record["argv"] == ["run", "-m", "freellmpool/agent",
                              "Reply with exactly: AGENT_OK"]
    assert record["config"] == (
        json.dumps(launcher.opencode_config(
            gateway_port, "agent", authenticated=(auth == "protected")), indent=2) + "\n"
    )
    assert record["key_present"] is (auth == "protected")

    # P-53: provider hits, tool round trip, loopback pin.
    assert len(provider.log) >= 2
    bodies = [json.dumps(body) for _, body, _ in provider.log]
    assert any("call_1" in body for body in bodies)  # tool round trip
    tool_names = set()
    for _, body, _ in provider.log:
        for item in body.get("tools", []) or []:
            tool_names.add((item.get("function") or {}).get("name"))
    assert "get_number" in tool_names
    if seed == "cold":
        assert "record_number" in tool_names  # real verify traffic
    else:
        assert "record_number" not in tool_names  # verify skipped
    config_base = json.loads(record["config"])["provider"]["freellmpool"]["options"]["baseURL"]
    endpoint = receipt[0].split("freellmpool: endpoint ", 1)[1]
    for raw_url in (
        f"http://127.0.0.1:{provider.port}/v1",
        config_base + "/chat/completions",
        config_base,
        endpoint,
    ):
        _assert_loopback_url(raw_url)
    for _, _, peer in provider.log:
        assert ipaddress.ip_address(peer).is_loopback

    # P-55: socket-guard parent pin (non-loopback raises before connecting).
    with pytest.raises(AssertionError, match="non-loopback egress"):
        socket.create_connection(("8.8.8.8", 80), timeout=1)

    # P-54: rerun-before-cleanup, then ordered cleanup.
    popen_calls: list[object] = []
    real_popen = launcher.subprocess.Popen

    def counting_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = list(args[0]) if args else []
        if "proxy" in argv:
            popen_calls.append(argv)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", counting_popen)
    try:
        cli_mod.main(argv)
    except _ExecCapture:
        pass
    else:
        raise AssertionError("rerun returned instead of exec'ing")
    rerun_err = capfd.readouterr().err
    assert popen_calls == []
    rerun_receipt = _receipt_lines(rerun_err)
    assert any(line.startswith("freellmpool: proxy reused (pid ") for line in rerun_receipt)
    assert cli_mod.main(["proxy", "--stop", "--port", str(gateway_port)]) == 0
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", gateway_port), timeout=3)
    assert not (tmp_path / "data" / f"proxy-{gateway_port}.pid").exists()
