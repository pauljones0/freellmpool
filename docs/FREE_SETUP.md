# Free setup, $0: from zero to a first agent reply

Spend nothing. No paid keys, no paid accounts, no credit card — every step
below uses free-tier routes only, and `status` proves it after each stage.

Time: about 10 minutes, mostly one-time downloads. Prerequisite:
[uv](https://docs.astral.sh/uv/) installed.

```sh
uv --version
```

## 1. Install (one command, no checkout)

This downloads freellmpool from this repo's tarball, discovers free routes
automatically (one-time, no signup while a keyless provider is up), and asks
for a first reply:

```sh
TARBALL=https://github.com/pauljones0/freellmpool/archive/refs/heads/main.tar.gz
fp() { uvx --from "$TARBALL" freellmpool "$@"; }
fp ask --max-tokens 32 "Reply with one short sentence: freellmpool is ready."
```

`fp` is just a shortcut so the rest of this guide stays readable. The first
run takes a minute or two; later runs reuse the local catalog. (Same command
as the README's audited install path.)

## 2. Confirm strict-free status

```sh
fp status
```

Expect `Strict free access` with ready routes. If a reply ever fails with a
capacity error, wait a minute and re-run — free allowances refill — then
check `status` again. Unknown or exhausted capacity stays explicit; the
gateway never silently switches to a paid fallback.

## 3. Add one free key (OpenRouter)

OpenRouter's free models need only a free account — no payment method:
<https://openrouter.ai/keys>. Create a key, then:

```sh
export OPENROUTER_API_KEY="sk-or-..."
fp status
fp status --json | grep -o '"strict_free": [a-z]*'
```

`status` now lists `openrouter` routes as ready, and `strict_free` stays
`true`: the key only unlocked more free routes. Prove it with one keyed reply
(the `-p` pin keeps this request on OpenRouter):

```sh
fp ask -p openrouter --max-tokens 32 --timeout 60 "Reply with one short sentence: my free key works."
```

Prefer the interactive wizard (per-provider signup links, key checks,
account confirmations) or want to add Groq/Cerebras/etc. next? Run
`fp setup --provider groq` and see
[integrations/setup/README.md](../integrations/setup/README.md).

Scripting a key into the private store without the wizard? Pipe it —
`--stdin` requires `--provider`, never echoes the value, and every failure
exits 2 leaving the stored config untouched:

```sh
printf '%s' "$GROQ_API_KEY" | fp setup --provider groq --stdin
```

Unknown `--provider` values exit 2 naming the literal (`setup`, `update`,
and `verify` all validate before doing anything else — a typo never burns a
heal run or a catalog refresh). Provider matching is case-insensitive. Every
other provider filter (`ask`, `models`, `providers health`, `status-page
publish`, `benchmark`, `conformance run`, `rag ask`, `rag leaderboard`,
`python -m freellmpool.discovery`) validates identically: typos exit 2 before
any probe, refresh, embed, or file write.

## 4. Connect one coding agent (opencode)

Install opencode, then check it:

```sh
curl -fsSL https://opencode.ai/install | bash
export PATH="$HOME/.opencode/bin:$PATH"
opencode --version
```

See what freellmpool recommends for opencode (print-only recipe, changes
nothing):

```sh
fp code opencode
```

Save the project config it describes (loopback proxy, no key needed on
`127.0.0.1`):

```sh
cat > opencode.json <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "model": "freellmpool/agent",
  "provider": {
    "freellmpool": {
      "name": "freellmpool (free pool)",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://localhost:8080/v1"
      },
      "models": { "agent": {}, "spread": {}, "auto": {}, "fast": {}, "quality": {}, "fair": {} }
    }
  }
}
EOF
```

Agents need tool-capable routes. Record fresh capability evidence (bounded:
at most 20 probes, 45s each):

```sh
fp verify --limit 20 --timeout 45
```

Look for at least one line ending in `tools=pass`. If none pass, wait a few
minutes and re-run — evening out free capacity is normal.

Terminal 1 — keep the proxy running (loopback only, port 8080):

```sh
fp proxy --port 8080
```

Terminal 2 — your first agent reply:

```sh
opencode run -m freellmpool/agent "Reply with exactly: AGENT_OK"
```

Done: a coding agent just answered through free models, and you spent $0.
For the managed multi-client setup (persistent proxy key, editor profiles,
rollback): [integrations/setup/README.md](../integrations/setup/README.md).
For all agent profiles and routing aliases: [docs/INTEGRATIONS.md](INTEGRATIONS.md).

## Conservative defaults used here

Every knob this guide exposes is pinned low: `--max-tokens 32`,
`--timeout 60` (45 for verify probes), `verify --limit 20`, one `-p`
provider pin, loopback-only proxy, and the `agent` routing alias instead of
wide fan-out. Raise them once you know your free allowances.
