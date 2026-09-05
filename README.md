# freellmpool

A maintained local gateway for legitimate free LLM allowances. Coding agents use one endpoint; the gateway selects an eligible model, reserves shared quota, and tries another provider when capacity is unavailable.

Free allowances are finite. Account eligibility, prices, model discovery and protocol support are checked separately. Unknown or exhausted capacity stays explicit; the gateway does not silently switch to a paid fallback.

[FAQ](FAQ.md) · [Setup guide](integrations/setup/README.md) · [Maintenance](docs/maintenance.md)

## Install and set up

Requires Python 3.11+ and Git. Install this fork from source:

```sh
git clone https://github.com/pauljones0/freellmpool.git
cd freellmpool
sh integrations/setup/bootstrap.sh
```

The wizard opens official provider pages, explains required account details, and accepts keys privately. Skip a provider or resume with `freellmpool setup --resume`; use `freellmpool setup --provider groq` for one provider. Setup offers only providers with reviewed free routes.

Setup prepares an authenticated loopback service and supported client profiles. Use `freellmpool setup --no-start` to prepare files without starting services. See [setup and rollback](integrations/setup/README.md).

## Connect a coding agent

```sh
opencode-free
hermes-free
```

These profiles route main, helper, delegated-agent and fallback calls through the gateway. In T3, select its configured OpenCode adapter and `freellmpool/auto`. Regenerate client configuration with `freellmpool setup-clients`.

Managed routing currently admits reviewed chat routes. Other OpenAI-compatible clients use:

| Setting | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:8080/v1` |
| Model | `auto` |
| API key | The private local gateway key in `~/.config/freellmpool/proxy.key` |

Keep independent paid fallbacks disabled in clients using the free profile. MCP hosts can run `freellmpool mcp`. See [agent integration](docs/AGENTS.md), the [metaswarm adapter](integrations/metaswarm/), and [protocol support](docs/PROTOCOL_CONFORMANCE.md).

## Operate and maintain

```sh
freellmpool status
freellmpool maintenance
freellmpool maintenance --refresh
freellmpool update
freellmpool verify --limit 4
```

`maintenance` shows the next actions; `--refresh` checks supported account observations, catalogs, official sources and reviewed policy updates. `update` refreshes listings. `verify` spends a bounded amount of eligible free quota on synthetic protocol checks.

Daily local maintenance keeps private observations on your machine and reports new actionable problems once. The public GitHub workflow checks public models, prices and sources without provider credentials or inference. It records review proposals and deduplicated issues; changed terms require review before eligibility or allowances expand.

Reviewed data-only policy updates preserve local exclusions and shared consumption. Failed checks retain the last valid evidence with its original age. Unsupported account APIs and opaque quotas remain unknown. GitHub schedules can be delayed or disabled after inactivity; local checks continue independently.

Read [maintenance and recovery](docs/maintenance.md), [API coverage](docs/api-coverage.md), and [provider evidence](docs/provider-registry.md) for supported APIs, timers, policy review and rollback.

## Develop

```sh
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Changes to free eligibility or limits need authoritative evidence and regression tests. Follow [CONTRIBUTING.md](CONTRIBUTING.md), including the separate line and branch coverage gates. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Provenance and license

This MIT-licensed fork builds on [0xzr/freellmpool](https://github.com/0xzr/freellmpool) and its 0.13.0 compatibility baseline. Original attribution is retained in [LICENSE](LICENSE).

Install this repository to obtain the maintained gateway, source version 0.14.0. This fork does not claim a new PyPI, npm, MCP Registry or container release. Historical release assets, the [legacy guide](docs/legacy-0.13-guide.md), and [Spanish guide](README.es.md) describe earlier compatibility behavior; their catalog counts are not current capacity.
