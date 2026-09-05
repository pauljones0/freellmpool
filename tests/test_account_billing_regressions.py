"""Billing exhaustion must cool the account without changing ordinary errors."""

from __future__ import annotations

import asyncio

import pytest
from helpers import make_post

from freellmpool.aio import AsyncPool
from freellmpool.errors import AllProvidersExhausted, ProviderHTTPError
from freellmpool.router import Pool, _health_failure_class, _is_account_quota_exhaustion


@pytest.mark.parametrize("status", [402, 429])
@pytest.mark.parametrize("message", ["Insufficient balance", "No resource package. Please recharge."])
def test_billing_exhaustion_is_provider_quota(status, message):
    error = ProviderHTTPError(status, message, retryable=status == 429)
    assert _is_account_quota_exhaustion(error)
    assert _health_failure_class(error) == "provider_quota"


@pytest.mark.parametrize(
    ("status", "message", "classification"),
    [
        (402, "Upgrade required for this model", "capability"),
        (429, "Requests per minute exceeded", "rate_limit"),
        (403, "Insufficient balance", "auth"),
        (400, "No resource package", "client"),
    ],
)
def test_unrelated_errors_keep_their_scope(status, message, classification):
    error = ProviderHTTPError(status, message, retryable=status == 429)
    assert not _is_account_quota_exhaustion(error)
    assert _health_failure_class(error) == classification


@pytest.mark.parametrize("status", [402, 429])
@pytest.mark.parametrize("message", ["Insufficient balance", "No resource package"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_billing_exhaustion_skips_siblings_and_backs_off_account(
    providers, env, quota, status, message, asynchronous
):
    post = make_post({"alpha.test": (status, {"error": {"message": message}})})
    pool = Pool(providers[:1], env=env, quota=quota, post=post, clock=lambda: 0.0)

    async def apost(url, headers, body, timeout):
        return post(url, headers, body, timeout)

    with pytest.raises(AllProvidersExhausted) as caught:
        if asynchronous:
            asyncio.run(AsyncPool(pool, apost=apost).aask("hello"))
        else:
            pool.ask("hello")

    assert len(post.calls) == 1
    assert caught.value.client_status is None
    assert pool.cooldown_snapshot(0.0)["alpha"] >= 15 * 60
