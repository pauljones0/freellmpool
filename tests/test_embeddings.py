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


def test_bundled_embedder_catalog_has_no_reviewed_free_routes():
    assert load_embedders() == []


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
