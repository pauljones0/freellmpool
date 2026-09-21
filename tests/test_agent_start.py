"""G39: one-command agent start — pins P-01..P-52, P-56..P-61.

Offline fixtures only; loopback-only fakes. No provider or network calls.
Hermetic acceptance (P-53..P-55) lives in tests/test_agent_coldstart.py.
"""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_managed_runtime import make_pool

from freellmpool import cli as cli_mod
from freellmpool import launcher
from freellmpool.conformance import ConformanceStore

# P-01/P-02 FROZEN expectation: json.dumps(builder(8080, "agent",
# authenticated=True, host="localhost"), indent=2), default ensure_ascii
# (em-dashes as \u2014 escapes), NO trailing newline. Raw string so the
# backslash-u sequences stay literal.
FROZEN = r"""{
  "$schema": "https://opencode.ai/config.json",
  "model": "freellmpool/agent",
  "provider": {
    "freellmpool": {
      "name": "freellmpool (free pool)",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "apiKey": "{env:FREELLMPOOL_PROXY_KEY}",
        "headerTimeout": 600000,
        "timeout": 600000,
        "chunkTimeout": 120000
      },
      "models": {
        "agent": {
          "name": "Agent — strongest healthy tier"
        },
        "spread": {
          "name": "Spread — maximum pool breadth"
        },
        "auto": {
          "name": "Auto — proxy default routing"
        },
        "fast": {
          "name": "Fast — lowest latency"
        },
        "quality": {
          "name": "Quality — capability matched"
        },
        "fair": {
          "name": "Fair — provider quota spread"
        }
      }
    }
  }
}"""


def _frozen_ascii() -> str:
    return FROZEN.replace("—", "\\u2014")


class _ExecCapture(Exception):
    """Sentinel raised by the patched exec: carries argv + env (never runs)."""

    def __init__(self, argv: list[str], env: dict[str, str]):
        super().__init__("exec captured")
        self.argv = argv
        self.env = env


def _capture_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execvpe(prog: str, argv: list[str], env: dict[str, str]) -> None:
        raise _ExecCapture(list(argv), dict(env))

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)


def _ns(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "harness": "opencode",
        "agent_args": [],
        "port": 8080,
        "model": None,
        "api_key": None,
        "verify_limit": 20,
        "verify_timeout": 45,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def _status(
    *,
    generation: str = "test",
    eligible: int = 4,
    tools: int = 5,
    allowances: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "generation": generation,
        "eligible_routes": eligible,
        "tools_ready": tools,
        "allowances": allowances if allowances is not None else [],
    }


class _FakePool:
    def __init__(self, status: dict[str, object] | Exception):
        self._status = status

    def managed_status(self, snapshot: object = None) -> dict[str, object]:
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def _patch_pools(monkeypatch: pytest.MonkeyPatch, pools: list[object]) -> None:
    iterator = iter(pools)

    def fake(*args: object, **kwargs: object) -> object:
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("freellmpool.managed.ManagedPool.from_default_config", fake)


def _s0_ok(
    monkeypatch: pytest.MonkeyPatch,
    registry: dict[str, object] | Exception | None = None,
) -> None:
    """Pass S0 checks (1),(2),(4): POSIX real, binary present, registry cached."""
    monkeypatch.delenv("FREELLMPOOL_PROXY_KEY", raising=False)
    monkeypatch.delenv("FREELLMPOOL_LEGACY_ROUTER", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}")
    if registry is None:
        registry = {"groq": {}, "alpha": {}}

    def fake_load(env: object = None, **kwargs: object) -> dict[str, object]:
        if isinstance(registry, Exception):
            raise registry
        assert isinstance(registry, dict)
        return registry

    monkeypatch.setattr("freellmpool.provider_registry.load_registry", fake_load)


def _flow_ok(
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, object] | None = None,
    *,
    registry: dict[str, object] | Exception | None = None,
) -> dict[str, object]:
    """Stage S0+S1+S2 to pass (S2 skipped via tools_ready>0); returns the status."""
    _s0_ok(monkeypatch, registry)
    resolved = status if status is not None else _status()
    _patch_pools(monkeypatch, [_FakePool(resolved)])
    _capture_exec(monkeypatch)
    return resolved


def _marker_rows(n: int = 6) -> list[dict[str, str]]:
    return [{"id": f"m{i}", "object": "model", "owned_by": "freellmpool"} for i in range(n)]


class _FakeGateway:
    """Loopback fake gateway with a programmable behavior hook (ThreadingHTTPServer)."""

    def __init__(self, behavior):  # type: ignore[no-untyped-def]
        self.log: list[tuple[str, str, dict[str, str]]] = []
        self._behavior = behavior
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # type: ignore[no-untyped-def]
                pass

            def do_GET(self):  # type: ignore[no-untyped-def]
                headers = {k.lower(): v for k, v in dict(self.headers).items()}
                outer.log.append(("GET", self.path, headers))
                code, body = outer._behavior("GET", self.path, headers, outer.log)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
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
def gateway_factory():
    gateways: list[_FakeGateway] = []

    def make(behavior):  # type: ignore[no-untyped-def]
        gateway = _FakeGateway(behavior)
        gateways.append(gateway)
        return gateway

    yield make
    for gateway in gateways:
        gateway.close()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeProc:
    def __init__(self, pid: int = 4242, poll_seq: tuple[object, ...] = (None,)):
        self.pid = pid
        self._poll_seq = list(poll_seq)
        self.returncode: int | None = None
        self.calls: list[str] = []

    def poll(self) -> object:
        value: object = self._poll_seq.pop(0) if len(self._poll_seq) > 1 else self._poll_seq[0]
        if value is not None:
            self.returncode = value if isinstance(value, int) else 1
        return value

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        return 0

    def kill(self) -> None:
        self.calls.append("kill")


def _popen_spy(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProc | None = None
) -> tuple[list[tuple[list[str], dict[str, object]]], _FakeProc]:
    calls: list[tuple[list[str], dict[str, object]]] = []
    proc = proc if proc is not None else _FakeProc()

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProc:
        calls.append((list(argv), dict(kwargs)))
        assert proc is not None
        return proc

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    return calls, proc


def _popen_boom(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(argv: list[str], **kwargs: object) -> object:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)


def _err_tempfiles() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("freellmpool-proxy-err-*"))


# ---------------------------------------------------------------- P-01..P-07
def test_p01_recipe_matches_frozen_dict():
    rendered = launcher.opencode_config(8080, "agent", authenticated=True, host="localhost")
    assert rendered == json.loads(_frozen_ascii())


def test_p02_byte_identity_builder_snippet_and_code():
    from freellmpool import agents
    from freellmpool.profiles import PROFILES

    text = json.dumps(
        launcher.opencode_config(8080, "agent", authenticated=True, host="localhost"), indent=2
    )
    assert text == _frozen_ascii()
    assert PROFILES["opencode"].config_snippets["opencode.json"] == _frozen_ascii()
    indented = "\n".join("      " + line for line in _frozen_ascii().splitlines())
    assert indented in (agents.render("opencode") or "")


def test_p03_emission_order_both_halves_extra_last():
    keyed = launcher.opencode_config(8080, "agent", authenticated=True)
    assert list(keyed) == ["$schema", "model", "provider"]
    inner = keyed["provider"]["freellmpool"]  # type: ignore[index]
    assert list(inner) == ["name", "npm", "options", "models"]
    assert list(inner["options"]) == ["baseURL", "apiKey", "headerTimeout", "timeout", "chunkTimeout"]
    assert list(inner["models"]) == ["agent", "spread", "auto", "fast", "quality", "fair"]
    plain = launcher.opencode_config(8080, "agent", authenticated=False)
    inner_plain = plain["provider"]["freellmpool"]  # type: ignore[index]
    # P-04: the unauthenticated half-A is exactly {baseURL}; the timeout
    # half-B exists only when authenticated (P-03's half-B order is pinned
    # on the authenticated case above).
    assert list(inner_plain["options"]) == ["baseURL"]
    union = launcher.opencode_config(8080, "groq/llama", authenticated=True)
    inner_union = union["provider"]["freellmpool"]  # type: ignore[index]
    assert list(inner_union["models"])[-1] == "groq/llama"


def test_p04_authenticated_halves():
    off = launcher.opencode_config(8080, "agent", authenticated=False)["provider"]["freellmpool"]["options"]  # type: ignore[index]
    assert off == {"baseURL": "http://127.0.0.1:8080/v1"}
    on = launcher.opencode_config(8080, "agent", authenticated=True)["provider"]["freellmpool"]["options"]  # type: ignore[index]
    assert on == {
        "baseURL": "http://127.0.0.1:8080/v1",
        "apiKey": "{env:FREELLMPOOL_PROXY_KEY}",
        "headerTimeout": 600000,
        "timeout": 600000,
        "chunkTimeout": 120000,
    }


