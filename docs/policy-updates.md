# Reviewed policy updates

Daily local maintenance can adopt reviewed policy data without installing new
Python code. Run `freellmpool maintenance --refresh` for an immediate check;
`freellmpool maintenance` reads the saved result without network access.

The default trusted source is `pauljones0/freellmpool`. Trust in this repository's
maintainers is part of enabling the channel: a reviewed commit can change free
model eligibility and documented capacities within the existing mechanisms.
Automated findings and proposals require review before becoming policy commits.
The channel never merges proposals or downloads executable code.

Compatible updates can remove providers and obsolete tombstones, which only reduces
eligibility. A newer client cannot load an old bundle containing a removed identity.

## What can change

The client validates downloaded data against its packaged registry. Compatible
updates can change the following existing facts:

| Policy data | Permitted update |
| --- | --- |
| Existing limits | Numeric capacities, documented maximums, model capacities, evidence references and explanatory notes |
| Limits with `scope: model` | Model membership; shared account/IP rule membership remains protected |
| Existing grants | Verification status, model lists, exclusions and an existing selector's suffix |
| Existing evidence | Reviewed source content/hash, timestamps and same-origin HTTPS source paths |
| Existing model costs | Supported neuron-per-token values and minimum audio seconds, with evidence references |
| Model exclusions | Blocked and paid-required model lists |

New provider identities, changed tombstones, credential variables, request destinations,
authentication, grant kinds, billing conditions and no-charge boundaries cannot
change through a compatible data update. Quota rule identities, units, scope,
window/reset definitions and shared membership remain protected. New source
origins or cost units also require a reviewed client release. The client rejects
incompatible data and reports `requires_client_update`.

Account confirmations and operator limits stay
independent. A public policy update cannot attest private account billing
settings. Monetary account observations do not become free requests remaining;
see [account observations](account-observations.md).

## Publication contract

`maintenance/policy-channel.json` contains four fields:

```json
{
  "schema": 1,
  "revision": 3,
  "minimum_client_version": "0.14.0",
  "registry_sha256": "<SHA-256 of the exact provider_registry.json bytes>"
}
```

Every change to `src/freellmpool/provider_registry.json`, including formatting,
requires a strictly higher integer revision. Reusing a revision with different
bytes is rejected even if the replacement digest is correct. A client must meet
`minimum_client_version` and understand the schema and protected field contract.

The reader resolves the trusted repository's current default-branch commit,
then reads the manifest and registry from that immutable commit. Requests use
HTTPS GET, bounded JSON, duplicate-field rejection and no redirects. Provider
keys are never sent to GitHub. The digest binds the manifest to the registry;
it is an integrity check within the trusted repository, not a signature from an
independent authority.

After validation, the registry and revision are saved together in a private
atomic bundle. Serving a request uses local data. The default files are
`$XDG_STATE_HOME/freellmpool/policy-bundle.json` and `policy-update.json`, falling
back to `~/.local/state/freellmpool/`. Status reports the latest attempt and the
last successfully checked revision, commit and time.

## Freshness and recovery

Fetching the same policy again does not extend the policy sources' verification
dates. Each source has its own checked/expiry timestamps and at most seven days
between them. Local evidence checks may renew identical reviewed content, bound
to its source hash and the complete provider policy digest. A policy edit
invalidates old renewal bindings. Failed or changed-source checks preserve the
last valid age. Account confirmations, account telemetry and protocol proof
expire independently.

A failed fetch or invalid candidate leaves the last valid active bundle in
place. If an activated bundle is missing or corrupt, routing fails closed
instead of silently restoring potentially looser packaged rules. Maintenance
can repair it using the last successful status as a revision/digest/repository
floor. The same revision repairs only identical source bytes; a newer reviewed
revision can replace it. Without a trustworthy floor, automatic repair fails
and reports an error for operator review.

To undo an incorrect published change, publish the earlier safe content under
a **new, higher revision**, with its matching digest. Moving the branch back or
decreasing the revision cannot roll back clients that already adopted it.

Set `FREELLMPOOL_POLICY_UPDATES=0` to explicitly disable the channel and use the
packaged policy. This is also an explicit operator recovery option after
reviewing a damaged local state; packaged rules may differ from the last active
policy. Set the variable in the gateway and maintenance service environments
when persistent behavior is needed. Normal provider/account/proof checks still
apply. `FREELLMPOOL_POLICY_REPOSITORY=owner/repo` selects another explicitly
trusted repository; bundles are bound to their repository and a source switch
does not silently reuse the former repository's state.

## Authoring and CI

After reviewing a proposed change and editing the registry, prepare its manifest
against the trusted base commit for the change:

```sh
python scripts/check_policy_channel.py --base origin/main --prepare
git diff -- src/freellmpool/provider_registry.json maintenance/policy-channel.json
python scripts/check_policy_channel.py --base origin/main
python -m pytest tests/test_check_policy_channel.py tests/test_policy_updates.py
```

`--prepare` changes only the manifest digest and any required revision bump. It
preserves an intentionally higher revision and validates before writing. It
does not review evidence, change the registry or approve a proposal. A protected
change also needs a client release and a minimum version above the base commit's
client version; preparation cannot grant that compatibility automatically.

CI supplies the actual trusted pull-request base or previous push commit with
`--base`. An unavailable supplied commit is an error and must be fetched. A
valid base commit predating the channel is treated as initial publication. For
the first public commit with no base, omit `--base`; this validates the current
manifest, digest, version and document without claiming a history comparison.
Subsequent changes must use a base comparison to enforce revision monotonicity.
