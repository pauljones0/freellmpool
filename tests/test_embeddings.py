"""Pooled embeddings: catalog, client.embed, Pool.embed, proxy route."""

from __future__ import annotations

import pytest

from freellmpool import client as C
from freellmpool.config import configured_embedders, load_embedders
from freellmpool.errors import AllProvidersExhausted, NoProvidersConfigured
from freellmpool.models import Model, Provider
from freellmpool.router import Pool


def _embedder(eid: str, key_env: str | None) -> Provider:
    return Provider(
        id=eid,
        label=eid,
        adapter="openai",
        base_url=f"https://{eid}.test/v1",
        key_env=key_env,
        models=(Model("emb-1"),),
    )


def _embed_body(dim: int = 3):
    return {"data": [{"embedding": [0.1] * dim}], "usage": {"prompt_tokens": 4}}


REVIEWED_EMBEDDERS = {
    "ovh": ("Qwen3-Embedding-8B",),
    "mistral": ("mistral-embed",),
    "cloudflare": ("@cf/baai/bge-small-en-v1.5",),
}


def test_bundled_embedder_catalog_has_reviewed_free_routes():
    from freellmpool.free_policy import _matches
    from freellmpool.provider_registry import load_registry

    catalog = load_embedders()
    assert {e.id for e in catalog} == set(REVIEWED_EMBEDDERS)
    registry = load_registry()
    for embedder in catalog:
        assert embedder.models
        assert [m.name for m in embedder.models] == list(
            REVIEWED_EMBEDDERS[embedder.id]
        )
        for model in embedder.models:
            assert any(
                grant["hard_free_boundary"] is True
                and "embedding" in grant["allowed_modalities"]
                and _matches(grant["model_selector"], {"id": model.name})
                for grant in registry[embedder.id]["grants"]
            ), f"Bundled embedder lacks reviewed free coverage: {embedder.id}/{model.name}"


def _registry_provider(pid):
    from freellmpool.provider_registry import load_registry

    return load_registry()[pid]


def _fresh_now(pid):
    from datetime import datetime

    provider = _registry_provider(pid)
    checked = min(
        datetime.fromisoformat(row["checked_at"]) for row in provider["evidence"]
    )
    return checked.timestamp() + 3600


def _fresh_account(pid):
    provider = _registry_provider(pid)
    checked = min(row["checked_at"] for row in provider["evidence"])
    expires = max(row["expires_at"] for row in provider["evidence"])
    grant = next(
        g
        for g in provider["grants"]
        if "embedding" in g["allowed_modalities"]
    )
    if not grant.get("requires_account_evidence"):
        return {}
    return {
        "tier": grant["required_account_tier"],
        "verified_at": checked,
        "expires_at": expires,
    }


@pytest.mark.parametrize("pid", sorted(REVIEWED_EMBEDDERS))
def test_reviewed_embedding_grant_admits_exact_model(pid):
    from freellmpool.free_policy import admit

    provider = _registry_provider(pid)
    now = _fresh_now(pid)
    for model_name in REVIEWED_EMBEDDERS[pid]:
        model = {"id": model_name, "modalities": ["embedding"]}
        account = _fresh_account(pid)
        assert admit(provider, model, account, modality="embedding", now=now).allowed


@pytest.mark.parametrize("pid", sorted(REVIEWED_EMBEDDERS))
def test_reviewed_embedding_grant_denies_chat_and_unlisted_models(pid):
    from freellmpool.free_policy import admit, model_matches_grant

    provider = _registry_provider(pid)
    now = _fresh_now(pid)
    account = _fresh_account(pid)
    grants = [
        g
        for g in provider["grants"]
        if "embedding" in g["allowed_modalities"]
    ]
    assert grants
    for grant in grants:
        assert not model_matches_grant(
            grant, {"id": REVIEWED_EMBEDDERS[pid][0], "modalities": ["chat"]}
        )
        assert not model_matches_grant(
            grant, {"id": "unlisted-model", "modalities": ["embedding"]}
        )
    assert not admit(
        provider,
        {"id": "unlisted-model", "modalities": ["embedding"]},
        account,
        modality="embedding",
        now=now,
    ).allowed