def test_p05_six_set_and_name_pairs():
    models = launcher.opencode_config(8080, "agent")["provider"]["freellmpool"]["models"]  # type: ignore[index]
    assert {"agent", "spread", "auto", "fast", "quality", "fair"} <= set(models)
    assert models["agent"] == {"name": "Agent — strongest healthy tier"}
    assert models["spread"] == {"name": "Spread — maximum pool breadth"}
    assert models["auto"] == {"name": "Auto — proxy default routing"}
    assert models["fast"] == {"name": "Fast — lowest latency"}
    assert models["quality"] == {"name": "Quality — capability matched"}
    assert models["fair"] == {"name": "Fair — provider quota spread"}


def test_p06_union_top_level_emission_only():
    rendered = launcher.opencode_config(8080, "groq/llama")
    assert rendered["model"] == "freellmpool/groq/llama"
    # Emission-only: no routing asserted here (F-7 residual, proxy judges).


def test_p07_union_entry_empty_and_last():
    models = launcher.opencode_config(8080, "groq/llama")["provider"]["freellmpool"]["models"]  # type: ignore[index]
    assert models["groq/llama"] == {}
    assert list(models)[-1] == "groq/llama"


# ------------------------------------------------------- P-08..P-12 verify
def _verify_pool(tmp_path: Path, ids: tuple[str, ...] = ("a", "b", "c", "d"), **kwargs: object):
    kwargs.setdefault("conformance", ConformanceStore(tmp_path / "c.json"))
    return make_pool(tmp_path, ids=ids, **kwargs)  # type: ignore[arg-type]


def _verify_ns(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "provider": None,
        "limit": 2,
        "features": "tools",
        "timeout": 5,
        "json": False,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_p08_all_canary_pass_exits_zero(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    pool = _verify_pool(tmp_path)
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)

    def canaries(provider, model, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["call_fn"].__self__ is pool
        assert kwargs["stream_fn"].__self__ is pool
        return {"tools": {"status": "pass", "classification": "ok"}}

    monkeypatch.setattr("freellmpool.conformance.run_target_canaries", canaries)
    assert managed_cli.cmd_verify(_verify_ns()) == 0
    out = capsys.readouterr().out
    assert "tools=pass" in out


def test_p09_all_fail_exits_three(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    pool = _verify_pool(tmp_path)
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)

    def canaries(provider, model, **kwargs):  # type: ignore[no-untyped-def]
        return {"tools": {"status": "fail", "classification": "no_tool_call"}}

    monkeypatch.setattr("freellmpool.conformance.run_target_canaries", canaries)
    assert managed_cli.cmd_verify(_verify_ns()) == 3
    out = capsys.readouterr().out
    assert "tools=fail" in out


def test_p10_no_routes_leg1_text(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    pool = _verify_pool(tmp_path, ids=())
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)
    assert managed_cli.cmd_verify(_verify_ns()) == 3
    captured = capsys.readouterr()
    assert captured.out == (
        "No verifiable routes: run freellmpool update, or "
        "freellmpool setup to connect access.\n"
    )
    assert "Bench thin" not in captured.err


def test_p11_provider_filtered_leg2_text(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    pool = _verify_pool(tmp_path, ids=("a", "b", "c", "d"))
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)
    # Real packaged registry: groq is a known id, so G36 validation passes
    # while the fixture pool has zero groq routes.
    assert managed_cli.cmd_verify(_verify_ns(provider=["groq"])) == 3
    captured = capsys.readouterr()
    assert captured.out == (
        "No current free route is ready to verify. Run freellmpool status or setup.\n"
    )
    assert "Traceback" not in captured.err


def test_p12_guide_holds_step4_command_and_sentences():
    guide = (Path(__file__).resolve().parents[1] / "docs/FREE_SETUP.md").read_text()
    assert (
        'fp agent-start --port 8080 opencode -- run -m freellmpool/agent '
        '"Reply with exactly: AGENT_OK"' in guide
    )
    assert (
        "Before this step, finish steps 1–3 above and install opencode with the "
        "install block; the command below verifies tool routes, starts the "
        "proxy if needed, and replaces itself with opencode." in guide
    )
    assert (
        "On success it prints a seven-line `freellmpool: ...` receipt (endpoint, "
        "harness, model, auth, tools_ready, proxy, config) and becomes opencode; "
        "the proxy keeps running after opencode exits." in guide
    )
    assert (
        "Only `freellmpool/<alias>` model names route through the proxy; other "
        "`a/b` entries in the generated config are menu entries only." in guide
    )


# ------------------------------------------------------------------ P-13..P-18 S0
def test_p13_non_posix_exits_two(monkeypatch, capsys):
    _popen_boom(monkeypatch)
    monkeypatch.setattr(launcher.os, "name", "nt")
    assert launcher.agent_start(_ns()) == 2
    assert capsys.readouterr().err == (
        "freellmpool: agent launch requires POSIX (Linux or macOS); "
        "Windows is not supported\n"
    )


def test_p14_missing_binary_exits_two_without_spawn(monkeypatch, capsys):
    _popen_boom(monkeypatch)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.agent_start(_ns(harness="claude")) == 2
    assert capsys.readouterr().err == (
        "freellmpool: claude not found on PATH — install it first "
        "(npm i -g @anthropic-ai/claude-code)\n"
    )
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.agent_start(_ns(harness="opencode")) == 2
    assert "opencode not found on PATH — install it first (https://opencode.ai/install)" in (
        capsys.readouterr().err
    )


def test_p15_bad_ports_exit_two_without_spawn(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    for bad in (0, 99999):
        assert launcher.agent_start(_ns(port=bad)) == 2
        assert capsys.readouterr().err == (
            f"freellmpool: invalid --port {bad}: must be 1-65535\n"
        )
    with pytest.raises(SystemExit) as exited:
        cli_mod.main(["agent-start", "--port", "abc", "opencode"])
    assert exited.value.code == 2


def test_p16_model_grammar_legs(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(Exception("no S1 in grammar legs"))] * 4)
    _popen_boom(monkeypatch)
    # Alias, shipped claude default (bare), and provider/model pass validation:
    # S0 does not exit 2, so S1 runs (fake pool raises -> S1 exit 3).
    for model in ("agent", "claude-3-5-sonnet", "groq/llama", "groq/a/b"):
        assert launcher.agent_start(_ns(model=model)) == 3
    capsys.readouterr()  # drain the four S1 literals
    # Unknown provider exits 2 with the valid-forms literal.
    assert launcher.agent_start(_ns(model="typo-provider/x")) == 2
    assert capsys.readouterr().err == (
        "freellmpool: unknown provider 'typo-provider'. Known registry ids: alpha, groq "
        "(model must be an alias [auto, agent, spread, fast, quality, fair], "
        "provider/model, or a bare name)\n"
    )
    # Empty/whitespace exits 2.
    assert launcher.agent_start(_ns(model="  ")) == 2
    assert "freellmpool: invalid --model '  ':" in capsys.readouterr().err


def test_p16_dead_registry_skips_model_validation(monkeypatch):
    _s0_ok(monkeypatch, ValueError("registry down"))
    _patch_pools(monkeypatch, [Exception("registry down")])
    _popen_boom(monkeypatch)
    # Bad provider skips S0 validation, then S1 exits 3 (not 2).
    assert launcher.agent_start(_ns(model="typo-provider/x")) == 3


def test_p17_verify_flag_ranges_exit_two():
    for argv in (
        ["agent-start", "--verify-limit", "0", "opencode"],
        ["agent-start", "--verify-limit", "33", "opencode"],
        ["agent-start", "--verify-timeout", "0", "opencode"],
        ["agent-start", "--verify-timeout", "-1", "opencode"],
    ):
        with pytest.raises(SystemExit) as exited:
            cli_mod.main(argv)
        assert exited.value.code == 2


def test_p18_dead_registry_continues_to_s1_literal(monkeypatch, capsys):
    _s0_ok(monkeypatch, ValueError("registry down"))
    _popen_boom(monkeypatch)
    # Real pool construction hits the same dead registry -> S1 literal.
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: provider registry unreadable — cannot judge routes "
        "(reinstall or clear the policy bundle)\n"
    )


# ------------------------------------------------------------------ P-19..P-23 S1
def test_p19_invalid_config_adopts_source_text(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(_status(generation="invalid-config"))])
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: invalid local restrictions; repair providers.toml\n"
    )


