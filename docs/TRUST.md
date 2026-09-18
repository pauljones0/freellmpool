# Trust

What this fork promises, and where to verify each promise. Every claim
below links to its proof; anything without a link is in Non-promises.

## Pins

- **Locked dependencies.** `uv.lock` pins every dependency; CI installs
  from the lock. Verify: [`uv.lock`](../uv.lock),
  [ci.yml](../.github/workflows/ci.yml).
- **Pinned CI tooling.** Scanners and actions are version-pinned, not
  floating. Verify: [security.yml](../.github/workflows/security.yml)
  (`bandit==1.9.4`, `pip-audit==2.10.1`, `zizmor==1.30.1`), pinned
  `actions/*` SHAs across [workflows](../.github/workflows/).

## Audit gates (run on this fork, every push to main)

- **Source audit.** Bandit fails the build on high-confidence,
  high-severity findings (`--ignore-nosec`, so `# nosec` cannot silence
  it). Verify: [security.yml](../.github/workflows/security.yml),
  plus [`scripts/security_exceptions.py`](../scripts/security_exceptions.py)
  which validates every suppression.
- **Dependency audit.** `pip-audit --strict` with aliases. Verify:
  [security.yml](../.github/workflows/security.yml).
- **Actions audit.** Zizmor lints the workflows themselves. Verify:
  [security.yml](../.github/workflows/security.yml).
- **CodeQL.** Runs on push, PR, and weekly schedule. Verify:
  [codeql.yml](../.github/workflows/codeql.yml).
- **Coverage gate.** Package line ≥80% and branch ≥70%, enforced
  separately. Verify:
  [`.coverage-thresholds.json`](../.coverage-thresholds.json),
  [`scripts/check_coverage.py`](../scripts/check_coverage.py).
- **Catalog/count honesty.** Docs cannot drift from the packaged
  catalog: `scripts/check-counts` enforces provider/route/model claims.
  Verify: [`scripts/check-counts`](../scripts/check-counts).

## Evidence process

- **Reviewed provider claims.** Every free allowance traces to an
  official source with a recorded content hash, re-checked on a
  schedule. Verify:
  [`src/freellmpool/provider_registry.json`](../src/freellmpool/provider_registry.json),
  [`maintenance/policy-channel.json`](../maintenance/policy-channel.json)
  (policy rev 8, minimum client 0.14.2), and
  [`MAINTENANCE_REVIEW.md`](../MAINTENANCE_REVIEW.md).
- **Live conformance, not stale badges.** Routes must re-prove chat /
  tools / streaming within 7 days or they stop receiving traffic; `status`
  warns when the fresh tools bench drops below 3. Verify:
  [`src/freellmpool/conformance.py`](../src/freellmpool/conformance.py)
  (`EVIDENCE_MAX_AGE`), `freellmpool verify`, `freellmpool status`.
- **Free-only routing.** Unknown, exhausted, or paid capacity is never
  silently used. Verify: the free gate in
  [`src/freellmpool/free_policy.py`](../src/freellmpool/free_policy.py)
  and the admission tests in `tests/test_free_policy.py`.

## ToS posture

freellmpool is a router over providers you are allowed to use: your keys
stay yours, keyless endpoints are used as keyless, per-day hints spread
load, and real `429`s are honored. It does not create accounts, rotate
identities, solve captchas, share keys, or evade provider limits. You
remain responsible for each provider's terms. Verify: the full statement
in [FAQ](../FAQ.md#what-is-the-tos-posture).

## Secrets and traffic

- **Local-first secrets.** Keys come from your environment or local
  files; runtime state is created private (0700/0600). Verify:
  [FAQ](../FAQ.md) for where prompts and keys go.
- **No product telemetry.** Network traffic goes only to configured
  provider endpoints and reviewed evidence URLs. Verify:
  `grep -rn "https://" src/freellmpool/provider_registry.json` for the
  endpoint set.
- **Honest metrics.** "Estimated cost avoided" uses published rates and
  says so.

## Non-promises

- This fork publishes no SBOM, PyPI, npm, MCP Registry, or container
  releases — those pipelines are gated to upstream (`0xzr/freellmpool`):
  [docker.yml](../.github/workflows/docker.yml),
  [release-evidence.yml](../.github/workflows/release-evidence.yml).
  Install from the repository tarball or source (see
  [README](../README.md)). The `freellmpool` PyPI name belongs to
  upstream.
- Free tiers belong to their providers: caps, bans, and ToS are theirs.
  Read [FAQ](../FAQ.md) before routing anything sensitive.
- Not a privacy layer: prompts go to the selected upstream provider.
