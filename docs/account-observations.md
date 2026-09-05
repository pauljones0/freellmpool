# Account observations for free routing

`freellmpool maintenance --refresh` checks supported account metadata through
fixed read endpoints. It records plan status, reported usage, rate capacities and monetary
telemetry where the API exposes it. A mixed balance is not the remaining free allocation.
These observations do not perform inference or renew operator account
confirmations. `freellmpool maintenance` reads their saved status.

## Supported reads

| Provider | Request | Permission | Retained facts |
| --- | --- | --- | --- |
| OpenRouter | `GET https://openrouter.ai/api/v1/key` | `OPENROUTER_API_KEY`, Bearer authorization | Key flags, USD spending limit/remaining budget, reset schedule, expiration and usage totals |
| Vercel | `GET https://ai-gateway.vercel.sh/v1/credits` | `AI_GATEWAY_API_KEY`, Bearer authorization | Team `balance` and `total_used`, in gateway credits |
| Ollama | Empty `POST https://ollama.com/api/me`, then `GET https://ollama.com/api/usage` | `OLLAMA_API_KEY`, Bearer authorization | Plan, monthly provider-reported usage with unknown unit, and consumed requests per model |
| Mistral | `GET https://api.mistral.ai/v1/admin/rate-limit` | Separate `MISTRAL_ADMIN_API_KEY`, `x-api-key` header | Organization requests/second and per-model tokens/minute and tokens/month |

OpenRouter's nullable budget fields and USD usage totals are spending telemetry,
not free-request counters. `is_free_tier` does not establish historical purchases
or a larger daily free-request allowance. Key labels, personal identifiers and
the deprecated `rate_limit` object are discarded. The API exposes total and
daily/weekly/monthly usage, including BYOK usage when present.
[Current-key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key).

