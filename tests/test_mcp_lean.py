"""MCP lean mode: single-router default surface (progressive disclosure)."""

from __future__ import annotations

import json

import pytest
from helpers import make_post

from freellmpool.mcp_server import handle_message
from freellmpool.router import Pool

LEGACY_ACTIONS = (
    "free_llm_ask",
    "free_llm_panel",
    "free_llm_second_opinion",
    "free_llm_battle",
    "free_llm_recipe",
    "free_llm_roles",
    "free_llm_tailnet_info",
    "free_llm_quota_wise",
    "tokenmax",
    "free_llm_route",
    "free_llm_models",
    "free_llm_quota",
    "free_llm_stats",
)


def _pool(providers, env, quota):
    return Pool(providers, quota=quota, env=env, post=make_post({}))


def _call(pool, name, args, **kw):
    return handle_message(
        pool,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        **kw,
    )


def _text(resp):
    return resp["result"]["content"][0]["text"]


def _list(pool, **kw):
    resp = handle_message(pool, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **kw)
    return resp["result"]["tools"]


def test_lean_list_serves_single_router(providers, env, quota):
    tools = _list(_pool(providers, env, quota))
    assert [t["name"] for t in tools] == ["free_llm"]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == [*LEGACY_ACTIONS, "help"]
    assert schema["properties"]["args"]["type"] == "object"


def test_lean_surface_stays_within_token_ratchet(providers, env, quota):
    pool = _pool(providers, env, quota)
    listed = handle_message(pool, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    init = handle_message(
        pool, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    list_chars = len(json.dumps(listed["result"], sort_keys=True))
    instructions = init["result"]["instructions"]
    # <=550 tokens (chars/4) all-in vs the 2791-token wire baseline = 5.07x diet.
    assert list_chars <= 1700
    assert len(instructions) <= 500
    assert list_chars + len(instructions) <= 2200


def test_full_list_serves_legacy_tools(providers, env, quota):
    tools = _list(_pool(providers, env, quota), full_tools=True)
    assert {t["name"] for t in tools} == set(LEGACY_ACTIONS)


# (action, args, marker substring proving the right handler ran)
ROUTED_CALLS = (
    ("free_llm_ask", {"prompt": "hi"}, "via alpha/"),
    ("free_llm_panel", {"prompt": "hi", "n": 1}, "panel"),
    ("free_llm_second_opinion", {"prompt": "hi", "n": 1}, "ok"),
    ("free_llm_battle", {"prompt": "compare"}, "# freellmpool battle"),
    ("free_llm_recipe", {"name": "pr-review", "prompt": "diff"}, "pr-review"),
    ("free_llm_route", {"prompt": "hi"}, "resolved task"),
    ("tokenmax", {"prompt": "hi", "max_models": 1}, "TOKENMAX"),
)


@pytest.mark.parametrize("action,args,marker", ROUTED_CALLS)
def test_router_dispatches_live_actions(providers, env, quota, action, args, marker):
    resp = _call(_pool(providers, env, quota), "free_llm", {"action": action, "args": args})
    assert resp["result"]["isError"] is False
    assert marker in _text(resp).lower() or marker in _text(resp)


# Pure-local actions are deterministic: router output must equal legacy output.
LOCAL_ACTIONS = (
    ("free_llm_roles", {}),
    ("free_llm_tailnet_info", {}),
    ("free_llm_quota_wise", {}),
    ("free_llm_models", {}),
    ("free_llm_quota", {}),
    ("free_llm_stats", {}),
)


@pytest.mark.parametrize("action,args", LOCAL_ACTIONS)
def test_router_matches_legacy_for_local_actions(providers, env, quota, action, args):
    routed = _call(_pool(providers, env, quota), "free_llm", {"action": action, "args": args})
    legacy = _call(_pool(providers, env, quota), action, args)
    assert routed["result"]["isError"] == legacy["result"]["isError"]
    assert _text(routed) == _text(legacy)


def test_legacy_names_still_dispatch_in_lean_mode(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {})
    assert resp["result"]["isError"] is False
    assert "alpha/" in _text(resp)


def test_router_rejects_unknown_action(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm", {"action": "nope", "args": {}})
    assert resp["result"]["isError"] is True
    assert "free_llm_ask" in _text(resp)  # error lists valid actions


def test_router_requires_action(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm", {"args": {}})
    assert resp["result"]["isError"] is True


def test_router_requires_object_args(providers, env, quota):
    resp = _call(
        _pool(providers, env, quota), "free_llm", {"action": "free_llm_models", "args": [1]}
    )
    assert resp["result"]["isError"] is True


def test_help_lists_all_actions(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm", {"action": "help", "args": {}})
    assert resp["result"]["isError"] is False
    text = _text(resp)
    for action in LEGACY_ACTIONS:
        assert action in text


@pytest.mark.parametrize("action", LEGACY_ACTIONS)
def test_help_returns_full_schema_for_each_action(providers, env, quota, action):
    resp = _call(
        _pool(providers, env, quota), "free_llm", {"action": "help", "args": {"name": action}}
    )
    assert resp["result"]["isError"] is False
    detail = json.loads(_text(resp))
    assert detail["name"] == action
    assert detail["inputSchema"]["type"] == "object"


def test_help_unknown_name_is_tool_error(providers, env, quota):
    resp = _call(
        _pool(providers, env, quota), "free_llm", {"action": "help", "args": {"name": "nope"}}
    )
    assert resp["result"]["isError"] is True


def test_mcp_cli_defaults_to_lean_with_full_tools_opt_in():
    from freellmpool.cli import build_parser

    assert build_parser().parse_args(["mcp"]).full_tools is False
    assert build_parser().parse_args(["mcp", "--full-tools"]).full_tools is True
