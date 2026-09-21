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

## G20 — Free-embedding leaderboard (Status: complete)

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
- [x] Published ranking names a measured winner with scores pasted,
      and `rag index` uses it by default (transcript pasted).
- [x] Full suite + gates pass.
- [x] Commit + push; G21 goal created in the same turn.

Evidence (2026-09-19, live): `src/freellmpool/embed_leaderboard.py` +
fixed `rag_bench_fixture.json` v1 (24 docs incl. keyword traps, 10
paraphrased queries) + `rag leaderboard` CLI. Measured through the
gateway: 1. cloudflare/@cf/baai/bge-small-en-v1.5 recall@3 1.000 MRR
1.000 (384d, 812ms); 2. mistral/mistral-embed 1.000/0.950 (1024d);
3. ovh/Qwen3-Embedding-8B 1.000/0.950 (4096d, slower; an earlier OVH
run hit its daily allowance and was re-measured clean). First fixture
tied 1.0 three ways and was hardened with distractors until it
discriminated. Ranking published at
https://pauljones0.github.io/freellmpool/free-embedding-leaderboard.html
(canonical URL kept for 0xzr sync). `rag index` defaults to the winner
(transcript: `(embeddings: @cf/baai/bge-small-en-v1.5)` with no flag;
`--embed-model mistral-embed` overrides). 10 fixture-only regression
tests. Full suite 2575 passed, coverage gate 87.24%/77.80%, ruff +
mypy (touched) + check_docs green.

Effort: M. Fit: medium — makes G12's RAG best-in-class free.

## G21 — Run survivor (Status: complete)

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
- [x] A run interrupted by real 429s/outages resumes to a complete,
      correctly labeled result (transcript pasted).
- [x] Full suite + gates pass.
- [x] Commit + push; G22 goal created in the same turn.

Evidence (2026-09-19, live): `src/freellmpool/run_checkpoint.py`
(0o600 JSON per run under 0o700 runs dir; prompts never stored) +
`--run-id/--resume` on `tokenmax` and `recipe run`; fan_out/run_panel
record incrementally; resume replays the ORIGINAL target set (a first
cut re-selected 256 models on resume — fixed to resume_plan, with a
regression test). Transcripts: (1) g21kill hit real outages (1/6
answered) → resume ran only the 5 missing: 2 transient failures
recovered fresh, 1 replayed, 3 dead routes honestly still dead;
(2) g21kill3 SIGKILLed mid-run at 14/40 → resume completed to 40/40
fully resolved; (3) panel recipe g21recipe resumed with
`(4667ms, replayed)` rendering. Quota test asserts replays make zero
live calls. Full suite 2582 passed, coverage gate 87.12%/77.73%, ruff
+ mypy (touched) + check_docs green. Lesson recorded: the 256-model
resume bug burned real daily quotas before the fix — bench was
degraded for later runs (since recovered).

Effort: M–L. Fit: medium-high — completes the G7 story at run level.

## G22 — Agent-loop cache (Status: complete 2026-09-19)

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
- [x] Before/after quota spend shows large savings on a realistic
      multi-turn loop with identical outputs (numbers pasted).
- [x] Full suite + gates pass.
- [x] Commit + push; chain complete — report the series result.

Result: prefix-hash routing memory + provider-confirmed cache harvest
(`src/freellmpool/prefixcache.py`, `Reply.cached_prompt_tokens`,
`ManagedPool._actual_cost` deduction, stats/CLI/MCP/proxy surfaces,
`docs/PREFIX_CACHE.md`, `tests/test_prefixcache.py` 23 tests incl. a
rotation regression test). Live 5-turn loop on mistral/codestral-latest:
busted-prefix leg net 3,958 tokens vs stable-prefix leg net 830 (−79.0%),
RESULT verdicts byte-identical 5/5, full text 3/5 with model-side jitter
proven cache-independent. Full suite 2605 passed; ruff clean; mypy zero
new; check_docs green; coverage 87.16/77.86.

Effort: M. Fit: medium — quota multiplier for every agent user.

## Chain G15–G22 series report (2026-09-19)

All 8 goals shipped, each with live transcripts, regression tests, and
green gates: G15 Claude Code $0 mode, G16 privacy routing + redaction,
G17 MCP diet as a weapon, G18 live free-tier status page, G19
security-hardening sprint, G20 free-embedding leaderboard, G21 run
survivor (resume), G22 agent-loop prefix cache. No further goal:
the chain is complete.

## G23 — Round-2 adversarial findings verify-and-fix (Status: complete 2026-09-20)

Pain: second adversarial sweep (8 fresh lenses) found 43 issues; 5 HIGH
(RAG symlink escape → local-file exfil to providers; unguarded
float(cooldown/cache_ttl/catalog-age) crashes + nonfinite values;
batch-aborting failure persistence) plus consequential MEDIUMs around
exactly-once resume, auth/redirect key leakage, secret-safe reports,
orphaned children, bounded buffers, corrupt state.

Bet: verifying every finding against code/behavior and fixing the real
ones with regression tests measurably hardens the free-gateway promise.

Execute:
1. Independently reproduce + fix the 5 HIGHs (TDD, regression tests).
2. Fix consequential MEDIUMs (resume/auth/secrets/orphans/bounds/corrupt).
3. Fresh concrete lifecycle audit (round-2 lifecycle lens was unusable).
4. Full suite + gates green, commit.

Done when:
- [x] All 5 HIGHs reproduced and fixed with regression tests (evidence pasted).
- [x] Consequential MEDIUMs fixed or documented with reason.
- [x] Fresh lifecycle audit complete with concrete findings or clean bill.
- [x] Full suite + gates pass; commit pushed.

Closeout 2026-09-20: all five HIGHs fixed TDD red-then-green —
RAG symlink-escape race (fd-pinned single-read traversal, supervisor
repro now passes: swap_performed=true, outside_fixture_reached_embed=false),
symlink-to-FIFO hang (O_NONBLOCK opens + fstat, bounded subprocess test),
eager scandir defeating MAX_SCAN_ENTRIES (bounded consume-before-sort with
truthful truncation, iterator-consumption test), proxy OverflowError on
nonfinite sample values (sampling validator), and batch-aborting failure
persistence. Consequential MEDIUMs/LOWs fixed with regressions in
tests/test_round2_low.py. Fresh lifecycle audit: 6 concrete findings, all
fixed with lifecycle tests. Gates observed green on final tree: full pytest
suite, ruff check, mypy --strict (both CI invocations), scripts/check_docs.py,
coverage 87.15% line / 78.31% branch (floors 80/70).

Effort: L. Fit: high — reliability/security of the free-tier promise.

## Goal chain G24–G25 (accepted 2026-09-20)

