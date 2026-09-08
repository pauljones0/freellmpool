# Goal: a public gateway that stays maintained

Status: published and validated, including clean GitHub history, the free-only catalog migration and the installed gateway. Repository: [pauljones0/freellmpool](https://github.com/pauljones0/freellmpool). Ongoing provider review is tracked in GitHub issues.

Publish this maintained fork with a concise README and no credentials or personal runtime evidence in its published history. Automate model and price discovery, supported account/quota observations, policy-change detection, maintenance issues, and distribution of reviewed provider rules. Keep uncertain information explicit and preserve the gateway's no-charge boundary.

## Acceptance

- [x] Publish an audited, attributed source tree and history to the user's public GitHub repository. Ignore private keys, account state, reports and database sidecars; retain original MIT attribution.
- [x] Provide a short README with a working clone/setup path, generic agent connection settings, useful commands, current maintenance behavior and limits.
- [x] Run scheduled public catalog/price/source checks with no provider credentials, and private account observations locally using documented endpoints and existing legitimate credentials.
- [x] Present model changes, price changes, stale evidence, failed checks, unknown limits and actionable account requirements in a shared maintenance report/status interface.
- [x] Notify the local user once about new actionable private failures or approaching expiry, with a plain recovery command and a persistent attention file when desktop notifications are unavailable. Keep unchanged unsupported facts quiet.
- [x] Open/update deduplicated GitHub issues for reviewable provider changes or failures. Successful recovery closes only automation-owned incidents proved resolved by complete fresh evidence. Preserve manual notes and avoid repeated notifications for unchanged findings.
- [x] Produce machine-readable proposed rule changes where the source has a reliable parser, with source provenance and regression checks. Unparseable or contradictory changes require review; never guess numeric limits or free grants.
- [x] Keep remaining-quota observations separate from catalog prices, account attestations and nominal rate limits. Supported API/header observations cannot expand free eligibility or reset shared consumption.
- [x] Deliver reviewed data-only policy updates from the trusted public repository, validate their schema/hash/version and endpoint boundaries, preserve exclusions, and retain the last valid policy on failure.
- [x] Complete free-only publication and installed-state migration; validate public CI and maintenance, the installed gateway, active schedules and preserved quota records.
- [x] Replace earlier published history with the audited clean root after explicit approval. The repository was recreated at the same URL; prior Git history, issue records and available artifacts were backed up privately outside the repository.
- [x] Document unsupported APIs, review boundaries, scheduling limitations, rollback and reproducible validation. Do not promise unlimited free capacity or perfect visibility into external usage.

## Design

### Two execution environments

Public GitHub Actions checks known public model APIs and official policy sources without provider keys, private configuration, account identifiers, prompts or inference. Its issue step receives only a validated public report and repository-scoped GitHub token. Issue creation is explicitly authorized by this goal. No untrusted response is executed as a command or inserted into workflow expressions. A serialized workflow and stable provider/problem identifiers prevent duplicate issues.

Private local maintenance refreshes authenticated free catalogs and documented free-plan/limit observations. It reads existing private credentials, persists bounded normalized observations atomically with mode 0600, and never uploads those observations. Public model APIs must not be mistaken for authentication or account checks. Each supported account field has its own source, unit, scope and timestamp; unsupported facts remain unknown. An API that confirms only a plan does not confirm no paid balance or disabled billing.

### Reports and changes

A common maintenance report separates catalog freshness, source review, account observation, conformance freshness, policy-channel health and uncertainty. Model/price deltas compare complete snapshots only; failed/partial responses retain last-good data without renewing it. Reports redact free-form upstream errors and private identifiers. Local status shows the latest failed attempt even while previous evidence remains valid.

Private attention is automatic and deduplicated. A new actionable fingerprint or approaching deadline may generate a local desktop notification when available; unresolved reminders are bounded, and ordinary unchanged unknowns remain quiet. Every actionable report is also written to a private human-readable attention file. A single `freellmpool maintenance` command presents the next actions, with exact provider setup/recheck commands; it never requires reading JSON or journals. Tests simulate expiry and prove one clear notification plus a persistent fallback, no duplicate on identical runs, and no private upload.

Public monitoring keeps a bounded previous public snapshot to detect changes across fresh runners. The retained snapshot is data only, loaded with strict limits from a trusted workflow artifact or reviewed repository baseline; it cannot contribute credentials, policy or executable code. Missing history is a baseline event, not an assertion that every model was just added. Each report is tied to the checked source revision and time.

Issues contain a stable marker, affected provider/fact, before/after values when known, approved source links and exact review/check instructions. Only bot-managed sections are updated; user discussion remains. Unknown/failing checks cannot close an issue. Repeated identical results do not create comments. Public failures that preclude the report itself are surfaced as a separate workflow incident. A manually suppressed incident remains suppressed until its underlying evidence changes, rather than being reopened on every run.

### Account and allowance observations

Implement only endpoint contracts verified in official documentation or first-party source. Account checks retain useful free-plan, balance, usage and rate-limit facts: OpenRouter key metadata, Vercel credit balance, Ollama plan identity and reported monthly usage, and optional Mistral administration limits using a separately supplied credential. A mixed balance is never classified as remaining free credit. Free-only routing does not require deleting useful read-only account APIs. Unsupported free remaining-quota facts stay unknown. Each saved catalog is projected through reviewed free grants after complete upstream pagination; if the final free model disappears, an empty complete generation replaces it. Cached paid models cannot remain routable after a failed refresh. Account conditions are still verified separately before inference.


The ledger continues to reserve all applicable scopes transactionally. Account/project/IP limits remain shared across model switches and clients. Registry rule changes preserve prior usage and the existing definition-epoch safeguards. Do not add an unverified universal header parser that confuses requests/day with requests/minute. Record unsupported adapters explicitly so extending coverage is an ordinary provider-adapter task.

### Reviewed policy delivery

The trusted repository publishes a data-only channel manifest with a monotonic revision, schema/client compatibility and registry digest. A client resolves the configured trusted repository/default-branch commit, fetches the manifest and registry from that immutable commit, validates bounded JSON and digest, and atomically activates a private bundle. It executes no downloaded Python or shell. New providers, authentication changes, credential destinations and incompatible quota semantics require a client/reviewer change rather than an unvalidated override. Tombstones and local disabled/manual restrictions survive. Invalid, rolled-back or unavailable bundles retain the last valid data and produce a visible maintenance finding.

Policy changes are reviewed commits. Structured parsers can propose changes and tests, but page hash changes alone never authorize new free access or bigger limits. Automation does not merge unreviewed eligibility changes. Initial publication does not publish to the upstream author's PyPI, npm, MCP or container namespaces; inherited release workflows must be restricted accordingly.

### Maintenance health

Daily local work updates catalogs, observations and source evidence; daily bounded canaries prioritize renewing known useful proof while reserving a scouting budget. Public checks run regularly and can be dispatched manually. Show last successful/attempted checks and deadlines, including an overdue public workflow where observable. GitHub schedules can be delayed or disabled after inactivity; local checks continue independently and documentation must explain how to restore a disabled schedule. No artificial commits are made merely to evade inactivity rules.

## Validation boundary

Regression tests precede implementation. Design receives five independent role reviews; the subsequent implementation plan receives three adversarial reviews. Required package coverage remains 80% lines and 70% branches separately. Test schema/privacy failures, stale/partial baseline handling, issue idempotency and recovery, account credential changes, incompatible telemetry units, policy rollback and endpoint changes, shared-budget preservation, and effective CLI/runtime behavior. Run public GitHub checks and bounded read-only local account checks; inference is unnecessary for these maintenance changes. Preserve existing service/client integration tests.

## Delivered and ongoing work

The public repository has an independently audited history with no detected
credentials or personal runtime records. Original MIT attribution is retained;
inherited upstream publishing jobs are restricted. Public secret scanning and
push protection are enabled.

The maintained system provides a daily public workflow, private local updates,
validated baseline artifacts, persistent review findings, deduplicated issues,
account observations, structured Groq limit proposals, independent freshness
reporting, local attention files, workflow health checks and a reviewed policy
channel. The public workflow has been exercised on GitHub, including baseline
recovery on a fresh runner and a repeated issue synchronization. Unchanged facts
stay quiet; real model and price deltas generate review work. The three local
timers and authenticated loopback gateway have been verified.

Validation includes the complete package suite with warnings promoted to errors,
separate 80% line/70% branch coverage gates, strict types, lint, catalog and policy
revision checks, build/Twine, protocol/client tests, actual container startup,
and GitHub's Python 3.11–3.14 matrix. See the [current Actions results](https://github.com/pauljones0/freellmpool/actions)
for the tested commit rather than relying on a permanent test-count claim.

[Maintenance issues](https://github.com/pauljones0/freellmpool/issues?q=is%3Aissue+is%3Aopen+%5Bmaintenance%5D)
are the ongoing review queue. Some supporting policy pages still need reviewed
content baselines, and some providers block automated page fetching. Those
findings are not silently acknowledged or mistaken for changed terms. Numeric
quota proposals, changed prices and new models require review where they affect
policy; fetching a page is not approval for more free access.

The maintained source is version 0.14.1. Its catalog contains free candidates only;
setup omits providers without a reviewed free grant. No disabled account records,
parked static model rows remain in the maintained flow. Read-only balance
observations remain separate from route admission. Some reviewed providers use dynamic discovery without static
pins. The legacy raw catalog-sentinel implementation has been removed.

The installed migration preserves existing quota charges and reservations,
removes obsolete active credentials and model overrides, and keeps runtime data
private. Intentional free-catalog projection is acknowledged separately from real
model changes, so old paid inventory does not flood the local review queue.
Account attention checks the same required plan, billing conditions and credential
binding used for admission; a recent timestamp alone does not establish eligibility.

Ollama exposes identity/plan and reported monthly usage with consumed request
counts by model. It omits the exact free allowance and reset; its usage scale
remains unverified. Optional Mistral administration requires a separate
credential. These endpoints do not currently prove a scope/reset-matched free
request balance. Most remaining quota visibility comes from the transactional
ledger and supported provider-specific response headers. Unknown external usage
remains unknown. See [account observations](docs/account-observations.md),
[maintenance and recovery](docs/maintenance.md), and
[reviewed policy delivery](docs/policy-updates.md) before extending adapters.

## Provider classification correction

A nonzero list price does not rule out a legitimate free allowance. An absent
price/quota field or an unverified spending boundary does not prove paid-only
access. Check live model, price and account APIs first; use official policies for
terms those APIs do not expose. Record unresolved facts as unresolved.

The September 5 review rechecks Vercel's conditional monthly credit and other
finite offers. These offers must not be
called paid-only merely because the current gateway cannot yet safely admit
them. Keep unsupported offers out of active routing while their exact account,
model, reset and no-charge conditions are investigated. See the API coverage
notes for live observations and remaining questions.
