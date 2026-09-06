# Contributing to freellmpool

Thanks for helping! The two highest-value contributions are **adding free
providers** and **keeping the existing catalog accurate** as free tiers drift.

## Dev setup

```bash
git clone https://github.com/pauljones0/freellmpool.git
cd freellmpool
python -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
ruff check .
pytest          # 0 network calls; provider traffic is faked
```

Focused loop while you are working:

```bash
ruff check .
pytest tests/test_cli.py              # or the smallest relevant test file
scripts/check-counts                  # when README/docs count claims change
python3 scripts/validate_catalog.py   # when providers.toml changes
```

Useful release-readiness checks:

```bash
python3 scripts/stress_proxy.py --profile ci
python3 scripts/check_release_ready.py --skip-build
python3 scripts/check_release_ready.py
```

`stress_proxy.py` starts a local fake-backed proxy and sends mixed chat,
streaming, embeddings, Responses, Anthropic Messages, transcription, models,
and health traffic through the real HTTP server. `check_release_ready.py`
cross-checks version/provider/model-count metadata; without `--skip-build` it
also builds the sdist/wheel, runs `twine check`, and fresh-installs the wheel.

Good first issue drafts live in [`docs/GOOD_FIRST_ISSUES.md`](docs/GOOD_FIRST_ISSUES.md).
They include context, pointers, acceptance checks, labels, and the exact
maintainer commands for filing them.

## Adding a provider

The maintained runtime uses [`provider_registry.json`](src/freellmpool/provider_registry.json)
plus validated model discovery and private account evidence. A provider needs
official signup/key instructions, a bounded authoritative discovery adapter,
dated free-grant evidence, and correctly scoped quota/cost rules. Unknown or
billable access stays excluded. See the [registry guide](docs/provider-registry.md).

The public maintenance and local verification contracts are documented in
[`docs/CATALOG_SENTINEL.md`](docs/CATALOG_SENTINEL.md). The older protected
GitHub credential/probe workflow is retired; do not configure its secret map
for the new public workflow. Inference checks run locally through the same
free policy and ledger as ordinary traffic.
Protocol-feature verification, bounded canary rules, and strict exact-pin
admission are documented in
[`docs/PROTOCOL_CONFORMANCE.md`](docs/PROTOCOL_CONFORMANCE.md).

The old [`providers.toml`](src/freellmpool/providers.toml) remains the static
compatibility catalog. A TOML block alone does not admit a provider to the
maintained gateway. Its legacy shape is:

```toml
[[provider]]
id = "myprovider"
label = "My Provider"
adapter = "openai"                       # "openai" | "gemini" | "cloudflare"
base_url = "https://api.myprovider.ai/v1"
key_env = "MYPROVIDER_API_KEY"           # env var the user sets; never a key
models = [
    { name = "some-model", rpd = 0 },    # rpd = free daily request hint, 0 = unknown
]
```

Rules of thumb:

- **Verified recurring free access only.** A free grant must have an enforced
  billing boundary. Neither a zero balance, a card on file, nor a local budget
  switch proves that an upstream cannot bill the request.
- **Never commit a key.** Only the *name* of the env var goes in the catalog.
- If the provider isn't OpenAI-compatible, it needs a small adapter in
  [`src/freellmpool/client.py`](src/freellmpool/client.py) (see the `gemini` one for
  a ~30-line template) and a unit test in `tests/`.
- Add the env var to [`.env.example`](.env.example) and the signup steps to
  [`docs/ACCOUNTS.md`](docs/ACCOUNTS.md).

## Fixing a stale limit or endpoint

Free tiers change constantly. Correct the relevant registry evidence, discovery
parser or account-specific rule with an official dated source and a meaningful
regression. Account, pricing, model and capability evidence expire independently;
refreshing inventory must not silently renew the other grants. Preserve explicit
user restrictions and the last valid snapshot after partial/failed discovery.

## Tests

Every code path is unit-tested without touching the network via an injected
fake transport (`tests/helpers.py`). Please keep it that way — new behavior
should come with a fake-backed test. Run `pytest` and `ruff check` before
opening a PR.

## Code of conduct

Be kind. Assume good faith. We're all here to make free LLMs easier to use.
