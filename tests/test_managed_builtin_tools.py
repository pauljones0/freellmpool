"""Automatic server tools must not bypass the selected free grant's exclusions.

The enforcement plumbing (_automatic_tool_conflict at snapshot and
pre-dispatch time) stays as an extension point, but no current grant needs
a rule: groq/compound and groq/compound-mini (the only known automatic-tools
models) were shut down 2026-09-21 and pruned from grants. This module pins
that ordinary models are never held by that gate.
"""

from dataclasses import replace

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool.models import Model


def model_pool(tmp_path, monkeypatch, model, *, provider="groq", prohibited=("web_search", "paid_tools")):
    calls = []

    def post(*args):
        calls.append(args)
        return successful()

    def stream(*args):
        calls.append(args)
        return 200, {}, iter(['data: {"choices":[{"delta":{"content":"OK"}}]}', "data: [DONE]"])

    pool = make_pool(tmp_path, ids=(provider,), post=post, stream_post=stream)
    pool._catalog_override = [replace(pool._catalog_override[0], models=(Model(model),))]
    pool._discovery_override["providers"][provider]["models"] = [
        {"id": model, "modalities": ["chat"], "pricing": {"input": "0", "output": "0"}},
    ]
    pool._registry_override[provider]["grants"][0]["prohibited_addons"] = list(prohibited)
    monkeypatch.setattr(pool.conformance, "passes", lambda *_: True)
    return pool, calls


@pytest.mark.parametrize("model", [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
])
@pytest.mark.parametrize("streaming", [False, True])
def test_guard_holds_no_current_model(tmp_path, monkeypatch, model, streaming):
    pool, calls = model_pool(tmp_path, monkeypatch, model)
    if streaming:
        assert list(pool.stream_chat([{"role": "user", "content": "fixture"}], model=model))[1] == "OK"
    else:
        assert pool.ask("fixture", model=model).text == "OK"
    assert len(calls) == 1
    assert "tools" not in calls[0][2]