def test_p20_cold_pool_exits_three(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(_status(eligible=0, tools=0))])
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: no eligible routes — run freellmpool status, then "
        "freellmpool update or freellmpool setup as directed\n"
    )


def test_p21_quota_exhaustion_and_unmetered_rows(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    exhausted = _status(allowances=[{"remaining": 0}, {"remaining": 0.0}])
    _patch_pools(monkeypatch, [_FakePool(exhausted)])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: all allowances exhausted — wait for reset (see freellmpool quota)\n"
    )


def test_p21_none_row_passes_s1(monkeypatch, gateway_factory):
    status = _status(allowances=[{"remaining": 0}, {"remaining": None}, {}])
    _flow_ok(monkeypatch, status)
    gateway = gateway_factory(
        lambda *a: (200, {"object": "list", "data": _marker_rows()})
    )
    _popen_boom(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))


def test_p21_no_mutation_boom_legs(tmp_path, monkeypatch):
    """S1 reads managed_status only: real pool + boom mutators still passes."""
    from freellmpool.allowances import AllowanceLedger

    _s0_ok(monkeypatch)
    pool = _verify_pool(tmp_path)
    for route in pool.snapshot().routes:
        pool.conformance.record(
            route.provider, route.model, "tools", status="pass", classification="ok"
        )
    assert pool.managed_status()["tools_ready"] >= 1
    _patch_pools(monkeypatch, [pool])
    _capture_exec(monkeypatch)
    gateway = _FakeGateway(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    gateways = [gateway]
    try:
        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("S1 must not mutate quota state")

        monkeypatch.setattr(AllowanceLedger, "reserve", boom)
        monkeypatch.setattr(AllowanceLedger, "settle", boom)
        monkeypatch.setattr(AllowanceLedger, "observe_remaining", boom)
        monkeypatch.setattr(AllowanceLedger, "block", boom)
        _popen_boom(monkeypatch)
        with pytest.raises(_ExecCapture):
            launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    finally:
        for gateway in gateways:
            gateway.close()


def test_p21_legacy_quota_store_untouched(tmp_path, monkeypatch):
    from freellmpool.quota import QuotaStore

    _s0_ok(monkeypatch)
    pool = _verify_pool(tmp_path)
    for route in pool.snapshot().routes:
        pool.conformance.record(
            route.provider, route.model, "tools", status="pass", classification="ok"
        )
    _patch_pools(monkeypatch, [pool])
    _capture_exec(monkeypatch)
    gateway = _FakeGateway(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    try:
        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("S1 must not touch the legacy quota store")

        monkeypatch.setattr(QuotaStore, "record", boom)
        _popen_boom(monkeypatch)
        with pytest.raises(_ExecCapture):
            launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    finally:
        gateway.close()


def test_p22_legacy_router_exits_three(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    monkeypatch.setenv("FREELLMPOOL_LEGACY_ROUTER", "1")
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: agent-start requires the managed router "
        "(unset FREELLMPOOL_LEGACY_ROUTER)\n"
    )


def test_p23_snapshot_failures_exit_three(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    literal = (
        "freellmpool: provider registry unreadable — cannot judge routes "
        "(reinstall or clear the policy bundle)\n"
    )
    _patch_pools(monkeypatch, [sqlite3.OperationalError("path is a directory")])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == literal
    _patch_pools(monkeypatch, [_FakePool(ValueError("bad snapshot"))])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == literal


# ------------------------------------------------------------------ P-24..P-26 S2
def test_p24_skip_silent_or_warns(monkeypatch, capsys):
    from freellmpool import managed_cli

    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    # tools_ready=5: silent skip. S4 probe hits a refused port -> spawn, but
    # Popen booms, proving S2 skipped verify (cmd_verify would have run first).
    _patch_pools(monkeypatch, [_FakePool(_status(tools=5))])
    with pytest.raises(AssertionError, match="must not spawn"):
        launcher.agent_start(_ns(port=_free_port(), harness="claude"))
    assert capsys.readouterr().err == ""
    # tools_ready=2: skip + verbatim warning.
    _patch_pools(monkeypatch, [_FakePool(_status(tools=2))])
    with pytest.raises(AssertionError, match="must not spawn"):
        launcher.agent_start(_ns(port=_free_port(), harness="claude"))
    assert capsys.readouterr().err == (
        managed_cli.tools_bench_warning(_status(tools=2)) + "\n"
    )


def test_p25_verify_zero_continues_with_reread_n(monkeypatch, capsys, gateway_factory):
    _s0_ok(monkeypatch)
    _patch_pools(
        monkeypatch,
        [_FakePool(_status(tools=0)), _FakePool(_status(tools=4))],
    )
    seen = {}

    def fake_verify(args):  # type: ignore[no-untyped-def]
        seen.update(vars(args))
        print("a/free: tools=pass")
        return 0

    monkeypatch.setattr("freellmpool.managed_cli.cmd_verify", fake_verify)
    _capture_exec(monkeypatch)
    _popen_boom(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    captured = capsys.readouterr()
    assert seen["provider"] is None
    assert seen["limit"] == 20
    assert seen["features"] == "tools"
    assert seen["timeout"] == 45
    assert seen["json"] is False
    assert "a/free: tools=pass" in captured.out
    assert "freellmpool: tools_ready 4" in captured.err


def test_p26_verify_nonzero_reread_zero_exits_three(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _patch_pools(
        monkeypatch,
        [_FakePool(_status(tools=0)), _FakePool(_status(tools=0))],
    )

    def fake_verify(args):  # type: ignore[no-untyped-def]
        print("No current free route is ready to verify. Run freellmpool status or setup.")
        return 3

    monkeypatch.setattr("freellmpool.managed_cli.cmd_verify", fake_verify)
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns()) == 3
    captured = capsys.readouterr()
    assert "No current free route is ready" in captured.out
    assert captured.err == (
        "freellmpool: verification found no tool-capable route "
        "(see verify output above); run the named command, or wait and re-run\n"
    )


def test_p26_verify_nonzero_reread_positive_continues(monkeypatch, gateway_factory):
    _s0_ok(monkeypatch)
    _patch_pools(
        monkeypatch,
        [_FakePool(_status(tools=0)), _FakePool(_status(tools=1))],
    )
    monkeypatch.setattr("freellmpool.managed_cli.cmd_verify", lambda args: 3)
    _capture_exec(monkeypatch)
    _popen_boom(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))


# ----------------------------------------------------------------------- P-27 S3
def test_p27_auth_precedence_and_empty_fallthrough(monkeypatch):
    monkeypatch.setattr("freellmpool.config.settings", lambda *a, **k: {"proxy_key": "K-settings"})
    monkeypatch.setenv("FREELLMPOOL_PROXY_KEY", "K-env")
    assert launcher.resolve_proxy_auth("K-flag") == ("K-flag", "flag")
    assert launcher.resolve_proxy_auth("") == ("K-env", "env")
    assert launcher.resolve_proxy_auth(None) == ("K-env", "env")
    monkeypatch.setenv("FREELLMPOOL_PROXY_KEY", "")
    assert launcher.resolve_proxy_auth(None) == ("K-settings", "settings")
    monkeypatch.setattr("freellmpool.config.settings", lambda *a, **k: {"proxy_key": ""})
    assert launcher.resolve_proxy_auth(None) == (None, "none")
    monkeypatch.setattr("freellmpool.config.settings", lambda *a, **k: {})
    assert launcher.resolve_proxy_auth("") == (None, "none")


# ------------------------------------------------------------------ P-28..P-37 S4
def _fail_first_then_ready(n_fail: int):
    state = {"n": 0}

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] <= n_fail:
            return 503, b""
        return 200, {"object": "list", "data": _marker_rows()}

    return behavior


def test_p28_keyless_fresh_spawn_full_line(monkeypatch, capsys, gateway_factory, tmp_path):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch)
    calls, proc = _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        sys.executable, "-m", "freellmpool", "proxy",
        "--host", "127.0.0.1", "--port", str(gateway.port),
    ]
    child_env = kwargs["env"]
    assert "FREELLMPOOL_PROXY_KEY" not in child_env
    expected_pidfile = str(default_data_dir() / f"proxy-{gateway.port}.pid")
    assert child_env["FREELLMPOOL_MANAGED_PIDFILE"] == expected_pidfile
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert hasattr(kwargs["stderr"], "name")
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] is None
    assert proc.pid == 4242
    assert "freellmpool: proxy spawned (pid 4242)" in capsys.readouterr().err


