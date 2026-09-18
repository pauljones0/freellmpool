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

## G4 — Easy free embeddings (Status: pending)

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
- [ ] Embedding requests succeed against reviewed free routes (keyless
      where available), verified live.
- [ ] The RAG quickstart runs end to end at $0 (transcript pasted).
- [ ] Full suite, coverage, and policy gates pass.

Effort: S–M. Fit: high — embeddings are tokens too.

## G5 — Claude Code compat hardening (Status: pending)

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
- [ ] A real multi-turn Claude Code session with tool use completes via
      the gateway (transcript pasted).
- [ ] Zero known compat breaks remain; each historical break has a
      regression test.
- [ ] Full suite + gates pass.

Effort: M–L. Fit: high — meets users where they already are.

## G6 — Trust page + relaunch (Status: pending)

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
- [ ] Every claim on the trust page links to live verification.
- [ ] Launch post published (link recorded here).
- [ ] No new code; docs checks pass.

Effort: S. Fit: medium — distribution of proof, not product.

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
