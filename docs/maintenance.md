# Maintenance with little routine attention

Run `freellmpool maintenance` to see the actions that currently matter. It reads
local state and prints recovery commands. To refresh evidence now:

```sh
freellmpool maintenance --refresh
```

Refresh reads reviewed policy, model catalogs, official policy sources, supported
account observations and public workflow health. It does not perform inference.
Ollama's identity read uses the provider's documented empty POST; the remaining
maintenance reads use GET. Account observations never change billing settings,
buy credits or renew operator account confirmations.

## What runs automatically

`freellmpool setup-clients` installs the gateway and local user systemd timers.
Daily maintenance refreshes policy and available evidence. A separate daily
verification timer checks at most four eligible routes through the managed free
gateway. It prioritizes retaining useful protocol proof and reserves a scouting
slot when its budget permits. Weekly local public review remains credentialless.

```sh
systemctl --user list-timers 'freellmpool-*'
freellmpool maintenance
freellmpool maintenance --json
```

The public GitHub workflow runs daily at 05:17 UTC and supports manual dispatch.
Its collection job has read permissions and no provider credentials. A separate
job receives only a validated public report and a repository-scoped issue token.
Neither job imports local account observations, prompts, credentials or private
runtime state.

On machines without a user systemd manager, setup can write the units without
starting them. Arrange an equivalent daily invocation of
`freellmpool maintenance --refresh` in the user's private environment; installing
files alone does not establish a running schedule.

## Local attention

A new actionable finding or approaching expiry produces one desktop notification
when `notify-send` is available. The readable fallback is always retained at
`$XDG_STATE_HOME/freellmpool/maintenance-attention.txt`, or
`~/.local/state/freellmpool/maintenance-attention.txt` by default. Its permissions
are 0600. The notification points to `freellmpool maintenance`; the command gives
the exact next commands without requiring JSON or journal inspection.

Identical active findings do not produce repeated notifications. An approaching
deadline and its eventual expiry are distinct findings. A resolved problem that
later recurs can notify again. Unconfigured providers, disabled accounts,
excluded grants and unchanged unsupported account APIs do not generate account
attention. Their coverage and uncertainty remain visible in the report.

Account confirmation typically needs `freellmpool setup --provider PROVIDER`.
A model listing or successful identity request cannot establish missing billing
conditions. Review findings need a maintainer to inspect the linked public issue
and official source; repeating refresh does not approve a changed grant or limit.

## Thin tool bench (demand-driven heal)

Tool evidence expires after 7 days; when fewer than 3 chat routes hold fresh
tool passes, `status` offers `freellmpool verify --heal`, which re-probes up to
4 verification targets through the exact `verify` path (free-only, allowances
reserved normally). `status` itself never probes. Bounds: ≤12 probes per run,
≤3 runs and ≤36 probes per day, 1h cooldown after any run (doubling on
zero-pass or 429-heavy runs, max 24h, reset when the bench is restored), and a
`FREELLMPOOL_HEAL_BUDGET_SECONDS` wall-box (default 300, clamped 60–600,
checked between probes). `verify` without `--heal` only offers; timers and
`maintenance --refresh` heal only with `FREELLMPOOL_AUTOHEAL=1` (explicit
`--heal` always runs; the proxy demand daemon below heals under the
same flag). Run history lives in `heal.json` next to the other
state files — never inside conformance evidence, so fresh and healed runs
stay distinguishable by trigger. For background healing, add
`Environment=FREELLMPOOL_AUTOHEAL=1` to the installed verify/refresh units
(`systemctl --user edit freellmpool-verify.service`); the installer never
enables it unprompted.

## Proxy demand ticks (G33 autoheal daemon)