def test_p29_protected_existing_reuse_zero_spawn(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    monkeypatch.setenv("FREELLMPOOL_PROXY_KEY", "K29")

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != "Bearer K29":
            return 401, {"error": "invalid or missing API key"}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(behavior)
    calls, _ = _popen_spy(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert calls == []
    err = capsys.readouterr().err
    assert "freellmpool: proxy reused (pid unknown)" in err
    assert "freellmpool: auth protected:env" in err


def test_p30_protected_spawned_child_env(monkeypatch, gateway_factory):
    _flow_ok(monkeypatch)
    calls, _ = _popen_spy(monkeypatch)

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        if len(log) <= 1:
            return 503, b""
        if headers.get("authorization") != "Bearer K30":
            return 401, {"error": "denied"}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(behavior)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude", api_key="K30"))
    assert len(calls) == 1
    child_env = calls[0][1]["env"]
    assert child_env["FREELLMPOOL_PROXY_KEY"] == "K30"
    assert child_env["FREELLMPOOL_MANAGED_PIDFILE"].endswith(f"proxy-{gateway.port}.pid")


def test_p31_wrong_key_fast_fail(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != "Bearer K-real":
            return 401, {"error": "denied"}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(behavior)
    calls, _ = _popen_spy(monkeypatch)
    start = time.monotonic()
    rc = launcher.agent_start(_ns(port=gateway.port, api_key="K-wrong"))
    elapsed = time.monotonic() - start
    assert rc == 3
    assert elapsed < 5
    assert calls == []
    assert capsys.readouterr().err == (
        f"freellmpool: auth mismatch on port {gateway.port}: the flag key was "
        "rejected (401); fix the key and re-run\n"
    )


def test_p32_keyless_vs_protected_distinct_literal(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    gateway = gateway_factory(lambda *a: (401, {"error": "denied"}))
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns(port=gateway.port)) == 3
    assert capsys.readouterr().err == (
        f"freellmpool: proxy on port {gateway.port} requires a key (protected); "
        "pass --api-key or set FREELLMPOOL_PROXY_KEY\n"
    )


def test_p33_key_against_keyless_benign_reuse(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    calls, _ = _popen_spy(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude", api_key="K-unused"))
    assert calls == []
    err = capsys.readouterr().err
    assert "freellmpool: proxy reused (pid unknown)" in err
    assert "freellmpool: auth keyless" in err


def test_p34_foreign_legs_fail_fast_without_spawn(monkeypatch, capsys, gateway_factory):
    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)] * 4)
    _popen_boom(monkeypatch)
    foreign_rows = [{"id": "x", "object": "model", "owned_by": "someone-else"}]

    def round2_foreign(method, path, headers, log):  # type: ignore[no-untyped-def]
        if "ready=1" in path:
            return 200, {"object": "list", "data": []}
        return 200, {"object": "list", "data": foreign_rows}

    legs = [
        round2_foreign,
        lambda *a: (404, {"error": "unknown route"}),
        lambda *a: (200, b"<html>not json</html>"),
        lambda *a: (200, b"x" * (launcher._MAX_MODELS_BYTES + 2)),
    ]
    for behavior in legs:
        gateway = gateway_factory(behavior)
        assert launcher.agent_start(_ns(port=gateway.port)) == 3
        assert capsys.readouterr().err == (
            f"freellmpool: port {gateway.port} serves a non-freellmpool service "
            "(foreign); free the port or pick another with --port\n"
        )


def test_p35_no_routes_deadline_both_branches(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    monkeypatch.setattr(launcher, "READY_TIMEOUT", 1.0)

    def unready(method, path, headers, log):  # type: ignore[no-untyped-def]
        if "ready=1" in path:
            return 200, {"object": "list", "data": []}
        return 200, {"object": "list", "data": _marker_rows()}

    # Reuse path: live-but-unready from the start -> no spawn attempted.
    gateway = gateway_factory(unready)
    _popen_boom(monkeypatch)
    start = time.monotonic()
    assert launcher.agent_start(_ns(port=gateway.port)) == 3
    assert time.monotonic() - start < 10
    assert capsys.readouterr().err == (
        f"freellmpool: proxy on port {gateway.port} has no ready routes "
        "(see freellmpool status); gave up after 1s\n"
    )


def test_p35_no_routes_deadline_spawn_path(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    monkeypatch.setattr(launcher, "READY_TIMEOUT", 1.0)
    state = {"n": 0}

    def down_then_unready(method, path, headers, log):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] <= 1:
            return 503, b""
        if "ready=1" in path:
            return 200, {"object": "list", "data": []}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(down_then_unready)
    calls, proc = _popen_spy(monkeypatch)
    assert launcher.agent_start(_ns(port=gateway.port, harness="claude")) == 3
    assert len(calls) == 1
    assert "terminate" in proc.calls  # spawned child rolled back on deadline
    assert capsys.readouterr().err == (
        f"freellmpool: proxy on port {gateway.port} has no ready routes "
        "(see freellmpool status); gave up after 1s\n"
    )


def test_p35_reuse_turns_unreachable_reports_not_ready(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    monkeypatch.setattr(launcher, "READY_TIMEOUT", 1.0)
    state = {"n": 0}

    def unready_then_down(method, path, headers, log):  # type: ignore[no-untyped-def]
        # Initial probe round: ready=1 empty + plain marker -> no-routes path.
        # Everything after: refused-style 503-empty -> last=unreachable.
        state["n"] += 1
        if state["n"] <= 2:
            if "ready=1" in path:
                return 200, {"object": "list", "data": []}
            return 200, {"object": "list", "data": _marker_rows()}
        return 503, b""

    gateway = gateway_factory(unready_then_down)
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns(port=gateway.port)) == 3
    assert capsys.readouterr().err == (
        f"freellmpool: proxy on port {gateway.port} did not become ready within 1s\n"
    )


def test_p36_shed_503_retried_not_failfast(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    calls, _ = _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(4))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert len(calls) == 1
    assert "freellmpool: proxy spawned (pid 4242)" in capsys.readouterr().err


def test_p37_child_exit_code_and_tail(monkeypatch, capsys):
    _flow_ok(monkeypatch)
    proc = _FakeProc(poll_seq=(None, 1))
    proc.returncode = None
    calls, _ = _popen_spy(monkeypatch, proc)
    real_ntf = tempfile.NamedTemporaryFile

    def seeded_ntf(*args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_ntf(*args, **kwargs)
        handle.write(b"HEAD-" + b"x" * 6000 + b"-TAILMARK")
        handle.flush()
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", seeded_ntf)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher.agent_start(_ns(port=_free_port(), harness="claude")) == 3
    err = capsys.readouterr().err
    assert "freellmpool: proxy exited during startup (code 1)" in err
    assert "TAILMARK" in err
    assert "HEAD-" not in err  # only the last 4KB quoted
    assert len(calls) == 1


def test_p37_occupied_foreign_no_spawn(monkeypatch, capsys, gateway_factory):
    _flow_ok(monkeypatch)
    gateway = gateway_factory(
        lambda *a: (200, {"object": "list", "data": [{"id": "x", "owned_by": "other"}]})
    )
    _popen_boom(monkeypatch)
    assert launcher.agent_start(_ns(port=gateway.port)) == 3
    assert "serves a non-freellmpool service (foreign)" in capsys.readouterr().err


def test_p37_dead_occupant_spawns_and_unlinks_stale(monkeypatch, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))
    dead = subprocess.Popen(["true"])
    dead.wait()
    calls, _ = _popen_spy(monkeypatch)
    pidfile = default_data_dir() / f"proxy-{gateway.port}.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": dead.pid, "port": gateway.port}) + "\n")
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert len(calls) == 1
    assert not pidfile.exists()


def test_p37_success_unlinks_stderr_tempfile(monkeypatch, gateway_factory):
    before = _err_tempfiles()
    _flow_ok(monkeypatch)
    _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert _err_tempfiles() - before == set()


# ------------------------------------------------------------------ P-38..P-39 S6
def _assert_no_secret(receipt: str, key: str | None = None) -> None:
    for line in receipt.splitlines():
        assert re.match(
            r"^freellmpool: (endpoint|harness|model|auth|tools_ready|proxy|config) ", line
        ), line
    if key:
        assert key not in receipt


def test_p38_keyless_spawned_receipt_byte_exact(monkeypatch, capsys, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch, _status(tools=5))
    _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="opencode"))
    port = gateway.port
    expected = (
        f"freellmpool: endpoint http://127.0.0.1:{port}/v1\n"
        "freellmpool: harness opencode\n"
        "freellmpool: model agent\n"
        "freellmpool: auth keyless\n"
        "freellmpool: tools_ready 5\n"
        "freellmpool: proxy spawned (pid 4242)\n"
        f"freellmpool: config {default_data_dir()}/freellmpool-opencode-{port}.json\n"
    )
    err = capsys.readouterr().err
    assert err == expected
    _assert_no_secret(err)


