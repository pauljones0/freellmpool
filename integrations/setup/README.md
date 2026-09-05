# Guided free-access setup

From this reviewed checkout, run:

```sh
sh integrations/setup/bootstrap.sh
```

The wizard shows one provider's signup link and exact account/key instructions, accepts a hidden key or clipboard input, and checks the provider without paid inference. Press `o` to open its exact key page, `s` to skip, or `q` to stop. After a failed check, `r` retries and `k` replaces the key without starting over. Resume with:

```sh
freellmpool setup --resume
```

Retry one provider with `freellmpool setup --provider groq`. Resume skips a successful check only for 24 hours and only while its credentials and required account evidence still match. Changed keys, changed required fields, or expired account confirmations are checked again. If a terminal exports an old key, setup prints the exact `unset VARIABLE` command so that the saved replacement takes effect.

A saved key or successful model listing does not establish free access. The wizard reports when a free route is actually admitted; otherwise `freellmpool status` explains the remaining requirement. Trial/paid-only providers are skipped. Account plan confirmations must describe the actual provider dashboard.

Setup also installs the free client profiles and local maintenance. To install those separately, run `freellmpool setup-clients`; use `--no-start` to prepare files without starting services. It creates `opencode-free` and `hermes-free` launchers for installed clients, an authenticated loopback service, and configures T3's supported OpenCode adapter. The launchers use isolated state and gateway-only model/fallback configuration. Existing normal client configurations remain available through their normal commands.

The generated service unit references a private launcher; no credential appears in its command line. The gateway key is stored as `~/.config/freellmpool/proxy.key` with mode 0600. The service forces the managed free gate even if a legacy-router environment variable is set. For files prepared with `--no-start`, start the gateway with:

```sh
systemctl --user daemon-reload
systemctl --user enable --now freellmpool.service
```

Use T3's Refresh provider status after its OpenCode configuration changes. If an existing managed helper has cached the old config, let it remain idle for 30 seconds before refreshing. Existing threads must select OpenCode with the `freellmpool/auto` model. T3's separate title/summary and source-control writing model selections also point to this gateway. Other provider choices remain available as separate sessions outside the free profile.

## Verified client behavior

On September 5, 2026, the installed OpenCode 1.18.21 resolved the generated profile from a project containing conflicting paid-model settings. Its effective main and small models were both `freellmpool/auto`; the only enabled provider was `freellmpool`, with no external plugins or MCP servers. The actual installed launcher also passed the server flow used by T3: authenticated `/provider` listed only the connected gateway, and unauthenticated requests returned HTTP 401.

The installed Hermes runtime resolved its main model, all 12 auxiliary client slots, and delegated agents to the same local endpoint and private gateway key, with empty fallbacks. Its checkout can load a development `.env` even with an isolated profile, so the launcher sets the installed dotenv library's `PYTHON_DOTENV_DISABLED=1` switch. Verification found no inherited upstream provider keys.

T3 0.0.38's installed settings schema and server launcher support the generated `binaryPath`, `serverPassword`, and `customModels` fields. Setup also updates an existing `providerInstances.opencode` entry so it cannot override the free settings. This verification covers configuration resolution and the authenticated adapter server; a real coding conversation still depends on admitted, current, tool-capable gateway routes.

Generated files keep a `.before-freellmpool` backup when replacing existing contents. To roll back, stop the gateway and its scheduled maintenance:

```sh
systemctl --user disable --now freellmpool.service freellmpool-update.timer freellmpool-review.timer freellmpool-verify.timer
```

Restore the T3 settings backup and use the normal `opencode`/`hermes` launchers. Do not delete provider credentials or account evidence when reverting client integration.