With `FREELLMPOOL_AUTOHEAL=1`, `proxy` (and `tailnet serve`) start a daemon
that heals only when real tool traffic is being rate-limited: each terminal
tools-bearing request that exhausts all providers with a 429 records one
tick — a memory-only increment, never disk I/O on the request path. Text
requests, non-429 failures, and anything but terminal exhaustion never tick.
The daemon evaluates once a minute and heals only when ≥5 ticks land inside
a 600-second window (same bounds and cooldowns as above); healthy benches
and closed gates consume the demand without probing. A steady
below-threshold trickle never heals — windows expire, so low-traffic
rate-limiting starves by design rather than accumulating stale demand.

Demand state lives in `heal_ticks.json` next to the other state files
(`FREELLMPOOL_HEAL_TICKS_PATH` overrides). Ticks move memory→disk exactly
once across threads and processes; a corrupt tick file backs the flush off
(ticks restored to memory, retried next pass) and a failed flush never
migrates ticks into an expired window — backed-off ticks restore only to
their matching live window, otherwise they expire under the same honest
crash-loss policy as a crash between passes (documented, never silently
invented). If a move stays in flight past the 50 ms demand spin budget,
that pass reads conservative no-demand and the next pass sees the
flushed ticks: healing can be delayed by one pass, never duplicated
or lost.
A tick recorded while demand is being read likewise surfaces on the next
pass. A long-running heal pass skips missed minute anchors rather than
stacking catch-up runs, and consumes into the live window when it lands.
Overlapping runs from separate processes sharing one tick file (proxy +
`tailnet serve`) may drop same-window peer demand on consume; windows,
thresholds, and fresh ticks re-arm, so the loss is bounded and
self-healing. Shutdown stops the daemon (bounded 5 s join, mid-run pass
detached by design with its post-run consume skipped) and flushes pending
ticks so the next start resumes full demand. Account-quota 429s tick like
any other tools-429 — the low-yield backoff contains the resulting heal
spend to a decaying trickle instead. These interleavings are pinned by
`tests/test_proxy_heal.py` (`test_020_*`, ported from independent
frozen-source probes).

## Serving preserved routes (warning-while-serving)

A listing that comes back adverse — denied scope cut, failed or
missing auth, unsupported listing, error, partial, rate limit, or
another deferred pass — does not stop the provider's last-good
routes: while the previous evidence is fresh
they keep serving, because a listing-only refusal must not assert
inference death. `status`, `providers`, and `quota` say so on the row
itself: `ready; WARNING: serving N preserved routes; last listing
<verdict>: <guidance>; run freellmpool update --provider PID to
re-check`. The same text rides the `warning` key of each provider row
in `status --json` and the proxy `/status` eligibility payload, so
scripts and MCP agents (`free_llm_quota` shows it too) see what the
terminal shows. Admission is unchanged — the warning observes, the
routes still serve — and it clears on the next successful update.
`models` stays a pure route catalog and readiness stays a live
routability axis; neither carries the warning by design.

## Public issues and history

Complete catalogs are compared with a bounded baseline from this repository's
known workflow. The report records the checked source revision and time. Missing
history starts a baseline; it does not announce every model as newly added.
Failed or partial responses preserve prior facts and their age.

Discovery retains only candidates selected by a reviewed hard-free grant, with
known zero prices wherever that grant requires them. Account-dependent grants
still need current account proof before routing. A complete, nonempty upstream
listing that contains no eligible free candidates becomes a fresh empty catalog
and can remove prior routes. An empty or malformed upstream response remains
partial and preserves prior facts.

