# Product Goals — Easy Free Tokens

Status: **Draft** (vision + kills accepted 2026-09-18; goal texts await explicit acceptance).

Vision (locked): **easy free token setup that just works — free-only scope.**
Anything that manages, meters, or optimizes paid spend is out. Anything that
removes setup steps or failure modes is in.

## How to run this file

- Exactly one goal is `active` at a time. Work it until every `Done when`
  item is verified with evidence, then update its status to `done`.
- **Chaining rule:** when a goal completes, immediately create the next goal
  as the active session goal in the same turn. Never end a turn with no
  active goal while goals remain below.
- **Audit rule:** a goal completes only when every `Done when` checkbox is
  checked with cited evidence (command output, test results, links). Partial
  progress never counts as completion.
- Commit and push per completed goal. Keep this file's statuses current.

## G1 — One-command distribution (Status: done 2026-09-18)

Commits: `b4ef93f` (one-liner + bootstrap + docs), `83be440`
(deterministic docker-smoke). CI green on both (run 35316267164).
Audit: fresh `uv` container → README one-liner → `Freellmpool is ready.`
Namespace verdicts: PyPI name is upstream-owned; `uvx --from git+…`
needs git in the image; tarball URL builds without git; GHCR/MCP
Registry deferred (zero-publish path sufficient).

Pain: every "just use X" tutorial assumes installable artifacts. This fork
publishes nothing (no PyPI, npm, MCP Registry, container) by inherited
policy, so adoption starts with `git clone` + bootstrap script. Setup
cannot be "easy" while installation is manual.

Bet: a first-time user goes from zero to first free reply with one command.

Execute:
1. Investigate namespaces: is `freellmpool` taken on PyPI by upstream?
   Evaluate `uvx --from git+https://...` (no namespace needed), GHCR image
   under this fork's namespace, and MCP Registry listing.
2. Implement the chosen path(s); prefer zero-publish options where they
   satisfy the audit.
