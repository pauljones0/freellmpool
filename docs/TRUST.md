# Trust

What this fork promises, and where to verify each promise. v1 — written for
the one-command install; extended by later goals.

## Promises

- **Free-only routing.** Unknown, exhausted, or paid capacity is never
  silently used. Verify: the free gate in
  [`src/freellmpool/free_policy.py`](../src/freellmpool/free_policy.py) and the
  admission tests in `tests/test_free_policy.py`.
- **Reviewed provider claims.** Every free allowance traces to an official
  source with a recorded content hash, re-checked on a schedule. Verify:
  [`src/freellmpool/provider_registry.json`](../src/freellmpool/provider_registry.json),
  [`maintenance/policy-channel.json`](../maintenance/policy-channel.json),
  and [`MAINTENANCE_REVIEW.md`](../MAINTENANCE_REVIEW.md).
- **Pinned, scanned supply chain.** Dependencies lock in `uv.lock`; CI runs
  Bandit, pip-audit, and Zizmor on every push. Verify:
  [security.yml](../.github/workflows/security.yml) and
  [`tests/test_supply_chain.py`](../tests/test_supply_chain.py). No SBOM is
  published for the fork (the container pipeline is upstream-only) — a known
  gap, not a claim.
- **Local-first secrets.** Keys come from your environment or local files;
  runtime state is created private (0700/0600). Network traffic goes only
  to configured provider endpoints and reviewed evidence URLs — no product
  telemetry. Verify: `grep -rn "https://" src/freellmpool/provider_registry.json`
  for the endpoint set; [FAQ](../FAQ.md) for where prompts go.
- **Honest metrics.** "Estimated cost avoided" uses published rates and says
  so; catalog counts are enforced by `scripts/check-counts` so docs cannot
  drift from the packaged catalog.

## Non-promises

- This fork publishes no PyPI, npm, MCP Registry, or container releases;
  install from the repository tarball or source (see [README](../README.md)).
  The `freellmpool` PyPI name belongs to upstream.
- Free tiers belong to their providers: caps, bans, and ToS are theirs.
  Read [FAQ](../FAQ.md) before routing anything sensitive.
