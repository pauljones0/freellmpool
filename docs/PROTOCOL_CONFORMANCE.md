# Protocol conformance canaries

A successful short completion proves basic availability, not support for
streaming, tools, structured JSON, images, the OpenAI Responses bridge, or the
Anthropic Messages bridge. freellmpool records those capabilities separately
for each exact provider/model target.

## Run and inspect canaries

Run the deterministic matrix against at most one admitted free model from each
of eight providers:

```bash
freellmpool conformance run
freellmpool conformance status
freellmpool conformance status --json
```

Narrow the quota spend when needed:

```bash
freellmpool conformance run \
  --providers groq,cerebras \
  --model some-exact-model \
  --features chat,streaming,tools,json,json_schema,vision,responses,anthropic_messages \
  --max-targets 2 \
  --timeout 10 \
  --json
```

Each selected feature makes one request per target, except tools, which verifies
both the initial function call and the subsequent tool-result response. Each
request reserves up to 512 output tokens and clamps its timeout to 60 seconds.
The tools canary uses default automatic selection with `tool_choice` omitted,
then validates the exact function, arguments and tool-result response. It does
not claim support for optional forced named-tool selection. This distinction
matters for compatibility APIs: on September 5, Cohere Command A+ completed both
legs with HTTP 200 while the named-choice object returned HTTP 400. Its
[official compatibility guide](https://docs.cohere.com/v2/docs/compatibility-api)
also demonstrates the tool-result flow without `tool_choice`.
All canaries pass the same free eligibility gate and transactional allowance
ledger as ordinary requests. A truncated or empty answer is inconclusive.
The prompts,
tool schema, and one-pixel vision image are fixed synthetic constants. Canaries
never send repository files, user prompts, or previous conversations.

The managed runtime lists and probes only providers with reviewed free policy
and fresh model discovery. Registering a plugin does not establish free access.
`freellmpool verify --limit 4` rotates across oldest or unverified targets for a
smaller bounded check. Public catalog maintenance performs no inference.

The state defaults to
`~/.config/freellmpool/conformance.json`. Set
`FREELLMPOOL_CONFORMANCE_FILE` to use another path. The file contains only a
target fingerprint, bounded status/classification values, timestamps, and
verification counts. It never stores credentials, provider response text,
exception messages, prompts, repository content, or user content.

## Routing contract

Feature-specific, unpinned traffic is eligible only for targets with a current
`pass` for every required feature. If no target qualifies, routing fails
locally without spending provider quota. Explicit provider/model pins must pass
the same eligibility and feature checks. Disabled routes cannot be probed by
using `--include-disabled` on the managed runtime.

Evidence is bound to the provider id, adapter, base URL, and model id. Changing
an adapter, endpoint, or model id invalidates the old evidence automatically;
run the canaries again. Evidence expires after seven days; future timestamps are
rejected. Old tools evidence from the former single-call probe remains in audit
history but cannot admit tool requests until the full round trip is verified.
An unavailable probe (quota, timeout, output truncation or temporary availability)
preserves a still-fresh strong pass without changing its original verification
date. Separate `last_attempt_status`, `last_attempt_classification` and
`last_attempt_at` fields explain the latest result and drive bounded probe
rotation. A definitive failure or unsupported result replaces the pass; expired
or older single-call tools proof cannot be revived by an inconclusive attempt.
Unsupported feature results remain separate from
provider availability health and do not open availability circuits.

`freellmpool models --json`, the proxy `/v1/models` responses, and `/status`
include `capabilities` and `verified_features`.

## Streaming delivery contract

A passing `streaming` canary is target-specific evidence that an upstream can
produce a stream. At the proxy boundary, text-only OpenAI Responses and
Anthropic Messages requests are forwarded incrementally rather than collected
into a complete answer first. Tool calls and richer content still use the
buffered compatibility path and are framed only after the complete upstream
reply is available.

Streaming failover is allowed only before freellmpool commits the downstream
event stream. The proxy selects an upstream and obtains its first usable text
delta before sending downstream stream headers or events; a failure in that
pre-commit phase may try another eligible target. After commit, it never
replays the request on another provider. If the committed stream fails, the
proxy emits the protocol's error framing when the connection is still writable
and closes without a successful terminal event.

## Protected automation

The maintained provider-evidence workflow checks public catalogs and official
sources without credentials or inference. Authenticated discovery and bounded
verification belong to the local operator's managed runtime.

The older protected workflow's compatibility input is
`FREELLMPOOL_CONFORMANCE_KEYS_JSON`. This input is capped at 64 KiB; only
catalog-declared `key_env` and `extra_env` names are imported, values are
bounded strings, unknown names are ignored, and values are never printed or
persisted in evidence. This variable is intended for protected automation;
normal local use should continue to use ordinary provider environment
variables or the freellmpool key configuration.
