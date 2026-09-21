"""G38: MCP provider-filter honesty (validate-first, verbatim echo, dead-fail-closed)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from helpers import make_post
from test_managed_runtime import make_pool

import freellmpool.mcp_server as ms
from freellmpool.catalog import ExternalProvider
from freellmpool.mcp_server import handle_message
from freellmpool.models import Model, Provider
from freellmpool.provider_registry import load_registry
from freellmpool.router import Pool

S2_ASK = ("Optional free provider id to restrict to (e.g. groq; unknown or "
          "unverifiable ids return an error).")
S2_MODELS = ("Only list routes for this provider id (keeps the compact surface "
             "fully usable; unknown or unverifiable ids return an error).")
A5_FULL = ("NoProvidersConfigured: no provider has an API key set; "
           "see .env.example for the env vars")
REGISTRY_UNAVAILABLE = "provider registry unavailable; cannot validate provider '{}'"
TYPE_TEXT = "'provider' must be a string"

GROQ = Provider("groq", "Groq", "openai", "https://groq.test/v1",
                (Model("m1"), Model("m2")), key_env="GROQ_KEY")
CUSTOMX = Provider("CustomX", "CustomX", "openai", "https://customx.test/v1",
                   (Model("cx1"),))
UPPER_GROQ = Provider("GROQ", "Upper Groq", "openai", "https://upper.test/v1",
                      (Model("up"),), key_env="UPPER_GROQ_KEY")
EXTX = ExternalProvider(name="Ext X", slug="extx", category=None, url=None,
                        base_url=None, description="", model_count=0, best_rpd=0,
                        best_rpm=0, best_tpd=0, generous_score=0)


def _pool(providers: list, env: dict, quota: Any) -> Pool:
    return Pool(list(providers), quota=quota, env=dict(env), post=make_post({}))


def _groq_pool(providers: list, env: dict, quota: Any) -> Pool:
    return _pool([*providers, GROQ], {**env, "GROQ_KEY": "g"}, quota)


def _custom_pool(providers: list, env: dict, quota: Any) -> Pool:
    return _pool([*providers, CUSTOMX], env, quota)


def _call(pool: Any, name: str, args: dict) -> dict:
    return handle_message(pool, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}})


def _text(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


def _err(resp: dict) -> bool:
    return bool(resp["result"].get("isError", False))


def _unknown(raw: str) -> str:
    ids = ", ".join(sorted(load_registry()))
    return f"unknown provider '{raw}'. Known registry ids: {ids}"


def _canned() -> SimpleNamespace:
    return SimpleNamespace(text="t", provider_id="p", model="m", cached=False)


def _dead_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: Any, **k: Any) -> Any:
        raise OSError("registry down")
    monkeypatch.setattr("freellmpool.managed_cli.load_registry", _raise)


# --- M pins: models -----------------------------------------------------

def test_m1_unknown_errors(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")


def test_m2_routeless_unmatched_and_empty_legacy(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": "groq"})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'groq'"
    resp = _call(_pool([], env, quota), "free_llm_models", {})
    assert not _err(resp) and _text(resp) == "no providers configured"


@pytest.mark.parametrize("blank", ["", "   "])
def test_m3_blank_unfiltered(providers, env, quota, blank):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": blank})
    assert not _err(resp)
    assert _text(resp).splitlines()[0] == "5 chat routes (alpha: 2, beta: 1, free: 1, gee: 1)"


def test_m4_groq_canonical_rows_and_header(providers, env, quota):
    resp = _call(_groq_pool(providers, env, quota), "free_llm_models", {"provider": "GROQ"})
    assert not _err(resp)
    assert _text(resp).splitlines()[0] == "2 chat routes (groq: 2)"


def test_m5_dead_unknown_legacy_text(providers, env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == REGISTRY_UNAVAILABLE.format("NOSUCH")


def test_m6_full_filtered(providers, env, quota):
    resp = _call(_groq_pool(providers, env, quota), "free_llm_models",
                 {"provider": "groq", "full": True})
    assert not _err(resp) and _text(resp) == "groq/m1\ngroq/m2"


def test_m7_full_unmatched_wins(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models",
                 {"provider": "groq", "full": True})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'groq'"


@pytest.mark.parametrize("bad", [None, 123, ["x"], {"a": 1}])
def test_m8_nonstring_unfiltered(providers, env, quota, bad):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": bad})
    assert not _err(resp)
    assert _text(resp).splitlines()[0] == "5 chat routes (alpha: 2, beta: 1, free: 1, gee: 1)"


def test_m9_comma_whole_and_1list(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(ms, "validate_mcp_provider",
                           wraps=ms.validate_mcp_provider) as spy:
        resp = _call(pool, "free_llm_models", {"provider": "groq,NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("groq,NOSUCH")
    assert spy.call_count == 1 and spy.call_args[0][1] == ["groq,NOSUCH"]


def test_m10_managed_triple(tmp_path):
    pool = make_pool(tmp_path, ids=("groq",))
    resp = _call(pool, "free_llm_models", {"provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    resp = _call(pool, "free_llm_models", {"provider": "GROQ"})
    assert not _err(resp)
    assert _text(resp).splitlines()[0] == "1 chat routes (groq: 1)"
    assert "groq/free" in _text(resp)
    resp = _call(pool, "free_llm_models", {"provider": "gemini"})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'gemini'"


def test_m11_dead_groq_legacy_text(providers, env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    resp = _call(_groq_pool(providers, env, quota), "free_llm_models", {"provider": "GROQ"})
    assert not _err(resp) and _text(resp).splitlines()[0] == "2 chat routes (groq: 2)"


def test_m12_custom_catalog_rows(providers, env, quota, monkeypatch):
    monkeypatch.setattr("freellmpool.config.load_catalog", lambda: [CUSTOMX])
    resp = _call(_custom_pool(providers, env, quota), "free_llm_models",
                 {"provider": "customx", "full": True})
    assert not _err(resp) and _text(resp) == "CustomX/cx1"


def test_m13_spaced_echo(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": "  NOSUCH  "})
    assert _err(resp) and _text(resp) == _unknown("  NOSUCH  ")


def test_m14_unmatched_canonical_triple(providers, env, quota, monkeypatch):
    pool = _pool(providers, env, quota)
    resp = _call(pool, "free_llm_models", {"provider": "GROQ"})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'groq'"
    monkeypatch.setattr("freellmpool.catalog.load_external_catalog", lambda: [EXTX])
    resp = _call(pool, "free_llm_models", {"provider": "extx"})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'extx'"
    resp = _call(pool, "free_llm_models", {"provider": "Ext X"})
    assert not _err(resp) and _text(resp) == "no chat routes for provider 'extx'"


@pytest.mark.parametrize("raw", ["groq", "GROQ", "  GroQ  "])
def test_m15_configured_case_spelling_wins_registry(providers, env, quota, raw):
    pool = _pool([*providers, UPPER_GROQ], {**env, "UPPER_GROQ_KEY": "x"}, quota)
    resp = _call(pool, "free_llm_models", {"provider": raw, "full": True})
    assert not _err(resp) and _text(resp) == "GROQ/up"


def test_m15b_case_colliding_configured_ids_error_before_routes(env, quota):
    lower = Provider("groq", "Lower", "openai", "https://lower.test/v1", (Model("lo"),))
    pool = _pool([UPPER_GROQ, lower], {**env, "UPPER_GROQ_KEY": "x"}, quota)
    resp = _call(pool, "free_llm_models", {"provider": "groq"})
    assert _err(resp) and "ambiguous configured provider ids" in _text(resp)


def test_m16_blank_full_unfiltered(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models",
                 {"provider": "", "full": True})
    assert not _err(resp)
    assert _text(resp) == "alpha/alpha-small\nalpha/alpha-big\nbeta/beta-1\ngee/gee-flash\nfree/free-1"


# --- A pins: ask ----------------------------------------------------------

def test_a1_unknown_uncalled(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


@pytest.mark.parametrize("raw", ["NOSUCH", 123])
def test_a2_model_overwrite_ignores_raw(providers, env, quota, raw):
    pool = _groq_pool(providers, env, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "model": "groq/m", "provider": raw})
    assert not _err(resp) and chat.call_args[1]["providers"] == ["groq"]


def test_a3_spaced_forwards(providers, env, quota):
    pool = _groq_pool(providers, env, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": " groq "})
    assert not _err(resp) and chat.call_args[1]["providers"] == ["groq"]


def test_a4_blank_absent(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": ""})
    assert not _err(resp) and chat.call_args[1]["providers"] is None


def test_a5_dead_empty_verbatim(providers, env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    pool = _pool([], env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == REGISTRY_UNAVAILABLE.format("NOSUCH")
    chat.assert_not_called()


def test_a6_numeric_model_still_provider_error(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "model": 123, "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


@pytest.mark.parametrize("model", ["auto", "gpt-4o-mini"])
def test_a7_auto_models_provider_error(providers, env, quota, model):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "model": model, "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


def _assert_g28_markers(text: str) -> None:
    assert "UnknownModel" in text
    assert "unknown provider" not in text and "no chat routes" not in text


def test_a8a_absent_slash_unknown_model(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_ask",
                 {"prompt": "hi", "model": "NOSUCH/m"})
    assert _err(resp)
    _assert_g28_markers(_text(resp))


def test_a8b_live_provider_slash_unknown_model(providers, env, quota):
    resp = _call(_groq_pool(providers, env, quota), "free_llm_ask",
                 {"prompt": "hi", "model": "NOSUCH/m", "provider": "groq"})
    assert _err(resp)
    _assert_g28_markers(_text(resp))


def test_a8c_provider_beats_model(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "model": "NOSUCH/m", "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


def test_a9_upper_slash_unknown_model(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_ask",
                 {"prompt": "hi", "model": "GROQ/m"})
    assert _err(resp)
    _assert_g28_markers(_text(resp))


def test_a10_alias_overwrite(providers, env, quota):
    pool = _groq_pool(providers, {**env, "FREELLMPOOL_ALIAS_XM": "groq/m"}, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "model": "XM", "provider": "NOSUCH"})
    assert not _err(resp) and chat.call_args[1]["providers"] == ["groq"]


def test_a11_chutes_pin_miss(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_ask",
                 {"prompt": "hi", "model": "chutes/m"})
    assert _err(resp)
    _assert_g28_markers(_text(resp))


def test_a12_managed_forward_and_error(tmp_path):
    pool = make_pool(tmp_path)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": " groq "})
    assert not _err(resp) and chat.call_args[1]["providers"] == ["groq"]
    with mock.patch.object(pool, "chat") as chat2:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat2.assert_not_called()


def test_a13_provider_beats_task(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "provider": "NOSUCH", "task": "BOGUS"})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


def test_a14_provider_beats_garbage(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "provider": "NOSUCH",
                      "routing": "bogus", "max_tokens": -5})
    assert _err(resp) and _text(resp) == _unknown("NOSUCH")
    chat.assert_not_called()


def test_a15_dead_extra_hit_forwards_canonical(providers, env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    monkeypatch.setattr("freellmpool.config.load_catalog",
                        lambda: [SimpleNamespace(id="groq")])
    pool = _pool([], env, quota)
    with mock.patch.object(pool, "chat", wraps=pool.chat) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": "GROQ"})
    assert _err(resp) and _text(resp) == A5_FULL
    assert chat.call_args.kwargs["providers"] == ["groq"]


def test_a15b_dead_registry_known_pool_uses_actual_id_and_strips(providers, env, quota,
                                                                 monkeypatch):
    _dead_registry(monkeypatch)
    pool = _pool([*providers, UPPER_GROQ], {**env, "UPPER_GROQ_KEY": "x"}, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "provider": "  groq  "})
    assert not _err(resp)
    assert chat.call_args.kwargs["providers"] == ["GROQ"]


def test_a15c_unavailable_registry_never_calls_chat_or_transport(env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    network = []
    pool = Pool([UPPER_GROQ], quota=quota, env={**env, "UPPER_GROQ_KEY": "x"},
                post=lambda *a, **k: network.append((a, k)))
    with mock.patch.object(pool, "chat", wraps=pool.chat) as chat:
        resp = _call(pool, "free_llm_ask",
                     {"prompt": "hi", "provider": "NOSUCH"})
    assert _err(resp) and _text(resp) == REGISTRY_UNAVAILABLE.format("NOSUCH")
    chat.assert_not_called()
    assert network == []


@pytest.mark.parametrize("bad", [123, True, ["groq"], {"a": 1}])
def test_a16_type_error(providers, env, quota, bad):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": bad})
    assert _err(resp) and _text(resp) == TYPE_TEXT
    chat.assert_not_called()


def test_a16_dead_still_type_error(providers, env, quota, monkeypatch):
    _dead_registry(monkeypatch)
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat") as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": 123})
    assert _err(resp) and _text(resp) == TYPE_TEXT
    chat.assert_not_called()


@pytest.mark.parametrize("blank", [None, 0, False, []])
def test_a17_falsy_absent(providers, env, quota, blank):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": blank})
    assert not _err(resp) and chat.call_args[1]["providers"] is None


def test_a18_space_absent(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(pool, "chat", return_value=_canned()) as chat:
        resp = _call(pool, "free_llm_ask", {"prompt": "hi", "provider": "   "})
    assert not _err(resp) and chat.call_args[1]["providers"] is None


def test_a19_comma_whole_and_1list(providers, env, quota):
    pool = _pool(providers, env, quota)
    with mock.patch.object(ms, "validate_mcp_provider",
                           wraps=ms.validate_mcp_provider) as spy:
        with mock.patch.object(pool, "chat") as chat:
            resp = _call(pool, "free_llm_ask",
                         {"prompt": "hi", "provider": "groq,NOSUCH"})
    assert _err(resp) and _text(resp) == _unknown("groq,NOSUCH")
    chat.assert_not_called()
    assert spy.call_count == 1 and spy.call_args[0][1] == ["groq,NOSUCH"]


# --- R/S/H/L pins ---------------------------------------------------------

def test_r1_router_equals_direct(providers, env, quota):
    pool = _pool(providers, env, quota)
    direct = _text(_call(pool, "free_llm_ask", {"prompt": "hi", "provider": "NOSUCH"}))
    routed = _text(_call(pool, "free_llm",
                         {"action": "free_llm_ask",
                          "args": {"prompt": "hi", "provider": "NOSUCH"}}))
    assert routed == direct


@pytest.mark.parametrize("tool, expected", [("free_llm_ask", S2_ASK),
                                            ("free_llm_models", S2_MODELS)])
def test_s2_help_descriptions(providers, env, quota, tool, expected):
    import json
    pool = _pool(providers, env, quota)
    resp = _call(pool, "free_llm", {"action": "help", "args": {"name": tool}})
    schema = json.loads(_text(resp))
    assert schema["inputSchema"]["properties"]["provider"]["description"] == expected


def test_h1_exotic_never_raises(monkeypatch):
    from freellmpool.managed_cli import validate_mcp_provider

    def _raise(*a: Any, **k: Any) -> Any:
        raise OSError("down")
    monkeypatch.setattr("freellmpool.managed_cli.load_registry", _raise)
    monkeypatch.setattr("freellmpool.config.load_catalog", _raise)
    monkeypatch.setattr("freellmpool.catalog.load_external_catalog", _raise)
    monkeypatch.setattr("freellmpool.plugins.registered_providers", _raise)
    assert validate_mcp_provider(object(), ["NOSUCH"]) == (
        "error", REGISTRY_UNAVAILABLE.format("NOSUCH"))


def test_h2_blank_is_unknown(providers, env, quota):
    from freellmpool.managed_cli import validate_mcp_provider
    assert validate_mcp_provider(_pool(providers, env, quota), [""]) == ("error", _unknown(""))


def test_h3_case_colliding_extra_ids_are_ambiguous(providers, env, quota, monkeypatch):
    from freellmpool.managed_cli import validate_mcp_provider
    monkeypatch.setattr("freellmpool.config.load_catalog", lambda: [
        SimpleNamespace(id="LocalX"), SimpleNamespace(id="localx")])
    verdict, detail = validate_mcp_provider(_pool(providers, env, quota), ["localx"])
    assert verdict == "error" and "ambiguous provider ids" in detail


def test_l1_lists_live_registry(providers, env, quota):
    resp = _call(_pool(providers, env, quota), "free_llm_models", {"provider": "NOSUCH"})
    listed = _text(resp).split("Known registry ids: ")[1].split(", ")
    assert listed == sorted(load_registry())
