# Claude Code $0 mode

Run Claude Code entirely on free-tier routes through the gateway's
Anthropic shim — no Anthropic account, no API spend.

## Setup (one command)

```sh
freellmpool setup-clients
```

With the `claude` binary on `PATH`, this installs a `claude-free`
launcher beside `opencode-free`/`hermes-free`, then starts the
gateway service. From then on:

```sh
claude-free -p "Fix the failing test in ./pkg"
```

The launcher pins `ANTHROPIC_BASE_URL` at the local gateway,
injects the local proxy key as `ANTHROPIC_API_KEY`, and isolates
`CLAUDE_CONFIG_DIR` — no OAuth login, stored skills, or MCP servers
leak in, and no upstream credentials leak out. Any `claude-*` model
name routes to a free model automatically.

## The receipt

```sh
freellmpool receipt
```

```
Lifetime free usage: 348 requests, 1,123,601 tokens (0 cache hits)
Would have cost ~$19.18 at Claude Opus 4.8 rates — you paid $0.
```

Display only: lifetime free usage plus what it would have cost at
Claude Opus 4.8 list rates. See `freellmpool receipt --json` for the
machine-readable shape.

## Troubleshooting

- **`claude-free: command not found`** — `~/.local/bin` is not on
  `PATH`, or `claude` was not installed when setup ran. Re-run
  `freellmpool setup-clients` after installing Claude Code.
- **Login / OAuth prompts** — the isolated config dir means Claude
  Code never sees your personal login. That is intentional: API-key
  mode against the gateway is the whole trick. Do not log in inside
  a `claude-free` session or traffic may leave the free path.
- **Model errors naming `claude-*`** — the gateway maps any
  `claude-*` name to free routes. If a request fails, it is a bench
  problem, not a naming problem: run `freellmpool status` and
  `freellmpool verify --features tools` (Claude Code sends tools on
  every request — keep 3+ tool-verified routes fresh).
- **Thinking/reasoning models stall** — free reasoning routes need
  headroom; the gateway reserves it automatically. If output is
  empty, retry once; persistent emptiness is a route problem, check
  `freellmpool status`.
- **Suspected paid leakage** — by construction there is none: the
  launcher allowlists a fixed env, drops all upstream keys and
  Bedrock/Vertex switches, and the gateway admits only verified
  zero-price routes. Verify with `freellmpool status --json`.
