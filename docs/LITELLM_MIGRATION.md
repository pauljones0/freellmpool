# LiteLLM migration guide

Teams leaving LiteLLM (proxy or SDK) can point at the freellmpool
gateway with a base-URL swap. Verified 2026-09-19 with the real
`litellm` 1.101.0 client: 12/12 probes pass, zero breaks found.

## The 1-line switch

```python
import litellm

r = litellm.completion(
    model="openai/auto",  # openai/ prefix + gateway route (see table)
    api_base="http://localhost:8080/v1",
    api_key="any-string-when-no-proxy-key",
    messages=[{"role": "user", "content": "Say OK."}],
)
```

If the proxy sets `FREELLMPOOL_PROXY_KEY`, pass it as `api_key`
(Bearer [REDACTED] the standard way).

## Remap table

| LiteLLM config | Gateway equivalent |
|---|---|
| `model="groq/llama-3.3-70b"` | `model="openai/groq/llama-3.3-70b"` (any `provider/model` from `/v1/models`) |
| deployment `model_name` aliases | `openai/auto`, `openai/agent`, `openai/fast`, `openai/quality`, `openai/fair`, `openai/spread` |
| `api_base` + `api_key` per deployment | single `api_base` + one proxy key (or none on loopback) |
| `Router(fallbacks=[...])` | works unchanged (verified: dead primary → `auto` backup served `OK.`) |
| `stream=True` | SSE `chat.completion.chunk` + `[DONE]` (verified) |
| `stream_options={"include_usage": True}` | accepted; usage on streams is estimated client-side by LiteLLM (authoritative spend stays in the gateway ledger) |
| `response_format`, `tools`/`tool_choice` | pass through; tool-call SSE deltas carry per-call `index` (verified) |
| `litellm.embedding(model="openai/...")` | `/v1/embeddings` incl. `openai/auto` (verified, dim=4096 OVH / 384 Cloudflare) |
| `temperature/top_p/stop/max_tokens` | pass through (verified) |
| `response.usage` / cost callbacks | `prompt/completion/total_tokens` present on non-stream replies (verified: 19/13/32) |

`/v1/models` returns the OpenAI list shape (`object: "list"`,
entries with `id`/`object: "model"`/`owned_by`) plus two harmless
extra keys per entry (`capabilities`, `verified_features`).

## Checklist

1. `freellmpool update` (catalog) → `freellmpool verify` (fresh
   evidence) → `freellmpool status` (eligible routes > 0).
2. Start the proxy: `freellmpool proxy --port 8080` (loopback by
   default; set `FREELLMPOOL_PROXY_KEY` if exposed).
3. Swap `api_base` to `http://host:8080/v1`; prefix model names with
   `openai/`; keep one `api_key` string.
4. Keep LiteLLM `Router` fallbacks — they compose with (not replace)
   the gateway's internal failover.
5. Check `freellmpool status` / `drift` when answers degrade; free
   tiers move under you.

## Explicit non-goals

- Spend tracking / budgets: LiteLLM cost callbacks see usage but the
  gateway prices everything at $0; there is no budget enforcement
  (killed bet #2 stays dead — use the allowance ledger for pacing).
- Admin UI / key management: no dashboard, no virtual keys; one
  optional proxy key only.
- LiteLLM-proxy admin APIs (`/key/*`, `/team/*`, spend logs): not
  implemented and not planned.
- Upstream LiteLLM provider plugins: the gateway is the provider
  surface; LiteLLM-specific params outside the OpenAI schema are
  not mapped.
