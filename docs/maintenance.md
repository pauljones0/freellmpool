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
