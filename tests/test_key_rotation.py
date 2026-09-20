"""G7: numbered key slots per provider with sticky-until-429 rotation."""

from __future__ import annotations

import asyncio

import pytest
from test_managed_runtime import fixture_data

from freellmpool.allowances import AllowanceLedger
from freellmpool.client import HTTPResult
from freellmpool.errors import AllProvidersExhausted
from freellmpool.key_rotation import KeyRotator
from freellmpool.managed import ManagedPool
from freellmpool.models import Model, Provider


def keyed_providers(ids=("alpha",)):
    providers = []
    for pid in ids:
        providers.append(
            Provider(pid, pid, "openai", f"https://{pid}.test/v1",
                     (Model("free", context=32000),), auth="key",
                     key_env=f"{pid.upper()}_API_KEY")
        )
    return providers


def make_keyed_pool(tmp_path, env, *, post=None, clock=None, ids=("alpha",)):
    _, registry, snapshot = fixture_data(ids, 100)
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return ManagedPool(
        keyed_providers(ids), registry=registry, discovery=snapshot, accounts={},
        env={"FREELLMPOOL_WAIT_SECONDS": "0", **env},
        ledger=AllowanceLedger(tmp_path / "allowances.db"),
        post=post or (lambda *args: _ok()), **kwargs,
    )


def _ok(text="OK"):
    return HTTPResult(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 1}}, "")


def _limited(retry_after="1"):
    return HTTPResult(429, {"error": {"message": "slow down"}}, "",
                      headers={"Retry-After": retry_after})


def test_api_keys_enumerates_numbered_slots():
    p = Provider("alpha", "alpha", "openai", "https://alpha.test/v1", (), key_env="ALPHA_API_KEY")
    env = {"ALPHA_API_KEY": "one", "ALPHA_API_KEY_2": "two", "ALPHA_API_KEY_3": "three"}
    assert p.api_keys(env) == ("one", "two", "three")
    assert p.api_key(env) == "one"  # first slot keeps legacy behavior


def test_api_keys_tolerates_gaps_and_blanks():
    p = Provider("alpha", "alpha", "openai", "https://alpha.test/v1", (), key_env="ALPHA_API_KEY")
    env = {"ALPHA_API_KEY": "one", "ALPHA_API_KEY_2": "", "ALPHA_API_KEY_3": "three"}
    assert p.api_keys(env) == ("one", "three")


def test_rotator_is_sticky_until_cooled():
    rot = KeyRotator()
    assert rot.usable_slots("alpha", 2, now=100.0) == [0, 1]
    assert rot.usable_slots("alpha", 2, now=101.0) == [0, 1]  # sticky, no drift
    rot.cool("alpha", 0, until=200.0)
    assert rot.usable_slots("alpha", 2, now=150.0) == [1]
    rot.advance("alpha", 2)
    assert rot.usable_slots("alpha", 2, now=150.0) == [1]
    assert rot.usable_slots("alpha", 2, now=250.0) == [1, 0]  # cooldown expired


def test_managed_rotates_to_second_key_on_429(tmp_path):
    seen = []

    def post(url, headers, body, timeout):
        seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == "Bearer key-one":
            return _limited()
        return _ok("via-two")

    pool = make_keyed_pool(tmp_path, {"ALPHA_API_KEY": "key-one", "ALPHA_API_KEY_2": "key-two"}, post=post)
    assert pool.ask("hi").text == "via-two"
    assert seen == ["Bearer key-one", "Bearer key-two"]


def test_cooled_key_is_skipped_without_spend(tmp_path):
    seen = []

    def post(url, headers, body, timeout):
        seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == "Bearer key-one":
            return _limited(retry_after="3600")
        return _ok()

    pool = make_keyed_pool(tmp_path, {"ALPHA_API_KEY": "key-one", "ALPHA_API_KEY_2": "key-two"}, post=post)
    assert pool.ask("hi").text == "OK"
    assert seen == ["Bearer key-one", "Bearer key-two"]
    # second request goes straight to key two; key one is not re-spent
    assert pool.ask("hi").text == "OK"
    assert seen == ["Bearer key-one", "Bearer key-two", "Bearer key-two"]


def test_all_slots_exhausted_reports_each_slot(tmp_path):
    pool = make_keyed_pool(
        tmp_path, {"ALPHA_API_KEY": "key-one", "ALPHA_API_KEY_2": "key-two"},
        post=lambda *args: _limited(),
    )
    with pytest.raises(AllProvidersExhausted) as error:
        pool.ask("hi")
    assert error.value.client_status == 429
    assert error.value.retry_after is not None
    joined = " ".join(reason for _, reason in error.value.attempts)
    assert "key slot 1" in joined and "key slot 2" in joined


def test_snapshot_admits_route_when_only_slot_two_qualifies(tmp_path):
    from datetime import UTC, datetime, timedelta

    from freellmpool.free_policy import credential_fingerprint

    _, registry, snapshot = fixture_data(("alpha",), 100)
    grant = registry["alpha"]["grants"][0]
    grant["requires_account_evidence"] = True
    grant["required_account_tier"] = "free"
    now = datetime.now(UTC)
    accounts = {
        "alpha": {
            "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "credential_ref": credential_fingerprint("alpha", "key-two"),
            "tier": "free",
            "limits": [],
        }
    }

    def build(env):
        return ManagedPool(
            keyed_providers(), registry=registry, discovery=snapshot, accounts=accounts,
            env={"FREELLMPOOL_WAIT_SECONDS": "0", **env},
            ledger=AllowanceLedger(tmp_path / "allowances.db"),
        )

    assert build({"ALPHA_API_KEY": "key-one", "ALPHA_API_KEY_2": "key-two"}).snapshot().routes
    assert not build({"ALPHA_API_KEY": "key-one"}).snapshot().routes


