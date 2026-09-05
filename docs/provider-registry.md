# Provider evidence and maintained discovery

Reviewed September 5, 2026. The packaged registry contains 16 audited provider groups. GitHub Models is retired. A provider or model listing is not proof of recurring free access.

The registry separates public service terms, model pricing, account entitlement, quota limits, and tested capabilities. The router must require each applicable item to remain fresh. Discovery updates only listing facts; it never renews entitlement, reviewed prices, rate-limit evidence, or tool conformance.

## Provider decisions

A conditional grant needs current local account evidence. A zero-price route still needs current prices or an exact reviewed allowlist, correct upstream identity, and paid add-ons disabled. Unknown capacities and reset anchors remain unknown; they are never reported as unlimited.

| Provider | Reviewed grant | Scope and reset | Official evidence |
| --- | --- | --- | --- |
| LLM7 (key optional) | recurring_quota (verified) | 1 requests / rolling 1s / ip; 10 requests / rolling 60s / ip; 60 requests / rolling 3600s / ip; 500000 total_tokens / rolling 86400s / ip | [terms](https://docs.llm7.io/limits), [models](https://docs.llm7.io/guides/models) |
| OVHcloud AI Endpoints (keyless) | recurring_quota (verified) | 2 requests / rolling 60s / ip_model | [terms](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started), [pricing](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-billing) |
| Kilo Gateway (keyless) | zero_price (verified) | 200 requests / rolling 3600s / ip | [terms](https://kilo.ai/docs/gateway/authentication), [pricing](https://kilo.ai/docs/gateway/usage-and-billing) |
| OpenCode Zen (keyless) | zero_price (verified) | unknown requests / unknown 86400s / ip | [terms](https://opencode.ai/docs/zen), [limiter](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/ipRateLimiter.ts) |
| Groq | recurring_quota (conditional) — free | Model-specific RPM/RPD/TPM/TPD; Whisper audio-seconds/hour/day; unpublished model limits stay unknown | [terms](https://console.groq.com/docs/billing-faqs), [limits](https://console.groq.com/docs/rate-limits), [audio billing](https://console.groq.com/docs/speech-to-text) |
| Aion Labs | recurring_quota (conditional) — free | 15 requests / rolling 60s / account; 20000 total_tokens / rolling 60s / account; 20000 total_tokens / unknown 86400s / account | [terms](https://www.aionlabs.ai/docs/rate-limits/) |
| ModelScope API Inference | recurring_quota (conditional) — bound_free | Up to 2000 calls/day/account and 200/day/model; actual model quotas can be lower; reset timezone unknown | [terms](https://modelscope.ai/learn/434362), [limits](https://modelscope.cn/docs/model-service/API-Inference/limits) |
| Vercel AI Gateway | zero_price (verified) | Verified zero input/output prices required | [terms](https://vercel.com/docs/ai-gateway/pricing), [catalog](https://ai-gateway.vercel.sh/v1/models) |
| NVIDIA NIM | recurring_quota (conditional) — developer_prototyping | unknown requests / rolling 60s / account | [official Developer Program FAQ](https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317) |
| OpenRouter (free models) | zero_price (verified) | 20 requests / rolling 60s / account; 50 requests / unknown 86400s / account | [terms](https://openrouter.ai/docs/api_reference/limits.md), [pricing](https://openrouter.ai/docs/guides/routing/provider-selection) |
| Google Gemini | recurring_quota (conditional) — free | Model-specific project RPM/input-TPM/RPD; numeric limits in AI Studio; daily reset America/Los_Angeles | [terms](https://ai.google.dev/gemini-api/docs/pricing), [limits](https://ai.google.dev/gemini-api/docs/rate-limits), [auth](https://ai.google.dev/gemini-api/docs/api-key) |
| Cloudflare Workers AI | recurring_quota (conditional) — workers_free | 10000 neurons / calendar_day 86400s UTC / account; 300 requests / rolling 60s / account | [terms](https://developers.cloudflare.com/workers-ai/platform/pricing/), [catalog](https://developers.cloudflare.com/api/resources/ai/subresources/models/methods/list/) |
| Mistral | recurring_quota (conditional) — free | unknown requests / rolling 1s / account; unknown total_tokens / rolling 60s / model; unknown total_tokens / calendar_month / model | [terms](https://docs.mistral.ai/admin/billing-usage/usage-limits), [limits](https://docs.mistral.ai/api/endpoint/beta/admin/billing) |
| Cohere | recurring_quota (conditional) — trial | 1000 requests / calendar_month / account; 20 requests / rolling 60s / model | [terms](https://docs.cohere.com/v2/docs/rate-limits), [catalog](https://docs.cohere.com/reference/list-models) |
| Z.ai / Zhipu GLM | zero_price (verified) | unknown requests / rolling 60s / account | [terms](https://docs.z.ai/guides/overview/pricing) |
| Ollama Cloud | recurring_credit (conditional) — free_starter | unknown micro_usd / anniversary_month anchored to signup_at / account | [terms](https://ollama.com/pricing), [models](https://docs.ollama.com/cloud) |

## What the current evidence changes

- **Groq and Gemini:** account/project billing tier is a separate gate. A paid key can authenticate and list the same models while inference is charged. Groq request headers describe daily requests and token headers describe minute tokens. Gemini daily requests reset in `America/Los_Angeles`, including daylight saving. Current AI Studio authorization-key migration errors are authentication issues, not proof that a model was retired.
- **OpenRouter:** free routes share an account allowance. Monetary usage is not a free-request counter. Catalog pagination must follow `links.next` and validate totals. Exact free routes plus zero `provider.max_price` and no paid plug-ins give a stronger no-charge boundary.
- **Cloudflare:** 10,000 neurons is one account budget, not one budget per model. The registry stores exact per-model conversion rates and excludes all seven currently paid-required models even on a Free account with prepaid Gateway credits. Unknown model costs cannot be substituted with token counts.
- **ModelScope:** the international editor guide confirms account binding and shared daily inference requests. Model-specific ceilings are dynamic. Mainland real-name requirements and `.cn` credentials must not be silently copied to `.ai`.
- **NVIDIA:** current official hosted documentation describes free prototyping for Developer Program members. It does not verify the often repeated universal 40 RPM limit. Production licensing is separate from this development/research grant.
- **Ollama:** a Free starter account has recurring usage that resets on its signup anniversary. Its unknown starter amount is not unlimited. Current account evidence must establish the free plan and its hard boundary. Public model listings do not identify starter eligibility.
- **Vercel:** only verified zero-priced routes are retained. Account credits do not establish model eligibility.
- **Cohere:** trial keys have a recurring monthly evaluation-call cap. A production key with a superficially similar rate limit is still paid. Native model listing is paginated and exposes endpoint/deprecation metadata.
- **Mistral:** use account/model limits from the console. Optional admin-only GET billing endpoints can supply richer data; ordinary inference credentials need not be administrators. Buying credit or receiving a 429 does not establish a larger recurring free allowance.
- **Aion:** account evidence must establish the provider's recurring free tier before routing.
- **LLM7, OVH and Kilo:** anonymous grants use IP-related shared scopes and omit `Authorization` completely. Adding a key does not create another IP allowance. LLM7 anonymous eligibility uses reviewed Turbo/non-usage-based candidates; positive price metadata for paid modes does not erase a separately verified anonymous grant.
- **OpenCode:** Zen's free models are temporary offers with their own conditions. Go is paid. Free-only profiles must not inherit Zen auto-reload or paid fallback. Responses-only models need the correct protocol and independent conformance.
- **Z.ai:** the global listing omits some exact models documented as free. The reviewed grant can supply unlisted candidates after a complete, nonempty listing succeeds; it does not prove availability. Positive prices in the listing take precedence. Separate bounded checks found two working routes and one timeout, as recorded in [API coverage](api-coverage.md#reviewed-candidates-missing-from-a-model-listing).

## Registry contract

`load_registry()` returns a fresh packaged dictionary keyed by provider ID. `load_registry(env=...)` additionally applies valid local source renewals to evidence dates only. Each record contains `discovery`, `grants`, `limits`, `evidence`, `setup`, and restrictions. A grant has a typed model selector, account requirements, hard no-charge boundary, allowed modalities, prohibited add-ons and evidence references. `required_account_conditions` is an additional conjunction, not an alternative to tier and freshness checks.

Limit metrics distinguish requests, input/output/total tokens, audio seconds, neurons and micro-USD. Optional `model_capacities` maps exact model IDs to reviewed limits; the ordinary `capacity` remains the fallback for unpublished models. Scope identities use account/project references or stable egress-IP references; credential rotation cannot reset an account budget. Windows distinguish rolling intervals, token buckets, calendar day/month and anniversary months. A null capacity, timezone or reset anchor means unknown. Local conservative pacing must be labeled as local pacing, not represented as an authoritative upstream quota.

The ledger persists each quota's unit and reset/refill definition. A semantic change waits out prior usage, observed resets and live leases before starting new accounting; old headers cannot refill the new accounting period. Capacity changes retain existing usage. An old unknown window with no evidenced reset stays blocked for review rather than silently clearing consumption.

`model_costs` records neurons per input/output token for Cloudflare, with source references. Reserve uncached input and round the final neuron cost upward. Groq Whisper records the conservative 10-second audio billing floor. A model's price cannot be inferred from a similarly named sibling. Built-in paid web search and other add-ons are outside the ordinary input/output grant.

## Discovery contract

```python
from freellmpool.provider_registry import load_registry
from freellmpool.discovery import check_provider, load_discovery, refresh_catalog

registry = load_registry()
check = check_provider("openrouter", {})  # GET-only public listing; not a key check
snapshot = refresh_catalog({}, ["openrouter"], public_only=True)
```

Snapshots use `schema: 1`, a generation identifier, and per-provider `checked_at`, `last_attempt_at`, `status`, `complete`, normalized `models`, and provenance. `checked_at` and `complete` describe the last good catalog; `status` describes the latest attempt. An auth error, timeout, malformed page or empty response preserves prior models and their original age. New providers have no executable models until one complete listing succeeds. A failed refresh never renews old evidence.

Normalized models expose `id`, context, modalities, pricing, `is_free`, `supports_tools`, stream support, upstream provider and a limited metadata object. Decimal prices use USD per token, while non-token fields retain their per-request/image units. Missing API prices require an exact reviewed zero-price allowlist; unknown prices alone cannot pass a zero-price grant. Stored catalogs retain only candidates selected by a reviewed hard-free grant; account-dependent candidates still need current account proof before routing. Catalog capability hints are not a substitute for a tested tool-result loop. Where a generic listing lacks task metadata, `modalities_inferred` marks the candidate hint.

The optional protected discovery setting `supplement_from_reviewed_grants` names
reviewed grants that may supply exact zero-price allowlist IDs missing from a
complete, nonempty upstream listing. It currently applies only to Z.ai. Added
rows retain empty API pricing and original policy provenance, and explicitly
mark listing absence and unverified availability. Existing listed rows always
win; this setting cannot override a positive price, invent an ID or revive a
blocked model. Cached supplemental rows must still match the current setting
and grant. Discovery never advances the grant's evidence dates or replaces
independent runtime freshness and conformance checks.

Duplicate JSON fields, repeated model IDs and conflicting price aliases invalidate a refresh; a later zero value cannot overwrite an earlier paid price. Unknown fees remain unknown. Invalid cached model shapes fail closed instead of crashing routing or restoring malformed evidence.

Pagination stays on the approved HTTPS origin. Gemini uses `nextPageToken`; Cohere uses `next_page_token`; Cloudflare checks page metadata; OpenRouter checks linked pages and total counts. Responses and page counts are bounded. Redirects are not followed with credentials. Public listing checks omit credentials even if supplied, and clearly state that key validity was not established.

All 16 retained catalogs were fetched successfully on September 5. Aion uses a native `models` array. Credentialless controls established that Aion, ModelScope and NVIDIA listings are also public, alongside the previously known public gateways. Their listing success cannot validate an inference key. Private catalog APIs are checked with existing credentials locally; none of these checks performs inference.

## Scheduled maintenance

The installed package can refresh authenticated model listings without inference:

```sh
python -m freellmpool.discovery
```

The separate source check can renew unchanged reviewed policy:

```sh
python -m freellmpool.discovery --renew-evidence
```

For a credentialless review artifact:

```sh
python -m freellmpool.discovery --public-only \
  --output artifacts/public-catalogs.json \
  --check-sources artifacts/public-sources.json
```

The default private snapshot is `$XDG_STATE_HOME/freellmpool/discovery.json`, falling back to `~/.local/state/freellmpool/discovery.json`; `FREELLMPOOL_DISCOVERY_FILE` overrides it. Public-only refreshes default to a separate `public-discovery.json` and never preserve authenticated or unclassified rows from an existing output. Files are atomically replaced with mode 0600, with writers serialized across processes. Runtime snapshots retain local restrictions separately; catalog refresh never flips a user's enabled/automatic flags.

The daily `provider-evidence-review.yml` workflow has no provider secrets and uploads model facts plus hashes of official policy sources. Compare successive artifacts to review additions, removals, prices, statuses and source hashes. Auth-only listings are explicitly skipped in public mode. A changed source never extends its policy expiry, increases a limit or enables a model by itself.

Model evidence admission defaults to 48 hours, allowing daily refresh scheduling to vary without immediately losing a working catalog. `FREELLMPOOL_CATALOG_MAX_AGE_SECONDS` adjusts that bounded age. Reviewed policy evidence initially expires after seven days. Packaged `source_hash` baselines were reviewed against the actual claims. A separate source check can renew a matching baseline for at most seven days, bound to a hash of the entire packaged provider policy. The overlay cannot change grants, capacities, prices, models or account state. Modified packaged policy invalidates prior renewals. A failed or changed source retains the last good verification and expiry without extending either; `last_status` reports the latest problem. Missing baselines require review.

`visible_text_v1` includes all published text and ignores scripts/styles and whitespace changes. ModelScope's article is embedded JSON, so `modelscope_article_v1` hashes the full article content, titles and publication status while ignoring counters and avatars. Hashing its nearly empty HTML shell would miss changes; a regression covers that failure. A hash can still change for harmless navigation or formatting updates, which requires review rather than automatic acceptance.

Local renewal state uses `$XDG_STATE_HOME/freellmpool/evidence.json` or `FREELLMPOOL_EVIDENCE_FILE`. Account entitlement and capability evidence have their own expiries. Discovery and source matching never renew them. Account APIs that omit plan, balance, or billing state leave those conditions for the guided setup to confirm. Historical imports preserve original evidence dates, bind explicit user statements to the latest configured credential fingerprint, and retain uncertainty; later credential replacement requires new verification.

## Validation and limitations

The packaged grants currently admit chat only. Embedding and transcription catalogs are discovered, and protocol adapters remain available, but there are **zero admitted upstream embedding/transcription routes** until each exact endpoint, recurring grant, and non-text accounting path is reviewed together. Cloudflare neuron metadata and Groq's audio floor are prerequisites, not a claim that those modalities are already enabled or live-tested.

Tests exercise every provider parser, native pagination, same-origin enforcement, partial/empty/auth failures, secret-free output, exact route identity, zero-price contradictions, private atomic replacement, and immutable source review. Bounded live public listing checks use no inference or credentials. Public catalogs can change during one day; current normalized upstream-route counts are deliberately not treated as enduring advertising claims.

A local ledger cannot see unrelated applications using the same account. Provider-enforced hard free boundaries remain required for billable-overage accounts. Successful listing never supplies a missing balance, model price, account plan or quota reset. Unknown or contradictory evidence remains excluded until resolved.
