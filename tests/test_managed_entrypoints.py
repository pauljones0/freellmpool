"""Default public entry points must not accidentally select the old router."""

import asyncio

import pytest
from test_managed_runtime import fixture_data, successful

from freellmpool import AsyncPool, Pool
from freellmpool.cli import build_parser
from freellmpool.errors import AllProvidersExhausted


@pytest.fixture
def default_registry(monkeypatch):
    providers, registry, discovery = fixture_data(("alpha",), capacity=1)
    monkeypatch.setattr("freellmpool.provider_registry.load_registry", lambda env=None: registry)
    monkeypatch.setattr("freellmpool.discovery.load_discovery", lambda env: discovery)
    monkeypatch.setattr("freellmpool.managed.load_catalog", lambda: providers)


def test_default_pool_uses_managed_admission(default_registry):
    calls = []
    def post(*args):
        calls.append(args)
        return successful()
    pool = Pool.from_default_config(env={}, post=post)
    assert pool.managed is True
    pool.ask("hello")
    with pytest.raises(AllProvidersExhausted):
        pool.ask("hello")
    assert len(calls) == 1


def test_async_default_cannot_bypass_the_same_ledger(default_registry):
    calls = []
    async def post(*args):
        calls.append(args)
        return successful()

    async def run():
        base = Pool.from_default_config(env={})
        async with AsyncPool(base, apost=post) as pool:
            assert (await pool.aask("hello")).text == "OK"
            with pytest.raises(AllProvidersExhausted):
                await pool.aask("again")
            with pytest.raises(AllProvidersExhausted):
                await pool.aask("paid", model="paid", providers=["alpha"])
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("argv", [
    ["setup", "--resume"], ["setup", "--provider", "groq"],
    ["update", "--public-only"], ["verify", "--limit", "2"],
    ["status", "--json"], ["setup-clients"],
])
def test_simple_commands_have_real_parser_entrypoints(argv):
    args = build_parser().parse_args(argv)
    assert callable(args.func)
