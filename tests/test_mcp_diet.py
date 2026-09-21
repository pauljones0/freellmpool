"""G14: MCP response diet — compact by default, labeled, full on demand."""

from __future__ import annotations

from types import SimpleNamespace

from freellmpool import mcp_server
from freellmpool.battle import render_battle_markdown
from freellmpool.mcp_server import TOOLS, _call_tool, _compact_text
from freellmpool.panel import PanelAnswer, PanelResult, PanelSynthesis, render_panel_markdown

LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 60  # ~3420 chars


def _answer(i, text=LOREM):
    return PanelAnswer(provider_id=f"p{i}", model=f"m{i}", label=f"p{i}/m{i}", family=None,
                       text=text, error=None, cached=False, latency_ms=100)


def _panel():
    return PanelResult(prompt="Q?", answers=[_answer(0), _answer(1)],
                       synthesis=PanelSynthesis(provider_id="s", model="m", text=LOREM, error=None),
                       requested_count=2, selected_count=2, max_tokens=512, truncated=False)


def _text_of(resp):
    body = resp["result"] if "result" in resp else resp
    return body["content"][0]["text"]


def test_compact_text_passthrough_and_labeled_cut():
    assert _compact_text("short", 100, label="answer") == "short"
    out = _compact_text("x" * 500, 100, label="answer")
    assert len(out) < 500 and "400 chars" in out and '"full": true' in out
    assert out.startswith("x" * 100)


def test_panel_renderer_budgets_answers_and_synthesis():
    full = render_panel_markdown(_panel())
    diet = render_panel_markdown(_panel(), max_chars_per_answer=800)
    assert len(diet) < len(full)
    assert diet.count('"full": true') == 3  # 2 answers + synthesis
    assert LOREM[:800] in diet


def test_battle_renderer_budgets_cells():
    full = render_battle_markdown(_panel())
    diet = render_battle_markdown(_panel(), max_chars_per_answer=800)
    assert len(diet) < len(full)
    assert '"full": true' in diet


def test_models_lists_compact_with_counts_and_full_escape():
    routes = [SimpleNamespace(name=f"p{i % 5}/m{i}", modality="chat") for i in range(100)]
    pool = SimpleNamespace(managed=True, snapshot=lambda: SimpleNamespace(routes=routes),
                           providers=["p0", "p1", "p2", "p3", "p4"])
    diet = _text_of(_call_tool(pool, {"name": "free_llm_models", "arguments": {}}))
    assert len(diet.splitlines()) < 100
    assert "100" in diet and '"full": true' in diet and "p0" in diet
    everything = _text_of(_call_tool(pool, {"name": "free_llm_models", "arguments": {"full": True}}))
    assert len(everything.splitlines()) == 100
    filtered = _text_of(_call_tool(pool, {"name": "free_llm_models",
                                          "arguments": {"provider": "p1"}}))
    route_lines = [line for line in filtered.splitlines() if line and "/" in line and "routes" not in line]
    assert len(route_lines) == 20 and all(line.startswith("p1/") for line in route_lines)


def test_quota_summarizes_allowances_with_full_escape():
    rows = [{"key": f"k{i}", "remaining": 1.0, "unit": "requests", "capacity": 10.0,
             "algorithm": "rolling"} for i in range(100)]
    status = {"eligible_routes": 50, "note": "n", "allowances": rows,
              "providers": [{"id": "x", "eligible": False, "reason": "no key"}]}
    pool = SimpleNamespace(managed=True, managed_status=lambda: status,
                           snapshot=lambda: SimpleNamespace(routes=[]))
    diet = _text_of(_call_tool(pool, {"name": "free_llm_quota", "arguments": {}}))
    assert "50 eligible routes" in diet and '"full": true' in diet
    assert "k99" not in diet and "x: no key" in diet
    everything = _text_of(_call_tool(pool, {"name": "free_llm_quota", "arguments": {"full": True}}))
    assert "k99" in everything


def test_ask_caps_long_reply_with_full_escape():
    long = LOREM * 2
    pool = SimpleNamespace(env={}, providers=[],
                           chat=lambda *a, **k: SimpleNamespace(text=long, provider_id="p",
                                                               model="m", cached=False, attempts=1))
    diet = _text_of(_call_tool(pool, {"name": "free_llm_ask", "arguments": {"prompt": "hi"}}))
    assert len(diet) < len(long) and '"full": true' in diet and "via p/m" in diet
    everything = _text_of(_call_tool(pool, {"name": "free_llm_ask",
                                            "arguments": {"prompt": "hi", "full": True}}))
    assert long in everything


def test_panel_tool_compact_and_full(monkeypatch):
    monkeypatch.setattr(mcp_server, "run_panel", lambda *a, **k: _panel())
    pool = SimpleNamespace()
    diet = _text_of(_call_tool(pool, {"name": "free_llm_panel", "arguments": {"prompt": "hi"}}))
    assert '"full": true' in diet
    everything = _text_of(_call_tool(pool, {"name": "free_llm_panel",
                                            "arguments": {"prompt": "hi", "full": True}}))
    assert '"full": true' not in everything and LOREM in everything


def test_battle_tool_compact(monkeypatch):
    monkeypatch.setattr(mcp_server, "run_battle", lambda *a, **k: _panel())
    diet = _text_of(_call_tool(SimpleNamespace(), {"name": "free_llm_battle",
                                                   "arguments": {"prompt": "hi"}}))
    assert '"full": true' in diet and len(diet) < len(render_battle_markdown(_panel()))


def test_tokenmax_caps_per_model(monkeypatch):
    monkeypatch.setattr(mcp_server, "select_targets", lambda *a, **k: ([("m1", "p1"), ("m2", "p2")], 2))
    monkeypatch.setattr(mcp_server, "fan_out",
                        lambda *a, **k: ([("m1", LOREM), ("m2", LOREM)], []))
    diet = _text_of(_call_tool(SimpleNamespace(), {"name": "tokenmax", "arguments": {"prompt": "hi"}}))
    assert diet.count('"full": true') == 2
    assert "2 answered" in diet


def test_compact_tools_advertise_full_flag():
    by_name = {t["name"]: t for t in TOOLS}
    for name in ("free_llm_ask", "free_llm_panel", "free_llm_second_opinion", "free_llm_battle",
                 "free_llm_recipe", "tokenmax", "free_llm_models", "free_llm_quota",
                 "free_llm_quota_wise"):
        props = by_name[name]["inputSchema"]["properties"]
        assert props["full"]["type"] == "boolean", name


def test_diet_proxy_bounds_unanswered_call_state():
    from freellmpool.mcp_diet import MAX_CACHED_RESULTS, DietProxy

    proxy = DietProxy(["true"])
    for i in range(MAX_CACHED_RESULTS * 4):
        proxy._handle_client_message({"jsonrpc": "2.0", "id": f"never-{i}",
                                      "method": "tools/call",
                                      "params": {"name": "big", "arguments": {}}})
    assert len(proxy._pending) <= MAX_CACHED_RESULTS * 2
    proxy2 = DietProxy(["true"])
    for i in range(MAX_CACHED_RESULTS * 4):
        proxy2._handle_client_message({"jsonrpc": "2.0", "id": f"full-{i}",
                                       "method": "tools/call",
                                       "params": {"name": "x_full",
                                                  "arguments": {"key": "missing"}}})
    assert len(proxy2._full_passthrough) <= MAX_CACHED_RESULTS * 2
