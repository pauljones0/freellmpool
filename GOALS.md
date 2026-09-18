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

## G2 — MCP context diet (Status: active)

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
- [ ] Before/after token counts show a large reduction (target: 5–10× on
      the default surface) with measurements pasted.
- [ ] Claude Desktop/Cursor/Claude Code acceptance: connect, list, call
      one tool from each group successfully.
- [ ] Full test suite + MCP conformance checks pass.

Effort: M. Fit: high — MCP is a flagship surface.

## G3 — $0 setup guide (Status: pending)

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
- [ ] A fresh-persona run completes end to end in under 15 minutes
      (transcript pasted, timed).
- [ ] Every command in the guide is copy-paste verified; no step requires
      a paid key or paid account.
- [ ] Docs checks pass.

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
