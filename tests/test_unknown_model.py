"""G28: removed/unknown model pins fail loudly with a recovering next step.

A pin that names no catalog model must not report "all providers exhausted"
(the opposite of the truth when hundreds of routes are ready). Offline
fixtures only: no test here touches the network.
"""

import asyncio

import pytest
from helpers import make_post
from test_managed_runtime import make_pool as make_managed_pool

from freellmpool import UnknownModel
from freellmpool.errors import AllProvidersExhausted, NoProvidersConfigured
from freellmpool.router import Pool


def _async_post(script):
    sync = make_post(script)

    async def apost(url, headers, body, timeout):
        return sync(url, headers, body, timeout)

    return apost


# --- managed (sync ask / library Pool) ---


def test_managed_pin_miss_names_pin_with_models_update_pointers(tmp_path):
    calls = []
    pool = make_managed_pool(tmp_path, post=lambda *a: calls.append(a) or None)
    with pytest.raises(UnknownModel) as error:
        pool.ask("hi", model="no-such-model")
    message = str(error.value)
    assert "unknown model 'no-such-model'" in message
    assert "freellmpool models" in message
    assert "freellmpool update" in message
    assert "exhausted" not in message
    assert error.value.client_status == 404
    assert error.value.client_message == message
    assert isinstance(error.value, AllProvidersExhausted)
    assert calls == []


def test_managed_provider_model_pin_reconstructed(tmp_path):
    pool = make_managed_pool(tmp_path)
    with pytest.raises(UnknownModel) as error:
        pool.ask("hi", model="no-such-model", providers=["alpha"])
    assert "unknown model 'alpha/no-such-model'" in str(error.value)


def test_managed_multi_provider_pin_has_no_prefix(tmp_path):
    pool = make_managed_pool(tmp_path)
    with pytest.raises(UnknownModel) as error:
        pool.ask("hi", model="no-such-model", providers=["alpha", "beta"])
    assert "unknown model 'no-such-model'" in str(error.value)


def test_managed_empty_catalog_keeps_generic_403(tmp_path):
    pool = make_managed_pool(tmp_path, ids=())
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi", model="no-such-model")
    assert type(error.value) is AllProvidersExhausted
    assert error.value.client_status == 403


def test_managed_unpinned_feature_miss_keeps_generic(tmp_path):
    pool = make_managed_pool(tmp_path)
    tools = [{"type": "function", "function": {"name": "f"}}]
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi", tools=tools)
    assert type(error.value) is AllProvidersExhausted
    assert "unknown model" not in str(error.value)


def test_managed_pin_is_sanitized_and_truncated(tmp_path):
    pool = make_managed_pool(tmp_path)
    with pytest.raises(UnknownModel) as error:
        pool.ask("hi", model="a\nb\x00c" + "x" * 200)
    message = str(error.value)
    assert "\n" not in message and "\x00" not in message
    assert len(message.split("'")[1]) <= 121


def test_managed_pseudo_model_still_auto_routes(tmp_path):
    pool = make_managed_pool(tmp_path)
    assert pool.ask("hi", model="free").text == "OK"


def test_managed_discovery_only_paid_pin_keeps_generic(tmp_path):
    """A paid model the discovery generation lists is known-but-unserved (015)."""
    from test_managed_runtime import fixture_data

    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool

    providers, registry, snapshot = fixture_data(("alpha",))
    snapshot["providers"]["alpha"]["models"].append(
        {"id": "ghost-paid", "modalities": ["chat"], "pricing": {"input": "1", "output": "1"}})
    calls = []
    pool = ManagedPool(providers, registry=registry, discovery=snapshot, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=lambda *args: calls.append(args))
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi", model="ghost-paid")
    assert type(error.value) is AllProvidersExhausted
    assert error.value.client_status == 403
    assert calls == []


def test_managed_explicit_provider_only_pin_keeps_generic(tmp_path):
    from test_managed_runtime import fixture_data

    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool
    from freellmpool.models import Model, Provider

    providers, registry, snapshot = fixture_data(("alpha",))
    alpha = providers[0]
    ghosted = Provider(id=alpha.id, label=alpha.label, adapter=alpha.adapter,
                       base_url=alpha.base_url, key_env=alpha.key_env,
                       models=tuple(alpha.models) + (Model("ghost-explicit"),))
    calls = []
    pool = ManagedPool([ghosted], registry=registry, discovery=snapshot, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=lambda *args: calls.append(args))
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi", model="ghost-explicit")
    assert type(error.value) is AllProvidersExhausted
    assert calls == []


