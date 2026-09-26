# Relaunch post (easy-setup story, G1–G5)

Venue: GitHub Discussion on this fork (first post; external channels
stay human-gated per `reddit-targets.md` ground rules). Disclosure: I
maintain this fork. Single post, no cross-spray.

Published: <https://github.com/pauljones0/freellmpool/discussions/122>
(2026-09-18, Announcements).

## Title

Relaunch: freellmpool now goes from zero to a free agent reply in one command

## Body

Hi all — I maintain this fork of `freellmpool`, the MIT-licensed local
gateway that pools LLM provider free tiers behind one CLI/proxy/MCP
interface. The setup story has been rebuilt end to end (goals G1–G5 in
`GOALS.md`), so this is a relaunch of the "easy free setup" claim:

- **One-command install** (G1): fresh container → one-liner →
  `Freellmpool is ready.` Verified in CI, no published artifacts needed.
- **$0 setup guide** (G3): zero to first agent reply spending $0, every
  command copy-paste verified in a clean container in under 15 minutes.
  Docs: `docs/FREE_SETUP.md`.
- **MCP without the context flood** (G2): progressive disclosure keeps
  every tool reachable at a fraction of the `tools/list` token cost.
- **Free embeddings + $0 RAG** (G4): `/v1/embeddings` on reviewed free
  routes, with a runnable RAG quickstart (`docs/RAG_QUICKSTART.md`).
- **Claude Code compat** (G5): real multi-turn `claude` sessions with
  tool use complete through the Anthropic bridge; setup is 3 commands
  (`docs/INTEGRATIONS.md`).
- **Trust page** (G6): every claim links to its proof — pinned supply
  chain, Bandit/pip-audit/zizmor/CodeQL gates, the evidence process, and
  the ToS posture. Docs: `docs/TRUST.md`.

Current catalog: 13 providers, 128 enabled chat routes, 128 cataloged
chat models (enforced by `scripts/check-counts`, so the docs can't drift).

Honest caveats, up front:

- Prompts go to whichever upstream provider/model is selected. This is a
  local router, not a privacy layer.
- Free tiers belong to their providers: caps, bans, and ToS are theirs.
  The gateway honors real 429s and never evades limits.
- Free-tier models are slower and weaker than paid frontier models; this
  is for side tasks, triage, docs, and scripts where "good enough" wins.
- This fork publishes no PyPI/container releases; install from source.

Most useful feedback: provider rows that have drifted, free-tier
providers I missed, and whether the proxy/MCP setup fits real agent
workflows. Model-id and limit reports are especially valuable.

If it saves you a Claude/Codex call, a star helps other developers find it.