def test_p39_protected_reused_receipt_and_n_from_reread(
    monkeypatch, capsys, gateway_factory
):
    _s0_ok(monkeypatch)
    monkeypatch.setenv("FREELLMPOOL_PROXY_KEY", "K39")
    _patch_pools(monkeypatch, [_FakePool(_status(tools=0)), _FakePool(_status(tools=2))])
    monkeypatch.setattr("freellmpool.managed_cli.cmd_verify", lambda args: 0)
    _capture_exec(monkeypatch)

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != "Bearer K39":
            return 401, {"error": "denied"}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(behavior)
    _popen_boom(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    err = capsys.readouterr().err
    assert "freellmpool: auth protected:env" in err
    assert "freellmpool: proxy reused (pid unknown)" in err
    assert "freellmpool: tools_ready 2" in err
    assert "freellmpool: config none (claude uses env)" in err
    _assert_no_secret(err, "K39")


# ------------------------------------------------------------------ P-40..P-41 S7
def test_p40_claude_env_keyed_dummy_and_default_signature():
    assert launcher.claude_env(18091, "m", api_key="K40")["ANTHROPIC_API_KEY"] == "K40"
    assert launcher.claude_env(18091, "m") == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18091",
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_MODEL": "m",
    }


def test_p40_exec_argv_env_per_harness(monkeypatch, gateway_factory):
    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)] * 4)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    _popen_boom(monkeypatch)
    # Claude keyed: x-api-key gate input.
    with pytest.raises(_ExecCapture) as captured:
        launcher.agent_start(
            _ns(port=gateway.port, harness="claude", api_key="K40",
                agent_args=["-p", "hi"])
        )
    assert captured.value.argv == ["claude", "-p", "hi"]
    assert captured.value.env["ANTHROPIC_API_KEY"] == "K40"
    assert captured.value.env["ANTHROPIC_MODEL"] == "claude-3-5-sonnet"
    assert captured.value.env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{gateway.port}"
    # Claude keyless: dummy placeholder.
    with pytest.raises(_ExecCapture) as captured:
        launcher.agent_start(_ns(port=gateway.port, harness="claude", agent_args=[]))
    assert captured.value.argv == ["claude"]
    assert captured.value.env["ANTHROPIC_API_KEY"] == "dummy"
    # Opencode keyed: config path + key; leading -- stripped.
    with pytest.raises(_ExecCapture) as captured:
        launcher.agent_start(
            _ns(port=gateway.port, harness="opencode", api_key="K40",
                agent_args=["--", "run", "hi"])
        )
    assert captured.value.argv == ["opencode", "run", "hi"]
    assert captured.value.env["OPENCODE_CONFIG"].endswith(
        f"freellmpool-opencode-{gateway.port}.json"
    )
    assert captured.value.env["FREELLMPOOL_PROXY_KEY"] == "K40"
    # Opencode keyless: no key in env (TUI form, empty args).
    with pytest.raises(_ExecCapture) as captured:
        launcher.agent_start(_ns(port=gateway.port, harness="opencode", agent_args=[]))
    assert captured.value.argv == ["opencode"]
    assert "FREELLMPOOL_PROXY_KEY" not in captured.value.env


def test_p41_exec_oserror_rolls_back(monkeypatch, capsys, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    before = _err_tempfiles()
    _flow_ok(monkeypatch)
    calls, proc = _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))

    def boom_exec(prog, argv, env):  # type: ignore[no-untyped-def]
        raise OSError("no such binary")

    monkeypatch.setattr(launcher.os, "execvpe", boom_exec)
    pidfile = default_data_dir() / f"proxy-{gateway.port}.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": 4242, "port": gateway.port}) + "\n")
    assert launcher.agent_start(_ns(port=gateway.port, harness="claude")) == 3
    assert len(calls) == 1
    assert "terminate" in proc.calls
    assert not pidfile.exists()
    assert _err_tempfiles() - before == set()
    err = capsys.readouterr().err
    assert err.endswith("freellmpool: cannot exec claude: no such binary\n")
    assert "freellmpool: proxy spawned (pid 4242)" in err  # S6 ran before S7 failed


# ------------------------------------------------------------- P-42..P-46 lifecycle
def test_p42_s5_failure_rolls_back_spawned(monkeypatch, capsys, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch)
    calls, proc = _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))

    def boom_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise launcher.LauncherError("refusing to write opencode config to /x: boom")

    monkeypatch.setattr(launcher, "write_opencode_config", boom_write)
    pidfile = default_data_dir() / f"proxy-{gateway.port}.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": 4242, "port": gateway.port}) + "\n")
    assert launcher.agent_start(_ns(port=gateway.port, harness="opencode")) == 3
    assert len(calls) == 1
    assert "terminate" in proc.calls
    assert not pidfile.exists()
    assert "refusing to write opencode config" in capsys.readouterr().err


def test_p42_s5_failure_reused_untouched(monkeypatch, capsys, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    calls, _ = _popen_spy(monkeypatch)

    def boom_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise launcher.LauncherError("refusing to write opencode config to /x: boom")

    monkeypatch.setattr(launcher, "write_opencode_config", boom_write)
    pidfile = default_data_dir() / f"proxy-{gateway.port}.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": 999999, "port": gateway.port}) + "\n")
    assert launcher.agent_start(_ns(port=gateway.port, harness="opencode")) == 3
    assert calls == []
    assert pidfile.exists()  # reused proxies are never signalled or unrecorded


def test_p43_rerun_reuse_keyless_and_protected(monkeypatch, capsys, gateway_factory):
    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)] * 2)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    calls, _ = _popen_spy(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert calls == []
    assert "freellmpool: proxy reused (pid unknown)" in capsys.readouterr().err

    def keyed(method, path, headers, log):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != "Bearer K43":
            return 401, {"error": "denied"}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway2 = gateway_factory(keyed)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway2.port, harness="claude", api_key="K43"))
    assert calls == []
    err = capsys.readouterr().err
    assert "freellmpool: proxy reused (pid unknown)" in err
    assert "freellmpool: auth protected:flag" in err


def test_p44_unwritable_config_keeps_previous_bytes(monkeypatch, capsys, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    _flow_ok(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    _popen_boom(monkeypatch)  # reuse path: the failure is the config write
    path = default_data_dir() / f"freellmpool-opencode-{gateway.port}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"previous": true}\n')
    real_replace = os.replace

    def boom_replace(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom_replace)
    try:
        assert launcher.agent_start(_ns(port=gateway.port, harness="opencode")) == 3
    finally:
        monkeypatch.setattr(os, "replace", real_replace)
    assert path.read_text() == '{"previous": true}\n'
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    assert "refusing to write opencode config" in capsys.readouterr().err


def test_p45_keyboard_interrupt_rolls_back_and_reraises(monkeypatch, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    before = _err_tempfiles()
    _flow_ok(monkeypatch)
    calls, proc = _popen_spy(monkeypatch)
    gateway = gateway_factory(_fail_first_then_ready(1))

    def boom_ready(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "await_proxy_ready", boom_ready)
    pidfile = default_data_dir() / f"proxy-{gateway.port}.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps({"pid": 4242, "port": gateway.port}) + "\n")
    with pytest.raises(KeyboardInterrupt):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert len(calls) == 1
    assert "terminate" in proc.calls
    assert not pidfile.exists()
    assert _err_tempfiles() - before == set()


def test_p46_config_written_for_opencode_only(monkeypatch, gateway_factory):
    from freellmpool.artifacts import default_data_dir

    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)] * 2)
    _popen_boom(monkeypatch)
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _marker_rows()}))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="opencode"))
    expected = default_data_dir() / f"freellmpool-opencode-{gateway.port}.json"
    assert expected.exists()
    assert json.loads(expected.read_text())["model"] == "freellmpool/agent"
    assert expected.read_text().endswith("\n")
    expected.unlink()
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert list(default_data_dir().glob("freellmpool-opencode-*.json")) == []


