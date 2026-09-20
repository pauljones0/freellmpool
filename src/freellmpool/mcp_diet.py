"""Reusable output diet for any MCP server (G17).

G14 proved labeled truncation on freellmpool's own MCP tools. This module
generalizes it into two reusable pieces for third-party servers:

- :func:`compact_text` / :func:`compact_content`: pure helpers any MCP
  author can call on tool output (budgets + labels + a named escape).
- A stdio proxy (``python -m freellmpool.mcp_diet -- <server...>``) that
  wraps servers the caller does not control: oversized text results are
  compacted in flight, every cut carries an in-band ``_full`` pointer,
  and re-calling the same tool with ``"_full": true`` returns the cached
  full text. There are no silent truncations by construction.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from collections import OrderedDict
from typing import Any, TextIO

FULL_ARG = "_full"
DEFAULT_BUDGET_CHARS = 2000
MAX_CACHED_RESULTS = 32
MAX_TRACKED_CALLS = 64  # cap on unanswered tools/call ids (pending + passthrough each)


def compact_text(text: str, budget: int, *, label: str = "text",
                 escape_hint: str | None = None) -> str:
    """Cap ``text`` at ``budget`` chars; over-budget cuts name the escape.

    Under-budget input returns byte-identical. Every cut appends a marker
    with the omitted char count, the caller-supplied ``label``, and how to
    fetch the full text (``_full`` by default), so no truncation is silent.
    """
    if budget < 1:
        raise ValueError(f"budget must be >= 1, got {budget}")
    if len(text) <= budget:
        return text
    hint = escape_hint if escape_hint is not None else f're-run with "{FULL_ARG}": true'
    return (f"{text[:budget]}\n[… {len(text) - budget} chars of {label} omitted — {hint}]")


def compact_content(blocks: Any, budget: int, *, label: str = "tool result",
                    escape_hint: str | None = None) -> Any:
    """Compact long ``text`` blocks inside an MCP ``content`` list.

    Non-list payloads and non-text blocks (images, resources) pass through
    untouched; only oversized text blocks are cut, each with its own marker.
    """
    if not isinstance(blocks, list):
        return blocks
    compacted: list[Any] = []
    for block in blocks:
        if (isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and len(block["text"]) > budget):
            compacted.append({**block, "text": compact_text(
                block["text"], budget, label=label, escape_hint=escape_hint)})
        else:
            compacted.append(block)
    return compacted


def _cache_key(tool: str, args: Any) -> str:
    try:
        return json.dumps({"tool": tool, "args": args}, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return f"{tool}:\u0000unserializable"


class DietProxy:
    """Stdio relay that compacts one MCP server's oversized text results."""

    def __init__(self, command: list[str], *, budget: int = DEFAULT_BUDGET_CHARS,
                 label: str = "tool result") -> None:
        if budget < 1:
            raise ValueError(f"budget must be >= 1, got {budget}")
        if not command:
            raise ValueError("wrapped server command must not be empty")
        self._command = command
        self._budget = budget
        self._label = label
        self._lock = threading.Lock()
        self._client_out_lock = threading.Lock()
        self._escape_responses: list[str] = []
        self._client_out: TextIO = sys.stdout
        self._pending: dict[Any, str] = {}  # tools/call id -> cache key
        self._full_passthrough: dict[Any, None] = {}  # ids that skip compaction
        self._cache: OrderedDict[str, Any] = OrderedDict()

    def _remember(self, key: str, content: Any) -> None:
        with self._lock:
            self._cache[key] = content
            while len(self._cache) > MAX_CACHED_RESULTS:
                self._cache.popitem(last=False)

    @staticmethod
    def _bound_tracked(tracked: dict) -> None:
        # Unanswered tools/call ids must not grow without bound: evict the
        # oldest first (a late response for an evicted id simply compacts
        # instead of reusing state — safe degradation, never unbounded memory).
        while len(tracked) > MAX_TRACKED_CALLS:
            tracked.pop(next(iter(tracked)))

    def _lookup(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            if key in self._cache:
                return True, self._cache[key]
            return False, None

    def _handle_client_message(self, message: Any) -> list[str]:
        """Rewrite one client->server message; returns lines to forward.

        A ``_full`` escape either answers from cache (returns [] and queues
        the synthesized response) or forwards stripped with passthrough.
        """
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return [json.dumps(message, separators=(",", ":"))]
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
            return [json.dumps(message, separators=(",", ":"))]
        args = params["arguments"]
        key = _cache_key(str(params.get("name", "")), {k: v for k, v in args.items()
                                                       if k != FULL_ARG})
        if args.get(FULL_ARG) is True:
            stripped = {k: v for k, v in args.items() if k != FULL_ARG}
            found, content = self._lookup(key)
            if found and "id" in message:
                response = {"jsonrpc": "2.0", "id": message["id"],
                            "result": {"content": content}}
                self._escape_responses.append(json.dumps(response, separators=(",", ":")))
                return []
            with self._lock:
                if "id" in message:
                    self._full_passthrough[message["id"]] = None
                    self._bound_tracked(self._full_passthrough)
            message = {**message, "params": {**params, "arguments": stripped}}
            return [json.dumps(message, separators=(",", ":"))]
        with self._lock:
            if "id" in message:
                self._pending[message["id"]] = key
                self._bound_tracked(self._pending)
        return [json.dumps(message, separators=(",", ":"))]

    def _handle_server_message(self, message: Any) -> str:
        """Compact one server->client message; caches full text first."""
        if not isinstance(message, dict):
            return json.dumps(message, separators=(",", ":"))
        msg_id = message.get("id")
        with self._lock:
            passthrough = msg_id in self._full_passthrough
            if passthrough:
                del self._full_passthrough[msg_id]
            key = self._pending.pop(msg_id, None) if msg_id is not None else None
        result = message.get("result")
        if passthrough or not isinstance(result, dict) or "content" not in result:
            return json.dumps(message, separators=(",", ":"))
        content = result["content"]
        if key is not None and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str) and len(b["text"]) > self._budget
                for b in content):
            self._remember(key, content)
        compacted = compact_content(content, self._budget, label=self._label)
        return json.dumps({**message, "result": {**result, "content": compacted}},
                          separators=(",", ":"))

    def _pump_client_to_server(self, client_in: TextIO, server_in: TextIO) -> None:
        try:
            for line in client_in:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = None
                if message is None:
                    forwards = [line if line.endswith("\n") else line + "\n"]
                    suffixed = False
                else:
                    forwards = self._handle_client_message(message)
                    suffixed = True
                try:
                    for forward in forwards:
                        server_in.write(forward + "\n" if suffixed else forward)
                    server_in.flush()
                except (OSError, ValueError):
                    break  # server went away (or pipes closing): stop cleanly
                with self._lock:
                    pending_escapes = self._escape_responses
                    self._escape_responses = []
                for escape in pending_escapes:
                    with self._client_out_lock:
                        self._client_out.write(escape + "\n")
                        self._client_out.flush()
        finally:
            try:
                server_in.close()
            except (OSError, ValueError):
                pass

    def _pump_server_to_client(self, server_out: TextIO, client_out: TextIO) -> None:
        try:
            for line in server_out:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    client_out.write(line if line.endswith("\n") else line + "\n")
                    client_out.flush()
                    continue
                with self._client_out_lock:
                    client_out.write(self._handle_server_message(message) + "\n")
                    client_out.flush()
        except (OSError, ValueError):
            pass

    def run(self) -> int:
        """Relay stdio until EOF or server exit; returns the server's exit code.

        Either side may finish first: stdin EOF ends the client pump (which
        closes the server's stdin so it can exit), while an early server
        exit stops the stdin wait and reaps promptly instead of hanging on
        ``join()`` with a zombie child. The child is always left reaped and
        the pipes closed, even on signals/exceptions — never orphaned.
        """
        proc = subprocess.Popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1,
        )
        assert proc.stdin is not None and proc.stdout is not None
        client_thread = threading.Thread(target=self._pump_client_to_server,
                                         args=(sys.stdin, proc.stdin), daemon=True)
        server_thread = threading.Thread(target=self._pump_server_to_client,
                                         args=(proc.stdout, sys.stdout), daemon=True)
        try:
            client_thread.start()
            server_thread.start()
            while client_thread.is_alive() and proc.poll() is None:
                client_thread.join(timeout=0.1)
            if proc.poll() is None:
                # stdin hit EOF first: the server should exit on closed stdin.
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            else:
                proc.wait()  # already exited: reap immediately, no zombie window
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            # Join only briefly: an in-flight blocking read (grandchild-held
            # stdout, caller-held stdin) can't be interrupted, so let those
            # daemons die with the process instead of stalling shutdown.
            client_thread.join(timeout=2)
            server_thread.join(timeout=2)
            # Close only exited pumps' pipes: closing across a thread that is
            # blocked in readline() would block on the I/O lock until ITS read
            # returns. Live daemons' fds die with the process instead.
            for thread, pipe in ((client_thread, proc.stdin),
                                 (server_thread, proc.stdout)):
                if not thread.is_alive():
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        code = proc.returncode
        assert code is not None
        return code


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``mcp_diet [--budget N] [--label L] -- <server...>``."""
    import signal

    def _sigterm(signum: int, _frame: object) -> None:
        # Let SIGTERM run the run() finally (terminate+reap the child) instead
        # of dying instantly and orphaning the wrapped server.
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (OSError, ValueError):
        pass  # non-main thread or unsupported platform: keep default handling
    parser = argparse.ArgumentParser(
        description="Wrap any stdio MCP server with a labeled output diet.")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_CHARS)
    parser.add_argument("--label", default="tool result")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="server command after '--'")
    args = parser.parse_args(argv)
    command = [c for c in args.command if c != "--"]
    try:
        proxy = DietProxy(command, budget=args.budget, label=args.label)
    except ValueError as exc:
        parser.error(str(exc))
    return proxy.run()


if __name__ == "__main__":
    raise SystemExit(main())