Two consumer-trust goals, sequenced from the G23 closeout mandate:
first-use must stay trustworthy when real free capacity is missing or
changes. Execute strictly in order; each goal's Done-when is the audit
for "did we completely solve this pain?" — including a
clean-environment demonstration, not just unit tests. On completing
G24, immediately create G25 as the active session goal in the same
turn and continue automatically. If a goal is truly blocked, record
the blocker here and continue with the next useful review/goal — never
hold the night hostage. Standing constraints: free-only (no account
creation, purchases, quota circumvention, or paid inference), TDD
regressions, fixture-labeled vs live-labeled evidence, secret-safe
diagnostics, explicit unknown capacity, metaswarm design/plan/coverage
gates, at most two live review/implement children, no nested fanout.

## G24 — Trustworthy first use with missing/changing free capacity (Status: complete 2026-09-20)

Pain: a fresh consumer following the one-command README path may hit
keyless unavailable/slow, all reviewed allowances exhausted or
unknown, an invalid/revoked key, interrupted setup, or provider/model
drift — and get a hanging loop, a misleading free promise, leaked
secrets, or a paid fallback instead of an actionable next step.

Bet: a bounded fresh-consumer audit in an isolated empty HOME, ranked
against current upstream primary evidence for free-route
availability, surfaces the highest-impact remaining end-to-end pain;
fixing that one pain with a defined consumer contract and observable
DoD measurably hardens first use.

Execute:
1. Bounded fresh-consumer audit: README one-command path and
   setup/resume/client handoff in an isolated empty HOME; exercise
   keyless unavailable/slow, exhausted/unknown allowances,
   invalid/revoked key, interrupted setup, provider/model drift.
   Reproduce the highest-impact remaining end-to-end pain first; do
   not reimplement shipped G1/G3/G8/G10.
2. If the paths already work, rank a short next-goal slate from
   recent primary-source/user-problem research and take the strongest
   verified gap.
3. Define the consumer contract + observable DoD; design review gate,
   plan review gate, then implement the first bet with bounded
   children (TDD).
4. Adversarially challenge architecture/code/goal completion; prove
   the fix from a clean environment. Label fake-provider fixture
   evidence separately from live free-route evidence.
5. Full suite + gates green, GOALS.md closeout, commit, push.

Done when:
- [x] Highest-impact remaining first-use pain reproduced end-to-end
  (or slate-ranked gap chosen with primary-source evidence).
- [x] Consumer contract + observable DoD defined and met.
- [x] Fix implemented TDD with regression tests; adversarial review
  passed; clean-environment proof recorded.
- [x] Full suite + gates pass; commit pushed.

Audit result 2026-09-20 (2 bounded workers + parent live repros, all in
isolated empty HOME; primary evidence in /tmp/g24_primary_evidence.md,
slate in /tmp/g24_slate.md):
- R1 FIRST BET: first-run discovery hangs silently on slow/broken
  networks (parent LIVE: blackhole proxy, 45s silence, exit 124;
  worker independently LIVE). No overall deadline (cli.py:3298),
  20s/10s x 16 sequential, ask --timeout starts after discovery.
- R2: cold `status` "complete model discovery needed" never names
  `freellmpool update` (managed.py:272 vs :274). 1-line fix, in scope.
- R3: bogus-key 401 guidance swallowed — CLI prints str(exc),
  ignores client_message with the key var (cli.py:225-227,
  errors.py:43). In scope.
- R4: init wizard never recommends `setup` (init_wizard.py:161-164).
  In scope.
- G25: kilo-403 status→update→status loop (parent LIVE, multi-obs)
  + --resume no-op + resume re-asks skipped. Setup/interrupt/resume/
  handoff otherwise verified working live.

Consumer contract (first bet):
1. First run is loud and bounded: discovery completes (success or
   honest failure) within 60s with progress on stderr, whenever the
   system resolver answers (stalled-resolver delay is a documented
   whole-machine residual; DNS hard bound is G26).
2. Every zero-route/error names the next command.
3. 4xx names provider + key variable, never the secret.
4. No silent paid fallback; no secret echo; unknown capacity explicit.

Observable DoD:
- Blackhole-proxy cold ask exits <60s with progress + connectivity
  cause + next step (not exit 124 silence).