# ------------------------------------------------------------- P-47..P-48 status/stop
@pytest.fixture
def named_sleep_factory():
    procs: list[subprocess.Popen] = []

    def make(name: str = "freellmpool-proxy") -> subprocess.Popen:
        proc = subprocess.Popen(["bash", "-c", f"exec -a {name} sleep 30"])
        procs.append(proc)
        # Reap promptly on death: without this the exit reads as a zombie
        # (kill-0 succeeds) and stop's dead-check never fires.
        threading.Thread(target=proc.wait, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                raw = Path(f"/proc/{proc.pid}/cmdline").read_bytes().decode()
            except OSError:
                time.sleep(0.02)
                continue
            if name in raw:
                return proc
            time.sleep(0.02)
        raise AssertionError("named sleep never exec'd")

    yield make
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write_pidfile(port: int, pid: int) -> Path:
    from freellmpool.artifacts import default_data_dir

    path = default_data_dir() / f"proxy-{port}.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "port": port}) + "\n")
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _tools_rows() -> list[dict[str, object]]:
    return [
        {"id": "a", "object": "model", "owned_by": "freellmpool",
         "verified_features": ["tools"]},
        {"id": "b", "object": "model", "owned_by": "freellmpool",
         "verified_features": []},
        {"id": "c", "object": "model", "owned_by": "freellmpool",
         "verified_features": ["chat", "tools"]},
    ]


def test_p47_status_live_pid_and_tools(monkeypatch, capsys, gateway_factory, named_sleep_factory):
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _tools_rows()}))
    sleeper = named_sleep_factory()
    _write_pidfile(gateway.port, sleeper.pid)
    assert cli_mod.main(["proxy", "--status", "--port", str(gateway.port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: proxy on port {gateway.port} is live "
        f"(pid {sleeper.pid}, 2 tools-ready routes)\n"
    )


def test_p47_status_live_protected(monkeypatch, capsys, gateway_factory):
    gateway = gateway_factory(lambda *a: (401, {"error": "denied"}))
    assert cli_mod.main(["proxy", "--status", "--port", str(gateway.port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: proxy on port {gateway.port} is live but protected "
        "(provide the key to inspect)\n"
    )


def test_p47_status_absent_unlinks_stale(monkeypatch, capsys):
    port = _free_port()
    pidfile = _write_pidfile(port, _dead_pid())
    assert cli_mod.main(["proxy", "--status", "--port", str(port)]) == 1
    assert capsys.readouterr().out == (
        f"freellmpool: no proxy on port {port} (nothing listening)\n"
    )
    assert not pidfile.exists()


def test_p47_status_foreign(monkeypatch, capsys, gateway_factory):
    gateway = gateway_factory(
        lambda *a: (200, {"object": "list", "data": [{"id": "x", "owned_by": "other"}]})
    )
    assert cli_mod.main(["proxy", "--status", "--port", str(gateway.port)]) == 1
    assert capsys.readouterr().out == (
        f"freellmpool: port {gateway.port} serves a non-freellmpool service (foreign)\n"
    )


def test_p47_status_works_unconfigured(monkeypatch, capsys):
    from freellmpool.router import Pool

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("status must not load the pool")

    monkeypatch.setattr(Pool, "from_default_config", boom)
    discovery_calls: list[object] = []
    monkeypatch.setattr(
        cli_mod, "_ensure_first_run_discovery", lambda: discovery_calls.append(1)
    )
    port = _free_port()
    assert cli_mod.main(["proxy", "--status", "--port", str(port)]) == 1
    assert discovery_calls == []
    assert f"freellmpool: no proxy on port {port} (nothing listening)" in (
        capsys.readouterr().out
    )


def test_p47_status_live_leg_unlinks_mismatched_pidfile(
    monkeypatch, capsys, gateway_factory
):
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _tools_rows()}))
    pidfile = _write_pidfile(gateway.port, _dead_pid())
    assert cli_mod.main(["proxy", "--status", "--port", str(gateway.port)]) == 0
    assert "(pid unknown, 2 tools-ready routes)" in capsys.readouterr().out
    assert not pidfile.exists()


def test_p47_status_port_rule_legs(monkeypatch, capsys, gateway_factory):
    assert cli_mod.main(["proxy", "--status", "--port", "99999"]) == 2
    assert capsys.readouterr().err == "freellmpool: invalid proxy port 99999: must be 1-65535\n"
    gateway = gateway_factory(lambda *a: (200, {"object": "list", "data": _tools_rows()}))
    monkeypatch.setattr(cli_mod, "settings", lambda *a, **k: {"port": gateway.port})
    assert cli_mod.main(["proxy", "--status"]) == 0
    assert "is live (pid unknown, 2 tools-ready routes)" in capsys.readouterr().out
    monkeypatch.setattr(cli_mod, "settings", lambda *a, **k: {"port": "abc"})
    assert cli_mod.main(["proxy", "--status"]) == 2
    assert capsys.readouterr().err == (
        "freellmpool: invalid proxy port: must be an integer 1-65535\n"
    )


def test_p48_stop_term_flow_ok(monkeypatch, capsys, named_sleep_factory):
    port = _free_port()
    sleeper = named_sleep_factory()
    pidfile = _write_pidfile(port, sleeper.pid)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: stopped proxy on port {port} (pid {sleeper.pid})\n"
    )
    assert not pidfile.exists()
    sleeper.wait(timeout=5)


def test_p48_stop_stale_unlink(monkeypatch, capsys):
    port = _free_port()
    dead = _dead_pid()
    pidfile = _write_pidfile(port, dead)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: removed stale proxy record for port {port} "
        f"(pid {dead} is not a freellmpool proxy)\n"
    )
    assert not pidfile.exists()


def test_p48_stop_no_record_ok(monkeypatch, capsys):
    port = _free_port()
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: no managed proxy record for port {port}; nothing to stop\n"
    )


def test_p48_stop_cmdline_mismatch(monkeypatch, capsys, named_sleep_factory):
    port = _free_port()
    sleeper = named_sleep_factory("plain-sleeper")
    pidfile = _write_pidfile(port, sleeper.pid)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: removed stale proxy record for port {port} "
        f"(pid {sleeper.pid} is not a freellmpool proxy)\n"
    )
    assert not pidfile.exists()
    assert sleeper.poll() is None  # mismatched processes are never signalled


