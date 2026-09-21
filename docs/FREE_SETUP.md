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

Before this step, finish steps 1–3 above and install opencode with the install block; the command below verifies tool routes, starts the proxy if needed, and replaces itself with opencode.

```sh
curl -fsSL https://opencode.ai/install | bash
export PATH="$HOME/.opencode/bin:$PATH"
opencode --version
```

Preview the config the launcher will write (print-only — the launcher writes it for you):

```sh
fp code opencode
```

Run one command — it verifies tool routes (bounded: at most 20 probes,
45s each), starts the loopback proxy if needed, and becomes opencode:

```sh
fp agent-start --port 8080 opencode -- run -m freellmpool/agent "Reply with exactly: AGENT_OK"
```

On success it prints a seven-line `freellmpool: ...` receipt (endpoint, harness, model, auth, tools_ready, proxy, config) and becomes opencode; the proxy keeps running after opencode exits.

Only `freellmpool/<alias>` model names route through the proxy; other `a/b` entries in the generated config are menu entries only.

Done: one command verified tool routes, started the proxy, and got a
coding agent answering through free models — you spent $0.
For the managed multi-client setup (persistent proxy key, editor profiles,
rollback): [integrations/setup/README.md](../integrations/setup/README.md).
For all agent profiles and routing aliases: [docs/INTEGRATIONS.md](INTEGRATIONS.md).
Every profile `fp code` prints points at this same loopback proxy, so installed clients and the launcher stay in sync.

The command writes the proxy config for you (keyless, or protected when a
proxy key resolves); the managed setup
([integrations/setup/README.md](../integrations/setup/README.md)) adds
persistent keys and long timeouts, as shown by `fp code`.

If something fails, match the line below (exit 2 is usage, exit 3 is
operational):

| You see | Meaning |
|---|---|
| `freellmpool: agent launch requires POSIX (Linux or macOS); Windows is not supported` | exit 2: agent launch needs Linux or macOS |
| `freellmpool: opencode not found on PATH — install it first (https://opencode.ai/install)` | exit 2: install the harness binary first |
| `freellmpool: invalid --port 99999: must be 1-65535` | exit 2: pick a valid port |
| `freellmpool: unknown provider 'typo'. Known registry ids: ... (model must be an alias [auto, agent, spread, fast, quality, fair], provider/model, or a bare name)` | exit 2: fix `--model` |
| `freellmpool: invalid local restrictions; repair providers.toml` | exit 3: fix the local catalog, then re-run |
| `freellmpool: no eligible routes — run freellmpool status, then freellmpool update or freellmpool setup as directed` | exit 3: cold pool, follow the named command |
| `freellmpool: all allowances exhausted — wait for reset (see freellmpool quota)` | exit 3: quotas spent, wait for reset |
| `freellmpool: allowance definitions changed — affected routes fail closed until reset (see freellmpool quota)` | exit 3: config changed, old accounting must expire; detail in `freellmpool quota` |
| `freellmpool: agent-start requires the managed router (unset FREELLMPOOL_LEGACY_ROUTER)` | exit 3: unset the legacy flag |
| `freellmpool: provider registry unreadable — cannot judge routes (reinstall or clear the policy bundle)` | exit 3: registry broken, reinstall or clear the bundle |
| `freellmpool: verification found no tool-capable route (see verify output above); run the named command, or wait and re-run` | exit 3: no tool route verified |
| `freellmpool: auth mismatch on port 8080: the flag key was rejected (401); fix the key and re-run` | exit 3: wrong proxy key |
| `freellmpool: proxy on port 8080 requires a key (protected); pass --api-key or set FREELLMPOOL_PROXY_KEY` | exit 3: the live proxy needs its key |
| `freellmpool: port 8080 serves a non-freellmpool service (foreign); free the port or pick another with --port` | exit 3: something else owns the port |
| `freellmpool: proxy on port 8080 has no ready routes (see freellmpool status); gave up after 60s` | exit 3: live proxy, zero ready routes |
| `freellmpool: proxy on port 8080 did not become ready within 60s` | exit 3: the proxy never became ready — free the port or pick another |

If verify exits 3 naming `update`, `setup`, or `status`, run that command; if it exits 3 after probing, wait a few minutes and re-run.

If exit 3 persists across re-runs, `fp verify --heal --features tools --limit 20` forces a fresh probe round.

agent-side 429s mean stale evidence — re-run the agent-start command.

## Conservative defaults used here

Every knob this guide exposes is pinned low: `--max-tokens 32`,
`--timeout 60`, orchestration flags (`--verify-limit 20`,
`--verify-timeout 45`), one `-p` provider pin, loopback-only proxy, and
the `agent` routing alias instead of wide fan-out. Raise them once you
know your free allowances.