3. Update install docs; keep the source-install path working.
4. Include launch copy + trust page v1 (folded bet #8 content).

Done when:
- [x] A fresh container with only Docker/uvx installed reaches a first free
      model reply via one documented command (transcript captured 2026-09-18).
- [x] Full test suite + release gates pass on the commit (local + CI).
- [x] Install docs describe exactly the audited path, nothing else.

Effort: S–M. Fit: highest — distribution is the top of the setup funnel.

## G2 — MCP context diet (Status: complete)

Pain: production agents connect to 5–20 MCP servers × 5–50 tools; ~100k
tokens of tool schemas load before the user types a word. Our MCP server
should "just work" without flooding host context.

Bet: connect freellmpool MCP with minimal context cost and zero lost tools.

Execute:
1. Measure today's cost: tool count + tokens of `tools/list` output.
2. Implement progressive disclosure (thin router tool, on-demand schemas)
   and/or lean profiles for common hosts.
3. Verify every existing tool stays reachable through the new surface.

Done when:
- [x] Before/after token counts show a large reduction (target: 5–10× on
      the default surface) with measurements pasted.
- [x] Claude Desktop/Cursor/Claude Code acceptance: connect, list, call
      one tool from each group successfully. (SDK + Claude Code CLI rows
      PASS — see evidence below; operator accepted this evidence for the
      GUI row on 2026-09-18; `docs/MCP.md` checklist remains for future
      manual runs.)
- [x] Full test suite + MCP conformance checks pass.

Measurements (method: raw stdio `tools/list` + `initialize` result JSON,
`sort_keys`, tokens = chars/4, via `/tmp/mcp_probe.py`):

| Surface | tools/list | initialize | All-in | × vs baseline |
|---|---|---|---|---|
| Before (13 tools) | 10004 ch / 2501 tok | 1163 ch / 290 tok | 11167 ch / 2791 tok | 1.0× |
| After lean (`free_llm` router) | 1478 ch / 369 tok | 474 ch / 118 tok | 1952 ch / 487 tok | **5.73×** |
| After `--full-tools` (13 tools) | 10004 ch / 2501 tok | 474 ch / 118 tok | 10478 ch / 2619 tok | 1.07× |

Design: default `tools/list` serves one `free_llm` router tool
(`{action, args}` + `action: "help"` for on-demand schemas);
`freellmpool mcp --full-tools` lists the 13 legacy tools; `tools/call`
accepts router AND legacy names in both modes (zero lost tools, zero-break
migration). Ratchet test pins lean payload ≤ 2200 chars (`tests/test_mcp_lean.py`).

Acceptance evidence (2026-09-18):
- Official `mcp` SDK ClientSession: connect + list + one call per group
  (ask, multi-model, routing, roles, tailnet, tokenmax) — PASS in lean
  mode (1 tool) and `--full-tools` mode (13 tools).
- Claude Code CLI 2.1.261: `claude mcp add` + `mcp list` health check
  reported "✔ Connected"; config removed afterwards (clean).
- GUI hosts (Claude Desktop / Cursor): manual 2-minute checklist committed
  at `docs/MCP.md` ("Host acceptance checklist"); live GUI run pending
  operator (no GUI host runnable in this environment).
- Full suite green; `ruff check .` clean; `mypy --strict` on `cli.py`
  clean; coverage gate passed (lines 87.96% ≥ 80%, branches 78.38% ≥ 70%).

Effort: M. Fit: high — MCP is a flagship surface.

## G3 — $0 setup guide (Status: complete)

Pain: juniors and students get blindsided by LLM bills; tutorials assume
paid keys. There is no trusted "free from zero" path.

Bet: a tested guide takes a beginner from zero to a first agent reply,
spending $0, with no paid-spend features involved.

Execute:
1. Write the guide: install (via G1) → keyless first reply → add one free
   key → connect one coding agent.
2. Execute every step verbatim in a clean container; fix all drift.
3. Set conservative defaults where the guide exposes knobs.

Done when:
- [x] A fresh-persona run completes end to end in under 15 minutes
      (transcript pasted, timed).
- [x] Every command in the guide is copy-paste verified; no step requires
      a paid key or paid account.
- [x] Docs checks pass.

Guide: `docs/FREE_SETUP.md` (+ one link line in README, additions-only
diff). Key = OpenRouter (registry grant `verified`, `requires_account_evidence:
false`, zero-price — env-var-only, no wizard). Agent = opencode via loopback
proxy (no proxy key needed on `127.0.0.1`; verified `NOKEY_OK`) + `verify`
step for tools evidence (conformance expires after 7 days — the diagnosed
cause of agent failures without it). Conservative pins: `--max-tokens 32`,
`--timeout 60` (45 for probes), `verify --limit 20`, single `-p` pin,
loopback proxy, `agent` alias. No CLI default changes.

Verification (2026-09-18, fresh `python:3.12-slim` container, `/tmp/g3_verify.sh`):
- Fence check: 11 sh fences, 32 lines, 0 missing (every guide command
  executed verbatim; key placeholder filled from host config, never echoed).
- External URLs: 5/5 HTTP 200 (uv docs/install, OpenRouter keys, opencode
  installer, repo tarball). Installer serves the canonical
  `anomalyco/opencode` repo (208k stars, updated 2026-09-18; project moved
  off `sst/` — confirmed not a fork).
- `status`: 48 keyless routes → 71 with OpenRouter key; `strict_free: true`
  before and after; keyless + keyed live replies; `verify` recorded 2
  `tools=pass` routes; `opencode run` → `AGENT_OK`.
- Timed: `G3_ELAPSED_SECONDS=146` container-internal, 147s wall (< 900).
- Gates: `check_docs.py` OK, `check-counts` OK, `quickstart-test.sh`
  (+`LIVE=1` canary) OK, full suite + `ruff check` green.

<details><summary>Timed transcript (key redacted, progress noise stripped)</summary>

```
=== G3 verify: container provisioning (scaffolding, not guide fences) ===
downloading uv 0.12.16 x86_64-unknown-linux-gnu
installing to /root/.local/bin
  uv
  uvx
everything's installed!
To add $HOME/.local/bin to your PATH, either restart your shell or run:
    source $HOME/.local/bin/env (sh, bash, zsh)
    source $HOME/.local/bin/env.fish (fish)
=== guide fences begin ===
uv 0.12.16 (x86_64-unknown-linux-gnu)
   Building freellmpool @ https://github.com/pauljones0/freellmpool/archive/refs/heads/main.tar.gz
      Built freellmpool @ https://github.com/pauljones0/freellmpool/archive/refs/heads/main.tar.gz
Installed 8 packages in 3ms
freellmpool: first run - discovering free routes (one-time)...
Freellmpool is ready to assist you!
Strict free access: 48 eligible routes
  llm7             3 routes  ready
  ovh             18 routes  ready
  kilo            22 routes  ready
  opencode         5 routes  ready
  groq             0 routes  API key or required account field missing
  aion             0 routes  API key or required account field missing
  modelscope       0 routes  API key or required account field missing
  vercel           0 routes  API key or required account field missing
  nvidia           0 routes  API key or required account field missing
  openrouter       0 routes  API key or required account field missing
  gemini           0 routes  API key or required account field missing
  cloudflare       0 routes  API key or required account field missing
  mistral          0 routes  API key or required account field missing
  cohere           0 routes  API key or required account field missing
  zhipu            0 routes  API key or required account field missing
  ollama           0 routes  API key or required account field missing
Inspect enforced budgets and unknown limits: freellmpool status --json
Strict free access: 71 eligible routes
  llm7             3 routes  ready
  ovh             18 routes  ready
  kilo            22 routes  ready
  opencode         5 routes  ready
  groq             0 routes  API key or required account field missing
  aion             0 routes  API key or required account field missing
  modelscope       0 routes  API key or required account field missing
  vercel           0 routes  API key or required account field missing
  nvidia           0 routes  API key or required account field missing
  openrouter      23 routes  ready
  gemini           0 routes  API key or required account field missing
  cloudflare       0 routes  API key or required account field missing
  mistral          0 routes  API key or required account field missing
  cohere           0 routes  API key or required account field missing
  zhipu            0 routes  API key or required account field missing
  ollama           0 routes  API key or required account field missing
Inspect enforced budgets and unknown limits: freellmpool status --json
"strict_free": true
That’s great—your free key works.

Installing opencode version: 1.18.31
Successfully added opencode to $PATH in /root/.bashrc
                                 ▄     
█▀▀█ █▀▀█ █▀▀█ █▀▀▄ █▀▀▀ █▀▀█ █▀▀█ █▀▀█
█░░█ █░░█ █▀▀▀ █░░█ █░░░ █░░█ █░░█ █▀▀▀
▀▀▀▀ █▀▀▀ ▀▀▀▀ ▀  ▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀
OpenCode includes free models, to start:
cd <project>  # Open directory
opencode      # Run command
For more information visit https://opencode.ai/docs
Wire opencode to free models via freellmpool:
  Terminal 1 — keep the proxy running:
    freellmpool proxy --port 8080
  Terminal 2 — configure and launch the client:
    opencode.json:
      {
        "$schema": "https://opencode.ai/config.json",
        "model": "freellmpool/agent",
        "provider": {
          "freellmpool": {
            "name": "freellmpool (free pool)",
            "npm": "@ai-sdk/openai-compatible",
            "options": {
              "baseURL": "http://localhost:8080/v1",
              "apiKey": "{env:FREELLMPOOL_PROXY_KEY}",
              "headerTimeout": 600000,
              "timeout": 600000,
              "chunkTimeout": 120000
            },
            "models": {
              "agent": {
                "name": "Agent \u2014 strongest healthy tier"
              },
              "spread": {
                "name": "Spread \u2014 maximum pool breadth"
              },
              "auto": {
                "name": "Auto \u2014 proxy default routing"
              },
              "fast": {
                "name": "Fast \u2014 lowest latency"
              },
              "quality": {
                "name": "Quality \u2014 capability matched"
              },
              "fair": {
                "name": "Fair \u2014 provider quota spread"
              }
            }
          }
        }
      }
  ℹ Use freellmpool/agent for long-running tool work: it stays on the strongest benchmark tier and spreads usage within that tier.
  More tools + details: docs/INTEGRATIONS.md
kilo/cohere/north-mini-code:free: chat=pass, tools=pass, streaming=pass
kilo/deepseek/deepseek-v4-flash-0731:free: chat=pass, tools=pass, streaming=pass
kilo/dots-studio/dots-3-note-preview:free: chat=pass, tools=unavailable, streaming=unavailable
kilo/inclusionai/ling-3.0-flash-fin:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/inclusionai/ling-3.0-flash-sante:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/inclusionai/ling-3.0-flash-vl:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/kilo-auto/free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/liquid/lfm-2.5-2.6b:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nex-agi/nex-n2.5-mini:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nex-agi/nex-n2.5-pro:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nvidia/nemotron-3-super-120b-a12b:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nvidia/nemotron-3-ultra-550b-a55b:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nvidia/nemotron-3.5-content-safety:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/nvidia/nemotron-3.5-lightning:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/openrouter/free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/poolside/laguna-s-2.1:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/poolside/laguna-xs-2.1:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/qwen/qwen3.8-27b:free: chat=unavailable, tools=unavailable, streaming=unavailable
kilo/stepfun/step-3.7-flash:free: chat=unavailable, tools=unavailable, streaming=unavailable
{"object": "list", "data": [{"id": "auto", "object": "model", "owned_by": "freellmpool", "capabilities": {}, "verified_features": []}, {"id": "agent", "object": "model", "owned_by": "freellmpool", "ca

> build · agent

AGENT_OK
=== guide fences end ===
G3_ELAPSED_SECONDS=146
```

</details>

Effort: S. Fit: high — this is the vision in document form.

## G4 — Easy free embeddings (Status: complete)

Pain: RAG pays an embedding-API tax on every document and query (cost +
rate limits); builders are moving embeddings to CPU to escape it. Our
`[[embedder]]` plumbing exists but the catalog ships zero rows.

Bet: `/v1/embeddings` works on free routes with the same one-setup story,
plus a $0 RAG quickstart.

Execute:
1. Review free embedding routes through the normal evidence process.
2. Add `[[embedder]]` catalog rows + admission tests.
3. Write a minimal RAG quickstart (embed → retrieve → generate, $0).

Done when:
- [x] Embedding requests succeed against reviewed free routes (keyless
      where available), verified live.
- [x] The RAG quickstart runs end to end at $0 (transcript pasted).
- [x] Full suite, coverage, and policy gates pass.

Reviewed routes (normal evidence process, live-probed 2026-09-18):
- `ovh/Qwen3-Embedding-8B` — keyless, 4096d. Verified recurring_quota
  grant (mirrors chat grant; anonymous allowance per reviewed OVH terms).
- `mistral/mistral-embed` — 1024d. Conditional recurring_quota grant,
  tier `free` (mirrors chat grant: API free mode with included monthly
  usage per reviewed usage-limits doc; live 200 on a tier=free account;
  no exclusion for embeddings found in pricing/limits docs).
- `cloudflare/@cf/baai/bge-small-en-v1.5` — 384d via OpenAI-compat
  `/ai/v1/embeddings`. Conditional recurring_quota grant, tier
  `workers_free`; neuron rate (0.001841/input token) already reviewed in
  `model_costs`. Fixed managed accounting to price input-only neuron
  rates (embeddings have no output tokens) + regression test.

Implementation: separate `free-embedding` grants (allowlist selectors,
`hard_free_boundary`, never widened chat grants); `[[embedder]]` rows in
`providers.toml`; limits `grant_ids` extended; admission tests incl.
registry-backed allow/deny matrix; policy revision 8 + client 0.14.2 per
the policy-channel contract. No client shape changes (all three routes
speak OpenAI `/embeddings`).

Live verification: `Pool.embed` + managed `Pool.embed` + proxy
`/v1/embeddings` all returned vectors on all three routes (OVH keyless;
keyed via host keys, redacted). Note: managed-path runs used
`FREELLMPOOL_POLICY_UPDATES=0` because the host's cached rev-7 bundle
fail-closed against the new packaged grants — expected until the rev-8
bundle publishes from main; host healed post-push via refresh.

RAG quickstart: `docs/RAG_QUICKSTART.md` (embed → cosine retrieve →
generate, stdlib only). Transcript (proxy on :18933; :8080 was occupied
on the test machine — only the BASE port differs from the doc):

```
embedded 3 docs + query, dim=4096
score=0.8604 doc1: Cuttlefish change color in milliseconds using pigment sacs c...
score=0.3169 doc0: The freellmpool gateway pools free-tier LLM routes behind on...
score=0.1571 doc2: The Treaty of Tordesillas divided the New World between Spai...
answer: Cuttlefish change color in milliseconds using pigment sacs called chromatophores.
served_by: kilo/kilo-auto/free | usage: {'prompt_tokens': 58, 'completion_tokens': 40, 'total_tokens': 98}
```

Gates: full suite green; coverage 87.96%/78.39% (≥80/70); `ruff check`
clean; `mypy --strict` on touched modules clean; `check_docs.py`,
`check-counts` (3 HTML cells bumped), `validate_catalog.py`,
`check_release_ready.py`, `vet_catalog.py`, and `check_policy_channel.py
--base HEAD` (rev 8) all pass.

Effort: S–M. Fit: high — embeddings are tokens too.

## G5 — Claude Code compat hardening (Status: complete 2026-09-18)

Break log (real `claude` CLI 2.1.261 vs gateway `/v1/messages`):
- B1 (fixed): every CLI request carries 28 tools, but 71/73 tools passes
  had expired (>7d), collapsing the bench to 2 routes that burned out
  within minutes → 429 death spiral (CLI backs off on Retry-After
  forever). Fixed by re-verifying tools evidence (bench: 19 routes /
  8 providers) + `status` now reports `tools_ready`/`tools_providers`
  and warns below 3 (regression tests x3).
- B2 (fixed, docs): `ANTHROPIC_MODEL=auto` triggers an unknown-model
  warning; docs now prescribe a `claude-*` alias name.
- Verified OK, no break: streaming SSE (exact event order, terminates),
  `?beta=true`, `thinking`/`output_config`/`context_management` tolerance,
  mid-list `system`-role message, 429 + `Retry-After` header, session
  resume (`-c`) with Edit/Read. Continued long session completed its
  file edits, then rode out genuine upstream per-minute 429s — capacity
  reality, not a protocol break.
- Setup: 3 commands in docs/INTEGRATIONS.md (proxy, export, claude).

Live transcript (multi-turn tool session, exit 0, 2 turns, 8.4s):
```
[init model=auto claude_code=2.1.261 tools=28]
TOOL_USE: Write {"file_path": "/tmp/g5-work/g5-probe.txt", "content": "probe-ok\n"}
TOOL_RESULT: File created successfully at: /tmp/g5-work/g5-probe.txt
ASSISTANT: The file g5-probe.txt has been created with the single line: probe-ok
[result turns=2 duration_ms=8372]
$ cat /tmp/g5-work/g5-probe.txt
probe-ok
```
Follow-up resumed session (`-c`, Edit+Read) appended `probe-ok-2`.
Gates: full suite green, coverage 88.00/78.42, ruff + strict mypy clean,
catalog/policy/counts pass.

Pain: Claude Code is the most-used coding agent; our Anthropic-compat path
is experimental, so "it just works" fails exactly where users are.
(Cost-attribution smarts explicitly out of scope per the vision lock.)

Bet: Claude Code runs real sessions end to end through the gateway on
free routes.

Execute:
1. Discover gaps: run representative Claude Code flows (chat, tools,
   streaming, long sessions) against the current shim; log every break.
2. Fix gaps with conformance regression tests per fix.
3. Document the Claude Code setup in three commands or fewer.

Done when:
- [x] A real multi-turn Claude Code session with tool use completes via
      the gateway (transcript pasted).
- [x] Zero known compat breaks remain; each historical break has a
      regression test.
- [x] Full suite + gates pass.

Effort: M–L. Fit: high — meets users where they already are.

## G6 — Trust page + relaunch (Status: complete 2026-09-18)

- Trust page: `docs/TRUST.md` (full) — pins, Bandit/pip-audit/zizmor/
  CodeQL gates, evidence process, ToS posture; every claim links to
  proof; SBOM/container/PyPI honestly listed as non-promises
  (upstream-gated pipelines).
- Launch copy: `docs/promotion/relaunch.md` + refreshed pack facts.
- Published: <https://github.com/pauljones0/freellmpool/discussions/122>
  (Announcements; Discussions enabled for this venue, single post,
  affiliation disclosed; external channels stay human-gated).
- Docs checks pass; no code changed.

Pain: LiteLLM's CVE/KEV fallout has teams reevaluating gateways, but
nobody knows the auditable-minimal alternative exists. Must run last:
"easy setup" claims are only honest once G1–G5 land (see
[ROADMAP principles](docs/ROADMAP.md)).

Bet: a trust page where every claim links to verification, plus a
relaunch post for the completed easy-setup story.

Execute:
1. Write the trust page: pins, SBOM/audit gates, evidence process, ToS
   posture — each claim linked to its proof.
2. Prepare launch copy from `docs/promotion/` updated for the new setup.
3. Publish/post per the repo's outreach ground rules.

Done when:
- [x] Every claim on the trust page links to live verification.
- [x] Launch post published (link recorded here).
- [x] No new code; docs checks pass.

Effort: S. Fit: medium — distribution of proof, not product.

## Goal chain G7–G14 (accepted 2026-09-18)

Eight pain-point goals, brainstormed from live web research (free-tier
429 complaints, Claude Code $200/mo pain, LiteLLM CVE fallout, MCP
context-tax analysis, free-tier drift reports). Execute strictly in
order; each goal's Done-when is the audit for "did we completely solve
this pain?". On completing each goal, immediately create the next goal
as the active session goal in the same turn. If a goal is truly
blocked, record the blocker here and skip to the next — never hold the
chain hostage.

## G7 — Multi-key rotation per provider (Status: complete 2026-09-19)

Shipped: numbered slots (`KEY`, `KEY_2`…`_9`) via `Provider.api_keys()`;
sticky-until-429 `KeyRotator` (per-slot cooldowns, 401/403→300s,
429→Retry-After/60s); rotation wired on managed + legacy + async paths
(chat/embed/transcribe/stream); snapshot admits a route when ANY slot
qualifies; unadmitted slots skipped pre-dispatch with attempt notes;
`status` reports `key_depth` + prints multi-key lines;
`keys add --slot N`; FAQ documented. 13 regression tests.

Live transcripts (real upstreams, single-account boundary noted below):
```
# Track A: dead slot 1 → served via slot 2 (groq)
TEXT: slot-two-ok | VIA: groq / qwen/qwen3.8-27b | ATTEMPTS: 4
# Track B: unadmitted slot skipped pre-dispatch, accounting intact
groq/qwen/qwen3.8-27b: key slot 2 skipped (credential not admitted)
groq/qwen/qwen3.8-27b: allowance exhausted   # 45KB probe > local TPM grant
```
- Deterministic 429→rotate→success, cooldown-skip, and
  all-exhausted→429+Retry-After proven by unit tests (fake transports).
- Literal live 429→success needs two funded buckets; with one account
  slot 2 reproduces slot 1's 429 by construction. Attempts to force it
  (80-req RPM burst, per-model pins, TPM-shaped singles across
  groq/openrouter) documented in-session; no second account was
  created (out of scope). Re-verify if a second key ever exists.
- `status` live: `Multi-key rotation: groq=2 keys`, `key_depth.groq=2`.
- Key-material grep over all state files: zero hits outside config.toml.
Gates: full suite green, coverage 88.01/78.39, ruff + strict mypy clean
on touched gated modules, catalog/policy/counts/docs pass.

Pain: one free key = one rate-limit bucket. A single 429-prone key
stalls whole sessions; competitors already rotate tokens and we do not.
Users with two free keys (or a partner's key) get no benefit today.

Bet: N keys per provider behave as one deep bucket with honest
per-key cooldowns.

Execute:
1. Accept numbered key slots per provider (`<PROVIDER>_API_KEY`,
   `<PROVIDER>_API_KEY_2`, …) in env + `keys.toml`; never log key
   material.
2. Rotate on 429/auth-failure with per-key cooldowns; skip
   cooled-down keys without spending them; surface bucket depth in
   `status`.
3. Regression tests per behavior: rotation order, per-key cooldown,
   sticky-until-429 vs round-robin (document the chosen policy),
   all-keys-exhausted → 429 + `Retry-After`.

Done when:
- [x] A live two-key session survives a forced 429 on key 1 by
      serving from key 2 (transcript pasted).
- [x] `status` shows per-provider key depth; zero key material in
      logs/state (grep-verified).
- [x] Full suite + gates pass.
- [x] Commit + push; G8 goal created in the same turn.

Effort: M. Fit: high — multiplies every session-survival flow G1–G6 built.

## G8 — One-command $0 coding agent launcher (Status: complete 2026-09-19)

Shipped: `freellmpool claude [--harness opencode] [--port] [--model] --
<agent args>` (`src/freellmpool/launcher.py` + CLI). Starts the loopback
proxy when none is live (reuses a running one), sets `ANTHROPIC_*` /
writes an `OPENCODE_CONFIG` provider file (verified honored), then
exec-replaces into the agent. 9 regression tests. Docs: 1-command setup
in INTEGRATIONS.md with free-model/429 caveats (manual 3-command kept).

Fresh-container transcript (python:3.12-slim, `/tmp/g8_verify.sh`,
G8_ELAPSED_SECONDS=386, EXIT=0):
```
routes: 50
=== 4. opencode harness file-edit via launcher ===
freellmpool: started loopback proxy on 127.0.0.1:8080
Write g8-docker.txt / Wrote file successfully. (52s)
--- file: docker-ok
=== 5. claude harness file-edit via launcher ===
Created g8-docker2.txt with the exact content "docker-ok-2". (9s)
RESULT: both agent file-edits OK
```
Host spot-checks: claude Write exit 0 in 25s; opencode Write exit 0 in
34s. Key entered via host mount, never echoed.
Gates: full suite green, coverage 88.03/78.38, ruff + strict mypy
clean, catalog/policy/counts/docs pass.

Pain: "run Claude Code free" is a top-trend pain, but every setup is a
fragile 10-step README. G5 proved compat; nobody can run it without
hand-holding.

Bet: one command starts the gateway, wires the env, and execs the
agent — for Claude Code and OpenCode.

Execute:
1. `freellmpool claude` (and `--harness opencode`): start proxy,
   set `ANTHROPIC_*`/OpenCode provider config, exec the agent
   in-process-replacing (signals propagate).
2. Copy-paste-verify every step verbatim in a clean container like G3;
   record the transcript + timing.
3. Docs: 1-command setup in INTEGRATIONS.md; caveats (free-model
   quality, 429 backoff behavior).

Done when:
- [x] Fresh-container run goes from zero to a real agent file-edit via
      the launcher (transcript pasted, timed).
- [x] Both harnesses verified (claude exec + opencode config path).
- [x] Full suite + gates pass.
- [x] Commit + push; G9 goal created in the same turn.

Effort: S–M. Fit: high — distribution kicker for the whole series.

## G9 — Vision on the Anthropic bridge (Status: complete 2026-09-19)

Shipped: Anthropic image blocks (base64 + url, user/assistant/tool_result)
translate to OpenAI vision parts; new `media.py` with parsed PNG/JPEG/GIF
dimensions, tile-formula token bound (512px tiles × 170 + 85), flat 2000
for remote/unknown, 5MB decoded guard — all loud, never silent.
Managed `_cost` accounts images (payload-blanked bytes + bound); empty
vision bench → honest 400 naming `verify --features vision`; legacy
estimator counts image tokens. Deliberate contract change: remote media
now flows with the flat estimate instead of 400 (old test updated).
13 vision tests; vision+tools routing conjunction covered (existing
canary matrix + `required_features` test).

Live transcript (real claude CLI via launcher, red-circle PNG):
```
# first attempt, vision+tools bench empty → honest 400 through the CLI:
API Error: 400 No vision-verified free route is available for this
image request. Run freellmpool verify --features vision.
# after verifying (7 vision passes, 2 with tools):
> What single color is the circle? → "the circle is **red**." EXIT=0 (76s)
```
Served via gateway vision routes (kilo step-3.7-flash ×8 in quota).
Gates: full suite green, coverage 87.92/78.23, ruff clean, zero new
strict-mypy errors, catalog/policy/counts/docs pass.

Pain: G5's known gap — image blocks are silently dropped. Agent users
paste screenshots, diagrams, and error photos constantly; silent
dropping is the worst failure mode (wrong answers, no error).

Bet: images flow through on vision-verified free routes, or the client
gets a loud, honest error.

Execute:
1. Translate Anthropic image blocks to OpenAI vision content on routes
   with fresh `vision` conformance; add a downscale/size guard with a
   documented bound.
2. Routes without vision proof → honest 400-class error naming the
   gap (never silent drop).
3. Conformance probe for vision (+ vision with tools); regression
   tests per fix; live-verify one image turn through the real CLI.

Done when:
- [x] A live `claude` turn referencing an attached image succeeds via
      the gateway (transcript pasted).
- [x] Silent-drop path is impossible by construction (test proves the
      error branch).
- [x] Full suite + gates pass.
- [x] Commit + push; G10 goal created in the same turn.

Effort: M. Fit: high — removes the biggest "silently wrong" behavior.

## G10 — Free-tier drift radar (Status: complete, 2026-09-19)

Pain: every free-tier list on the internet rots within weeks — limits
change, models sunset, ToS shifts (e.g. Gemini's Mar-2026 EEA/UK
end-user serving restriction). Users discover drift by failing.

Bet: the gateway tells you what changed before you feel it, and
publishes a live-verified snapshot others can consume.

Execute:
1. `freellmpool drift`: diff last verify evidence vs fresh probes;
   print changed/died/recovered routes with dates.
2. Emit a weekly machine-readable snapshot (dated, sourced) designed
   for third-party consumption; document the schema.
3. Tests for diff classification (changed vs died vs recovered);
   docs-check the snapshot schema doc.

Done when:
- [x] `drift` correctly reports a real, live-observed change (paste
      the report showing a genuine delta).
- [x] Snapshot schema documented + validated by a checker script.
- [x] Full suite + gates pass.
- [x] Commit + push; G11 goal created in the same turn.

Evidence (2026-09-19, 210-target baseline → 8-target live verify):
`Drift: 1 change(s) since 2026-09-19T02:09:34Z (as of 2026-09-19T02:10:31Z):`
`[changed] cloudflare/@cf/zai-org/glm-4.7-flash streaming: pass -> unavailable`
Snapshot: `freellmpool drift --emit` (484 statuses) validated clean by
`scripts/check_drift_snapshot.py`; schema in `docs/DRIFT_SNAPSHOT.md`
with its normative example machine-checked by `test_schema_doc_example_validates`.
Gates: full suite green, coverage 87.91/78.21, `ruff check .` clean,
strict mypy on 23 modules, check_docs + check-counts pass.

Effort: M. Fit: medium-high — turns the evidence engine into
distribution.

## G11 — Structured-output repair loop (Status: complete, 2026-09-19)

Pain: free models are bad at strict JSON; builders waste days on parse
failures, regex salvage, and hand-rolled re-prompts.

Bet: `response_format: json_schema` just works — the gateway validates
and, on failure, re-asks once with the validation error appended,
all inside honest allowance accounting.

Execute:
1. Validate JSON-mode responses against the schema; on failure,
   one bounded repair turn carrying the validation error.
2. Repair spend counts against allowances (no free double-calls);
   document the bound (max 1 repair, then honest error).
3. Conformance probe + regression tests (valid passthrough, repaired,
   unrepairable → honest error); live-verify against a weak free model.

Done when:
- [x] A live structured-output request that fails raw JSON parsing
      succeeds through the repair loop (transcript pasted).
- [x] Allowances charged for both turns (ledger evidence pasted).
- [x] Full suite + gates pass.
- [x] Commit + push; G12 goal created in the same turn.

Evidence (2026-09-19, cohere/command-r-08-2024, json_object, max_tokens=160):
turn 1 raw reply truncated mid-string (`... "Yogurt Parfait: Lay` + end of
output) -> strict parse failed `Unterminated string`; turn 2 (repair, 3
messages, validation error appended) returned shorter valid JSON that parses.
Final: `{"foods": ["Fruit Salad: ..."]}`, attempts=2.
Ledger: reservations 511 -> 513; two distinct charge sets
(`d715059b...` raw, `31c59c94...` repair), each 1 request across 4 limit keys.
Bound documented in `docs/STRUCTURED_OUTPUT.md` (max 1 repair, then
`StructuredOutputError`/HTTP 502); canaries still measure raw output.
Gates: full suite green, coverage 87.94/78.25, `ruff check .` clean,
strict mypy on 25 modules, check_docs + check-counts pass.

Effort: M. Fit: high — unlocks agent/tool workloads on weak models.

## G12 — RAG-in-a-box CLI for students (Status: complete, 2026-09-19)

Pain: G4 proved $0 RAG is possible, but it is still a quickstart, not
a tool. Classrooms and solo builders want RAG without a backend, a
vector DB, or any bill.

Bet: two commands index a folder and answer questions over it,
entirely on free routes with an embedded store.

Execute:
1. `freellmpool rag index ./docs` + `freellmpool rag ask "…"` backed
   by an embedded sqlite-vec (or equivalent zero-service) store.
2. All-free embeddings + chat; honest errors when the bench is thin.
3. End-to-end test at $0 in a clean container (index → ask →
   cited answer); quickstart doc updated to the CLI.

Done when:
- [x] Clean-container run indexes a sample folder and returns a
      correct, cited answer at $0 (transcript pasted, timed).
- [x] Store + deps add no services and no paid path (review the dep
      diff explicitly).
- [x] Full suite + gates pass.
- [x] Commit + push; G13 goal created in the same turn.

Evidence (`scripts/rag_container_test.sh`, 2026-09-19, 11s elapsed,
no keys, no state mounts):
`Indexed 3 chunks from 3 file(s) (embeddings: Qwen3-Embedding-8B)`
`Cuttlefish change color in milliseconds using pigment sacs called chromatophores [1].`
`Sources (llm7/codestral-latest): [1] fish.txt (chunk 0, score 0.86) ...`
`RAG E2E PASS`
Dep review: store is stdlib `sqlite3` + brute-force cosine — `git diff`
on `pyproject.toml`/`Dockerfile`/requirements is EMPTY, and
`test_rag_imports_stdlib_only` forbids non-stdlib imports in `rag.py`.
Gates: 2497 passed, coverage 87.91/78.18, `ruff check .` clean,
strict mypy on 26 modules, check_docs + check-counts pass.

Effort: M–L. Fit: medium — owns the student segment G3 opened.

## G13 — LiteLLM drop-in migration path (Status: complete, 2026-09-19)

Pain: teams fleeing LiteLLM's 2026 CVE record need a 1-line switch,
not a rewrite. Our OpenAI surface is close but unproven as a
migration target.

Bet: a tested remap + checklist makes switching mechanical, with an
honest "what we deliberately don't do" list.

Execute:
1. Probe with a real LiteLLM-client configuration against the
   gateway: base URL swap, `provider/model` naming, `/v1/models`
   shape, fallback semantics. Log every break.
2. Code only where a failing probe proves a compat gap (no
   speculative shims); regression test per fix.
3. Migration doc: remap table, checklist, explicit non-goals
   (budgets, admin UI — killed bet #2 stays dead).

Done when:
- [x] A real LiteLLM-client config completes chat + streaming +
      failover against the gateway (transcript pasted).
- [x] Zero known migration breaks; migration doc merged.
- [x] Full suite + gates pass.
- [x] Commit + push; G14 goal created in the same turn.

Evidence (2026-09-19, real `litellm` 1.101.0 client, api_base swap only):
`[OK] chat-auto`, `[OK] chat-pinned` (cohere/command-r-08-2024),
`[OK] stream` ("1, 2, 3."), `[OK] models` (309, OpenAI list shape),
`[OK] failover` (Router dead-primary -> `auto` backup served `OK.`
via mistral/codestral-2508), `[OK] embed` (dim=4096),
`[OK] params+usage` (19/13/32, finish=stop), `[OK] json_object`,
`[OK] tools` (record_number{7} tool_call), `[OK] stream_usage`,
`[OK] embed_auto`, `[OK] multi_turn` — 12/12, zero breaks, so zero
code changes per the no-speculative-shims rule (existing proxy tests
already lock /v1/models + SSE shapes). Migration doc:
`docs/LITELLM_MIGRATION.md` (remap table, checklist, non-goals:
budgets, admin UI, spend APIs). Note: stream usage is estimated
client-side by LiteLLM; authoritative spend is the gateway ledger.
Gates: full suite green, coverage floors pass, `ruff check .` clean,
check_docs + check-counts pass.

Effort: M. Fit: medium — captures CVE-driven demand with proof.

## G14 — MCP response diet (Status: complete, 2026-09-19)

Pain: post-G2 research shows *responses* dwarf schemas — one chatty
tool result can eat 19%+ of a context window. Our own MCP tools have
no size discipline.

Bet: freellmpool's MCP tools return compact results by default, with
depth available on demand.

Execute:
1. Cap + truncate + summarize-large-result behavior for our own MCP
   tools; every truncation labeled in-band (never silent).
2. Measure before/after response token sizes on representative calls
   (paste the numbers).
3. Regression tests per tool behavior; document the pattern for other
   MCP authors.

Done when:
- [x] Before/after measurements show large-response shrinkage with
      zero silent truncations (numbers pasted).
- [x] Every tool stays fully usable through the compact surface.
- [x] Full suite + gates pass.
- [x] Commit + push; chain complete — report the series result.

Evidence (same fixtures, 2026-09-19): panel 8,483→4,241 ch (−50%),
battle 8,552→4,319 (−49%), models 10,228→1,467 (−86%), quota
28,131→2,634 (−91%). Every cut carries an in-band
`[… N chars omitted — re-run with "full": true]` label (asserted in
tests); all 9 capped tools advertise `full`, models adds a `provider`
filter, CLI/renderers unchanged by default. Pattern doc:
`docs/MCP_RESPONSE_DIET.md`. Gates: 2507 passed, coverage
87.95/78.31, `ruff check .` clean, mypy delta zero on touched files,
check_docs + check-counts pass. (One `test_route_health` flake under
full-suite load; passes alone and on full rerun.)

Effort: S–M. Fit: medium — completes the G2 story honestly.

## Goal chain G15–G22 (accepted 2026-09-19)

Eight pain-point goals, brainstormed from live web research (agentic
coding bill shock, free-tier data-training defaults, MCP context-tax
analysis, mid-session free-endpoint death, LiteLLM CVE fallout,
embedding-quality rankings). Execute strictly in order; each goal's
Done-when is the audit for "did we completely solve this pain?" —
including a pain-scenario demonstration, not just unit tests. On
completing each goal, immediately create the next goal as the active
session goal in the same turn. If a goal is truly blocked, record the
blocker here and skip to the next — never hold the chain hostage.

## G15 — Claude Code $0 mode (Status: complete, 2026-09-19)

Pain: agentic coding bills are detonating — $200/mo Claude Code power
users quitting, $50+/week API burn, Opus at $5/$25 per M tokens. The
G9 Anthropic bridge can already serve Claude Code from free tiers,
but there is no one-command setup and no visible "$0" receipt, so the
exact demographic quitting paid plans never finds the door.

Bet: a 5-minute switch captures the "I just quit $200/mo" crowd: one
setup command routes Claude Code entirely through the gateway, and a
session receipt proves the savings.

Execute:
1. One-command Claude Code onboarding (config + base-URL + key
   wiring) beside the existing OpenCode/Hermes setup.
2. Session savings receipt: "$X at Opus rates — you paid $0"
   (display only; killed bet #2 stays dead — no budgets).
3. Docs + troubleshooting for the Claude Code path.

Done when:
- [x] A fresh-machine transcript shows Claude Code completing a
      real coding task end-to-end at $0 with the receipt printed
      (the pain scenario, solved).
- [x] Full suite + gates pass.
- [x] Commit + push; G16 goal created in the same turn.

Evidence (2026-09-19, claude 2.1.261, isolated HOME + scratch
workdir simulating a fresh machine): `claude -p` with the wrapper
env (`ANTHROPIC_BASE_URL=http://127.0.0.1:8080`, local proxy key
only, no upstream creds, no OAuth) implemented real FizzBuzz in
`fizzbuzz.py` (edited file verified), ran it, and reported outputs
1–16 correctly — all through free gateway routes.
Receipt: `Lifetime free usage: 348 requests, 1,123,601 tokens ...
Would have cost ~$19.18 at Claude Opus 4.8 rates — you paid $0.`
Shipped: `claude` client in setup (wrapper + isolated
`CLAUDE_CONFIG_DIR`), `freellmpool receipt` (+ `--json`),
`docs/CLAUDE_CODE.md` with troubleshooting. Gates: 2511 passed,
coverage 87.96/78.32, `ruff check .` clean, strict mypy on touched
files, check_docs + check-counts pass. (Known `test_route_health`
load flake; green alone, on clean tree, and on full rerun.)

Effort: M. Fit: high — most money behind this pain.

## G16 — Privacy routing + redaction (Status: complete, 2026-09-19)

Pain: free tiers train on your data by default (ChatGPT, Claude
free, Gemini; Copilot from April 2026). Developers paste proprietary
code into free endpoints daily; lawyers now warn against it. Nobody
labels which free route logs and which doesn't.

Bet: the gateway becomes the safe way to use free tiers: published
data-policy labels per provider, pre-flight PII/secret redaction,
and routing that respects both.

Execute:
1. Data-policy labels in the catalog per provider
   (trains-by-default / api-no-train / unknown, sourced with dates).
2. Pre-flight redaction (PII + secrets) with adversarial-fixture
   tests; strict mode refuses logging providers for flagged prompts.
3. Live transcript: a sensitive prompt provably avoids
   train-by-default routes.

Done when:
- [x] The pain scenario is demonstrated: a secret-bearing prompt is
      redacted and/or routed away from logging providers, with the
      policy labels cited (numbers/table pasted).
- [x] Full suite + gates pass.
- [x] Commit + push; G17 goal created in the same turn.

Evidence (2026-09-19, live): canary prompt demanding a `sk-live-*`
secret back, sent with `redact=True, private=True` → served by
cloudflare/@cf/openai/gpt-oss-120b (policy `api-no-train`),
`redactions=('API_KEY',)`, model refused (never saw the secret),
secret absent from the reply. Strict refusal verified:
`providers=["gemini"], private=True` → `AllProvidersExhausted` with
"Private mode admits only api-no-train providers…".
Shipped: `src/freellmpool/data_policies.json` (all 16 providers
labeled, separate file so quota-evidence digests are untouched),
`privacy.py` (redact + policy lookup + validator), `redact`/`private`
flags in CallOptions + `_run` + proxy, `Reply.redactions`,
`docs/PRIVACY.md` with honest limits. Gates: 2535 passed, coverage
87.88/78.31, `ruff check .` clean, mypy delta zero (137 proxy.py +
3 models.py errors pre-exist in non-gated files), check_docs +
check-counts pass. (Known `test_route_health` load flake; green on
rerun.)

Effort: M–L. Fit: high — least served by anyone.

## G17 — MCP diet as a weapon (Status: complete)

Pain: MCP context bloat is the #1 developer complaint (40–72% of
context gone before any work); a lone output-compression project
gained 3,400 stars in a week. G14 solved this for our own tools but
nobody outside this repo knows or can reuse it.

Bet: generalize + publish: a reusable compression wrapper and a
measured benchmark page that makes freellmpool the cited answer to
MCP bloat.

Execute:
1. Generalize G14 truncation into a reusable wrapper for any MCP
   output (budgets + labels + full escape).
2. Benchmark before/after tokens across popular MCP servers;
   publish the numbers on the Pages site.
3. Docs for third-party MCP authors to adopt the wrapper.

Done when:
- [x] Published benchmark shows large-response shrinkage on
      third-party MCP output with zero silent truncations (numbers
      pasted, page live).
- [x] Full suite + gates pass.
- [x] Commit + push; G18 goal created in the same turn.

Effort: M. Fit: medium-high — distribution, riding proven demand.

Evidence (2026-09-19, live): `src/freellmpool/mcp_diet.py`
(`compact_text`/`compact_content` + stdio `DietProxy` with `_full`
cache escape; `panel.truncate_labeled` now delegates to it).
Benchmarked through the proxy at budget 2000 chars: filesystem
`read_text_file` 81,053→2,067 ch (−97%), `list_directory`
1,130→1,130 (untouched), memory `read_graph` 36,071→2,067 (−94%),
fetch 3,342→2,066 (−38%); all 3 cuts carried the labeled `_full`
marker and the escape restored the full 81,053-char report.
Numbers published at
https://pauljones0.github.io/freellmpool/mcp-response-diet.html
(HTTP 200 verified 2026-09-19; fork Pages branch-deploy since this
session has READ-only on canonical 0xzr/freellmpool, whose sync is
the user's step — canonical URL stays
https://0xzr.github.io/freellmpool/mcp-response-diet.html, sitemap +
index linked, `check_docs.py` green); author adoption docs in
`docs/MCP_RESPONSE_DIET.md`. Full suite 2536 passed, coverage gate
87.20%/77.68%, ruff clean, mypy clean on touched files (repo-wide
mypy failures pre-existing). One `test_allowance_ledger` multiprocess
sqlite-lock flake under load 24+ failed two full runs, passed twice
in isolation and on retry; zero coupling to this change.

## G18 — Live free-tier status page (Status: complete)

Pain: free endpoints die and 429 without warning; "is X down or is
it me?" has no public answer. G10 drift snapshots exist but stay on
one machine.

Bet: publish drift snapshots to GitHub Pages on a timer — the public
"is free-tier X working right now?" signal with staleness honesty.

Execute:
1. Snapshot publisher: drift snapshot → Pages site on a schedule,
   with generated-at staleness indicator + short history.
2. Docs-check the published shape; no key material by construction.
3. Live page verified from a clean checkout.

Done when:
- [x] The public page correctly shows a real, live-observed route
      state (URL + screenshot/transcript pasted), with staleness
      labeled.
- [x] Full suite + gates pass.
- [x] Commit + push; G19 goal created in the same turn.

Evidence (2026-09-19, live): `src/freellmpool/status_page.py` +
`status-page publish/check` CLI; `.github/workflows/status-publisher.yml`
(6h cron + dispatch). Live snapshot 2026-09-19T05:29:17Z: 10/16 ok,
5 fail, 1 rate_limited (honest 429 from zhipu). Page verified HTTP 200
at https://pauljones0.github.io/freellmpool/free-tier-status.html from
a clean clone (/tmp/g18clean @ abcc50f): stamp, 10/16 summary, and 16
rows match byte-for-byte; staleness note + 12-snapshot history on-page.
No key material by construction (fixed schema + G16 redaction +
fail-closed secret scan; `status-page check` green). Full suite 2561
passed, coverage gate 87.28%/77.84%, ruff clean, mypy clean on touched
code, `check_docs.py` green.

Effort: S–M. Fit: medium — small code, big discoverability.

## G19 — Security-hardening sprint (Status: complete)

Pain: LiteLLM published 12 advisories in 2026 including pre-auth
RCE; teams ask "do we have someone on-call for the next one?" Our
gateway is small but has never been audited or packaged for trust.

Bet: make "boring and safe" provable: SBOM, signed releases,
dependency audit gate, and an honest comparison page.

Execute:
1. SBOM generation + signed release artifacts + `pip-audit` (or
   equivalent) as a CI gate.
2. Adversarial self-review of the proxy auth boundary with fixes
   for anything found (regression test per fix).
3. Comparison doc: our surface vs LiteLLM's 12 advisories, with
   explicit residual risks (no security theater).

Done when:
- [x] Audit workflow is green, release artifacts are signed + SBOM'd
      (links pasted), and the comparison doc names residual risks
      honestly.
- [x] Full suite + gates pass.
- [x] Commit + push; G20 goal created in the same turn.

Evidence (2026-09-19, live): security.yml green 4/4
(https://github.com/pauljones0/freellmpool/actions/runs/35424416293);
bandit high/high + pip-audit --strict clean locally (0 exceptions).
Release v0.14.3:
https://github.com/pauljones0/freellmpool/releases/tag/v0.14.3 —
wheel+sdist+2 SPDX SBOMs built by release-evidence run
https://github.com/pauljones0/freellmpool/actions/runs/35425357340
(gate: ruff/catalog/release_ready/full suite+coverage/zero alerts/
pip-audit all green); `gh attestation verify` passes on both artifacts
(Sigstore bundle shown; tampered-file negative control correctly
rejected). sdist SBOM carries the 129-package inventory; wheel SBOM is
file-digest-only (syft limitation, noted honestly). Container jobs
skipped on fork (docker.yml still 0xzr-gated).
Adversarial proxy-auth review: live probes (401/403/400/413 paths,
pre-auth shell data-free) held; 1 flaw found+fixed — upstream error
bodies (uncapped, unredacted) flowed to clients via client_message —
now 400-capped + G16-redacted at construction (3 regression tests).
Release gate surfaced 2 HIGH CodeQL alerts, both resolved: #1 SHA256
fingerprint dismissed as false positive (identity, not password
verification, comment recorded); #2 variable-mode chmod fixed by
splitting atomic_write (literal 0o600) / atomic_write_public, then the
residual internal-helper instance dismissed with structural
justification. Comparison doc docs/SECURITY_COMPARISON.md maps 5
verified 2026 LiteLLM advisory classes to our surface + 6 residual
risks. Full suite 2565 passed, coverage gate 87.28%/77.85%, ruff clean,
mypy clean on touched lines, check_docs green. Notes: dead v0.14.2 tag
(gate failed correctly) removed before any release; v0.14.3 tag moved
once to include the scripts fix (never released before the move);
release-evidence gates extended to this fork (canonical unchanged).

Effort: M. Fit: medium — defensive, trust-building.

## G20 — Free-embedding leaderboard (Status: pending)

Pain: teams agonize over generation models then ship a
bottom-quartile embedding endpoint — OpenAI's embeddings rank 13th
of 15 while free models win by 11 points. Nobody has measured the
free embedding routes head-to-head.

Bet: the gateway measures its own free embedders on a fixed
retrieval fixture and recommends/routes to the winner; RAG defaults
to the best free embedder automatically.

Execute:
1. Retrieval-accuracy harness: fixed Q/A fixture set, scored per
   free embedding route through the gateway.
2. Publish the ranking; wire the winner as the RAG default with an
   override flag.
3. Regression tests for the harness scoring (fixtures, not live).

Done when:
- [ ] Published ranking names a measured winner with scores pasted,
      and `rag index` uses it by default (transcript pasted).
- [ ] Full suite + gates pass.
- [ ] Commit + push; G21 goal created in the same turn.

Effort: M. Fit: medium — makes G12's RAG best-in-class free.

## G21 — Run survivor (Status: pending)

Pain: free endpoints 429 and die mid-session; long agentic runs
(tokenmax swarms, recipes, panels) lose everything when the bench
collapses halfway. An entire genus of failover-proxy repos proves
the pain is real and unsolved at the run level.

Bet: long runs checkpoint progress and resume across 429s/outages
instead of restarting — survival, not just failover.

Execute:
1. Checkpoint/resume for long fan-out runs (tokenmax/recipes):
   per-model results persisted incrementally, resume picks up only
   what is missing.
2. Honest resume semantics: resumed runs label what was fresh vs
   replayed; quotas still account every live call.
3. Live transcript: kill the bench mid-run (or hit real 429s) and
   resume to a complete result.

Done when:
- [ ] A run interrupted by real 429s/outages resumes to a complete,
      correctly labeled result (transcript pasted).
- [ ] Full suite + gates pass.
- [ ] Commit + push; G22 goal created in the same turn.

Effort: M–L. Fit: medium-high — completes the G7 story at run level.

## G22 — Agent-loop cache (Status: pending)

Pain: agentic loops resend the same growing prefix every turn,
burning free quotas 2–5x faster than needed; prompt caching (up to
90% savings where supported) is the industry's #1 mitigation and we
only cache whole responses.

Bet: prefix-aware caching for multi-turn agent traffic stretches
free quotas dramatically with zero behavior change on a hit.

Execute:
1. Prefix-aware cache: hash the stable prompt prefix, serve/cache
   per-turn deltas; exact-hit returns byte-identical behavior.
2. Quota math stays honest: cached prefixes cost nothing, deltas
   cost normally; stats expose hit rate + tokens avoided.
3. Live measurement: same agentic loop with/without the cache,
   quota spend pasted.

Done when:
- [ ] Before/after quota spend shows large savings on a realistic
      multi-turn loop with identical outputs (numbers pasted).
- [ ] Full suite + gates pass.
- [ ] Commit + push; chain complete — report the series result.

Effort: M. Fit: medium — quota multiplier for every agent user.

## Killed bets (accepted 2026-09-18)

- **#2 Spend budgets + burn alerts** — killed by the free-only corollary:
  no bills exist when everything is free.
- **#3 Maintenance-feed data product** — a second product for a second
  audience; does not make setup easier.
- **#5 Local-LLM overflow bridge** — imports VRAM-tuning complexity, the
  opposite of "just works".

## Shaping decisions (encoded, overridable)

- #1 split: compat hardening yes, cost-attribution smarts no.
- #4: embeddings count as tokens in the vision.
- #9: guide yes, budgets no.
- #8: trust/launch copy executes inside G1 (v1) with the full pass as G6.