def test_managed_static_catalog_only_pin_still_404(tmp_path):
    """The static TOML catalog never decides identity; the generation does."""
    from freellmpool.config import load_catalog
    from freellmpool.router import PSEUDO_MODELS

    fixture_models = {"free", "paid"}
    static_only = next(m.name for p in load_catalog() for m in p.models
                       if m.name not in PSEUDO_MODELS and m.name not in fixture_models)
    pool = make_managed_pool(tmp_path)
    with pytest.raises(UnknownModel) as error:
        pool.ask("hi", model=static_only)
    assert error.value.client_status == 404


# --- router (legacy Pool.chat / stream_chat) ---


def test_router_pin_miss_raises_unknown_model_404(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(UnknownModel) as error:
        pool.ask("hello", model="no-such-model")
    assert "unknown model 'no-such-model'" in str(error.value)
    assert error.value.client_status == 404


def test_router_provider_excluded_existing_model_stays_generic(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(NoProvidersConfigured):
        pool.ask("hello", model="alpha-small", providers=["beta"])


def test_router_pin_plus_feature_miss_stays_no_providers(env, quota, tmp_path):
    from freellmpool.conformance import FEATURE_TOOLS, STATUS_UNSUPPORTED, ConformanceStore
    from freellmpool.models import Model, Provider

    solo = Provider(id="alpha", label="Alpha", adapter="openai",
                    base_url="https://alpha.test/v1", key_env="ALPHA_KEY",
                    models=(Model("alpha-small", rpd=2),))
    store = ConformanceStore(tmp_path / "conformance.json")
    store.record(solo, "alpha-small", FEATURE_TOOLS, status=STATUS_UNSUPPORTED,
                 classification="unsupported")
    pool = Pool([solo], quota=quota, env=env, post=make_post({}), conformance=store)
    tools = [{"type": "function", "function": {"name": "f"}}]
    with pytest.raises(NoProvidersConfigured):
        pool.chat([{"role": "user", "content": "hi"}], model="alpha-small", tools=tools)


def test_router_pseudo_model_exempt(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(NoProvidersConfigured):
        pool.ask("hello", model="free")


def test_router_static_catalog_only_pin_still_404(providers, env, quota):
    """Router identity binds to constructor providers, never a reloaded catalog."""
    from freellmpool.config import load_catalog
    from freellmpool.router import PSEUDO_MODELS

    fixture_models = {m.name for p in providers for m in p.models}
    static_only = next(m.name for p in load_catalog() for m in p.models
                       if m.name not in PSEUDO_MODELS and m.name not in fixture_models)
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(UnknownModel) as error:
        pool.ask("hello", model=static_only)
    assert error.value.client_status == 404


def test_router_stream_pin_miss_raises_unknown_model(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(UnknownModel):
        list(pool.stream_chat([{"role": "user", "content": "hi"}], model="no-such-model"))


def test_router_warm_cache_serves_stale_over_pin_miss(providers, env, quota, tmp_path):
    from freellmpool.cache import Cache

    cache = Cache(ttl=999.0, path=tmp_path / "c.db")
    warm = Pool(providers, quota=quota, env=env, post=make_post({}), cache=cache)
    assert warm.ask("hello", model="alpha-small").text == "ok"
    trimmed = Pool([p for p in providers if p.id != "alpha"], quota=quota, env=env,
                   post=make_post({}), cache=cache)
    reply = trimmed.ask("hello", model="alpha-small")
    assert reply.text == "ok" and reply.cached is True


# --- aio (AsyncPool twin) ---


def test_aio_pin_miss_raises_unknown_model(providers, env, quota):
    from freellmpool.aio import AsyncPool

    pool = AsyncPool(Pool(providers, quota=quota, env=env), apost=_async_post({}))
    with pytest.raises(UnknownModel) as error:
        asyncio.run(pool.achat([{"role": "user", "content": "hi"}], model="no-such-model"))
    assert error.value.client_status == 404


def test_aio_pin_plus_feature_miss_stays_no_providers(env, quota, tmp_path):
    from freellmpool.aio import AsyncPool
    from freellmpool.conformance import FEATURE_TOOLS, STATUS_UNSUPPORTED, ConformanceStore
    from freellmpool.models import Model, Provider

    solo = Provider(id="alpha", label="Alpha", adapter="openai",
                    base_url="https://alpha.test/v1", key_env="ALPHA_KEY",
                    models=(Model("alpha-small", rpd=2),))
    store = ConformanceStore(tmp_path / "conformance.json")
    store.record(solo, "alpha-small", FEATURE_TOOLS, status=STATUS_UNSUPPORTED,
                 classification="unsupported")
    inner = Pool([solo], quota=quota, env=env, conformance=store)
    pool = AsyncPool(inner, apost=_async_post({}))
    tools = [{"type": "function", "function": {"name": "f"}}]
    with pytest.raises(NoProvidersConfigured):
        asyncio.run(pool.achat([{"role": "user", "content": "hi"}], model="alpha-small",
                               tools=tools))


# --- CLI surfacing ---


def test_cli_ask_pin_miss_names_pin_rc4(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool as RouterPool

    def ask(self, prompt, **kwargs):
        raise UnknownModel([], pin=kwargs.get("model"))

    monkeypatch.setattr(RouterPool, "from_default_config",
                        classmethod(lambda cls: type("P", (), {"ask": ask})()))
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")
    assert main(["ask", "hello", "-m", "no-such-model"]) == 4
    err = capsys.readouterr().err
    assert "unknown model 'no-such-model'" in err
    assert "freellmpool models" in err and "freellmpool update" in err


def test_cli_ask_exhaustion_shows_client_message_tail(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool as RouterPool

    def ask(self, prompt, **kwargs):
        raise AllProvidersExhausted([("a/m", "HTTP 429")], client_status=429,
                                    client_message="retry after the reported cooldown")

    monkeypatch.setattr(RouterPool, "from_default_config",
                        classmethod(lambda cls: type("P", (), {"ask": ask})()))
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")
    assert main(["ask", "hello"]) == 4
    err = capsys.readouterr().err
    assert "a/m: HTTP 429" in err
    assert "retry after the reported cooldown" in err


def test_cli_ask_second_opinion_pin_miss_rc4_no_traceback(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool as RouterPool

    class FakePool:
        def rank_targets(self, *args, **kwargs):
            raise UnknownModel([], pin="no-such-model")

    monkeypatch.setattr(RouterPool, "from_default_config", classmethod(lambda cls: FakePool()))
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")
    assert main(["ask", "hello", "-m", "no-such-model", "--second-opinion"]) == 4
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "unknown model 'no-such-model'" in err


# --- router rank_targets / embed / transcribe (review M1/m1) ---


def _messages():
    return [{"role": "user", "content": "hi"}]


def test_router_rank_targets_pin_miss_raises_unknown_model(providers, env, quota):
    """Legacy rank_targets classifies pins like the chat path (M1)."""
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    with pytest.raises(UnknownModel) as error:
        pool.rank_targets(_messages(), model="no-such-model")
    assert "unknown model 'no-such-model'" in str(error.value)
    assert error.value.client_status == 404


def test_router_rank_targets_valid_pin_returns_targets(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    assert pool.rank_targets(_messages(), model="alpha-small") != []


def test_router_rank_targets_no_model_returns_all(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    assert pool.rank_targets(_messages()) != []


def test_router_rank_targets_pseudo_model_exempt_but_empty(providers, env, quota):
    """Pseudo-models never raise (exempt); legacy still ranks nothing for them."""
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    assert pool.rank_targets(_messages(), model="free") == []


def test_router_rank_targets_empty_pool_stays_empty(env, quota):
    pool = Pool([], quota=quota, env=env, post=make_post({}))
    assert pool.rank_targets(_messages(), model="no-such-model") == []


def _embedder(eid="alpha"):
    from freellmpool.models import Model, Provider

    return Provider(id=eid, label=eid, adapter="openai",
                    base_url=f"https://{eid}.test/v1", key_env="A_KEY",
                    models=(Model("emb-1"),))


def test_router_embed_pin_miss_raises_unknown_model(quota):
    """Legacy embed classifies pins against embedder models (m1)."""
    pool = Pool([], quota=quota, env={"A_KEY": "a"}, embedders=[_embedder()])
    with pytest.raises(UnknownModel) as error:
        pool.embed("hello", model="no-such-model")
    assert "unknown model 'no-such-model'" in str(error.value)
    assert error.value.client_status == 404


def test_router_embed_no_embedder_stays_generic(quota):
    pool = Pool([], quota=quota, env={}, embedders=[])
    with pytest.raises(NoProvidersConfigured):
        pool.embed("hello", model="no-such-model")


def _transcriber(tid="alpha"):
    from freellmpool.models import Model, Provider

    return Provider(id=tid, label=tid, adapter="openai",
                    base_url=f"https://{tid}.test/v1", key_env="A_KEY",
                    models=(Model("whisper-1"),))


def test_router_transcribe_pin_miss_raises_unknown_model(quota):
    """Legacy transcribe classifies pins against transcriber models (m1)."""
    pool = Pool([], quota=quota, env={"A_KEY": "a"}, transcribers=[_transcriber()])
    with pytest.raises(UnknownModel) as error:
        pool.transcribe(b"AUDIO", "a.wav", model="no-such-model")
    assert "unknown model 'no-such-model'" in str(error.value)
    assert error.value.client_status == 404


def test_router_transcribe_no_transcriber_stays_generic(quota):
    pool = Pool([], quota=quota, env={}, transcribers=[])
    with pytest.raises(NoProvidersConfigured):
        pool.transcribe(b"AUDIO", "a.wav", model="no-such-model")


# --- proxy (lives in tests/test_proxy.py: needs its module-local server fixture) ---
