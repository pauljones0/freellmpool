# Maintenance implementation record

The delivered system is documented in [MAINTENANCE_GOAL.md](MAINTENANCE_GOAL.md).
The design received Product, UX, Architecture, Operations and Security reviews;
feasibility, completeness and alignment plan reviews passed. Independent code
reviews and regression tests covered publication, account observations, policy
updates, catalog filtering, issue lifecycle and recovery.

## Final implementation

1. Publish an attributed public source tree with private configuration, runtime
   reports and personal working history excluded. Preserve the original local
   work in a private archive outside the public repository. Keep package and
   current source metadata distinct from archived upstream release assets.
2. Refresh model APIs and save only candidates matching reviewed free grants.
   Validate raw pagination first; a valid listing filtered to zero free models
   replaces the previous catalog. A failed fetch preserves the last valid age
   and cannot retain models excluded by the current free policy.
3. Observe useful free-plan, balance, usage and rate-limit facts through read-only
   APIs. Do not infer free request counters or free credit allocation from a mixed
   monetary balance. Keep account confirmation, catalog freshness, policy evidence
   and protocol proof separate. Missing evidence does not establish paid-only access.
4. Run public checks through GitHub Actions without provider credentials or
   inference. Persist validated bounded public baselines, proposals and pending
   review findings. Synchronize automation-owned issues, preserve human notes,
   avoid duplicates, respect manual suppression and require fresh matching
   recovery before closure. Removed providers and obsolete baseline records do
   not remain in the maintained free catalog.
5. Deliver reviewed data-only policy revisions from immutable trusted Git
   commits. Validate schema, digest, minimum client version, credential
   destinations and quota definitions. Deletion-only provider changes can reduce
   eligibility; new identities and incompatible quota changes require a client
   update. Activate data and revision atomically and preserve recovery floors.
6. Install daily local catalog/source/account checks, bounded protocol checks
   and a weekly independent public review. Show one maintenance command, private
   attention files and deduplicated notifications. Observe public workflow health
   without credentials; document schedule recovery and unsupported APIs.

## Validation

Tests precede behavior changes. Required checks include Ruff, strict types on
maintained modules, full pytest with resource/unraisable/thread warnings promoted,
separate 80% line/70% branch coverage gates, catalog/metadata/policy-channel checks,
build/Twine, fresh installation and real container startup. Publication validation
includes an exact-tree/history secret scan, actual GitHub workflow runs and issue
idempotency, private read-only observations, and authenticated gateway health.

Remaining provider evidence work is tracked through the public maintenance
issues. Automation does not acknowledge a changed policy or expand free access
merely because the latest request succeeded.
