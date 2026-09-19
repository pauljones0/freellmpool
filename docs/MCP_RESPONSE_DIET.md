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
