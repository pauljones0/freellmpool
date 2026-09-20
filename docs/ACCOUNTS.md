# Getting your free API keys (step by step)

`freellmpool` is only as good as the free tiers you plug into it. Many providers
below offer a cardless free tier, but requirements can change. Vercel can
require customer/card verification even for a zero-price route. You don't need
every provider — even **one** key gets you going. Start with Groq, then add
Gemini or another provider for failover.

> **No keys at all?** **OVHcloud** and **Kilo Gateway** expose
> keyless routes, and **LLM7** works without a key. Therefore
> `freellmpool ask "hi"` can answer after installation while at least one enabled
> keyless route is available. The credentials below can add routes, capacity,
> and failover; their eligibility and terms differ by provider.

Each key takes about a minute. Once you have one, either `export` it in your
shell or store it with `freellmpool keys add <provider>` in the user
`config.toml`. A plain CLI invocation does not parse `.env` by itself; copy
[`.env.example`](../.env.example) only when your shell, `direnv`, or Docker
Compose will explicitly load it.

> Tip: run `freellmpool providers` at any time to see which keys are detected.

---

## Start here (fastest to sign up for)

### Groq — *~1 min, no card*
1. Go to <https://console.groq.com/keys> and sign in with Google/GitHub.
2. Click **Create API Key**, name it anything, copy the value (`gsk_...`).
3. `export GROQ_API_KEY=gsk_...`

   The same key also powers free **audio transcription** (Whisper) via
   `/v1/audio/transcriptions`.

## Add more free pools (optional)

### OpenRouter — *many `:free` models*
1. <https://openrouter.ai/keys> → sign in → **Create Key**.
2. `export OPENROUTER_API_KEY=sk-or-...`

### Google Gemini (AI Studio) — *generous free tier*
1. <https://aistudio.google.com/apikey> → **Create API key**.
2. `export GEMINI_API_KEY=...`

### Mistral — *free tier*
1. <https://console.mistral.ai/api-keys> → **Create new key**.
2. `export MISTRAL_API_KEY=...`

   Free-mode monthly usage is account- and model-specific; check the current
   **Limits** page rather than treating catalog RPD hints as an entitlement.
   Labs models are experimental, and disabled/pin-only lifecycle entries are
   excluded from automatic routing.

   Also gives free **audio transcription** (Voxtral) — a failover for Groq's
   Whisper on `/v1/audio/transcriptions`.

### Cohere — *free trial keys*
1. <https://dashboard.cohere.com/api-keys> → copy your **Trial key**.
2. `export COHERE_API_KEY=...`

### NVIDIA NIM — *free credits, huge catalog*
1. <https://build.nvidia.com> → sign in → pick a model → **Get API Key**.
2. `export NVIDIA_API_KEY=nvapi-...`

### Z.ai / Zhipu GLM — *free GLM flash models*
1. <https://z.ai> → sign in → API keys.
2. `export ZHIPU_API_KEY=...`

### Ollama Cloud — *free tier*
1. <https://ollama.com/settings/keys> → **Create key**.
2. `export OLLAMA_API_KEY=...`

### Aion Labs — *20K free tokens/day, no card*
1. <https://api.aionlabs.ai> → create an account and API key.
2. `export AION_API_KEY=...`

   The free account tier is 15 requests/minute and 20,000 tokens/day. The
   allowance is shared across Aion's priced models and renews daily.

### ModelScope API Inference — *2,000 free calls/day*
1. <https://modelscope.cn/my/myaccesstoken> → create a long-lived access token.
2. `export MODELSCOPE_API_KEY=...`

   API-Inference-enabled models receive dynamic free quotas: currently 2,000
   calls/day in total and up to 200 calls/day for one model. Availability and
   per-model caps can change with platform capacity.

### Vercel AI Gateway — *verified zero-price routes*
1. <https://vercel.com/ai-gateway> → create or select a free Hobby team.
2. Complete Vercel's customer verification. As of the 2026-08-23 live check,
   the gateway requires a valid card on file before it will serve requests,
   even for a Hobby account and an explicitly zero-priced model.
3. Create an AI Gateway API key for the verified zero-price route.
4. `export AI_GATEWAY_API_KEY=...`
5. Confirm automatic top-up is disabled. A monetary budget is not a hard
   free-route eligibility boundary.

   Only the currently price-verified zero-price `poolside/laguna-s-2.1-free`
   route is retained. A key does not authorize other models, and account credit
   is not an eligibility signal. Check current public model and endpoint prices:

   ```bash
   python3 scripts/verify_vercel_gateway.py --public-only
   ```

   After Vercel has cleared any required customer verification, run the bounded
   zero-price acceptance canary:

   ```bash
   python3 scripts/verify_vercel_gateway.py
   ```

   See
   [the dated Vercel acceptance audit](VERCEL_ACCEPTANCE_2026-08-23.md) for
   pricing, provenance, privacy, and the current public-verification status.

### OVHcloud, Kilo Gateway & LLM7 — *no signup needed*
Nothing to do — OVHcloud and Kilo Gateway are anonymous, and LLM7
works without a key. For higher LLM7 limits you can optionally grab a token at
<https://token.llm7.io> and `export LLM7_API_KEY=...`.

**Kilo Gateway** is a keyless OpenAI-compatible aggregator of free models
(~200 req/hour per IP). Heads up: its free routes may log prompts — don't send
confidential data through Kilo models (`kilo/…`).

