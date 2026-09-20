"""G17: reusable MCP output diet (budgets + labels + full escape)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from freellmpool.mcp_diet import (
    FULL_ARG,
    compact_content,
    compact_text,
)

FIXTURE = Path(__file__).with_name("mcp_diet_fixture_server.py")


def test_compact_text_passthrough_under_budget_is_byte_identical() -> None:
    text = "x" * 100
    assert compact_text(text, 100, label="blob") == text
    assert compact_text(text, 10_000, label="blob") == text


def test_compact_text_marks_every_cut_with_label_counts_and_escape() -> None:
    out = compact_text("y" * 5000, 2000, label="tool result")
    assert out.startswith("y" * 2000 + "\n")
    assert "3000 chars of tool result omitted" in out
    assert FULL_ARG in out  # escape hatch named in-band: zero silent truncations


def test_compact_text_rejects_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="budget"):
        compact_text("abc", 0)


def test_compact_text_allows_custom_escape_hint_for_third_parties() -> None:
    out = compact_text("z" * 100, 10, label="page", escape_hint="page=2")
    assert "page=2" in out
    assert "90 chars of page omitted" in out


def test_compact_content_compacts_only_long_text_blocks() -> None:
    image = {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    blocks = [
        {"type": "text", "text": "short"},
        {"type": "text", "text": "L" * 5000},
        image,
    ]
    out = compact_content(blocks, 1000, label="tool result")
    assert out[0] == {"type": "text", "text": "short"}
    assert out[2] is image or out[2] == image
    assert out[1]["type"] == "text"
    assert "4000 chars of tool result omitted" in out[1]["text"]
    assert FULL_ARG in out[1]["text"]


def test_compact_content_passes_through_non_lists() -> None:
    assert compact_content(None, 100) is None  # type: ignore[arg-type]
    assert compact_content("raw", 100) == "raw"  # type: ignore[arg-type]


def _run_proxy_session(script: list[dict]) -> list[dict]:
    """Drive the diet proxy (wrapping the fixture server) with requests."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool.mcp_diet", "--budget", "1000",
         "--", sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    replies: list[dict] = []
    try:
        for message in script:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            replies.append(json.loads(proc.stdout.readline()))
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
    return replies


def test_proxy_relays_small_results_byte_identical() -> None:
    replies = _run_proxy_session([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "small", "arguments": {}}},
    ])
    assert replies[0]["result"]["serverInfo"]["name"] == "diet-fixture"
    assert replies[1]["result"]["content"] == [{"type": "text", "text": "tiny-ok"}]


def test_proxy_compacts_large_results_with_labeled_marker() -> None:
    replies = _run_proxy_session([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "big", "arguments": {}}},
    ])
    (block,) = replies[0]["result"]["content"]
    assert "9000 chars of tool result omitted" in block["text"]
    assert FULL_ARG in block["text"]
    assert len(block["text"]) < 1500


def test_proxy_full_escape_returns_cached_full_text() -> None:
    replies = _run_proxy_session([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "big", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "big", "arguments": {FULL_ARG: True}}},
    ])
    (block,) = replies[1]["result"]["content"]
    assert block["text"] == "0123456789abcdef" * 625
    assert "omitted" not in block["text"]


def test_proxy_full_escape_without_cache_passes_through_uncompacted() -> None:
    replies = _run_proxy_session([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "big", "arguments": {FULL_ARG: True}}},
    ])
    (block,) = replies[0]["result"]["content"]
    assert len(block["text"]) == 10_000
    assert "omitted" not in block["text"]


def test_proxy_relays_notifications_untouched() -> None:
    replies = _run_proxy_session([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/ping", "params": {"n": 7}},
    ])
    assert replies[1] == {"jsonrpc": "2.0", "method": "notifications/echo",
                          "params": {"n": 7}}


def test_diet_returns_promptly_when_server_exits_with_stdin_open() -> None:
    """G23 L1: an early server exit must reap + return, not hang on stdin."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool.mcp_diet", "--", "true"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        code = proc.wait(timeout=10)  # stdin deliberately held open
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.kill()
    assert code == 0


def test_diet_sigterm_reaps_wrapped_server(tmp_path) -> None:
    """G23 L2: SIGTERM must terminate+reap the child, never orphan it."""
    import os
    import time

    pidfile = tmp_path / "child.pid"
    script = (
        f"import os, time; open(r'{pidfile}', 'w').write(str(os.getpid())); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool.mcp_diet", "--",
         sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(200):
            if pidfile.exists():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.kill()


def test_diet_no_stall_when_server_leaves_stdout_open() -> None:
    """G23 L3: a grandchild-held stdout must not stall shutdown the full 15s."""
    import time

    proc = subprocess.Popen(
        [sys.executable, "-m", "freellmpool.mcp_diet", "--",
         "sh", "-c", "sleep 12 &"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        start = time.monotonic()
        proc.wait(timeout=20)
        elapsed = time.monotonic() - start
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.kill()
    assert elapsed < 10