@pytest.mark.parametrize("pid", ["mistral", "cloudflare"])
def test_conditional_embedding_grant_requires_free_tier(pid):
    from freellmpool.free_policy import admit

    provider = _registry_provider(pid)
    now = _fresh_now(pid)
    model = {"id": REVIEWED_EMBEDDERS[pid][0], "modalities": ["embedding"]}
    assert not admit(provider, model, {}, modality="embedding", now=now).allowed
    bad = dict(_fresh_account(pid), tier="paid")
    assert not admit(provider, model, bad, modality="embedding", now=now).allowed


def test_embedder_catalog_loads(tmp_path):
    path = tmp_path / "embedders.toml"
    path.write_text('''
[[embedder]]
id = "alpha"
base_url = "https://alpha.test/v1"
key_env = "ALPHA_KEY"
models = [{ name = "emb-large", context = 8192 }, { name = "emb-small", rpd = 100 }]
[[embedder]]
id = "keyless"
base_url = "https://keyless.test/v1"
auth = "none"
models = [{ name = "emb-fallback" }]
[[provider]]
id = "chat-only"
base_url = "https://chat.test/v1"
models = [{ name = "chat" }]
''')
    cat = load_embedders(path)
    assert [e.id for e in cat] == ["alpha", "keyless"]
    assert [m.name for m in cat[0].models] == ["emb-large", "emb-small"]
    assert cat[0].models[0].context == 8192
    assert cat[0].models[1].rpd == 100
    assert cat[0].key_env == "ALPHA_KEY"
    assert cat[1].key_env is None and cat[1].auth == "none"


def test_configured_embedders_filter():
    cat = [_embedder("alpha", "A_KEY"), _embedder("beta", "B_KEY"), _embedder("keyless", None)]
    assert [e.id for e in configured_embedders(cat, {"A_KEY": "x"})] == ["alpha", "keyless"]
    assert [e.id for e in configured_embedders(cat, {"A_KEY": "x", "B_KEY": "y"})] == [
        "alpha", "beta", "keyless",
    ]
    assert [e.id for e in configured_embedders(cat, {})] == ["keyless"]


def test_client_embed_shape():
    def post(url, headers, body, timeout):
        assert url.endswith("/embeddings")
        assert body["input"] == ["a", "b"]
        return C.HTTPResult(200, {"data": [{"embedding": [1, 2]}, {"embedding": [3, 4]}]}, "")

    e = _embedder("x", "X_KEY")
    reply = C.embed(e, "emb-1", ["a", "b"], api_key="k", env={}, post=post)
    assert reply.vectors == [[1, 2], [3, 4]]
    assert reply.provider_id == "x"


def test_client_embed_supplies_nvidia_input_type():
    def post(url, headers, body, timeout):
        assert body["input_type"] == "query"
        return C.HTTPResult(200, _embed_body(), "")

    e = _embedder("nvidia", "NVIDIA_API_KEY")
    C.embed(e, "nvidia/llama-nemotron-embed-1b-v2", ["a"], api_key="k", env={}, post=post)


def test_pool_embed_failover():
    def post(url, headers, body, timeout):
        if "alpha.test" in url:
            return C.HTTPResult(429, {"error": {"message": "rl"}}, "")
        return C.HTTPResult(200, _embed_body(), "")

    embedders = [_embedder("alpha", "A_KEY"), _embedder("beta", "B_KEY")]
    pool = Pool([], env={"A_KEY": "a", "B_KEY": "b"}, post=post, embedders=embedders)
    reply = pool.embed("hello")
    assert reply.provider_id == "beta"  # alpha 429 → failover
    assert len(reply.vectors) == 1


def test_pool_embed_skips_disabled_models():
    seen = []

    def post(url, headers, body, timeout):
        seen.append(body["model"])
        return C.HTTPResult(200, _embed_body(), "")

    emb = _embedder("alpha", "A_KEY")
    emb = Provider(**{**emb.__dict__, "models": (Model("retired", enabled=False), Model("working"))})
    reply = Pool([], env={"A_KEY": "a"}, post=post, embedders=[emb]).embed("hello")
    assert reply.model == "working"
    assert seen == ["working"]


def test_pool_embed_no_embedders_raises():
    pool = Pool([], env={}, embedders=[])
    with pytest.raises(NoProvidersConfigured):
        pool.embed("hi")


