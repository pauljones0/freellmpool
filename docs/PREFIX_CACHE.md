# Prefix-aware agent-loop cache

Agent loops resend a growing prefix every turn. The gateway cannot skip
sending that prefix upstream — the model needs the full context — so the
honest cache is provider-side prefix reuse, orchestrated gateway-side.
Nothing here fabricates replies; on providers without prefix caching the
whole feature is a silent no-op.

How it works:

1. **Hash the stable prefix.** Each chat turn fingerprints the message
   list (`prefixcache.hash_prefix`). A follow-up turn whose leading
   messages reproduce a remembered request is recognized as the same
   loop. Identical repeats are deliberately *not* steered: retries keep
   the pool's normal fairness rotation (and the whole-response cache).
2. **Route back to the warm target.** The remembered
   `provider/model` moves first in the candidate order, so the turn
   lands where the provider-side prefix cache is already warm.
   Advisory only — an unknown prefix or an inadmissible target leaves
   ordering untouched.
3. **Harvest provider-confirmed caching.** Usage blocks are parsed for
   `prompt_tokens_details.cached_tokens` (OpenAI/Mistral),
   `cache_read_input_tokens` (Anthropic),
   `prompt_cache_hit_tokens` (DeepSeek/OpenRouter), and
   `cachedContentTokenCount` (Gemini). Absent or malformed claims
   count as zero; claims are clamped to the provider's own prompt
   count so they can never manufacture a refund.
4. **Honest quota math.** Only confirmed cached tokens are deducted
   from token allowances (`ManagedPool._actual_cost`); request counts
   and neuron metering stay gross (conservative). Gross prompt tokens
   stay visible in stats next to the avoided count.

Surfaces: `freellmpool stats` shows prefix-cache hits, hit rate, and
tokens avoided; the same counters appear in `receipt`, MCP status, and
the proxy `/status` payload/HTML. `Reply.cached_prompt_tokens` carries
the per-reply confirmed count.

Measured 2026-09-19 (5-turn code-review loop on
mistral/codestral-latest, temp 0, gateway response cache off):

| Leg | Gross prompt | Avoided | Net billed | Hits |
|---|---|---|---|---|
| Unique nonce per turn (cache defeated) | 3,958 | 0 | 3,958 | 0/5 |
| Stable prefix (same turns) | 3,774 | 2,944 (78.0%) | 830 | 5/5 |

Net billed tokens fell 3,958 → 830 (−79.0%). Actionable per-turn
verdicts (`RESULT:` lines) were byte-identical 5/5 across legs; full
reply text matched 3/5 with prose-only jitter on turns 1 and 3. The
jitter is model-side sampling, not cache effect: two runs with
identical input *and* identical (cold) cache state also produced
different prose. Gateway exact-hits remain byte-identical by unit
test (`tests/test_cache.py::test_pool_uses_cache`).

Provider support observed live: Mistral reports
`prompt_tokens_details.cached_tokens` on its free route; llm7,
OpenRouter-free, and groq report no cache fields (feature no-ops
there, correctly).