For the reviewed Z.ai free grant, an explicit registry setting also retains
exact documented zero-price IDs omitted by a successful complete listing.
These are marked as unlisted candidates with unverified availability, not live
successes. Listed prices take precedence, and cached candidates are checked
against the current setting and grant. This does not renew policy evidence or
provide account, stream or tool proof. See
[model API coverage](api-coverage.md#reviewed-candidates-missing-from-a-model-listing).

Issues distinguish transient check failures from changes needing review. A
second successful fetch of the same changed price does not resolve its issue.
Pending changes and their machine-readable proposals survive baseline advances
and parser outages. A proven reversal or reviewed acknowledgement can resolve
the matching change; a successful fetch alone resolves only the matching fetch
incident. Issue synchronization independently checks fresh evidence before
closing an automation-owned incident.

The bot changes only its marked section, preserving human notes and discussion.
Identical findings do not create comments. Manually closing an issue suppresses
the unchanged finding; changed evidence can reopen it. A failure that prevents
creating a valid report produces a separate static workflow incident.

After inspecting a corrupt or incompatible baseline and the outstanding issues,
a maintainer can start a new baseline once:

```sh
gh workflow run provider-evidence-review.yml --repo pauljones0/freellmpool -f reset_baseline=true
```

This skips historical restoration for that run. It does not acknowledge old
changes or authorize closing existing review issues. Normal scheduled runs then
resume retaining the new validated baseline. Public reports are retained for
30 days and baseline artifacts for 90 days.

## Public workflow health

Local maintenance reads the public workflow state and its latest default-branch
run from GitHub without a token. It distinguishes disabled, failed, overdue and
unknown states. A failed API check preserves the last observed successful run;
it is not evidence that the workflow itself failed. An observed success older
than 48 hours is overdue for the daily schedule. Status reads only this cache.

GitHub can delay scheduled runs, and it disables public-repository schedules
after 60 days without repository activity. Local maintenance continues
independently. No artificial commits are created to evade inactivity rules.
[GitHub scheduling behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

```sh
gh workflow enable provider-evidence-review.yml --repo pauljones0/freellmpool
gh workflow run provider-evidence-review.yml --repo pauljones0/freellmpool
```

The workflow-health reader currently tracks this fork's `main` branch. A 404
before initial publication or inaccessible API is reported as unknown.

## Reviewed rules and account limitations

Model APIs refresh inventory and exposed prices. Official source hashes renew
only identical reviewed evidence. A changed or unavailable page cannot extend
its last valid verification, increase a quota or authorize paid access. Separate
account and protocol proof expires independently.

The current account adapters read OpenRouter key budgets/usage, Vercel team
credit telemetry, Ollama's free/starter plan and reported monthly usage, and
optional Mistral Admin limits. Ollama's consumed model request counts are usage
history, not an eligible-model list or a model allowance. Its usage value retains
an unknown unit until the provider's scale can be verified; no reset is inferred
from its separate activity-report window.
A mixed monetary balance is not the remaining free allocation. These reads do **not**
currently establish the scope and reset needed to import new routing limits.
Existing Groq and ModelScope response-header observations continue to constrain
the shared runtime ledger. Other applications' usage can remain unknown. Exact
API contracts and per-provider limitations are in
[account observations](account-observations.md).

The Groq official free-plan parser creates typed proposals for review, with
source provenance. It refuses ambiguous tables or missing reviewed model data.
Other rule sources remain explicit review tasks. Proposals do not activate
themselves and no bot merges unreviewed eligibility changes.

The policy channel resolves the trusted repository to an immutable commit,
validates the data-only manifest and registry, and atomically activates a private
bundle. Local disabled/manual restrictions and exclusions remain. Normal fetch
failures retain the last valid bundle; corrupt local policy fails closed and
requires repair. Unsupported provider/authentication/endpoint changes need a
client update. Rollback is a reviewed higher revision containing the earlier
safe policy, so accidental revision rollback remains rejected.

## Reproducible checks

Offline fixtures cover persistent review changes, reversal, parser outages,
partial catalogs, public schema/privacy checks, account expiry notifications,
quiet unsupported providers, workflow health and CLI behavior:

```sh
python -m pytest tests/test_maintenance.py tests/test_maintenance_cli.py tests/test_workflow_health.py
python -m pytest tests/test_maintenance_github.py tests/test_account_observations.py tests/test_limit_sources.py tests/test_policy_updates.py
```

The root publication checks additionally build/install the distribution, enforce
package coverage, validate the gateway and exercise the actual public workflow.
Those operational results are recorded separately from offline fixture coverage.