def test_pool_embed_all_models_disabled_raises_no_providers():
    emb = _embedder("alpha", "A_KEY")
    emb = Provider(**{**emb.__dict__, "models": (Model("retired", enabled=False),)})
    pool = Pool([], env={"A_KEY": "a"}, embedders=[emb])
    with pytest.raises(NoProvidersConfigured):
        pool.embed("hi")


def test_pool_embed_all_fail_raises():
    def post(url, headers, body, timeout):
        return C.HTTPResult(500, {}, "")

    pool = Pool([], env={"A_KEY": "a"}, post=post, embedders=[_embedder("alpha", "A_KEY")])
    with pytest.raises(AllProvidersExhausted) as exc_info:
        pool.embed("hi")
    assert exc_info.value.client_status is None


@pytest.mark.parametrize(
    ("status", "message", "expected_client_status"),
    [
        (400, "bad embedding input", 400),
        (402, "You have depleted your monthly included credits", None),
    ],
)
def test_pool_embed_classifies_nonretryable_error(status, message, expected_client_status):
    def post(url, headers, body, timeout):
        return C.HTTPResult(status, {"error": {"message": message}}, "")

    pool = Pool([], env={"A_KEY": "a"}, post=post, embedders=[_embedder("alpha", "A_KEY")])
    with pytest.raises(AllProvidersExhausted) as exc_info:
        pool.embed("hi")

    assert exc_info.value.client_status == expected_client_status
    if expected_client_status is not None:
        assert message in (exc_info.value.client_message or "")


def test_managed_embed_accounts_input_only_neuron_costs(tmp_path):
    """Embedding-only cost rows (no output-token rate) must still reserve."""
    from datetime import UTC, datetime, timedelta

    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool
    from freellmpool.models import Model

    now = datetime.now(UTC)
    checked = now.isoformat()
    expires = (now + timedelta(days=7)).isoformat()
    providers = [
        Provider(
            "cf",
            "cf",
            "openai",
            "https://cf.test/v1",
            (Model("emb"),),
            auth="none",
        )
    ]
    registry = {
        "cf": {
            "id": "cf",
            "display_name": "cf",
            "api_base_url": "https://cf.test/v1",
            "credential_env": None,
            "discovery": {"parser": "openai", "url": "https://cf.test/v1/models"},
            "evidence": [
                {
                    "id": "price",
                    "checked_at": checked,
                    "expires_at": expires,
                    "status": "verified",
                }
            ],
            "grants": [
                {
                    "id": "free",
                    "kind": "zero_price",
                    "status": "verified",
                    "evidence_ids": ["price"],
                    "model_selector": {"kind": "allowlist", "models": ["emb"]},
                    "paid_overage_possible": False,
                    "requires_account_evidence": False,
                    "allowed_modalities": ["embedding"],
                }
            ],
            "limits": [
                {
                    "id": "neurons_daily",
                    "scope": "account",
                    "metric": "neurons",
                    "algorithm": "calendar_day",
                    "capacity": 7500,
                    "window_seconds": 86400,
                    "grant_ids": ["free"],
                }
            ],
            "model_costs": {"emb": {"neurons_per_input_token": "0.5"}},
        }
    }
    snapshot = {
        "schema": 1,
        "generation": "test",
        "providers": {
            "cf": {
                "checked_at": checked,
                "status": "ok",
                "complete": True,
                "models": [
                    {
                        "id": "emb",
                        "modalities": ["embedding"],
                        "pricing": {"input": "0", "output": "0"},
                    }
                ],
            }
        },
    }

    def post(url, headers, body, timeout):
        assert url.endswith("/embeddings")
        return C.HTTPResult(200, {"data": [{"embedding": [0.1, 0.2]}]}, "")

    pool = ManagedPool(
        providers,
        registry=registry,
        discovery=snapshot,
        accounts={},
        env={"FREELLMPOOL_WAIT_SECONDS": "0"},
        ledger=AllowanceLedger(tmp_path / "allowances.db"),
        post=post,
    )
    reply = pool.embed(["hi"], providers=["cf"])
    assert reply.vectors == [[0.1, 0.2]]
