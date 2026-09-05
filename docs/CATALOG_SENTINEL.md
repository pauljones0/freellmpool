# Provider evidence maintenance

The old `catalog-sentinel` workflow is retired. Its protected inference job and
`FREELLMPOOL_SENTINEL_KEYS_JSON` secret are no longer needed; the former
requirement for GitHub environment protection does not authorize scheduled
inference in the maintained rewrite.

The replacement `provider-evidence-review.yml` workflow checks public model
catalogs and official policy sources daily. It is credentialless, uses bounded
GET requests, and publishes validated public reports and idempotent review issues. It does
not perform inference or change local account state. Reviewed policy and fresh
account evidence remain necessary before routing.

## Local maintenance

```sh
freellmpool update
freellmpool status
freellmpool verify --limit 4
```

`update` refreshes model listings using local credentials. It saves a complete
validated snapshot atomically; partial/error responses preserve the last valid
snapshot and its original timestamp. A 429, 402, authentication failure, empty
listing or timeout is not proof of retirement. New requests observe updates
without restarting the gateway.

`verify` is separate and uses fixed synthetic prompts through the managed free
policy and transactional allowance ledger. It performs a bounded rotating
canary sample. Never use the retired direct-probe script as a free-admission
bypass. See [protocol conformance](PROTOCOL_CONFORMANCE.md).

Setup installs local timers for authenticated catalog refresh, a small bounded
verification sample, and weekly public source review. Inspect them with:

```sh
systemctl --user list-timers 'freellmpool-*'
```

## Evidence and review

A model listing is inventory, not a free-price or account-tier assertion.
Provider policy, account eligibility and protocol capability evidence have
separate provenance and expiry. Reviewed official sources may renew unchanged
policy evidence; a changed/unavailable source cannot silently increase a limit
or enable a paid route. Opaque account tiers need explicit current account
confirmation when no trustworthy API exists.

Public workflow results are advisory. A maintainer reviews source changes,
checks billing boundaries and quota scopes, and adds regression fixtures before
changing the registry. Unknown quotas remain unknown. Catalog model counts do
not measure independent allowances.

The superseded direct-probe utility has been removed. Use the maintained
commands above and [maintenance guide](maintenance.md).

See the [provider registry guide](provider-registry.md),
[implementation design](../REWRITE.md), and
[contributor instructions](../CONTRIBUTING.md).
