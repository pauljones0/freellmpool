"""Plugin system: custom provider registration and custom adapters."""

from __future__ import annotations

import pytest
from helpers import make_post

from freellmpool import client as _client
from freellmpool import plugins
from freellmpool.models import Model, Provider, Reply
from freellmpool.router import Pool


@pytest.fixture(autouse=True)
def _clean_registries():
    plugins._reset_for_tests()
    yield
    plugins._reset_for_tests()


def test_register_provider_shows_up(providers, env, quota):
    custom = Provider(
        id="custom",
        label="Custom",
        adapter="openai",
        base_url="https://custom.test/v1",
        auth="none",
        models=(Model("custom-1"),),
    )
    plugins.register_provider(custom)
    assert any(p.id == "custom" for p in plugins.registered_providers())


def test_register_adapter_is_used_by_call():
    seen = {}

    def my_adapter(
        provider,
        model,
        messages,
        *,
        api_key,
        env,
        max_tokens,
        temperature,
        timeout,
        tools,
        tool_choice,
        post,
    ):
        seen["called"] = (provider.id, model)
        return Reply(text="from-plugin", provider_id=provider.id, model=model, raw={})

    plugins.register_adapter("weird", my_adapter)
    prov = Provider(
        id="w",
        label="W",
        adapter="weird",
        base_url="https://w.test",
        auth="none",
        models=(Model("w-1"),),
    )
    reply = _client.call(prov, "w-1", [{"role": "user", "content": "hi"}], api_key=None, env={})
    assert reply.text == "from-plugin"
    assert seen["called"] == ("w", "w-1")


def test_unknown_adapter_falls_back_to_openai(providers, env, quota):
    prov = Provider(
        id="z",
        label="Z",
        adapter="totally-unknown",
        base_url="https://z.test/v1",
        auth="none",
        models=(Model("z-1"),),
    )
    pool = Pool([prov], quota=quota, env={}, post=make_post({}))
    reply = pool.chat([{"role": "user", "content": "hi"}])
    assert reply.text == "ok"  # routed through the openai shape and parsed fine


def test_register_provider_rejects_non_provider():
    with pytest.raises(TypeError):
        plugins.register_provider("not a provider")  # type: ignore[arg-type]


def test_plugin_provider_merges_by_id(monkeypatch):
    # a plugin reusing a built-in id overrides it rather than duplicating, so
    # from_default_config never yields two providers with the same id.
    from freellmpool import router as router_mod

    builtin = Provider(
        id="dup",
        label="Builtin",
        adapter="openai",
        base_url="https://builtin.test/v1",
        auth="none",
        models=(Model("a"),),
    )
    plugin = Provider(
        id="dup",
        label="Plugin",
        adapter="openai",
        base_url="https://plugin.test/v1",
        auth="none",
        models=(Model("b"),),
    )
    monkeypatch.setattr(router_mod, "load_catalog", lambda: [builtin])
    plugins.register_provider(plugin)
    # Unreviewed third-party endpoints belong to the explicit low-level API.
    # The managed default requires sourced free policy before any dispatch.
    pool = Pool.from_default_config(env={"FREELLMPOOL_LEGACY_ROUTER": "1"})
    dups = [p for p in pool.providers if p.id == "dup"]
    assert len(dups) == 1
    assert dups[0].label == "Plugin"  # plugin won the id


def test_managed_default_does_not_admit_unreviewed_plugin(monkeypatch):
    provider = Provider(id="unreviewed", label="Plugin", adapter="openai",
                        base_url="https://unreviewed.test/v1", auth="none", models=(Model("free"),))
    plugins.register_provider(provider)
    pool = Pool.from_default_config(env={})
    assert pool.managed is True
    assert all(route.provider.id != "unreviewed" for route in pool.snapshot().routes)
