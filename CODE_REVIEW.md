# Code review — 2026-09-06

Reviewed routing, shared allowances, policy renewal, discovery, credential storage,
CLI/setup UX, HTTP boundaries, and their integration tests. Each confirmed defect
received a failing regression before its fix. Follow-up reviews checked the fixes
and related failure paths with different reviewers.

## Findings addressed

| Area | Defect and resulting behavior |
| --- | --- |
| Operator restrictions | Home-relative catalog paths bypassed local exclusions. Paths now expand consistently; malformed and unresolved paths produce a held configuration instead of dispatching or crashing. |
| Quota identity | Reconfirming an account replaced its existing quota identity. Confirmation now preserves the identity and refuses ambiguous corrupted identities. |
| Usage accounting | Aggregate usage ignored reported total tokens. Settlement now respects the larger valid aggregate; unknown totals preserve reservations and charge any higher known token or neuron lower bound. |
| Automatic upstream tools | Groq Compound enables server-side tools by default. Its two exact model IDs are held when a grant prohibits those tools, including pinned requests. Other Groq models are unaffected. |
| Quota reporting | Empty rule defaults hid known per-model capacities. Reports now distinguish known, partial, unknown, unassessed and inapplicable rules, list missing models in private JSON, and identify providers without recorded rules. |
| Policy renewal | Unbound renewal records could hide expired evidence in maintenance. Routing and reporting now share the same provenance and freshness predicate. |
| Download bounds | Response limits were applied after buffering or decompression. A shared reader bounds encoded and decoded bytes, checks compressed-stream completion, and closes failed downloads while preserving previous evidence. |
| Credential persistence | The compatibility key writer discarded configuration values and overwrote corrupt files. Setup and compatibility commands now share a locked atomic writer that preserves TOML types and Unicode. CLI errors preserve the original file and omit credentials. |
| Setup policy | The wizard ignored active reviewed policy updates and mishandled tier lists and alternative grants. It now follows active rules and records an actual accepted tier. |
| Account repair | Malformed account records could remain permanently held through repeated setup. Repair preserves explicit exclusions, quota identities and unrelated records; ambiguous damage requires repair rather than silent deletion. |
| Installation and recovery | The fallback installer left no persistent command launcher. Setup now provides a launcher, actual installed-client paths, truthful service status and concrete foreground/service recovery commands. |
| Verification UX | Empty feature lists could report success without probes. Empty, duplicate and unknown features, and invalid timeouts, are rejected before loading accounts or dispatching. |
| HTTP boundaries | Non-ASCII authentication headers produced server errors; explicit port zero could normalize to port 80. Invalid credentials and authorities now receive normal rejection responses. |
| Key-entry UX | Saving a key claimed to unlock routes without establishing eligibility. Output now distinguishes storage from admission and directs users to status. |
| CLI types and empty discovery | Strict checking exposed 35 typing errors and a crash when manual model discovery returned nothing. Explicit types and branch narrowing resolve the typing errors; empty discovery now gives a validation error without writing a catalog. CI now checks the CLI. |

Compound behavior was checked against the [official built-in tools documentation](https://console.groq.com/docs/compound/built-in-tools).
The runtime restriction expresses an incompatibility with the selected grant's
tool exclusions; it does not classify those models as paid-only. No provider
pricing or quota amounts were expanded by this review.

## Architecture and idiom

The maintained gateway separates catalog discovery, free eligibility, protocol
conformance and transactional quota reservations. Those are useful boundaries:
finding a model or successfully storing a key cannot establish permission to
spend. The review kept these decisions separate and consolidated duplicated
logic where routing, reporting and setup had diverged.

The compatibility CLI and proxy remain large modules, approximately 3,000 lines
each. Their size is a maintenance concern, not evidence that a broad rewrite
would improve correctness. This review extracted shared credential and HTTP
reading behavior and shared policy predicates while retaining the tested public
interfaces.

## Validation

Final validation passed:

- 2,360 tests and 14 subtests, with resource/thread warnings treated as errors.
- 87.91% package line coverage and 78.27% branch coverage; required floors are
  80% and 70%, respectively.
- Ruff; strict typing of the CLI, maintained gateway and shared helpers
  (18 modules), plus the four existing focused type-check targets.
- Catalog, policy-channel and release-metadata validation.
- Proxy stress: 144 requests across 12 API paths at concurrency 24.
- Source/wheel builds, Twine validation, and imports/CLI parsing from a fresh
  wheel installation.
- The configured high-severity/high-confidence Bandit gate. Its two medium SQL
  warnings were inspected: the interpolations are fixed operator/table
  allowlists, and request values remain bound SQL parameters. No suppressions
  were added.
- Final independent cross-review found no further actionable regressions,
  including additional ordinary, streaming and multipart accounting checks.

Tests use isolated local state and synthetic transports; they do not spend
provider credits or change account settings. The fixes and tests are committed
locally; this review did not restart the running gateway or publish a release.

A clean review pass means no further actionable issue was found in the inspected
paths. It cannot establish that the entire repository or every live provider
behavior is bug-free.
