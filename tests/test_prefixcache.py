"""Prefix-aware agent-loop cache (G22): provider-confirmed cached tokens cost nothing.

The gateway cannot skip sending an agent loop's growing prefix upstream, so the
honest cache is provider-side KV reuse, orchestrated gateway-side:
  * hash the stable prompt prefix and remember which target served it, so the
    next turn of the same loop lands on the already-warm target;
  * harvest provider-CONFIRMED cached-token counts from usage blocks;
  * deduct only confirmed cached tokens from token allowances;
  * expose hit rate + tokens avoided in stats.
"""

import pytest

from freellmpool.client import HTTPResult
from freellmpool.prefixcache import PrefixRoutes, cached_prompt_tokens, hash_prefix


def test_cached_tokens_openai_shape():
    usage = {"prompt_tokens": 1656, "completion_tokens": 3,
             "prompt_tokens_details": {"cached_tokens": 1536}}
    assert cached_prompt_tokens(usage) == 1536


def test_cached_tokens_anthropic_shape():
    assert cached_prompt_tokens({"input_tokens": 100, "cache_read_input_tokens": 60}) == 60


def test_cached_tokens_deepseek_shape():
    usage = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}
    assert cached_prompt_tokens(usage) == 80


def test_cached_tokens_gemini_shape():
    usage = {"promptTokenCount": 100, "cachedContentTokenCount": 60}
    assert cached_prompt_tokens(usage) == 60


