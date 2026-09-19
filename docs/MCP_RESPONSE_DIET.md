# MCP response diet (pattern)

Post-G2 finding: *responses* dwarf schemas — one chatty tool result
can eat a fifth of a context window. freellmpool's MCP tools are
compact by default with depth on demand. Copy the pattern:

1. **Budget per piece, not per response.** Cap each answer/listing at
   a fixed char budget (`800` per model answer, `2000` for a
   synthesis, `4000` for a direct answer). Shared helper:
   `panel.truncate_labeled(text, budget, label=...)`.
2. **Every cut labeled in-band, never silent.** The marker names the
   omitted char count and the escape hatch:
   `[… N chars of <label> omitted — re-run with "full": true]`.
3. **One escape hatch flag.** Every capped tool advertises
   `full: boolean` — `true` returns the complete untruncated result.
   Same name, same wording, every tool.
4. **Summarize listings, don't dump them.** `free_llm_models`
   returns per-provider counts + the first 40 rows; `free_llm_quota`
   leads with eligible-route headroom + the first 30 allowance rows.
   Add a filter arg (`provider`) so the compact surface stays fully
   usable without the dump.
5. **Renderers take budgets; callers choose.** CLI keeps full output
   (`max_chars_per_answer=None`); only the MCP handlers pass budgets.

Measured 2026-09-19 (same fixtures before/after):

| Tool call | Before | After | Δ |
|---|---|---|---|
| panel, 3 answers + synthesis | 8,483 ch (~2,120 tok) | 4,241 ch (~1,060 tok) | −50% |
| battle, 3 answers + synthesis | 8,552 ch (~2,138 tok) | 4,319 ch (~1,079 tok) | −49% |
| models, 303 routes | 10,228 ch (~2,557 tok) | 1,467 ch (~366 tok) | −86% |
| quota, managed allowances | 28,131 ch (~7,032 tok) | 2,634 ch (~658 tok) | −91% |

tokenmax/ask/recipe follow the same per-answer caps. Tools already
under ~1K chars (route, roles, tailnet_info, stats, help) were
measured and left alone.

## Adopting the wrapper (third-party MCP authors)

`src/freellmpool/mcp_diet.py` generalizes the truncation above into a
reusable diet for any MCP output. Two integration paths:

**1. In your server (Python): compact before you reply.**

```python
from freellmpool.mcp_diet import compact_content

result = {"content": [{"type": "text", "text": huge_report}]}
result["content"] = compact_content(result["content"], 2000,
                                    label="quarterly report")
```

Under-budget payloads return byte-identical; oversized text blocks are
cut with an in-band marker (`[… N chars of <label> omitted — re-run
with "_full": true]`) so no truncation is silent. Non-text blocks
(images, resources) pass through untouched.

**2. Around any server (any language): the stdio proxy.**

```bash
pip install freellmpool
python -m freellmpool.mcp_diet --budget 2000 -- \
    npx -y @modelcontextprotocol/server-filesystem /data
```

Point your MCP client at the proxy instead of the server. It relays
JSON-RPC both ways, compacts oversized text results in flight, caches
the full text, and answers a re-call with `"_full": true` from cache
(depth-on-demand; no second server round-trip). Requests that arrive
with `"_full": true` and no cache entry pass through uncompacted.

Marker contract: every cut names the label, the omitted char count,
and the escape. Measured third-party results (2026-09-19, budget 2000
chars, ~4 chars/token) — see the
[benchmark page](https://0xzr.github.io/freellmpool/mcp-response-diet.html):

| Server / call | Before | After | Δ |
|---|---|---|---|
| filesystem `read_text_file`, 81 KB report | 81,053 ch (~20,263 tok) | 2,067 ch (~516 tok) | −97% |
| filesystem `list_directory`, 60 files | 1,130 ch (~282 tok) | 1,130 ch (~282 tok) | 0% (under budget, untouched) |
| memory `read_graph`, 30 entities | 36,071 ch (~9,017 tok) | 2,067 ch (~516 tok) | −94% |
| fetch `fetch`, Wikipedia article | 3,342 ch (~835 tok) | 2,066 ch (~516 tok) | −38% |

Zero silent truncations: all 3 cuts carried the labeled `_full`
marker, and the escape restored the full 81,053-char report
marker-free.