def test_status_reports_key_depth(tmp_path):
    pool = make_keyed_pool(
        tmp_path,
        {"ALPHA_API_KEY": "key-one", "ALPHA_API_KEY_2": "key-two", "BETA_API_KEY": "solo"},
        ids=("alpha", "beta"),
    )
    assert pool.managed_status()["key_depth"] == {"alpha": 2, "beta": 1}


def test_keys_add_slot_writes_numbered_env(tmp_path, monkeypatch):
    from freellmpool.cli import main

    user_catalog = tmp_path / "providers.toml"
    config = tmp_path / "config.toml"
    inventory = tmp_path / "keys.toml"
    user_catalog.write_text(
        '[[provider]]\nid = "alpha"\nname = "Alpha"\nbase_url = "https://alpha.test/v1"\n'
        'key_env = "ALPHA_API_KEY"\n', encoding="utf-8")
    monkeypatch.setenv("FREELLMPOOL_CONFIG", str(user_catalog))
    monkeypatch.setenv("FREELLMPOOL_CONFIG_FILE", str(config))
    monkeypatch.setenv("FREELLMPOOL_KEYS_PATH", str(inventory))
    assert main(["keys", "add", "alpha", "--slot", "2", "--value", "second", "--yes"]) == 0
    assert 'ALPHA_API_KEY_2 = "second"' in config.read_text()
    assert "ALPHA_API_KEY_2" in inventory.read_text()


def test_status_prints_multi_key_providers(monkeypatch, capsys):
    from types import SimpleNamespace

    from freellmpool import managed_cli
    from freellmpool.managed import ManagedPool

    stub = SimpleNamespace(
        managed_status=lambda: {
            "eligible_routes": 2,
            "providers": [{"id": "alpha", "eligible": 1, "reason": "ready"}],
            "key_depth": {"alpha": 2},
        }
    )
    monkeypatch.setattr(ManagedPool, "from_default_config", classmethod(lambda cls: stub))
    assert managed_cli.cmd_status(SimpleNamespace(json=False)) == 0
    assert "Multi-key rotation: alpha=2 keys" in capsys.readouterr().out


def test_legacy_chat_rotates_to_second_key_on_429(providers, quota):
    from helpers import openai_body

    from freellmpool.router import Pool

    seen = []

    def post(url, headers, body, timeout):
        seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == "Bearer a":
            return _limited()
        return HTTPResult(200, openai_body("via-two"), "")

    pool = Pool(
        [p for p in providers if p.id == "alpha"],
        quota=quota,
        env={"ALPHA_KEY": "a", "ALPHA_KEY_2": "b"},
        post=post,
    )
    assert pool.chat([{"role": "user", "content": "hi"}]).text == "via-two"
    assert seen == ["Bearer a", "Bearer b"]


def test_async_rotates_to_second_key_on_429(tmp_path):
    from test_aio import _async_post  # noqa: PLC2701

    from freellmpool.aio import AsyncPool

    seen = []

    def rule(url, headers, body):
        seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == "Bearer key-one":
            return 429, {"error": {"message": "slow down"}}
        from helpers import openai_body

        return 200, openai_body("via-two")

    _, registry, snapshot = fixture_data(("alpha",), 100)
    sync = ManagedPool(
        keyed_providers(), registry=registry, discovery=snapshot, accounts={},
        env={"FREELLMPOOL_WAIT_SECONDS": "0", "ALPHA_API_KEY": "key-one",
             "ALPHA_API_KEY_2": "key-two"},
        ledger=AllowanceLedger(tmp_path / "allowances.db"),
    )
    pool = AsyncPool(sync, apost=_async_post({"alpha.test": rule}))
    assert asyncio.run(pool.aask("hi")).text == "via-two"
    assert seen == ["Bearer key-one", "Bearer key-two"]


def test_legacy_async_rotates_to_second_key_on_429(providers, quota):
    from helpers import openai_body
    from test_aio import _async_post  # noqa: PLC2701

    from freellmpool.aio import AsyncPool
    from freellmpool.router import Pool

    seen = []

    def rule(url, headers, body):
        seen.append(headers.get("Authorization"))
        if headers.get("Authorization") == "Bearer a":
            return 429, {"error": {"message": "slow down"}}
        return 200, openai_body("via-two")

    sync = Pool(
        [p for p in providers if p.id == "alpha"],
        quota=quota,
        env={"ALPHA_KEY": "a", "ALPHA_KEY_2": "b"},
    )
    pool = AsyncPool(sync, apost=_async_post({"alpha.test": rule}))
    assert asyncio.run(pool.aask("hi")).text == "via-two"
    assert seen == ["Bearer a", "Bearer b"]


def test_rotator_advance_is_thread_safe():
    """Concurrent advances must not lose cursor updates (cf. PrefixRoutes)."""
    import threading

    rot = KeyRotator()
    assert hasattr(rot, "_lock")

    def hammer():
        for _ in range(2000):
            rot.advance("alpha", 7)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert rot.usable_slots("alpha", 7, now=0.0)[0] == (8 * 2000) % 7