def test_p48_stop_ps_missing_falls_back_to_proc(
    monkeypatch, capsys, named_sleep_factory
):
    port = _free_port()
    sleeper = named_sleep_factory()
    pidfile = _write_pidfile(port, sleeper.pid)

    def no_ps(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("no ps here")

    monkeypatch.setattr(subprocess, "run", no_ps)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert capsys.readouterr().out == (
        f"freellmpool: stopped proxy on port {port} (pid {sleeper.pid})\n"
    )
    assert not pidfile.exists()
    sleeper.wait(timeout=5)


def test_p48_stop_neither_ps_nor_proc_refuses(monkeypatch, capsys, named_sleep_factory):
    port = _free_port()
    sleeper = named_sleep_factory()
    pidfile = _write_pidfile(port, sleeper.pid)

    def no_ps(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("no ps here")

    monkeypatch.setattr(subprocess, "run", no_ps)
    monkeypatch.setattr(launcher, "_read_proc_cmdline", lambda pid: None)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 3
    assert capsys.readouterr().err == (
        f"freellmpool: cannot verify pid {sleeper.pid} (no ps or /proc); "
        "refusing to signal\n"
    )
    assert pidfile.exists()
    assert sleeper.poll() is None


def test_p48_stop_ps_argv_list_no_shell(monkeypatch, capsys, named_sleep_factory):
    port = _free_port()
    sleeper = named_sleep_factory()
    _write_pidfile(port, sleeper.pid)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_run = subprocess.run

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert cli_mod.main(["proxy", "--stop", "--port", str(port)]) == 0
    assert len(calls) == 1
    assert calls[0][0] == (["ps", "-o", "args=", "-p", str(sleeper.pid)],)
    assert calls[0][1] == {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": 10,
        "check": False,
    }
    sleeper.wait(timeout=5)


# ------------------------------------------------------- P-49..P-50 forwarding/help
def _spy_agent_start(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    seen: list[argparse.Namespace] = []

    def fake(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(launcher, "agent_start", fake)
    return seen


def test_p49_forwarding_matrix(monkeypatch):
    seen = _spy_agent_start(monkeypatch)
    cells = 0
    for harness in ("claude", "opencode"):
        for port_opt in ([], ["--port", "9000"]):
            port_value = 9000 if port_opt else 8080
            for model_opt in ([], ["--model", "groq/llama"]):
                model_value = "groq/llama" if model_opt else None
                for sep in ([], ["--"]):
                    for remainder in ([], ["run", "-m", "x"], ["--port", "9000"]):
                        # Canonical: options before the harness.
                        argv = ["agent-start", *port_opt, *model_opt, harness, *sep, *remainder]
                        assert cli_mod.main(argv) == 0
                        parsed = seen[-1]
                        assert parsed.harness == harness
                        assert parsed.port == port_value
                        assert parsed.model == model_value
                        assert parsed.agent_args == remainder
                        cells += 1
                        # Swallow order: options after the harness stay defaulted
                        # and their tokens land in agent_args verbatim.
                        argv = ["agent-start", harness, *sep, *port_opt, *model_opt, *remainder]
                        assert cli_mod.main(argv) == 0
                        parsed = seen[-1]
                        assert parsed.port == 8080
                        assert parsed.model is None
                        assert parsed.agent_args == [*port_opt, *model_opt, *remainder]
                        cells += 1
    assert cells == 2 * 2 * 2 * 2 * 3 * 2
    # Bootstrap True-leg: agent-start self-discovers on fresh machines.
    assert cli_mod._needs_bootstrap(argparse.Namespace(command="agent-start")) is True
    # Status/stop carve-out: never bootstrap, never discover.
    assert (
        cli_mod._needs_bootstrap(argparse.Namespace(command="proxy", status=True)) is False
    )
    assert (
        cli_mod._needs_bootstrap(argparse.Namespace(command="proxy", stop=True)) is False
    )


def test_p50_help_embeds_prereq_and_posix_scope():
    parser = cli_mod.build_parser()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    raw = " ".join(subs.choices["agent-start"].format_help().split())
    text = re.sub(r"(\w)- (\w)", r"\1-\2", raw)  # rejoin hyphen-wraps
    assert "Prerequisites: finish freellmpool setup" in text
    assert "POSIX-only (Linux/macOS); Windows is not supported" in text
    assert "fail-closed" in text


def test_p50_posix_legs_both_callers(monkeypatch, capsys):
    full = "agent launch requires POSIX (Linux or macOS); Windows is not supported"
    monkeypatch.setattr(launcher.os, "name", "nt")
    assert launcher.agent_start(_ns()) == 2
    assert capsys.readouterr().err == f"freellmpool: {full}\n"
    with pytest.raises(launcher.LauncherError, match=re.escape(full)):
        launcher.launch("claude", [], port=8080, model="claude-3-5-sonnet")


# ------------------------------------------------------------------ P-51..P-52 docs
def _select_freellmpool_blocks(text: str, filename: str) -> list[dict[str, object]]:
    """P-51 selector: json fences + <pre><code> blocks with provider.freellmpool."""
    blocks: list[dict[str, object]] = []
    if filename.endswith(".html"):
        candidates = re.findall(r"<pre><code[^>]*>(.*?)</code></pre>", text, re.S)
        for raw in candidates:
            import html as _html

            try:
                data = json.loads(_html.unescape(raw))
            except ValueError:
                continue
            if (
                isinstance(data, dict)
                and isinstance(data.get("provider"), dict)
                and "freellmpool" in data["provider"]
            ):
                blocks.append(data)
        return blocks
    for info, body in re.findall(r"```([^\n]*)\n(.*?)```", text, re.S):
        if info.strip().casefold() != "json":
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("provider"), dict)
            and "freellmpool" in data["provider"]
        ):
            blocks.append(data)
    return blocks


def _assert_loopback_baseurl(base_url: object) -> None:
    assert isinstance(base_url, str)
    host = urlsplit(base_url).hostname or ""

    def ip_loopback(value: str) -> bool:
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    assert host.casefold().rstrip(".") == "localhost" or ip_loopback(host)


def test_p51_selector_edge_rules():
    fence_upper = '```JSON\n{"provider": {"freellmpool": {"options": {}}}}\n```\n'
    assert len(_select_freellmpool_blocks(fence_upper, "x.md")) == 1
    fence_ws = '```  json  \n{"provider": {"freellmpool": {"options": {}}}}\n```\n'
    assert len(_select_freellmpool_blocks(fence_ws, "x.md")) == 1
    fence_bad = '```json\n{not json\n```\n'
    assert _select_freellmpool_blocks(fence_bad, "x.md") == []
    fence_bash = '```bash\n{"provider": {"freellmpool": {"options": {}}}}\n```\n'
    assert _select_freellmpool_blocks(fence_bash, "x.md") == []
    pre = '<pre><code class="x">{"provider": {"freellmpool": {"options": {}}}}</code></pre>'
    assert len(_select_freellmpool_blocks(pre, "x.html")) == 1
    pre_bad = "<pre><code>not json</code></pre>"
    assert _select_freellmpool_blocks(pre_bad, "x.html") == []


def test_p51_doc_blocks_pin_apikey_loopback_model():
    root = Path(__file__).resolve().parents[1]
    files = [
        "docs/run-opencode-on-free-models.html",
        "docs/promotion/long-form-article.md",
        "docs/promotion/reddit-opencode.md",
        "docs/promotion/reply-bank.md",
    ]
    for name in files:
        blocks = _select_freellmpool_blocks((root / name).read_text(), name)
        assert len(blocks) >= 1, name  # the html pointer: >=1 must select
        for data in blocks:
            inner = data["provider"]["freellmpool"]  # type: ignore[index]
            assert inner["options"]["apiKey"] == "{env:FREELLMPOOL_PROXY_KEY}", name
            _assert_loopback_baseurl(inner["options"]["baseURL"])
            assert isinstance(data.get("model"), str) and data["model"].startswith(  # type: ignore[union-attr]
                "freellmpool/"
            ), name


def test_p52_exempt_files_compat_shape():
    root = Path(__file__).resolve().parents[1]
    for name in ("docs/INTEGRATIONS.md", "integrations/opencode/README.md"):
        text = (root / name).read_text()
        blocks = []
        for _info, body in re.findall(r"```([^\n]*)\n(.*?)```", text, re.S):
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if (
                isinstance(data, dict)
                and isinstance(data.get("provider"), dict)
                and "freellmpool" in data["provider"]
            ):
                blocks.append(data)
        assert len(blocks) >= 1, name
        for data in blocks:
            inner = data["provider"]["freellmpool"]  # type: ignore[index]
            assert "apiKey" in inner["options"], name
            assert "model" not in data, name


def test_p52_no_baseurl_in_any_help():
    parser = cli_mod.build_parser()
    assert "baseURL" not in parser.format_help()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    for name, sub in subs.choices.items():
        assert "baseURL" not in sub.format_help(), name


def test_p52_profile_install_transitivity_sentence():
    guide = (Path(__file__).resolve().parents[1] / "docs/FREE_SETUP.md").read_text()
    assert (
        "Every profile `fp code` prints points at this same loopback proxy, "
        "so installed clients and the launcher stay in sync." in guide
    )


# ------------------------------------------------------------------ P-56..P-61 misc
def test_p56_post_harness_swallow_all_options(monkeypatch):
    seen = _spy_agent_start(monkeypatch)
    assert cli_mod.main(["agent-start", "opencode", "--port", "9000"]) == 0
    assert seen[-1].port == 8080
    assert seen[-1].agent_args == ["--port", "9000"]
    assert cli_mod.main(["agent-start", "--port", "9000", "opencode"]) == 0
    assert seen[-1].port == 9000
    assert seen[-1].agent_args == []
    legs = [
        (["--model", "groq/llama"], "model", None),
        (["--api-key", "K"], "api_key", None),
        (["--verify-limit", "25"], "verify_limit", 20),
        (["--verify-timeout", "7"], "verify_timeout", 45),
    ]
    for tokens, field, default in legs:
        assert cli_mod.main(["agent-start", "opencode", *tokens]) == 0
        assert getattr(seen[-1], field) == default
        assert seen[-1].agent_args == tokens


def test_p57_key_composition_end_to_end(monkeypatch, gateway_factory):
    real_urlopen = launcher.urllib.request.urlopen
    for harness in ("opencode", "claude"):
        status = _flow_ok(monkeypatch)
        _patch_pools(monkeypatch, [_FakePool(status)])
        calls, _ = _popen_spy(monkeypatch)
        observed_auth: list[str | None] = []

        def spy_urlopen(  # type: ignore[no-untyped-def]
            target, *args, _real=real_urlopen, _seen=observed_auth, **kwargs
        ):
            if isinstance(target, launcher.urllib.request.Request):
                _seen.append(target.get_header("Authorization"))
            return _real(target, *args, **kwargs)

        monkeypatch.setattr(launcher.urllib.request, "urlopen", spy_urlopen)
        gateway = gateway_factory(_fail_first_then_ready(1))
        with pytest.raises(_ExecCapture) as captured:
            launcher.agent_start(_ns(port=gateway.port, harness=harness, api_key="K57"))
        assert "Bearer K57" in observed_auth
        assert calls[0][1]["env"]["FREELLMPOOL_PROXY_KEY"] == "K57"
        if harness == "opencode":
            config_path = captured.value.env["OPENCODE_CONFIG"]
            options = json.loads(Path(config_path).read_text())["provider"]["freellmpool"][
                "options"
            ]
            assert options["apiKey"] == "{env:FREELLMPOOL_PROXY_KEY}"
        else:
            assert captured.value.env["ANTHROPIC_API_KEY"] == "K57"


def test_p57_tampered_key_spies_observe_mismatch(monkeypatch, gateway_factory):
    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)])
    calls, _ = _popen_spy(monkeypatch)
    values = iter(["K_probe", "K_exec"])
    monkeypatch.setattr(
        launcher, "resolve_proxy_auth", lambda cli_key: (next(values), "flag")
    )
    observed_auth: list[str | None] = []
    real_urlopen = launcher.urllib.request.urlopen

    def spy_urlopen(target, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(target, launcher.urllib.request.Request):
            observed_auth.append(target.get_header("Authorization"))
        return real_urlopen(target, *args, **kwargs)

    monkeypatch.setattr(launcher.urllib.request, "urlopen", spy_urlopen)
    gateway = gateway_factory(_fail_first_then_ready(1))
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude", api_key="K"))
    assert "Bearer K_probe" in observed_auth
    assert calls[0][1]["env"]["FREELLMPOOL_PROXY_KEY"] == "K_exec"


def test_p58_concurrent_attribution_no_launcher_side_write(monkeypatch):
    from freellmpool.artifacts import default_data_dir

    status = _flow_ok(monkeypatch)
    _patch_pools(monkeypatch, [_FakePool(status)] * 2)
    calls, _ = _popen_spy(monkeypatch)
    ready_flag = threading.Event()
    barrier = threading.Barrier(3)
    seen_threads: set[int] = set()
    barrier_lock = threading.Lock()
    real_probe = launcher.probe_gateway

    def probe_and_sync(port, key):  # type: ignore[no-untyped-def]
        outcome = real_probe(port, key)
        first = False
        with barrier_lock:
            if threading.get_ident() not in seen_threads:
                seen_threads.add(threading.get_ident())
                first = True
        if first:
            barrier.wait(timeout=30)
        return outcome

    monkeypatch.setattr(launcher, "probe_gateway", probe_and_sync)

    def behavior(method, path, headers, log):  # type: ignore[no-untyped-def]
        if not ready_flag.is_set():
            return 503, b""
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = _FakeGateway(behavior)
    pidfile_writes: list[object] = []
    monkeypatch.setattr(
        cli_mod, "_write_managed_pidfile", lambda port: pidfile_writes.append(port)
    )
    errors: list[BaseException] = []

    def worker(api_key):  # type: ignore[no-untyped-def]
        try:
            launcher.agent_start(
                _ns(port=gateway.port, harness="claude", api_key=api_key)
            )
        except _ExecCapture:
            pass
        except BaseException as exc:  # noqa: BLE001 - re-raised on main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(None,), daemon=True),
        threading.Thread(target=worker, args=("K58",), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=30)  # both workers are inside the retry loop
        ready_flag.set()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(calls) == 2
        assert pidfile_writes == []
        assert list(default_data_dir().glob("proxy-*.pid")) == []
    finally:
        gateway.close()


def test_p59_status_stop_posix_gate(monkeypatch, capsys):
    monkeypatch.setattr(launcher.os, "name", "nt")
    literal = (
        "freellmpool: agent launch requires POSIX (Linux or macOS); "
        "Windows is not supported\n"
    )
    assert cli_mod.main(["proxy", "--status", "--port", "8080"]) == 2
    assert capsys.readouterr().err == literal
    assert cli_mod.main(["proxy", "--stop", "--port", "8080"]) == 2
    assert capsys.readouterr().err == literal
    # Gate fires before port validation: POSIX literal wins over bad ports.
    assert cli_mod.main(["proxy", "--status", "--port", "99999"]) == 2
    assert capsys.readouterr().err == literal


def test_p60_pidfile_helper_exact_bytes(tmp_path, monkeypatch):
    marker = tmp_path / "proxy-18091.pid"
    monkeypatch.setenv("FREELLMPOOL_MANAGED_PIDFILE", str(marker))
    cli_mod._write_managed_pidfile(18091)
    raw = marker.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    assert list(payload) == ["pid", "port", "started"]
    assert payload["pid"] == os.getpid()
    assert payload["port"] == 18091
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["started"])
    assert raw == (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    assert list(tmp_path.glob(".proxy-18091.pid.*.tmp")) == []


def test_p60_pidfile_helper_marker_unset_or_empty_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("FREELLMPOOL_MANAGED_PIDFILE", raising=False)
    cli_mod._write_managed_pidfile(18091)
    assert list(tmp_path.glob("*.pid")) == []
    monkeypatch.setenv("FREELLMPOOL_MANAGED_PIDFILE", "")
    cli_mod._write_managed_pidfile(18091)
    assert list(tmp_path.glob("*.pid")) == []


def test_p61_launch_on_unready_exits_three(monkeypatch, gateway_factory):
    def unready(method, path, headers, log):  # type: ignore[no-untyped-def]
        if "ready=1" in path:
            return 200, {"object": "list", "data": []}
        return 200, {"object": "list", "data": _marker_rows()}

    gateway = gateway_factory(unready)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    spawned: list[list[str]] = []
    real_popen = launcher.subprocess.Popen

    def wrapping_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        spawned.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", wrapping_popen)
    start = time.monotonic()
    with pytest.raises(launcher.LauncherError, match=r"proxy exited during startup \(code \d+\)"):
        launcher.ensure_proxy(gateway.port, timeout=30)
    assert time.monotonic() - start < 15
    assert len(spawned) == 1
    assert spawned[0] == [
        sys.executable, "-m", "freellmpool", "proxy", "--port", str(gateway.port)
    ]


def test_test_registry_seam_rejects_non_loopback(tmp_path, monkeypatch):
    from freellmpool import provider_registry

    document = {
        "schema": 1,
        "providers": [
            {
                "id": "evil",
                "display_name": "evil",
                "api_base_url": "https://example.com/v1",
                "inference_auth": "none",
            }
        ],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    monkeypatch.setenv("FREELLMPOOL_TEST_REGISTRY_PATH", str(path))
    with pytest.raises(ValueError):
        provider_registry.load_registry({"FREELLMPOOL_TEST_REGISTRY_PATH": str(path)})

    document["providers"][0]["api_base_url"] = "http://127.0.0.1:9/v1"
    path.write_text(json.dumps(document))
    registry = provider_registry.load_registry({"FREELLMPOOL_TEST_REGISTRY_PATH": str(path)})
    assert set(registry) == {"evil"}


# ------------------------------------------------------------------ G40 T1..T2 S1 conflict honesty
def test_g40_all_conflict_refuses_with_conflict_literal(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    conflicted = _status(allowances=[
        {"remaining": 0.0, "definition_status": "changed", "reason": "r", "retry_after": 5},
    ])
    _patch_pools(monkeypatch, [_FakePool(conflicted)])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: allowance definitions changed — affected routes fail closed "
        "until reset (see freellmpool quota)\n"
    )


def test_g40_mixed_conflict_exhausted_prefers_conflict_literal(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    mixed = _status(allowances=[
        {"remaining": 0},
        {"remaining": 0.0, "definition_status": "changed", "reason": "r", "retry_after": 5},
    ])
    _patch_pools(monkeypatch, [_FakePool(mixed)])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: allowance definitions changed — affected routes fail closed "
        "until reset (see freellmpool quota)\n"
    )


def test_g40_partial_conflict_warns_and_proceeds(monkeypatch, capsys, gateway_factory):
    status = _status(allowances=[
        {"remaining": 0},
        {"remaining": 0.0, "definition_status": "changed", "reason": "r", "retry_after": 5},
        {"remaining": None},
    ])
    _flow_ok(monkeypatch, status)
    gateway = gateway_factory(
        lambda *a: (200, {"object": "list", "data": _marker_rows()})
    )
    _popen_boom(monkeypatch)
    with pytest.raises(_ExecCapture):
        launcher.agent_start(_ns(port=gateway.port, harness="claude"))
    assert (
        "freellmpool: WARNING: 1 changed allowance definition(s) fail closed "
        "until reset (see freellmpool quota)"
    ) in capsys.readouterr().err


def test_g40_all_exhausted_keeps_exhausted_literal(monkeypatch, capsys):
    _s0_ok(monkeypatch)
    _popen_boom(monkeypatch)
    exhausted = _status(allowances=[{"remaining": 0}, {"remaining": 0}])
    _patch_pools(monkeypatch, [_FakePool(exhausted)])
    assert launcher.agent_start(_ns()) == 3
    assert capsys.readouterr().err == (
        "freellmpool: all allowances exhausted — wait for reset (see freellmpool quota)\n"
    )