@pytest.mark.parametrize("usage", [
    None, {}, {"prompt_tokens": 10},
    {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": True}},
    {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": "many"}},
    {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": -5}},
    {"prompt_tokens_details": ["cached_tokens"]},
    "usage",
])
def test_cached_tokens_malformed_is_zero(usage):
    assert cached_prompt_tokens(usage) == 0


def test_cached_tokens_clamped_to_prompt():
    usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 500}}
    assert cached_prompt_tokens(usage) == 100


def test_hash_prefix_stable_and_sensitive():
    a = [{"role": "system", "content": "hi"}, {"role": "user", "content": "yo"}]
    b = [{"role": "system", "content": "hi"}, {"role": "user", "content": "yo"}]
    assert hash_prefix(a) == hash_prefix(b)
    assert len(hash_prefix(a)) == 64
    assert hash_prefix(a) != hash_prefix([*a, {"role": "user", "content": "again"}])
    assert hash_prefix(a) != hash_prefix(list(reversed(a)))
    assert hash_prefix(a) != hash_prefix([{"role": "system", "content": "hi!"},
                                          {"role": "user", "content": "yo"}])


def test_prefix_routes_remember_and_lookup():
    routes = PrefixRoutes()
    assert routes.lookup(["deadbeef"]) is None
    routes.remember("fullhash1", "beta/free")
    assert routes.lookup(["nope", "fullhash1"]) == "beta/free"


def test_prefix_routes_evict_fifo():
    routes = PrefixRoutes(capacity=2)
    routes.remember("h1", "a/m")
    routes.remember("h2", "b/m")
    routes.remember("h3", "c/m")
    assert routes.lookup(["h1"]) is None
    assert routes.lookup(["h2"]) == "b/m"
    assert routes.lookup(["h3"]) == "c/m"


def _managed_fixture(ids=("alpha", "beta"), token_capacity=None):
    """Minimal ManagedPool fixture mirroring test_managed_runtime.py."""
    from datetime import UTC, datetime, timedelta

    from freellmpool.models import Model, Provider

    now = datetime.now(UTC)
    checked = now.isoformat()
    expires = (now + timedelta(days=7)).isoformat()
    registry, snapshot_providers, providers = {}, {}, []
    for pid in ids:
        providers.append(Provider(pid, pid, "openai", f"https://{pid}.test/v1",
                                  (Model("free", context=32000),)))
        limits = [{"id": "rpd", "scope": "account", "metric": "requests",
                   "algorithm": "rolling", "capacity": 100, "window_seconds": 86400,
                   "grant_ids": ["free"]}]
        if token_capacity is not None:
            limits.append({"id": "tok", "scope": "account", "metric": "total_tokens",
                           "algorithm": "rolling", "capacity": token_capacity,
                           "window_seconds": 86400, "grant_ids": ["free"]})
        registry[pid] = {
            "id": pid, "display_name": pid, "api_base_url": f"https://{pid}.test/v1",
            "credential_env": None,
            "discovery": {"parser": "openai", "url": f"https://{pid}.test/v1/models"},
            "evidence": [{"id": "price", "checked_at": checked, "expires_at": expires,
                          "status": "verified"}],
            "grants": [{"id": "free", "kind": "zero_price", "status": "verified",
                        "evidence_ids": ["price"], "model_selector": {"kind": "zero_price"},
                        "paid_overage_possible": False, "requires_account_evidence": False,
                        "allowed_modalities": ["chat"]}],
            "limits": limits,
        }
        snapshot_providers[pid] = {
            "checked_at": checked, "status": "ok", "complete": True, "models": [
                {"id": "free", "modalities": ["chat"], "context": 32000,
                 "pricing": {"input": "0", "output": "0"}}]}
    snapshot = {"schema": 1, "generation": "test", "providers": snapshot_providers}
    return providers, registry, snapshot


def _make_pool(tmp_path, **kwargs):
    from freellmpool.allowances import AllowanceLedger
    from freellmpool.managed import ManagedPool

    token_capacity = kwargs.pop("token_capacity", None)
    providers, registry, snapshot = _managed_fixture(token_capacity=token_capacity)
    post = kwargs.pop("post", None)
    pool = ManagedPool(providers, registry=registry, discovery=snapshot, accounts={},
                       env={"FREELLMPOOL_WAIT_SECONDS": "0"},
                       ledger=AllowanceLedger(tmp_path / "allowances.db"),
                       post=post or (lambda *a: _ok()), **kwargs)
    return pool


def _ok(prompt=5, completion=1, cached=0):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return HTTPResult(200, {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                            "usage": usage}, "")


def test_actual_cost_deducts_confirmed_cached_tokens(tmp_path):
    pool = _make_pool(tmp_path)
    route = pool.snapshot().routes[0]
    body = {"usage": {"prompt_tokens": 100, "completion_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 80}}}
    actual = pool._actual_cost(route, body, {"total_tokens": 5000, "tokens": 5000})
    assert actual["input_tokens"] == 20
    assert actual["total_tokens"] == 30
    assert actual["tokens"] == 30
    assert actual["output_tokens"] == 10
    assert actual["requests"] == 1


def test_actual_cost_ignores_unconfirmed_or_absurd_cache_claim(tmp_path):
    pool = _make_pool(tmp_path)
    route = pool.snapshot().routes[0]
    plain = pool._actual_cost(route, {"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
                              {"total_tokens": 5000, "tokens": 5000})
    assert plain["input_tokens"] == 100
    absurd = pool._actual_cost(
        route, {"usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": 999999}}},
        {"total_tokens": 5000, "tokens": 5000})
    # Clamped to the provider's own prompt count: never a negative or a refund.
    assert absurd["input_tokens"] == 0
    assert absurd["total_tokens"] == 10


def test_reply_carries_cached_tokens_and_stats_bump(tmp_path):
    pool = _make_pool(tmp_path, post=lambda *a: _ok(prompt=100, completion=5, cached=80))
    reply = pool.chat([{"role": "user", "content": "hi"}], model="free",
                      providers=["alpha"])
    assert reply.cached_prompt_tokens == 80
    assert pool.stats["prefix_cache_hits"] == 1
    assert pool.stats["prefix_tokens_avoided"] == 80
    assert pool.stats["requests"] == 1
    assert pool.stats["prompt_tokens"] == 100  # gross stays visible


def test_no_cache_fields_no_prefix_stats(tmp_path):
    pool = _make_pool(tmp_path)
    reply = pool.chat([{"role": "user", "content": "hi"}], model="free",
                      providers=["alpha"])
    assert reply.cached_prompt_tokens == 0
    assert pool.stats.get("prefix_cache_hits", 0) == 0
    assert pool.stats.get("prefix_tokens_avoided", 0) == 0


def test_loop_second_turn_prefers_warm_target(tmp_path):
    seen = []

    def post(url, headers, body, timeout):
        seen.append(url)
        return _ok(prompt=50, completion=5, cached=40)

    pool = _make_pool(tmp_path, post=post)
    turn1 = [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"}]
    r1 = pool.chat(turn1, model="free", providers=["beta"])
    assert r1.provider_id == "beta"
    # Control: a fresh pool with no prefix memory picks alpha first.
    control = _make_pool(tmp_path / "ctl")
    turn2 = [*turn1, {"role": "assistant", "content": "OK"},
             {"role": "user", "content": "u2"}]
    rc = control.chat(turn2, model="free")
    assert rc.provider_id == "alpha"
    # Same loop on the warmed pool sticks to beta via the prefix hash.
    r2 = pool.chat(turn2, model="free")
    assert r2.provider_id == "beta"
    assert pool.stats["prefix_routed"] == 1


def test_token_allowance_spends_net_not_gross(tmp_path):
    capacity = 100000  # reservation estimate is conservative (JSON bytes); settle is net
    pool = _make_pool(tmp_path, token_capacity=capacity,
                      post=lambda *a: _ok(prompt=100, completion=10, cached=80))
    pool.chat([{"role": "user", "content": "hi"}], model="free", providers=["alpha"])
    route = next(r for r in pool.snapshot().routes if r.provider.id == "alpha")
    limits = [lim for lim in route.limits if lim.unit == "total_tokens"]
    assert limits
    remaining = pool.ledger.status(limits)[0]["remaining"]
    assert remaining == capacity - (20 + 10)


def test_identical_repeat_keeps_rotation_no_steering(tmp_path):
    """Regression: an identical repeat is not a loop turn; fairness ordering
    (and the whole-response cache) owns it, not prefix memory."""
    pool = _make_pool(tmp_path)
    r1 = pool.chat([{"role": "user", "content": "hi"}], model="free")
    r2 = pool.chat([{"role": "user", "content": "hi"}], model="free")
    assert pool.stats["prefix_routed"] == 0
    assert {r1.provider_id, r2.provider_id} == {"alpha", "beta"}
