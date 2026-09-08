# GitHub maintenance review — September 8, 2026

Reviewed open issues #3–14 and #16–20, and dependency PRs #1, #2 and #15.

## Provider evidence

| Issues | Source review and disposition |
| --- | --- |
| #3 | Cloudflare model-search API documents account-scoped bearer authentication, pagination and model metadata. Baseline reviewed; listing is not proof of free account eligibility. |
| #4 | Cohere GET /v1/models documents next_page_token pagination and endpoint filtering. Baseline reviewed; trial-account allowance remains separately enforced. |
| #5 | Gemini API-key guidance now requires authorization keys; standard keys are rejected starting September 2026. Baseline reviewed; project billing and free model prices remain separate checks. |
| #6 | Kilo explicitly describes :free models as tracked but not billed. BYOK is zero cost only on Kilo's side and remains excluded. Baseline reviewed. |
| #7 | LLM7 describes turbo models for anonymous/free-token users and pro for subscriptions/paid balances. Baseline reviewed; the absent gpt-oss alias was removed from the allowlist. |
| #8 | Mistral documents GET /v1/admin/rate-limit with an admin key, including per-model token limits. Baseline reviewed; ordinary inference keys do not imply access to this administrative API. |
| #9 | The mainland ModelScope limits URL returns only a 15-character page title without policy content. Removed this unused evidence record, not the provider. The international embedded article still supports the account/model ceilings. |
| #10 | Ollama documents direct cloud API authentication and public /api/tags discovery. Baseline reviewed. This endpoint does not establish a free account's remaining monthly allowance. |
| #11 | OpenCode's source implements IP/day counting and model buckets; numeric limits come from deployment configuration. Baseline reviewed; no numeric deployment quota inferred. |
| #12 | OpenRouter documents price ceilings and fallback routing. Baseline reviewed; paid fallbacks remain excluded. |
| #13 | OVH documents billing and retirement procedures. Baseline reviewed; anonymous free eligibility remains supported by its separate getting-started evidence, not the billing page. |
| #14 | Vercel's public catalog contains operation pricing and modality metadata. Baseline reviewed; nonzero prices cannot qualify merely because an ID contains free. |
| #16 | OpenCode's free Ling ID is ling-3.0-flash-fin-free. Corrected the allowlist. The Muse Spark contributor route rejects generic Responses calls with MissingSessionID and an OpenCode-only message; removed it from this gateway's allowlist. Spark can still run inside OpenCode and invoke this MCP. Terms baseline reviewed. |
| #17 | Vercel documents a restricted $5/month credit tier and separately priced models. The pool continues admitting only verified zero-priced operations, not arbitrary models paid from credits. Terms baseline reviewed. |
| #18–20 | Live complete catalogs no longer contain OpenRouter z-ai/glm-5.2:free or Vercel minimax/minimax-m2.7-free and minimax/minimax-m3-free. Existing discovery already removes these routes; none are hard-coded in the package. No paid replacement was substituted. |

The fresh scan also confirmed removal of MiniMax M3/M2.7 free routes from Kilo and OpenRouter. Existing complete-catalog replacement handles these removals without new implementation.

Official source URLs and reviewed hashes are retained in src/freellmpool/provider_registry.json. Public source retrieval and model-list checks make no inference requests and read no account credentials. The Spark access check used a synthetic prompt only.

## Dependency PRs

- #1 updates the pinned Python 3.14 Alpine image digest; container and security checks passed.
- #2 updates pinned zizmor to 1.30.0. Its failures were an old version literal in the supply-chain test. The test now requires the same three tools with exact numeric version pins.
- #15 updates three pinned Actions. Three release-contract failures expected old SHAs. Tests now enforce the intended Action identity and full 40-character commit pin, while retaining release permissions and artifact validation checks. The repaired PR passed CI before merge.

## Baseline migration

Policy revision 4 requires source client 0.14.1 because removal of an evidence identity is incompatible with older policy clients. No package or container publication is implied by this source version bump.

Historical maintenance findings reference the removed ModelScope URL and deliberately fail the current public URL allowlist. After reviewing all outstanding findings, run provider-evidence-review.yml once with reset_baseline=true. This uses the existing explicit reset mechanism; validation is not weakened, and subsequent scheduled runs restore the new validated baseline normally.