**OpenCode Zen** is cataloged as a keyless OpenAI-compatible gateway, but its
routes are disabled by default pending explicit opt-in and provider policy
review. Treat it like other anonymous routes: not for confidential prompts.

### Cloudflare Workers AI — *needs two values*
1. Account ID: Cloudflare dashboard → **Workers & Pages** (right sidebar shows
   your Account ID), or **Workers AI** → **Use REST API**.
2. API token: <https://dash.cloudflare.com/profile/api-tokens> → **Create
   Token** → use the **Workers AI** template (read is enough to run models).
3. `export CLOUDFLARE_ACCOUNT_ID=...` and `export CLOUDFLARE_API_TOKEN=...`

---

## Keeping keys around

Rather than re-exporting every shell, drop them in a `.env` file at your project
root (it's gitignored by default in this repo):

```bash
cp .env.example .env
# edit .env, fill in the keys you have
```

`freellmpool` reads from the **environment**, so load the file however you like —
e.g. `set -a; source .env; set +a`, or a tool like
[`direnv`](https://direnv.net/).

## Checking saved keys (`keys check`)

`freellmpool keys check` validates every configured key slot with a read-only
model-listing request. It makes no inference calls, touches no rotation
state, and writes no snapshots — local usage counters are unchanged by a
check run. The listing requests themselves still occur; how a provider
meters listing calls is that provider's policy, not something this command
can promise about:

```bash
freellmpool keys check                       # every provider, every configured slot
freellmpool keys check --provider groq       # one provider (case-insensitive id)
freellmpool keys check --provider groq --slot 2
freellmpool keys check --json                # machine-readable envelope on stdout
```

Only providers whose listing request authenticates can yield a key verdict:
**Cloudflare, Cohere, Gemini, Groq, Mistral, Zhipu**. Every other provider
reports `unsupported` ("listing check does not authenticate; no key judgment")
without any network call — a keyless or public listing can never prove a key
good or bad. Slot 1 is the bare var (`GROQ_API_KEY`), slot N > 1 is `VAR_N`.

Verdicts judge the KEY from what the listing proved. `ok` means the
key was accepted; `auth_failed` means the credential was rejected at
authentication (HTTP 401 on a single-credential provider) — the key is
proven dead. `denied` means the listing was refused without proving the
key bad: an authenticated 403 (often permission scope or account
verification, but edge/geo blocks land here too — per RFC 9110 a 403
does not establish an invalid credential), or a Cloudflare 401, which
jointly authenticates (token, account ID) and so cannot isolate a bad
token from a wrong `CLOUDFLARE_ACCOUNT_ID`. `denied` is inconclusive
and its fix verifies scope out-of-band instead of replacing the key;
Cloudflare `denied` rows name the account-ID check first.

Scope notes. "Proven dead" applies to `keys check` rows, which always
authenticate a saved key. Discovery-level keyless 401s (a public
listing whose provider now requires auth, keyed attempt never sent)
mean "add the credential", not a dead key. Cloudflare keys can never
be proven dead by a listing check: a future improvement could
disambiguate via the token-verify endpoint, but today a Cloudflare
401 stays inconclusive rather than risk a false death sentence. The
setup wizard prints the same account-ID warning before offering
replace-key (which re-collects token AND account ID).

Exit codes (script-safe by default):

- `0` — no key proven dead. Inconclusive rows (`denied`, `rate_limited`,
  `blocked`, `partial`, `deferred`, `timeout`, transport `error`) and
  uncheckable rows never fail.
- `1` — a key was proven dead (`auth_failed`: HTTP 401 outside
  Cloudflare) or a deterministic local failure occurred (`config_error`:
  malformed `CLOUDFLARE_ACCOUNT_ID`, broken registry, internal error).
- `2` — usage error (unknown provider, `--slot` outside 1–9, bad `--timeout`).
  Note: `keys add` uses exit 3 for its usage errors; `keys check` uses 2.

`--strict` fails closed for CI: exit 1 unless every row is `ok`,
`unsupported`, or `missing` (and at least one slot was checked). Recipes:

```bash
# fail CI on anything not proven good-or-uncheckable
freellmpool keys check --strict

# dead keys only (default mode already exits 1 on these)
freellmpool keys check --json | jq '[.rows[] | select(.verdict == "auth_failed")]'

# retry list for retryable inconclusive rows (excludes scope-denied rows)
freellmpool keys check --json | jq -r '.rows[] | select(.fix != null and (.fix | startswith("retry:"))) | .fix'

# scope-denied rows need out-of-band permission checks, not retries
freellmpool keys check --json | jq -r '.rows[] | select(.fix != null and (.fix | startswith("scope:"))) | .fix'
```

`--timeout SECONDS` (default 180) bounds the whole run; slots still unattempted
when the budget expires report `timeout`. Worst case is the timeout plus one
in-flight listing call. Progress lines go to stderr, so `--json` stdout stays
pure. Every failure row names its fix; see also `keys checklist` for presence
todos (which keys to create) as opposed to validation (which saved keys work).

## A note on free-tier limits

Free tiers change. The per-day hints in
[`providers.toml`](../src/freellmpool/providers.toml) are conservative guesses
used only to spread load; `freellmpool` reacts to real `429` rate limits at call
time regardless. If a provider changes its limits, a one-line PR to
`providers.toml` keeps everyone current — see [CONTRIBUTING.md](../CONTRIBUTING.md).
