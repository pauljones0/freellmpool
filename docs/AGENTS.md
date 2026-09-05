# Coding agents and custom clients

Start with the [guided setup](../integrations/setup/README.md):

```sh
freellmpool setup
```

This checkout's maintained gateway requires reviewed free policy, current model discovery, account evidence where needed, and transactional allowance reservations before dispatch. The published 0.13.0 compatibility profiles only print configuration snippets. They do not perform the new setup; install the reviewed checkout rather than assuming `pip install freellmpool` includes this rewrite.

## OpenCode, T3, and Hermes

Use `opencode-free` or `hermes-free`. In T3, select the configured OpenCode adapter and `freellmpool/auto`. T3 does not merge Codex, Claude, OpenCode, or other harness subscriptions into one token pool.

The generated profiles cover main models, helper/title models, delegated agents and fallback paths. They isolate provider credentials and client state, and disable independent provider discovery and fallback plugins. Actual configuration verification and reversible backups are documented in the [setup guide](../integrations/setup/README.md). The old `freellmpool profile install` commands remain print-only compatibility helpers; use `freellmpool setup-clients` for the contained profiles.

## Another OpenAI-compatible application

Set the API base to `http://127.0.0.1:8080/v1`, the model to `auto`, and the API key to the private local gateway credential in `~/.config/freellmpool/proxy.key`. The normal service listens on loopback and requires authentication. Never substitute an upstream provider key for this gateway key.

Every inference path must use the gateway. Check auxiliary tasks, subagents, failover settings, automatic model discovery, and plugins as well as the main model setting. An application that independently switches to its own provider keys can still spend money outside freellmpool.

OpenAI Chat Completions and the Responses/Anthropic Messages bridges share the managed runtime. Embeddings and transcription are admitted only when the grant, modality, discovery, and cost accounting permit them. Text-only requests support incremental text streaming; rich tool requests stay on the buffered path. No request is replayed after streaming output begins.

Read [protocol conformance](PROTOCOL_CONFORMANCE.md) before relying on tools or structured output. Capability evidence is independent of a working short chat response and expires separately from the model catalog. Exact provider/model pins cannot bypass these checks.

## MCP and Python

An MCP host can run `freellmpool mcp` over stdio. Its model listings refresh from the current snapshot and its quota tool reports shared allowances instead of inferring a provider budget from per-model hints.

For Python, `Pool.from_default_config()` and the default async entry point use the managed runtime. Explicitly constructing the low-level `Pool(providers, ...)` remains a compatibility API; it does not establish reviewed free eligibility. Use the default managed path for applications that require the free policy.

## Operations

```sh
freellmpool status
freellmpool update
freellmpool verify --limit 4
```

`update` performs listing requests without inference. `verify` sends a bounded number of synthetic canaries through the same allowance ledger as normal traffic. Exhaustion is HTTP 429, with `Retry-After` when a truthful reset/backoff is known. Unknown limits are not unlimited capacity.

`/livez` is public. `/readyz`, `/v1/providers`, `/v1/models?ready=true`, and `/status` require gateway authentication. Readiness is a read-only local view of admitted routes, known limits and cooldowns; another application's account usage can remain unknown.

For metaswarm and older adapters, the [integration directory](../integrations/metaswarm/) and [archived 0.13 profile guide](legacy-agent-profiles.md) retain the older print-only commands and registry-readiness hardening notes. Review every external worker route before using those profiles with the maintained gateway. Prefer an explicit custom endpoint when containment cannot be verified.
