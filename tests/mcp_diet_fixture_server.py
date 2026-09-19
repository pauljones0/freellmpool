"""Deterministic stdio MCP fixture server for the G17 diet-proxy tests.

Speaks just enough JSON-RPC over stdio: ``initialize``, ``tools/list``,
``tools/call`` (``big`` returns a large text payload, ``small`` a short
one), and echoes notifications back untouched. Every response is emitted
as one compact JSON object per line.
"""

from __future__ import annotations

import json
import sys

BIG_TEXT = ("0123456789abcdef" * 625)[:10000]  # exactly 10_000 chars


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            # Notification: echo a marker notification back so tests can
            # prove the proxy relays notifications untouched.
            _send({"jsonrpc": "2.0", "method": "notifications/echo",
                   "params": message.get("params", {})})
            continue
        method = message.get("method")
        msg_id = message["id"]
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg_id,
                   "result": {"protocolVersion": "2024-11-05",
                              "capabilities": {},
                              "serverInfo": {"name": "diet-fixture", "version": "0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id,
                   "result": {"tools": [
                       {"name": "big", "inputSchema": {"type": "object"}},
                       {"name": "small", "inputSchema": {"type": "object"}},
                   ]}})
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            text = BIG_TEXT if name == "big" else "tiny-ok"
            _send({"jsonrpc": "2.0", "id": msg_id,
                   "result": {"content": [{"type": "text", "text": text}]}})
        else:
            _send({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32601, "message": f"unknown method {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
