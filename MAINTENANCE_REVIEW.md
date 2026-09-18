# GitHub maintenance review — September 18, 2026

Reviewed outstanding maintenance findings through issue #120 (policy revisions 5–6).
Revision 5 renewed every evidence timestamp after the September 15–17 public runs;
this review content-verified each changed source against its retained claim, then
published revision 6 with the corrections below. Changed-page text was read for every
renewed hash; hash equality alone renewed nothing.

## Evidence dispositions

| Provider evidence | Disposition |
| --- | --- |
| Cloudflare terms/catalog | Verified: 10,000 neurons/day free, reset 00:00 UTC, hard reject on excess, and the seven paid-required models, which exactly match the grant exclusions. Model search still requires a Workers AI read/write token with page/per_page pagination. |
| Cohere terms/catalog | Verified: evaluation keys free with a 1,000-call monthly trial cap, production keys paid. Listing still supports page_size up to 1000 with page_token/next_page_token and endpoint/deprecation metadata. |
| Gemini terms/limits/auth | Verified: per-model free/paid tier tables, per-project limits with midnight Pacific RPD reset and AI Studio as the numeric source. Standard API keys are now rejected (September 2026 migration live); authorization keys are bound to service accounts. Auth migration errors must not be labeled quota exhaustion. |
| Groq limits | Verified: organization-level limits with request headers for RPD and token headers for TPM. See the qwen3.6 removal below. |
| Kilo terms/pricing | Verified: anonymous :free access at 200 requests/hour/IP; :free routes have zero cost tracked but not billed; BYOK stays excluded. |
| Mistral limits | Verified: admin-key GET /admin/rate-limit, /admin/spend-limit and usage endpoints; ordinary inference keys need not carry admin rights. |
| Ollama terms/models | Verified: unquantified starter credits, one concurrent request, monthly reset from signup date; retirements and cloud/model-ID drift still require independent review. |
| OpenCode terms/limiter | Verified: the limited-time free Zen routes match the allowlist (Big Pickle, MiMo-V2.5, ling-3.0-flash-fin, Nemotron 3 Ultra/3.5 Lightning). The limiter still counts IP-keyed daily/lifetime buckets with config-driven numerics; renewed twice as upstream kept moving. |
| OpenRouter terms/pricing | Verified: 20 RPM with 50/1000 RPD tiers at the $10 credit threshold, /key monetary usage separate from free-request counters (/credits management-key requirement confirmed on the official endpoint reference). max_price-zero routing semantics unchanged; renewed to the current page text. |
| OVH terms/pricing | Verified: anonymous 2 requests/minute per IP and model with separate authenticated billing. The Qwen3.8-27B catalog price change (#119) describes billed usage only; the recurring_quota grant ignores catalog prices and the anonymous allowance is intact. |
| Vercel terms/catalog | Verified: free/paid credit tiers with separately charged add-ons; catalog still carries per-operation pricing and modality metadata. Newly added ling zero-price routes are genuinely zero and the parser still rejects unknown price dimensions. |
| Zhipu terms | Verified: GLM-4.7-Flash, GLM-4.5-Flash and GLM-4.6V-Flash remain zero input/output; FlashX and GLM-5.3-Flash remain paid; web search remains $0.01/use. Renewed to the current raw body. |
| ModelScope terms | The embedded article is byte-identical (same article hash) but moved from /learn/434362 to /posts/434362; the old URL now returns 301. Evidence URL updated to the canonical posts URL and the article-hash pattern in discovery.py follows it. |
| NVIDIA terms | Unchanged hash; first-post extraction re-probed stable after one transient miss. |

## Grant and limit changes

- Groq's official free table replaced qwen/qwen3.6-27b with qwen/qwen3.8-27b (already covered with matching limits). The undocumented 3.6 capacities are removed from all four rules and the model is added to Groq blocked_models plus the grant exclusion, mirroring the Cloudflare paid-model pattern. Admission now denies it even with a free-tier account; the structured limit mapping validates cleanly again (#79).
- Model catalog deltas (kilo, openrouter, modelscope, nvidia, vercel) are discovery-handled: removals are not pinned anywhere in the package and every newly added :free route was spot-checked at zero prompt/completion price before admission. glm-5.2:free returned to the kilo/openrouter catalogs after its September 8 removal; discovery re-admitted it automatically.
- Transient failures (OVH terms check #120, one NVIDIA post extraction) succeeded on retry with unchanged content and needed no policy change.

## Baseline migration

Policy revision 6 changes the ModelScope evidence URL identity, so retained public baselines holding the old URL deliberately fail validation (fail-closed). Local public state was backed up to public-baseline.before-policy-6.json and reset; the next clean run reports zero findings. After merging, run provider-evidence-review.yml once with reset_baseline=true, then close reviewed issues whose findings no longer fire. Absence after reset is not proof of recovery, so each closure references this review.

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