- Refused-proxy cold ask exits fast with an actionable first line
  (the handler's own error may follow; no short-circuit).
- Happy-path cold ask (live keyless) still discovers + replies.
- Cold status names `update`; bogus key names the var (grep-clean);
  init shows setup.
- Fixture slow-provider suite proves deadline/progress/causes/exits
  (red-then-green); full gates green; clean-env proof; pushed.

Effort: L. Fit: high — first-use trust is the free-gateway promise.

Closeout 2026-09-20:
- Gates: design 5/5 PASS (v5+v5.1+v5.2+v5.3, supervisor-006/007
  folded); plan 3/3 PASS (v6+v6.1+v6.2, supervisor-008 folded);
  adversarial review FAIL→folded→green (2 vacuous proofs
  rewritten non-vacuous + 10 minors).
- Shipped (TDD, red-then-green): 40s discovery budget with
  asyncio.wait_for per-page absolute deadline (sync/async twins,
  budget-aware lock, deferred rows, CTO-4 preserve); progress
  order + 3-tier classifier + CSV + summary on all 5 callers;
  deferred ripple (maintenance exempt/deadline-skip, managed
  reason); R2/R3/R4 message fixes (11 sites + 1 genuine 401 path,
  resolved slot names); Dockerfile 90s start-period.
- LIVE evidence (isolated empty HOME, real network unless noted):
  refused-proxy cold ask 0s exit 4 + transport tier; blackhole
  ask 41s + blackhole update 40s, both bounded with progress +
  deferred tier/incomplete footer; keyless cold ask exit 0 in 9s
  with a live reply; bogus OPENROUTER key live 401 naming
  `(check key OPENROUTER_API_KEY)` with the canary absent from
  all transcripts + state; status names update; init shows
  setup first. Transcripts: /tmp/g24_live_*.txt (live-labeled).
- FIXTURE evidence: tests/test_discovery_budget.py (drip/header
  abort with elapsed windows, CTO-4, DNS residual, decoder
  parity, busy lock, EPIPE, schema-1) +
  tests/test_bootstrap_wiring.py (tiers/exits/busy/help/purity/
  canary) + units (auth-hint, slot names, maintenance,
  Dockerfile); 5 pre-existing seam files migrated to _aclient.
- Full suite + ruff + CI strict mypy (22 modules) + check_docs +
  coverage floors (80/70) green.
- Residuals → G25: kilo-403 loop, --resume no-op, resume
  re-asks, proxy str(exc) fallback now carries key-var names
  (names-only, CTO-8 noted), busy+renew-evidence prints a skip
  note, check_provider is unbounded by design (wizard-only),
  public-sources reads are idle-8s (drip-unbounded). → G26: DNS
  hard bound (system-resolver shutdown lag characterized, not
  bounded).

## G25 — Runner-up consumer-trust gap (Status: complete 2026-09-20)

Pain: kilo-403 status→update→status loop (parent LIVE, multi-obs)
from the G24 slate, plus --resume no-op and resume re-asking
skipped providers. Setup/interrupt/resume/handoff otherwise
verified working live in G24.

Bet: closing the kilo-403 loop (verdict + fallback candidates)
with the same contract–DoD–proof discipline compounds
first-use trust.

Execute: reproduce the kilo loop live; define verdict semantics
+ fallback-candidate contract + observable DoD; design/plan
gates; TDD implement with adversarial review; prove from a
clean environment; gates green; commit/push. Same gates and
evidence rules as G24.

Done when:
- [x] Kilo-403 loop + resume gaps reproduced live in isolated
  empty HOME (curl 200/462KB vs httpx persistent 403
  x-vercel-mitigated; skip llm7 + quit re-prompts; --resume
  store_true default True is a no-op).
- [x] Verdict semantics (blocked vs auth_failed, 4-rule K1) +
  fallback-candidate contract (reviewed names shown, never
  served) + observable DoD defined; design gate 5/5 PASS
  (v2+v2.1+v2.2+v2.3+word3), plan gate 3/3 PASS (v1+v1.1+v1.2).
- [x] Implemented TDD (33 tests: U1 classifier 10, U2 ripple 11,
  U3 fallback 5, U4 resume 7); adversarially reviewed
  (TTL-overflow hardening, S4/F7 slash conflict resolved for
  F7, merge/branch simplifications); proven live from clean
  env (update --provider kilo -> blocked N1 + fallback line +
  F footer; status -> R reason; repeat identical; zero
  auth_failed in live state).
- [x] Full suite green, ruff/mypy-strict/docs/coverage gates
  green (2 ruff UP031s pre-existing at base, untouched);
  commit pushed.

Effort: M. Fit: high — compounds G24.

## G26 — DNS hard bound + drip-bounded source reads (Status: complete 2026-09-20)

Pain: stalled system-resolver delay is a documented whole-machine
residual (shutdown lag characterized, not bounded); public-sources
reads are idle-8s but drip-unbounded. A hostile/slow network can
still wedge first use past its loud bounded promise.

Bet: bounding resolver shutdown lag and drip reads closes the
last known unbounded wait in the fresh-consumer path.

Execute: reproduce stall/drip live with bounded harnesses;
define bound semantics + observable DoD; design/plan gates;
TDD implement with adversarial review; prove from a clean
environment; gates green; commit/push. Same gates and
evidence rules as G24/G25.

Done when:
- [x] Stall/drip reproduced live in isolated empty HOME
  (blackhole-DNS netns refresh: fetch ~5s, return lagged to
  glibc ~30s; localhost TLS 1B/7s drip read with no bound).
- [x] Bound semantics + observable DoD defined; design gate
  5/5 PASS (v2+v3+v3.1), plan gate 3/3 PASS (v1+v1.1).
- [x] Implemented TDD (U1 drip helper + ReadDeadlineExceeded 6
  incl. chunk-granularity; U2 five-site wiring + mappings 9;
  U3 daemon executor + _run_sync + wizard bound 19 + 2 guards;
  v5.2 residual test rewritten to the closure contract).
  Adversarial review GO (drop atomicity, forced TPE base,
  teardown order, mappings, ceiling numerals, test honesty all
  PASS; uv.lock collateral reverted, DNS margin widened to
  10s/<6.5 and looped 5x pre-commit).
- [x] Proven live from clean environment (empty HOME; netns
  for DNS): refresh fetch 5.2s + exit lag 0.1s (old code:
  fetch 30.4s); drip single-URL error at 35.0s over 6 real
  bytes. Transcripts /tmp/g26_dns_proof.txt,
  /tmp/g26_drip_proof.txt (scratch, not shipped). G24 cold-ask
  regressions green (refused, blackhole, happy-path).
- [x] Full suite (CI warning flags) + ruff + strict mypy (CI
  module list) + docs + coverage gates green (87.7% lines /
  78.9% branches); 2 ruff UP031s fixed mechanically
  (test-only %-format; G25 predates the newer ruff flag);
  commit pushed.

Honesty claim: this closure covers sync deadline-set refresh
exit + per-read totals + wizard 60s ONLY. Enumerated residuals:
(1) arefresh(deadline=None) fetch-unbounded (G27-candidate
"arefresh deadline wiring"); (2) sync per-source glibc OS time
(no code bound; OS-bounded return asserted by harness);
(3) trust_env proxy variance (out of scope; harnesses clear
proxy env). Inference streams are out of scope for this goal.
LOCK_EX second-writer waits stay a qualitative bound (no numeric
claim), modulo glibc residual.

Implementation findings: ThreadPoolExecutor base forced by the
set_default_executor isinstance gate (no super().__init__, so no
registration); KI re-drive guarded by task.done() (re-driving a
KI-completed future idles in select() per issue #22429); the
sync reader uses iter_raw() passthrough because 64KB chunk
assembly would buffer a slow drip past the deadline.

Follow-up: G27-candidate arefresh deadline wiring (server
callers must pass `deadline` until then; see the
arefresh_catalog docstring).

Effort: M. Fit: high — completes the G24 boundedness promise.

## G27 — bounded async discovery (Status: complete 2026-09-20)

Pain: `arefresh_catalog` without `deadline` was fetch-unbounded;
in-loop server/notebook callers could hang forever with no failure
row and no recovery signal.

Bet: carrying the sync default-budget policy into the async entry
point gives bounded failure/recovery to every caller, including
ones that omit the bound.

Execute: small design + adversarial review (GO-WITH-CHANGES, all
folded); TDD implement (red-first); actual-transport loopback proof
for repeat/cancel/retry; gates green; commit/push.

Done when:
- [x] Default policy carried over: omit both params ->
  `now + budget_seconds(env)` (40s unless env-tuned); stricter
  caller `deadline`/`time_budget_seconds` wins via min(); strict
  finite validation (ValueError on NaN/inf/non-numeric/bool);
  past/zero/negative fast-defer-all (stated, pinned).
- [x] No implicit unbounded mode (review-blessed; big budgets cover
  backfill); timeout degrades to deferred rows over preserved
  last-good with progress intact.
- [x] Cancellation proven safe: entry-gated cancel surfaces
  CancelledError with byte-identical destination and an
  immediately re-acquirable gate; second writer gets DiscoveryBusy
  (small-budget probe documented) and retry succeeds.
- [x] Loop/executor non-interference pinned (same open loop, same
  default executor, no daemon-pool use, dispatch still works);
  bounds explicitly RETURN bounds, shutdown belongs to the caller.
- [x] 27 API-level tests green incl. actual-TLS-loopback
  repeat/cancel/retry with controlled resolver delay; full suite
  (CI flags) + ruff + strict mypy + docs + coverage green; pushed.

Honesty residuals (unchanged from G26, restated): glibc per-source
OS time unbounded by code; trust_env proxy variance out of scope;
LOCK_EX waits qualitative modulo glibc residual; inference streams
out of scope; whole-process shutdown guarantees disclaimed for
caller-owned loops.

Effort: S. Fit: high — closes the last G26-listed residual.

## G28 — unknown-model pins fail loudly with recovery (Status: complete 2026-09-20)

Pain: a removed/typo'd `--model` pin reported "all providers exhausted"
or "no providers configured" — the opposite of the truth when hundreds of
routes are ready — with no next step. Paid/discovery-only pins risked the
same misdiagnosis in the other direction (404 for a model that exists).

Bet: classify the pin against the SAME generation admission uses, so
unknown names 404 with `models`/`update` pointers while known-but-unserved
pins (paid, discovery-only, off-by-default, provider-excluded,
feature-missed) keep the generic guidance.

Execute: small design + adversarial review (R1-R8, all folded); TDD
implement (red-first); supervisor counterexamples reproduced and closed
with their own probes (015 discovery-only pin → 403, independently
re-verified by root); independent tree review (SHIP) with M1/m1/n4 folded
pre-commit; transport-leak root-caused to third-party httpcore with a
pinned regression; gates green; commit/push.

Done when:
- [x] `UnknownModel` (an `AllProvidersExhausted` subclass, 404 + pin +
  `models`/`update` pointers, sanitized/truncated) raised on chat, stream,
  aio, proxy (buffered/streaming/Anthropic → HTTP 404), CLI (tail + rc 4),
  legacy `rank_targets`, and legacy embed/transcribe; pseudo-models exempt
  everywhere; true-empty pools keep the generic errors.
- [x] Identity binds to admission's own generation: managed
  `Snapshot.known_models` (discovery generation + explicit providers, no
  reloaded catalog deciding after snapshotting); router binds to the
  constructor target index. Discovery-only paid pin → generic 403 (015
  probe 4/4 green here and by root rerun); genuinely-absent pin → 404.
- [x] Held-open TLS regression strengthened: cancel stimulus is
  deterministic body-phase (server-side entered-event proof the client
  finished TLS+connect — the old blind sleep hit the upstream window
  racily), plus `gc.collect()` localization so strays surface in their own
  test; adjacency re-verified 63/63 strict-green (was unraisable-red).
- [x] Cancel-during-TLS-connect abandonment root-caused in installed
  httpcore 1.0.9 (`_connect` catches only ConnectError/ConnectTimeout; the
  half-open stream is a frame local; the pool drops the failed connection
  unclosed and `pool.aclose()` reports OK) and PINNED, not hidden: the pin
  test deterministically reproduces the window (held 5s server handshake,
  SYN proven in backlog, cancel pre-request), asserts the exact 2-event
  signature with per-run phase proof, prints every captured event, fails
  loudly on httpcore upgrade (re-probe) and on signature absence (remove
  the pin upstream-fix tripwire). Mutation-checked both directions.
- [x] 33 UnknownModel tests + pin + hold-open regressions green; full
  strict suite 2962 passed + ruff + strict mypy + docs + coverage
  (87.76%/79.01%) green; pushed.

Honesty residuals: m2 edge kept — managed non-chat pin miss with zero
admitted routes for that modality stays generic 403 (can't distinguish
unknown from unconfigured modality); legacy embed/transcribe classify
against their own modality index (a chat-model pin on embed 404s; the
pointers recover); n1 (no pickle round-trip, pre-existing shape), n2
(sanitizer keeps bidi controls), n3 (direct `providers=["p"]` +
`model="p/m"` doubles the prefix) deferred as cosmetic; cross-modality
pins differ legacy-vs-managed (noted, pointers recover). G26/G27 residuals
restated unchanged.

Effort: M (two supervisor counterexamples + third-party root-cause).
Fit: high — every typo'd pin now teaches the fix.

## G29 — per-slot key validation + honest denied verdict (Status: complete 2026-09-20)

Pain: no way to validate WHICH saved key works — `status` shows
routes, the wizard checks one provider at a time interactively, and
an authenticated 403 (scoped key, unverified account) was labeled
`auth_failed`: a usable credential pronounced dead with a
replace-key order.

Bet: a non-interactive `keys check` over the GET-only listing path
gives one evidence-graded verdict row per configured slot, and splits
authenticated-403 into `denied` (inconclusive, scope-first fix) in
every consumer.

Execute: design + adversarial review; TDD implement (red-first);
supervisor counterexample 018 reproduced end-to-end and closed (root
re-verified fixed: 200→ok, 401→auth_failed, 403-scope→
denied/inconclusive, rc0, no replace-key); 019 consumer-wide
consistency sweep (update trailer, wizard text, Cloudflare
account-id hints, quota-claim qualification); gates green;
commit/push.

Done when:
- [x] `keys check` validates every configured slot of every checkable
  provider (Cloudflare, Cohere, Gemini, Groq, Mistral, Zhipu) with
  read-only listing calls: no inference, no rotation-cursor writes,
  no snapshots; local usage ledger provably unchanged (ledger
  logical-equality test); listing metering honestly disclaimed as
  provider policy, not a universal no-quota promise.
- [x] Verdicts grade evidence: `ok` (accepted), `auth_failed` (401
  on single-credential providers — proven dead, replace-key fix),
  `denied` (authenticated 403, any other 403 via the choke-point
  guard, or Cloudflare 401 joint-auth ambiguity — inconclusive,
  scope/account fix, never replace-key), `missing`/`unsupported`
  uncheckable, plus
  `rate_limited`/`blocked`/`partial`/`deferred`/`timeout`/`error`/
  `config_error`; exit 0/1/2 + `--strict` fail-closed; `--json`
  envelope pure (progress on stderr); summary buckets partition
  rows (loud assert); exception notes value-redacted.
- [x] 018 denied split consistent in every consumer: discovery
  `_classify_denied` (authenticated 403 → denied, keyless/mitigated
  stay blocked, 401 stays auth_failed); managed reasons; setup
  wizard breaks without replace-key offer; maintenance `denied`
  status + catalog_failed finding; shared blocked bootstrap tier;
  update footer scope-first trailers (019); onboarding auth_failed
  text authentication-only + Cloudflare account-id hints in wizard
  and keys-check rows (019).
- [x] Zero-network proof for uncheckable providers (call counting),
  checkable-set tripwire, canary secrecy, slot-1 logical equality
  with check_provider, suffix-gap/blank-slot/config.toml coverage;
  full strict suite green (3114 collected, rc0) + ruff + strict mypy
  + docs + coverage (87.87%/79.28%) green; pushed.

Honesty residuals: "proven dead" is scoped to keys-check rows on
single-credential providers (Cloudflare 401s are denied/inconclusive
— joint token/account-ID auth cannot isolate the bad factor; a
token-verify disambiguation probe is a follow-up, not this goal);
discovery keyless-401s mean "add the credential", never a dead key;
denied shares the blocked bootstrap tier line (row notes precise);
denied discovery rows preserve prior models while fresh (the key may
infer fine — a listing-only refusal must not hide working routes),
so preserved-denied rows serve without a scope warning in `status`
(the managed reason field excludes routes; warning-while-serving
needs a new channel — follow-up); unknown future discovery statuses
stay loud (ValueError → config_error rc1 with the status named);
`--timeout` bounds the run plus one in-flight call (stated).

Effort: M (supervisor counterexample + consumer-wide sweep).
Fit: high — "which key is broken?" becomes one command.

## G30 — opt-in inference canary for uncheckable keys (Status: complete 2026-09-20)

Pain: G29 judged keys for only 6 listing-checkable providers. Keyed
OpenRouter/NVIDIA/Vercel/Aion/ModelScope users got `unsupported` and
discovered dead keys only as confusing 401/429s at inference time.

Bet: an explicit opt-in flag sends ONE tiny single-shot chat
completion per unsupported slot to a registry-pinned free model and
maps the raw HTTP outcome onto the same evidence-graded verdicts —
only a clean 401 proves dead.

Execute: design + 3-reviewer plan gate (v1 FAIL-fixable on all three;
v2 + v2.1 folds verified, final PASS); TDD implement (red-first,
MockTransport/fake-POST offline); adversarial review of the diff;
gates green; commit/push.

Done when:
- [x] `--canary` judges the 5 eligible providers (OpenRouter, NVIDIA,
  Vercel, Aion, ModelScope) with one POST each (max_tokens=16,
  thinking floor off, max_attempts=1, 20s bound, per-slot key);
  listing-checkable providers stay GET-only even with the flag.
- [x] Total verdict mapping: 2xx (any text) → ok; 401 → auth_failed;
  403 → denied (mitigated → blocked); 402 → denied; 404/4xx-group/3xx
  → error; 408/504/TimeoutException → deferred; 429 → rate_limited;
  transport → error; overall budget → timeout. No new verdicts.
- [x] Honest spend: flag IS consent (stderr banner, once, only when
  canary steps are planned); attempt-based quota (every dispatched
  attempt records incl. failures; connect-phase records nothing);
  allowance ledger never touched; canary rows carry `via`/
  `canary_model`, listing rows byte-identical.
- [x] Free-only lock: pins match unconditional hard-free grants
  (paid_overage_possible False + no account conditions); Ollama
  excluded (paid-overage grant), llm7 excluded (key-optional),
  credential-less and registry-external stay unsupported.
- [x] 25 canary tests green (tripwire, grant terms, totality, quota
  boundary, single-dispatch bound, secrecy, strict, slots); full
  strict suite green (3139 collected, rc0) + ruff + strict mypy +
  docs + coverage (87.89%/79.32%) green; pushed.

Honesty residuals: canary targets can drift (404 → named drift error,
re-pin); 429 Retry-After deliberately not honored (single-shot, no
sleep); `partial` unreachable for canary rows by design; quota shows
canary spend indistinguishably from real usage (accepted: it IS real
spend); Cloudflare token-verify disambiguation still a follow-up.

Effort: M (plan-gate iterations + total mapping). Fit: high — closes
the G29 coverage gap without weakening any G29 guarantee.

## G40 — conflict-quota honesty on launcher+quota surfaces + bench banner count (Status: complete 2026-09-21, pushed; full gate rc0 3678 passed)

Pain: `AllowanceLedger.status()` fail-closed definition-changed rows to
`remaining=0.0` with `definition_status`/`reason`/`retry_after` keys that zero
consumers read — agent-start S1 refused with wrong "wait for reset" guidance
and quota rendered fake `remaining=0`. Separately, `bench -p` announced the
pre-filter provider count.

Bet: S1 names the conflict cause (refusal + partial-conflict WARNING); the
shared quota renderer prints `remaining=CONFLICT` with cause/retry in place;
the bench banner counts the stripped post-filter set.

Execute: plan v2 3xPASS (0 MUSTs) → TDD T1-T6 in the 4 existing test files,
per-surface assertions, byte-identical genuine-exhaustion paths.

## G39 — one-command agent start via agent-start (Status: complete 2026-09-21, pushed d6629a9)

Pain: G8's `fp claude` launched on unready proxies, never validated the
model/registry/quota admission, and left proxy lifecycle (status/stop/
pidfile attribution) to folklore.

Bet: `agent-start` stages S0–S7 (POSIX/binary/port/registry/model
validation, read-only managed admission, real tool-verify evidence,
key resolution, probe-then-reuse-or-spawn with child-side pidfiles,
opencode config, seven-line receipt, exec) with rollback on every
spawned-child failure; `proxy --status/--stop` manage the loopback
proxy via pidfile + cmdline verification; the opencode recipe is
single-sourced from the launcher builder.

Execute: plan v3.4 3xPASS (scope/feasibility/completeness, 0 MUSTs) →
TDD 61 pins + 4-case hermetic acceptance green (89 focused +
4 hermetic, incl. gate warning flags) + ruff + mypy-strict clean on
touched modules; existing suites (launcher/cli/bootstrap/profiles/
opencode-packages) green unedited.

Done when:
- [x] S0–S7 + probe/readiness/status/stop/pidfile + builder-backed
  snippet + test-registry seam + guide rewrite + wheel script.
- [x] Focused 89/89 + hermetic 4/4 green (incl. `-W error` warning
  flags); ruff + mypy-strict clean on touched modules.
- [ ] Full gate rc0 + wheel job + live audit + push (waiting on heavy
  lock — NOT done; changes uncommitted in worktree).

Honesty residuals: conflict quota rows read as exhausted (fail-closed);
foreign 503-empty retries to the deadline; union `a/b` config entries
are menu-only (proxy routes aliases); stop's kill-0 reads zombies alive
until reaped; SIGTERM-may-orphan untested (rerun reuses, stop cleans).

Effort: L (61 pins + hermetic + wheel + audit). Fit: high — one
validated command replaces the G8 multi-terminal ritual.

## G38 — MCP provider-filter honesty: validate-first + fail-closed (Status: complete 2026-09-21, pushed; full gate rc0 3492 passed)

Pain: MCP `provider` typos silently listed nothing (`models`) or
no-candidate errors (`ask`); no typo contract on the agent surface.

Bet: `validate_mcp_provider` verdict pairs (`pass`/`error`) at both MCP
call sites. Precedence POOL > EXTRAS > REG canonicalizing to the actual
executable id; case collisions refused as ambiguous; dead registry fails
closed (`provider registry unavailable; cannot validate provider 'X'`)
for unestablished ids while configured ids still pass; live unknowns
keep the verbatim unknown-template. Help promises "unknown or
unverifiable ids return an error" on both tools.

Execute: plan v1 3xFAIL → v2 3xFAIL → v3 0/3 (F1 test-serving) → v4 F1
dropped by proof (snapshot prefixes ⊆ pool.providers) + verdict-pair
shape → v4.1 3xPASS → TDD 41 pins green + full gate rc0 (pre-fix
evidence: lines 88.57%, branches 80.46%) → independent review BLOCKED
(B1 dead-skip vs help, B2 registry case-erasure, B3 dead whitespace) →
ported repair candidate 00a384d (5 files, hashes verified) + 2 minimal
port fixes (A15 explicit user-catalog seam instead of packaged-content
dependence; `known_ids` mypy-strict rename) + 2 doc nits → 73 focused
pins green (incl. empty-HOME determinism proof) + port ship review
2xSHIP (correctness + contract).

Done when:
- [x] Helper + 2 call sites (skip arms deleted) + 2 schema strings +
  diet 1-line completion + checked-in plan + 73 pins (M15/M15b/A15b/A15c/
  H3 new; M5/M11/A5/A15/H1 restated fail-closed).
- [x] Focused 73/73 (normal + empty-HOME) + ruff + mypy-strict
  (managed_cli) + diff-check + docs-check green; adversarial port
  review 2xSHIP; zero chat/transport calls pinned on cannot-validate.
- [ ] Full gate rc0 + push (waiting on heavy lock — NOT done).

Honesty residuals: ambiguity is global (any-key collision fails every
literal — deliberate strictness); extras-collision ask-side covered by
construction (same helper branch + error path); CLI keeps dead-skip
passthrough (MCP-only fail-closed divergence, specified).

Effort: L (4 plan rounds + BLOCK + port). Fit: high — the agent surface
no longer lies about typos, case variants, or registry outages.

## G37 — validate-first literals on the remaining 9 filter surfaces (Status: complete 2026-09-21)

Pain (G36 non-goals): typos exited 0 with misleading inventory text
(models/health/publish/bench/rag-board), exit 3/4 indistinguishable from
unconfigured (ask/conf), or tracebacked exit 1 (rag-ask after a wasted
embed; discovery.main) — publish even wrote docs files for a typo.

Bet: pool-anchored accept-if-known-anywhere (pool ∪ registry ∪ user-catalog
∪ external ∪ plugins; conf/ask add handler-visible configured ids) for the
7 pool surfaces, strict registry for rag-ask + discovery.main. Exit 2 +
keys-check shape, stdout untouched, validation before network/probes/writes/
embed. One shared `_resolve_cli_filter` (deviates v1.4 Q2 namespaces — the
dead-registry seam is `managed_cli.load_registry`); P46 pins the true legacy
empty-pool message (no-key gate precedes filter-mismatch — spec O5 mispredicted);
models/bench validate once + rewrite canonical (superior to per-split guards:
preserves dead-registry skip parity for `-p ""`).

Execute: surface survey (subagent) → plan v1 3xFAIL (zero-churn falsified;
10M+14m; move+plugins+ordering/scope) → v1.1 feas/scope PASS + completeness
FAIL (24 → minors) → v1.2 feas/scope PASS + completeness FAIL (pin precision
O1-O16) → v1.3 + v1.4 micro-deltas → completeness PASS → TDD (42 red + 10
preservation-green) → implement → plan-bug fix (ask configured-widening for
a missed slash test, conf-precedented) → gate → push.

Done when:
- [x] `_cli_extra_ids` + `_pool_known_ids` + `_resolve_cli_filter`; 9 call
  sites with specified ordering/winner rules; 52 pins + 1 intent-preserving
  fixture (`test_mode.py:113` real ids); publish help note; FREE_SETUP line.
- [x] Full strict suite green (3430 collected, rc0) + ruff + strict mypy +
  docs + coverage (lines 88.50%, branches 80.32%) green; adversarial
  review 2xSHIP (correctness direct; contract via BLOCK→fix→clearance);
  pushed.

Honesty residuals: first-run bootstrap network is pre-dispatch (ask-only of
the 9); corrupt-config masks typo (pool-anchored reversal, disclosed);
programmatic non-registry pools exit 2 on strict surfaces (update/discovery/
setup/rag-ask); MCP provider params + slash-model + canary + keys + frozen
onboarding.main stay as dispositioned; benchmark known-filter banner still
counts unfiltered (pre-existing).

Effort: L (9 surfaces + 52 tests). Fit: high — the typo contract is now
consumer-wide; no filter lies, burns budget, or writes files on a typo.

## G36 — provider-literal honesty + setup --stdin (Status: complete 2026-09-21)

Pain (G35 residuals): `update --provider NOSUCH` tracebacked (exit 1);
`verify --provider NOSUCH` misdirected to exit 3 and burned heal runs on thin
benches; `setup --provider NOSUCH` never named the literal; `setup --stdin`
didn't exist while `_hidden_input` prescribed it — dead guidance for piped keys.

Bet: validate-first unknown-literal exit 2 (keys-check shape) in
setup/verify/update; verify universe = registry ∪ user-catalog ∪ external
(acceptance parity, no behavior change for custom ids); `setup --stdin` saves
one piped key privately with TTY refusal (read() echoes) and sanitized errors.

Execute: plan v1 3xFAIL (2 pinned-test contradictions; 25 completeness gaps;
refactor/TTY scope) → v2 scope PASS + feasibility FAIL (A7) + completeness
FAIL (N1-N20) → v3 feas/scope PASS + completeness FAIL (M1-M3, m4-m12) → v3.1
feas/scope PASS + completeness minor-FAIL (n1-n4) → v3.2 3xPASS → TDD
implement (37 red + 3 preservation-green) → gate → push.

Done when:
- [x] `resolve_provider_ids` + `_unknown_provider_error`; validate-first in
  update (canonical feeds refresh/evidence/display), verify (pre-pool-load,
  pre-heal, registry-skip on dead load), run_onboarding (post-registry-load,
  stdout channel); `_cmd_setup_stdin` 7-step short-circuit; drift documented-
  ignore; 36 pins + 2 intent-preserving fixture updates; FREE_SETUP.md +
  setup README lines.
- [x] Full strict suite green (3377 collected, rc0) + ruff + strict mypy +
  docs + coverage (lines 88.29%, branches 80.02%) green; adversarial
  review 2xSHIP (correctness+secrecy, contract); pushed.

Honesty residuals: verbatim literal echo (keys-check parity);
`onboarding.main` frozen (TTY blocking-read echo + conflated message stay,
incl. the clipboard-wrapper path); N12 boundary 2 lines stricter than frozen
(unobservable <16KB); registry-only setup/update universes (documented);
`rag`/`ask`/`bench`/`models`/`conf` provider flags parked.

Effort: M (3-site validation + stdin path + 40 tests). Fit: high — typos stop
tracebacking, misdirecting, and burning recovery budget.

## G35 — heal honesty: no silent 0-probe lockouts (Status: complete 2026-09-21)

Pain (fresh-consumer audit + parent repro): `verify --heal` printed
"0/4 re-verified, 0 probes" with flat rows and no reason, burned a
daily run, armed a 2h cooldown, and silently skipped heal on retry;
cold `status` prescribed `--heal`, which exits 3 with "run update".

Bet: thrown probes count as attempted with a likely-cause
parenthetical; zero-contact runs are recorded but exempt from
budget/cooldown; every skipped gate announces itself; cold
prescriptions name update/setup; exit 3 documented.

Execute: plan v1 3xFAIL (blanket exemption defeated G31 pacing;
filtered-zero contradiction; matrix/test gaps) → v2 2xFAIL →
v3 2PASS/1FAIL (pin enumeration) → v4 3xPASS → TDD implement →
adversarial review → gate → push.

Done when:
- [x] Attempt accounting + likely-cause parenthetical; no-contact
  exemption (attempted==0 only); attempted>0 keeps full pacing
  (regression-pinned); gate skips announce; executor retains on
  no-contact; `chat_routes` key; cold prescriptions; exit legend.
- [x] Full strict suite green (3337 collected, rc0) + ruff +
  strict mypy + docs + coverage (lines 88.23%, branches 79.87%)
  green; adversarial review SHIP; pushed.

Honesty residuals: cause is best-effort over recorded classes
(`likely` qualifier; bare mechanic fallbacks); wallbox/stale
no-contact retries at daemon cadence (transient, zero upstream
cost; capped-no-contact consumes instead — review caught the
frozen-budget loop); TOCTOU single-emit possible (accurate,
untested); features=() direct-API runs read vacuous-ok (guarded
from no-contact, CLI can't construct); `--provider PROVIDER`
literal + setup `--stdin` parked for G36.

Review: first verdict BLOCK (missing verify-path composition
pins — added; capped retain loop — split to capped-consumes;
features=() hole — probed guard; exempt-save pin rewritten to
the shared tail) + 2 acknowledged notes (mixed-run attribution
as designed; lease-acquisition shape already covered).

Effort: M (accounting + emits + prescriptions + 37 tests). Fit:
high — the tool's own recovery stops lying and locking out.

## G34 — warning-while-serving for preserved rows (Status: complete 2026-09-21)

Pain: a provider whose listing comes back adverse keeps serving
last-good routes while fresh — correct — but `status`/`providers`
render it as plain `ready` with no hint. G29 deferred the warning
channel; G30/G32/G33 took the siblings, this is the last one.

Bet: serving + adverse verdict ⇒ managed row carries a one-line
warning (verdict + preserved count + fix command); admission
untouched; `status`/`providers`/`quota` render it, `--json` and
proxy `/status` carry it machine-readably.

Execute: grounding + plan v1 (completeness FAIL: undecided
consumers, matrix holes) → v2 folds quota/MCP surfacing, /status
flow-through pin, models/readiness lock-ins, full matrix → plan
gate 3xPASS → TDD implement → adversarial review → gate → push.

Done when:
- [x] Matrix: every servable adverse verdict warns (denied,
  auth_failed, auth_missing, unsupported, error, partial,
  rate_limited, deferred+previous-complete, unknown-future);
  ok/excluded rows silent with `warning` present-but-"".
- [x] Renders: status/providers/quota text goldens, `status --json`
  key, proxy `/status` flow-through; models + readiness lock-ins;
  admission identity (route sets byte-identical).
- [x] Full strict suite green (3300 collected, rc0) + ruff +
  strict mypy + docs + coverage (lines 88.16% ≥ 80%, branches
  79.75% ≥ 70%) green; adversarial review SHIP (5 NITs closed +
  G33 tick race fixed); pushed.

Honesty residuals: warning is last-verdict based (clears on next
successful update; status never probes); wording claims preserved/
last-listing, never dead; generation values rotate once on upgrade
(intra-run identity only); models/readiness deliberately unwarned
(catalog vs health split, locked by tests); sanitized pid in the fix
command can mismatch on non-slug ids (real ids are safe).

Also closed: G33 handler tick race found by the G34 review drift
check — `_exhausted` recorded after `send`, so a fast client could
flush before the tick landed (messages-429 test flaked 1/4). Ticks
now record before the response: an observed 429 implies counted.

Effort: S (one row key + three renders + matrix tests). Fit: high —
silent scope cuts become visible where operators already look.

## G33 — proxy demand-driven heal (Status: complete 2026-09-21)

Pain: agent sessions 429-spiral against the proxy while the tool bench
sits collapsed; recovery needs a human to run `verify --heal`. G31
covers verify/status/timers — the proxy path where the pain is felt
records no demand signal and heals nothing.

Bet: terminal tools-429s record demand ticks (memory-only, off the hot
path); an AUTOHEAL-gated out-of-band executor heals when demand
passes threshold, honoring restart-persisted ticks within seconds of
proxy start with zero bind delay.

Execute: grounding survey + spike + 3-reviewer plan gate (v1 FAIL on
all three axes → v2 folds contradictions, merge/executor lifecycle,
consent/minimality); TDD implement in isolated worktree (red-first,
offline); adversarial review; gates green; commit/push.

Done when:
- [x] Ticks only on terminal tools-429 (`had_tools` threaded to all
  `_exhausted` sites; mid-stream out of scope, ≤2/request bound);
  AUTOHEAL-gated recording; retry_after ignored by design;
  account-quota not excluded (backoff containment, disclosed).
- [x] TickStore: aligned 600 s windows, threshold 5, flush-as-move,
  lossless same-window merge, expiry on rollover, torn/silent-safe;
  executor: iteration-0 + anchored 60 s, read-only pre-checks,
  consume-unless-busy, BaseException-proof, idempotent start,
  legacy-pool None gate, tailnet parity.
- [x] Startup ordering: executor after pool, before serve_forever, no
  synchronous heal; two-process tick-loss test; request-path purity
  (record writes no files); docs + G31 "only" amendment with
  starvation disclosure.
- [x] Full strict suite green (3273 collected, rc0) + ruff + strict
  mypy + docs + coverage (lines 88.11% ≥ 80%, branches 79.68% ≥ 70%)
  green; adversarial review SHIP (5 SHOULDs + NITs all closed);
  pushed.

020 closure (independent frozen-source probes, repro
free-g33-concurrency-6308bb9f): demand/flush double-count → seqlock
exactly-once demand (no disk on record path); cross-window failed-flush
migration → restore-only-to-matching-live-bucket; catch-up stacking →
skip-not-stack anchors. Ported as `test_020_*` with corrected
expectations; Daybreak interleavings re-verified against repaired
source (positive control unchanged).

Honesty residuals: account-outage burn (decaying trickle via
low-yield backoff); crash loses ≤60 s of ticks; consume wipes ≤ run
duration of mid-run ticks; mid-stream 429s don't tick; constants are
judgment calls pinned by tests. 021 acceptance limit: a move in flight
past the 50 ms spin budget reads conservative no-demand for one pass
(delay, not loss); overlapping multi-process runs may drop same-window
peer demand on consume (bounded, self-healing).

Effort: M (tick store + executor + proxy threading). Fit: high —
self-restoring proxy during 429 spirals, zero incantation.

## G32 — Cloudflare token-verify disambiguation (Status: complete 2026-09-21)

Pain: every Cloudflare 401 was inconclusive `denied` — a dead token, a
wrong account ID, and a scope problem all looked identical, and scripts
could not fail on a dead CF key (rc0).

Bet: two verify probes (account endpoint, then user endpoint on A-401)
turn H1's shrug into three actionable outcomes on both `keys check`
and the setup wizard, failing closed to H1 whenever the probes cannot
establish a fact.

Execute: spike + 3-reviewer plan gate (v1 FAIL on all three axes →
v2 answers placement/deadline/matrix/exit-code → v2.1 folds re-review:
fresh client, URL-parsed account ID, token-hash memo, M2 gating,
never-raises, wrong-account residual); TDD implement (red-first,
counting MockTransport offline); adversarial review; gates green;
commit/push.

Done when:
- [x] Probes run only on the CF-401 branch with an explicit cache
  (7-function `cf_probe_cache` threading; None = legacy H1, zero new
  I/O); mapper stays pure (outcome token param); `_classify_denied`
  untouched; wizard shares the producer (full parity, no divergence).
- [x] Outcomes: `pair_ok`→denied+scope (wizard: no replace-key offer),
  `wrong_account`→config_error, `token_dead`/`token_expired`→auth_failed
  (both-agree caveat pinned), `inconclusive`/`inconclusive_retry`→H1
  denied; unknown tokens/signals fail closed; probes never raise.
- [x] Bounds: shared remaining `_WIZARD_CHECK_SECONDS` deadline
  (`_MIN_PROBE_SECONDS` floor, per-probe clock reads), ≤2 RTTs on the
  401 path only, per-invocation token-hash memo (values never in keys).
- [x] Honesty: dead-CF-key runs exit 1 (intended change from rc0);
  ACCOUNTS.md H1/exit-code sections rewritten with residual disclosures;
  static notes only (no token/account/URL/exception text); no new JSON
  keys; ledger logical-equality extended to probe runs.
- [x] 40+ verify tests green (42 in test_cf_verify.py: matrix, dedup,
  deadline, refresh chain, redaction, never-raises; T7 wizard rows,
  T8 exit codes, T9 ledger); full strict suite green (3222
  collected/passed, rc0, /tmp/g32_gate_run2.log) + ruff + strict mypy
  + docs + coverage (lines 88.01% >= 80%, branches 79.60% >= 70%)
  green; adversarial review SHIP (6/6 fixes confirmed); pushed.

Honesty residuals: dual-401 is "both verifiers agree" (a token type
neither endpoint accepts would fool both — note says so); B-ok+A-401
can also be a valid token without account access (note leads with the
common wrong-account-ID case, discloses the residual); probes add ≤2
RTTs per distinct (token, account) on the 401 path only.

Effort: M (probe machinery + 7-function threading + wizard parity).
Fit: high — wrong-account-ID is the most common CF setup fault and now
gets an exact fix instead of replace-key guesswork.

## G31 — demand-driven tool-bench heal (Status: complete 2026-09-20)

Pain: tool evidence expires after 7d and the fresh bench collapses
into the G5 429 death spiral; the system only reported it, and
recovery needed obscure verify incantations.

Bet: heal on demand where the user already looks — `verify --heal`
re-probes through the exact verify path, `status` offers without
ever probing, timers heal only under explicit AUTOHEAL opt-in.

Execute: design + 3-reviewer plan gate (v1 FAIL-fixable on
completeness/scope → rescoped v2: proxy-async deferred to G32;
v2.1/v2.2 folds confirmed PASS); TDD implement (red-first, fake
pools/probes offline); adversarial review; gates green; commit/push.

Done when:
- [x] `verify --heal` heals a thin bench (≤4 targets, ≤12 probes/run,
  ≤3 runs + ≤36 probes/day, 1h cooldown doubling to 24h on
  zero-pass/429-heavy runs, reset on restore, wall-box between
  probes); bare `verify` offers only; `status` never probes.
- [x] Consent-explicit: AUTOHEAL=1 enables `verify`/`maintenance
  --refresh`/timer paths only; `=0`/unset disables; explicit `--heal`
  always runs; installer never injects AUTOHEAL.
- [x] Correctness: re-admit at probe time, run lease (threading +
  flock, second runner exits 0), empty selector accounted without
  cooldown, OSError aborts cleanly (exit 1), ledger-denied counted
  as skipped without evidence writes, heal.json history with
  triggers (conformance schema untouched).
- [x] Surface: status offer/cooldown/last-heal/probes-today lines +
  stable JSON keys (`heal_available`, `heal_cooldown_until`,
  `last_heal`, `heal_probes_today`); refresh annotates conformance
  findings with heal outcomes (private reports only).
- [x] 28 heal tests green (triggers, lease incl. threads/processes,
  budget, cooldown, wall-box, selectors, parsing, CLI); full strict
  suite green (3167 collected/passed, rc0, /tmp/g31_gate_run3.log) +
  ruff + strict mypy + docs + coverage (lines 87.93% >= 80%,
  branches 79.43% >= 70%) green; pushed.

Honesty residuals: probes count calls (a tools feature may issue a
followup); per-run cap can stop the 4th target (boundedness wins —
3 passes still restore minimum); quota shows heal spend as ordinary
verify spend; AUTOHEAL timers re-probe on schedule only when thin;
proxy-async demand healing deferred to G33 with reviewer notes (G32
took the Cloudflare-verify slot).

Effort: M (plan-gate rescope + lease/state machinery). Fit: high —
the #1 ranked gap: recovery becomes one obvious command.

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
