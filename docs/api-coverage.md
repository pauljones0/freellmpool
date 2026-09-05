# API coverage and free-allowance evidence

Use model and account APIs first. They give current machine-readable facts;
official grant documentation fills gaps the responses do not expose. Missing
price or quota fields mean unknown, not paid. A positive list price can coexist
with a free allowance. Reading balances does not authorize spending purchased credit.

## Read-only observations on 2026-09-05

These are dated upstream snapshots, not static catalog counts or guaranteed
capacity. No inference was required for these observations.

| Service | Model API observation | What it does not establish |
| --- | --- | --- |
| Vercel | 373 entries from `GET https://ai-gateway.vercel.sh/v1/models`, with prices; `/v1/credits` supplies `balance` and `total_used` | Free versus purchased credit split, grant reset and account-specific model restrictions |
| Cerebras | 3 entries from `GET https://api.cerebras.ai/v1/models`; basic model identifiers | Remaining trial allocation, expiration and exhaustion behavior |
| Ollama | `GET https://ollama.com/api/usage` supplies `limits.monthly.usage` and model `request_count` values; `POST /api/me` supplies plan metadata | Usage-number unit/scale, absolute remaining allowance, next reset, and full eligible-model inventory |

Account adapters retain OpenRouter and Vercel monetary telemetry, Ollama plan
metadata and monthly reported usage/counts, and optional Mistral capacities. They do not currently import new quota
limits or attest free entitlement. See [account observations](account-observations.md).

Ollama's usage endpoint is confirmed by a
[first-party collaborator](https://github.com/ollama/ollama/issues/12532#issuecomment-5117276581)
and a successful authenticated read on this date. Request counts describe
consumed requests, not per-model limits. The adapter preserves the monthly
usage number with an unknown unit and ignores activity cost and reporting
periods; neither supplies a verified free balance or reset timestamp.

## Reviewed candidates missing from a model listing

Z.ai's global model listing omits some exact models that its official pricing
page lists as free: `glm-4.5-flash`, `glm-4.6v-flash` and `glm-4.7-flash`.
[Global pricing](https://docs.z.ai/guides/overview/pricing).
Separate bounded checks on September 5 returned successful responses for the
first two; the third timed out and its availability remains unknown. These
checks do not establish stream or tool conformance.

An explicit reviewed registry setting can retain these exact zero-price grant
candidates after a complete, nonempty listing succeeds. Their metadata records
that they were not listed and that availability remains unverified; discovery
does not fabricate API prices or renew the original policy evidence. A listed
positive price takes precedence and excludes that route. Failed, partial or
empty listings cannot create candidates through this setting. Current policy,
account and protocol requirements still apply independently before routing.

## Published free allocations and remaining review

| Service | Official advertised allocation | Evidence still needed before activating allowance-funded routes |
| --- | --- | --- |
| Vercel | $5 monthly free credit; purchasing credits ends that recurring free tier. [Pricing](https://vercel.com/docs/ai-gateway/pricing) | Current account grant, eligible models, free-only balance/reset and a hard no-charge boundary |
| Cerebras | $5 free signup trial. [Pricing](https://www.cerebras.ai/pricing) | Remaining allocation, expiration, model costs and a stop at exhaustion; no recurring renewal is established |

These offers are real free allocations described by their providers; they are
not evidence that every catalog model costs zero or that a shared monetary
balance is entirely free. A recurring grant and a one-time trial have different
reset rules. Review must preserve that distinction instead of labeling missing
information as paid access.

Research-only entries in this document do not create active provider records,
parked models or disabled accounts. The active registry contains only reviewed
grants with a hard no-charge boundary, and current model/account evidence must
still support each request. The current maintained catalog admits chat only.