Vercel's `balance` and `total_used` do not distinguish free allocations from
purchased credits or prove the next grant reset. They remain decimal telemetry
in the API's gateway-credit unit, without inventing a currency or free/purchased
split. Reading these fields neither enables paid routing nor renews account
attestations. [Vercel pricing and balance](https://vercel.com/docs/ai-gateway/pricing).

Ollama's first-party client uses POST for its identity read, with no request
body. The adapter accepts the client's `id`/`plan` fields and the cloud service's
`ID`/`Plan` aliases. It rejects duplicate aliases, validates the UUID and discards
it. Any plan outside the recognized free labels becomes `unknown`; the raw
string is never retained. A plan read alone cannot establish all conditions
required for route eligibility.
[Ollama client](https://github.com/ollama/ollama/blob/main/api/client.go),
[response type](https://github.com/ollama/ollama/blob/main/api/types.go).

Ollama's usage read retains `limits.monthly.usage` unchanged as
`account.reported_usage.monthly`, a decimal string with unit `unknown`. The
API's numerical scale is not established, so the adapter does not convert it
to a percentage. `limits.monthly.models[].request_count` becomes
`model.request_count.monthly`, an integer count of consumed requests with
`account_model` scope. These counts are not model allowances or a complete
list of eligible models. Only the verified monthly shape is supported;
legacy session/weekly-only responses do not refresh the snapshot.
[Ollama collaborator's endpoint example](https://github.com/ollama/ollama/issues/12532#issuecomment-5117276581),
[request-count clarification](https://github.com/ollama/ollama/issues/12532#issuecomment-5235794057).

The adapter discards `activity.cost` because its currency and free/purchased
split are not established. It also discards `activity.period`: that reporting
interval does not establish the next allowance reset. Each fact cites the
endpoint that supplied it; the plan and usage reads must both succeed before
either group receives a fresh timestamp. The saved private snapshot contains
the values; maintenance reports currently display their status and freshness.

The Mistral adapter requires `requests_per_second` and a
`tokens_limits_by_model` object mapping model IDs to integer
`tokens_per_minute` and `tokens_per_month` fields. Capacities describe the
organization attached to the Admin key. The response does not prove that the
inference key belongs to that organization or provide a precise monthly reset
anchor. These values therefore remain telemetry. An inference key is never
substituted for the dedicated Admin credential.
[Admin authentication](https://docs.mistral.ai/admin/admin-api/authentication),
[rate-limit response](https://docs.mistral.ai/api/endpoint/beta/admin/billing).

## Coverage and unknowns

The reviewed policy registry contains 16 provider records. Unsupported checks are explicit
and do not send provider keys or create recurring attention items.

| Provider | Free allowance observation coverage |
| --- | --- |
| LLM7 | Anonymous IP usage outside this gateway remains unknown |
| OVHcloud | Anonymous IP/model usage outside this gateway remains unknown |
| Kilo | No reviewed account allowance endpoint |
| OpenCode | No reviewed account allowance endpoint |
| OpenRouter | Monetary key telemetry; free-request allowance and local accounting remain separate |
| Groq | Remaining RPD/TPM is observed from normal response headers |
| Aion | No reviewed account quota endpoint |
| ModelScope | Remaining requests are observed from normal response headers; reset binding remains unverified |
| Vercel | Credit balance telemetry; free allocation and zero model prices are checked independently |
| NVIDIA | No reviewed account quota endpoint |
| Gemini | Requires verified OAuth/project binding and `serviceusage.quotas.get` for quota configuration |
| Cloudflare | Requires verified access, Workers AI neuron mapping and reporting freshness |
| Mistral | Optional dedicated Admin read; telemetry only |
| Cohere | No reviewed remaining account-call endpoint |
| Z.ai / Zhipu | No reviewed quota endpoint for this inference service |
| Ollama | Plan, monthly reported usage and consumed model request counts; absolute remaining allowance and reset remain unknown |

Unknown remaining usage is never interpreted as an unlimited allowance.
`quota_restrictions(...)` currently returns no imported restrictions because
these account responses do not establish the full account, scope and reset
binding required by the shared runtime ledger. Existing verified provider
response-header restrictions remain separate.

See [API coverage and remaining evidence](api-coverage.md) for the distinction
between a model listing, an advertised free allocation and a verified no-charge route.

## Private storage and freshness

The snapshot is written atomically with mode 0600 to
`$XDG_STATE_HOME/freellmpool/account-observations.json`, falling back to
`~/.local/state/freellmpool/account-observations.json`. Set
`FREELLMPOOL_OBSERVATIONS_FILE` to choose another private path. Public reports
and GitHub artifacts never include these records.

Each provider record has `status`, `coverage`, `checked_at`, `expires_at`,
`last_attempt_at`, `credential_ref`, `observations` and a static `note`.
Observation rows retain `fact`, `kind`, `value`, `unit`, `scope`, `source_url`,
`checked_at` and `expires_at`, plus `window`/`model_id` where applicable.
Freshness is independent of catalogs, source evidence and account confirmations.

Successful observations expire after 24 hours. A failed attempt preserves the
last valid values and their original age while exposing the latest failure.
Changing credentials clears old observations, including a change during an
ongoing refresh. Mistral observations bind both the Admin credential and the
configured inference credential. Historical records for unsupported adapters
cannot be replayed into the current schema.

Requests cannot redirect or use inference URL overrides. Response reads are
bounded to 1 MiB per response; duplicate JSON fields and malformed capacities
are rejected. Ollama usage accepts at most 1,000 unique model identifiers with
nonnegative integer request counts. Cached facts must retain their exact
fact-specific source URL and a complete plan/usage pair.
Raw response bodies, account IDs, names, email addresses and arbitrary errors
are never persisted. No observation writes `accounts.json`.

## Reviewed Groq limit proposals

The official free-plan table parser reads bounded JSON literals embedded in
the [Groq rate-limit page](https://console.groq.com/docs/rate-limits). It selects
the `freeRows` table and validates exact column meanings: RPM, RPD, TPM, TPD,
audio seconds/hour and audio seconds/day. A dash remains unknown.

Changed numeric capacities produce proposals with rule/model IDs, scope,
metric, window, previous/new capacity and source URL/hash/parser provenance.
Ambiguous tables, missing reviewed models or unsupported units require review.
The parser never edits the registry, adds a model or renews evidence dates.
