"""Provider-reported aggregate usage must survive both transport paths."""

import json

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool.errors import AllProvidersExhausted


def usage_pool(tmp_path, usage, *, streaming=False, capacity=7500, metric="total_tokens", costs=None):
    calls = []
    reserved = []

    def record():
        calls.append(True)
        reserved.append(token_status(pool)["used"])

    def post(*args):
        record()
        return successful({"choices": [{"message": {"role": "assistant", "content": "OK"}}], "usage": usage})

    def stream(*args):
        record()
        return 200, {}, iter([
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            "data: " + json.dumps({"choices": [], "usage": usage}),
            "data: [DONE]",
        ])

    pool = make_pool(tmp_path, ids=("alpha",), capacity=100, post=post, stream_post=stream)
    pool._registry_override["alpha"]["limits"].append({
        "id": "tpm", "scope": "account", "metric": metric,
        "algorithm": "rolling", "capacity": capacity, "window_seconds": 60,
    })
    if costs is not None:
        pool._registry_override["alpha"]["model_costs"] = {"free": costs}

    def request():
        if streaming:
            return list(pool.stream_chat([{"role": "user", "content": "synthetic fixture"}], max_tokens=600))
        return pool.ask("synthetic fixture", max_tokens=600)

    return pool, request, calls, reserved


def token_status(pool):
    return next(row for row in pool.managed_status()["allowances"] if row["key"].endswith(":tpm"))


@pytest.mark.parametrize("streaming", [False, True])
def test_aggregate_usage_blocks_later_dispatch_when_components_omit_internal_work(tmp_path, streaming):
    pool, request, calls, _ = usage_pool(
        tmp_path, {"prompt_tokens": 116, "completion_tokens": 571, "total_tokens": 7340}, streaming=streaming,
    )
    request()
    assert token_status(pool)["used"] == 7340
    with pytest.raises(AllProvidersExhausted):
        request()
    assert len(calls) == 1


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 8}, 12),
    ({"prompt_tokens": 5, "completion_tokens": 7}, 12),
    ({"total_tokens": 19}, 19),
    ({"prompt_tokens": True, "total_tokens": 19}, 19),
    ({"prompt_tokens": 5, "completion_tokens": -1, "total_tokens": 19}, 19),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_valid_aggregate_and_component_usage_reconcile_independently(tmp_path, usage, expected, streaming):
    pool, request, _, _ = usage_pool(tmp_path, usage, streaming=streaming)
    request()
    assert token_status(pool)["used"] == expected


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "19", float("inf"), 10**400])
@pytest.mark.parametrize("streaming", [False, True])
def test_malformed_reported_total_does_not_refund_reserved_total(tmp_path, invalid, streaming):
    pool, request, _, reserved = usage_pool(
        tmp_path, {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": invalid}, streaming=streaming,
    )
    request()
    assert token_status(pool)["used"] == reserved[0]


@pytest.mark.parametrize("usage", [{}, None, {"prompt_tokens": 5}, {"prompt_tokens": False, "completion_tokens": 1}])
@pytest.mark.parametrize("streaming", [False, True])
def test_unknown_total_stays_reserved(tmp_path, usage, streaming):
    pool, request, _, reserved = usage_pool(tmp_path, usage, streaming=streaming)
    request()
    assert token_status(pool)["used"] == reserved[0]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("metric", ["total_tokens", "tokens"])
@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 5, "completion_tokens": 7000, "total_tokens": None}, 7005),
    ({"prompt_tokens": 7000}, 7000),
    ({"completion_tokens": 7000}, 7000),
    ({"prompt_tokens": 7000, "completion_tokens": True, "total_tokens": "unknown"}, 7000),
    ({"prompt_tokens": -1, "completion_tokens": 7000, "total_tokens": None}, 7000),
])
def test_unknown_aggregate_charges_known_lower_bound_and_blocks_next_dispatch(tmp_path, streaming, metric, usage, expected):
    pool, request, calls, reserved = usage_pool(tmp_path, usage, streaming=streaming, metric=metric)
    request()
    assert reserved[0] < expected
    assert token_status(pool)["used"] == expected
    with pytest.raises(AllProvidersExhausted):
        request()
    assert len(calls) == 1


NEURON_COSTS = {"neurons_per_input_token": "0.5", "neurons_per_output_token": "2"}


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 14000},
    {"completion_tokens": 3500},
    {"prompt_tokens": 14000, "completion_tokens": True, "total_tokens": 14000},
    {"prompt_tokens": -1, "completion_tokens": 3500, "total_tokens": None},
])
def test_partial_neuron_usage_charges_known_lower_bound_and_blocks_next_dispatch(tmp_path, streaming, usage):
    pool, request, calls, reserved = usage_pool(
        tmp_path, usage, streaming=streaming, metric="neurons", costs=NEURON_COSTS,
    )
    request()
    assert reserved[0] < 7000
    assert token_status(pool)["used"] == 7000
    with pytest.raises(AllProvidersExhausted):
        request()
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 5}, {"completion_tokens": 7}, {"prompt_tokens": 5, "completion_tokens": True},
])
def test_partial_neuron_usage_keeps_reservation_when_known_usage_is_lower(tmp_path, streaming, usage):
    pool, request, _, reserved = usage_pool(
        tmp_path, usage, streaming=streaming, metric="neurons", costs=NEURON_COSTS,
    )
    request()
    assert token_status(pool)["used"] == reserved[0]


@pytest.mark.parametrize("streaming", [False, True])
def test_complete_neuron_usage_settles_exactly_despite_malformed_total(tmp_path, streaming):
    pool, request, _, _ = usage_pool(
        tmp_path, {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": None},
        streaming=streaming, metric="neurons", costs=NEURON_COSTS,
    )
    request()
    assert token_status(pool)["used"] == 17
