"""Round-2 LOW findings: regression tests (strict TDD).

Each test reproduces its finding against the current code: it must FAIL
before the fix and PASS after. Findings #27 and #28 are REFUTED (verified
against current code): keyed fetches are https-gated (capability.py AA
hosts + _get_text; pinned by test_fetch_aa_scores_refuses_unsafe_url),
and over HTTPS env proxies see only CONNECT host:port (TLS end-to-end),
so no key routes to a non-MITM proxy. Residual narrow corner (http://
provider URL + key + env proxy) is cleartext to the whole network path —
proxy-bypass would not fix that class; https-enforcement for keyed
providers is a separate product decision.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import gemini_body, make_post, make_stream_post, openai_body

from freellmpool import catalog as catalog_module
from freellmpool import cli as cli_module
from freellmpool import client as client_module
from freellmpool import observe as observe_module
from freellmpool.cache import Cache
from freellmpool.client import HTTPResult
from freellmpool.credential_store import save_key_values
from freellmpool.jobs import Job, _execute_recipe_job
from freellmpool.mcp_server import _tool_recipe
from freellmpool.models import Model, Provider
from freellmpool.onboarding import run_onboarding
from freellmpool.proxy import serve
from freellmpool.recipes import (
    MAX_PATH_FILE_BYTES,
    MissingRecipeInputError,
    Recipe,
    collect_recipe_input,
)
from freellmpool.router import Pool

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _post_raw(url: str, payload: bytes) -> tuple[int, bytes]:
    """POST raw bytes; return (status, body), closing error responses."""
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (localhost test)
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read()
        finally:
            err.close()


def _serve_pool(pool: Pool):
    httpd = serve(pool, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _fake_recipes_module():
    class _Stub:
        def get_recipe(self, name):
            return SimpleNamespace(name=name)

        def collect_recipe_input(self, recipe, *, prompt, stdin, input_file, path):
            return prompt or "stub-input", path

        def run_recipe(self, pool, recipe, **kwargs):
            return SimpleNamespace(output="ran", provider_id="alpha", model="m")

    return _Stub()


def _text_recipe() -> Recipe:
    return Recipe(
        name="t",
        version="1",
        description="d",
        role="critic",
        prompt_template="$input",
        variables=("input",),
        input_mode="text",
        output_mode="text",
        example="e",
    )


def _onboarding_registry():
    return {
        "alpha": {
            "id": "alpha",
            "display_name": "Alpha",
            "credential_env": "ALPHA_KEY",
            "grants": [{"kind": "recurring_quota", "status": "verified"}],
            "setup": {
                "signup_url": "https://example.test/signup",
                "key_url": "https://example.test/keys",
                "steps": ["Choose the Free plan."],
                "required_env": [],
            },
        }
    }


# ---------------------------------------------------------------------------
# #29 jobs validation_output_file / recipe input_file uncapped (+unconfined)
# ---------------------------------------------------------------------------


def test_29_jobs_validation_output_file_oversize_rejected(tmp_path):
    big = tmp_path / "validation.txt"
    big.write_text("x" * (MAX_PATH_FILE_BYTES + 1))
    job = Job(
        job_id="j1",
        kind="recipe",
        created_at="2026-01-01T00:00:00+00:00",
        spec={"recipe": "pr-review", "prompt": "hi",
              "validation_output_file": str(big)},
        status="queued",
        attempt=1,
        events=(),
    )
    with pytest.raises(ValueError, match="too large"):
        _execute_recipe_job(
            job, pool_factory=lambda: pytest.fail("must reject before pool use"),
            recipes_module=_fake_recipes_module(),
        )


def test_29_jobs_validation_output_file_small_ok(tmp_path):
    small = tmp_path / "validation.txt"
    small.write_text("ok")
    seen = {}
    stub = _fake_recipes_module()

    def run_recipe(pool, recipe, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(output="ran", provider_id="alpha", model="m")

    stub.run_recipe = run_recipe
    job = Job(
        job_id="j1",
        kind="recipe",
        created_at="2026-01-01T00:00:00+00:00",
        spec={"recipe": "pr-review", "prompt": "hi",
              "validation_output_file": str(small)},
        status="queued",
        attempt=1,
        events=(),
    )
    _execute_recipe_job(job, pool_factory=lambda: object(), recipes_module=stub)
    assert seen["validation_output"] == "ok"


def test_29_collect_recipe_input_caps_input_file(tmp_path):
    big = tmp_path / "input.txt"
    big.write_text("x" * (MAX_PATH_FILE_BYTES + 1))
    with pytest.raises(MissingRecipeInputError, match="too large"):
        collect_recipe_input(_text_recipe(), prompt="", stdin="", input_file=str(big))


def test_29_collect_recipe_input_confines_input_file_under_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("shh")
    inside = root / "note.txt"
    inside.write_text("hello")
    with pytest.raises(MissingRecipeInputError, match="inside"):
        collect_recipe_input(
            _text_recipe(), prompt="", stdin="",
            input_file=str(outside), root=root,
        )
    text, _ = collect_recipe_input(
        _text_recipe(), prompt="", stdin="",
        input_file=str(inside), root=root,
    )
    assert text == "hello"


def test_29_cli_recipe_run_caps_validation_file(tmp_path, monkeypatch, capsys):
    big = tmp_path / "validation.txt"
    big.write_text("x" * (MAX_PATH_FILE_BYTES + 1))
    monkeypatch.setattr(cli_module, "_read_stdin", lambda: "")
    args = argparse.Namespace(
        name="pr-review", prompt="hi", input=None, path=None,
        validation_output=None, validation_output_file=str(big),
        resume=None, run_id=None, opinions=1, synthesize=False,
        max_tokens=8, timeout=5,
    )
    assert cli_module.cmd_recipe_run(args) == 3
    assert "too large" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #30 MCP recipe tool max_tokens unclamped (siblings clamp to 8192)
# ---------------------------------------------------------------------------


def test_30_recipe_tool_clamps_huge_max_tokens(providers, env, quota, monkeypatch):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    captured = {}

    def fake_run_recipe(pool_arg, recipe, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output="out", provider_id="alpha", model="m")

    monkeypatch.setattr("freellmpool.recipes.run_recipe", fake_run_recipe)
    result = _tool_recipe(pool, {"name": "pr-review", "input": "hello",
                                 "max_tokens": 10**9})
    assert result.get("isError") is not True
    assert captured["max_tokens"] == 8192


def test_30_recipe_tool_keeps_normal_max_tokens(providers, env, quota, monkeypatch):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    captured = {}

    def fake_run_recipe(pool_arg, recipe, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output="out", provider_id="alpha", model="m")

    monkeypatch.setattr("freellmpool.recipes.run_recipe", fake_run_recipe)
    _tool_recipe(pool, {"name": "pr-review", "input": "hello", "max_tokens": 100})
    assert captured["max_tokens"] == 100
    _tool_recipe(pool, {"name": "pr-review", "input": "hello"})
    assert captured["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# #31 proxy has no range check on max_tokens / temperature
# ---------------------------------------------------------------------------


def test_31_proxy_chat_rejects_negative_max_tokens(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}),
                stream_post=make_stream_post({}))
    httpd = _serve_pool(pool)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        status, _ = _post_raw(url, json.dumps({
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": -5,
        }).encode())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 400


@pytest.mark.parametrize("bad_temp", [float("nan"), float("inf"), float("-inf")])
def test_31_proxy_chat_rejects_nonfinite_temperature(providers, env, quota, bad_temp):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}),
                stream_post=make_stream_post({}))
    httpd = _serve_pool(pool)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        status, _ = _post_raw(url, json.dumps({
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "temperature": bad_temp,
        }).encode())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 400


def test_31_proxy_stream_rejects_negative_max_tokens(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}),
                stream_post=make_stream_post({}))
    httpd = _serve_pool(pool)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        status, _ = _post_raw(url, json.dumps({
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": -5, "stream": True,
        }).encode())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 400


# ---------------------------------------------------------------------------
# #32 observe._log interpolates raw values (log injection via newlines)
# ---------------------------------------------------------------------------


def test_32_observe_log_strips_control_characters(caplog):
    with caplog.at_level(logging.INFO, logger="freellmpool"):
        observe_module.emit(None, "error", target="alpha/m",
                            reason="boom\nINJECTED line\nsecond\x1b[31mred")
    assert caplog.records
    text = caplog.records[-1].getMessage()
    assert "\n" not in text
    assert "\x1b" not in text
    assert "INJECTED" in text


def test_32_observe_log_bounds_huge_values(caplog):
    with caplog.at_level(logging.INFO, logger="freellmpool"):
        observe_module.emit(None, "error", target="alpha/m", reason="x" * 5000)
    assert caplog.records
    assert len(caplog.records[-1].getMessage()) < 1000


# ---------------------------------------------------------------------------
# #33 cli prints raw upstream text (terminal escape injection)
# ---------------------------------------------------------------------------


def test_33_cli_ask_strips_terminal_escapes(monkeypatch, capsys):
    monkeypatch.setattr(cli_module, "_read_stdin", lambda: "")

    class _FakePool:
        env = {}

        @classmethod
        def from_default_config(cls):
            return cls()

        def ask(self, *args, **kwargs):
            return SimpleNamespace(
                text="hello\x1b[2Jworld\x1b]0;title\x07", prompt_tokens=1,
                completion_tokens=2, provider_id="alpha", model="m",
            )

    monkeypatch.setattr(cli_module, "Pool", _FakePool)
    args = argparse.Namespace(
        prompt="hi", role=None, model=None, providers=None, system=None,
        json=False, routing=None, mode="normal", task=None,
        second_opinion=False, opinions=3, synthesize=False,
        max_tokens=16, temperature=0.0, timeout=5, verbose=False,
    )
    assert cli_module.cmd_ask(args) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "hello" in out and "world" in out


# ---------------------------------------------------------------------------
# #34 gemini model / cloudflare account_id interpolated into URLs unencoded
# ---------------------------------------------------------------------------


def _gemini_provider() -> Provider:
    return Provider(
        id="gee", label="Gee", adapter="gemini",
        base_url="https://gee.test/v1beta", key_env="GEE_KEY",
        models=(Model("gee-flash", rpd=0),),
    )


def test_34_gemini_model_path_segment_encoded():
    captured = {}

    def post(url, headers, body, timeout):
        captured["url"] = url
        return HTTPResult(200, gemini_body("ok"), "")

    client_module._call_gemini(
        _gemini_provider(), "evil?x=1#frag/a b",
        [{"role": "user", "content": "hi"}],
        api_key="k", max_tokens=8, temperature=0.0, timeout=5, post=post,
    )
    assert captured["url"] == (
        "https://gee.test/v1beta/models/evil%3Fx%3D1%23frag%2Fa%20b:generateContent"
    )


def test_34_gemini_plain_model_unchanged():
    captured = {}

    def post(url, headers, body, timeout):
        captured["url"] = url
        return HTTPResult(200, gemini_body("ok"), "")

    client_module._call_gemini(
        _gemini_provider(), "gee-flash",
        [{"role": "user", "content": "hi"}],
        api_key="k", max_tokens=8, temperature=0.0, timeout=5, post=post,
    )
    assert captured["url"] == "https://gee.test/v1beta/models/gee-flash:generateContent"


def test_34_cloudflare_account_id_encoded():
    captured = {}
    provider = Provider(
        id="cf", label="CF", adapter="cloudflare",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        key_env="CF_KEY", models=(Model("m", rpd=0),),
    )

    def post(url, headers, body, timeout):
        captured["url"] = url
        return HTTPResult(200, openai_body("ok"), "")

    client_module._call_openai(
        provider, "m", [{"role": "user", "content": "hi"}],
        api_key="k", env={"CLOUDFLARE_ACCOUNT_ID": "abc/def?g"},
        max_tokens=8, temperature=0.0, timeout=5,
        tools=None, tool_choice=None, response_format=None, post=post,
    )
    assert "{account_id}" not in captured["url"]
    assert "abc%2Fdef%3Fg" in captured["url"]


def test_34_async_gemini_model_path_segment_encoded(providers, env, quota):
    from freellmpool.aio import AsyncPool

    captured = {}

    async def apost(url, headers, body, timeout):
        captured["url"] = url
        return HTTPResult(200, gemini_body("ok"), "")

    gee = next(p for p in providers if p.id == "gee")
    apool = AsyncPool(Pool(providers, quota=quota, env=env), apost=apost)
    asyncio.run(apool._acall_gemini(
        gee, "evil?x=1#y/z", [{"role": "user", "content": "hi"}],
        api_key="g", max_tokens=8, temperature=0.0, timeout=5,
    ))
    assert captured["url"] == (
        "https://gee.test/v1beta/models/evil%3Fx%3D1%23y%2Fz:generateContent"
    )


# ---------------------------------------------------------------------------
# #35 negative cache max_entries disables eviction (unbounded growth)
# ---------------------------------------------------------------------------


def test_35_cache_negative_max_entries_still_evicts(tmp_path):
    t = [100.0]
    c = Cache(ttl=999.0, path=tmp_path / "c.db", clock=lambda: t[0], max_entries=-5)
    assert c.max_entries >= 1
    for key in ("k1", "k2", "k3"):
        c.put(key, {"text": key})
        t[0] += 1
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(c.path)) as con:
        rows = con.execute("SELECT key FROM cache").fetchall()
    assert len(rows) <= 1


def test_35_cache_negative_env_max_entries_floored(tmp_path, monkeypatch):
    monkeypatch.setenv("FREELLMPOOL_CACHE_MAX_ENTRIES", "-7")
    from freellmpool.cache import default_max_entries

    assert default_max_entries() >= 1
    c = Cache(ttl=999.0, path=tmp_path / "c.db")
    assert c.max_entries >= 1


# ---------------------------------------------------------------------------
# #36 _usage_counts unbounded (a provider inflates stats)
# ---------------------------------------------------------------------------


def test_36_usage_counts_clamps_absurd_values():
    counts = client_module._usage_counts(
        {"prompt_tokens": 10**18, "completion_tokens": 5})
    assert counts["completion_tokens"] == 5
    assert 0 <= counts["prompt_tokens"] <= 1_000_000_000


def test_36_usage_counts_bounds_key_count():
    counts = client_module._usage_counts({f"k{i}": i for i in range(10000)})
    assert len(counts) <= 100


def test_36_usage_counts_passthrough_normal():
    assert client_module._usage_counts(
        {"prompt_tokens": 3, "completion_tokens": 5}) == {
        "prompt_tokens": 3, "completion_tokens": 5}


# ---------------------------------------------------------------------------
# #40 UnicodeDecodeError escapes the discovery-failed mapping
# ---------------------------------------------------------------------------


class _BadBytesResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return b"\xff\xfe\x00not-utf8"


def test_40_discover_maps_decode_error(monkeypatch):
    monkeypatch.setattr(
        catalog_module._NO_REDIRECT_OPENER, "open",
        lambda request, timeout=None: _BadBytesResponse(),
    )
    with pytest.raises(ValueError, match="model discovery failed"):
        catalog_module.discover_openai_models("https://x.test/v1", api_key="k")


def test_40_import_catalog_maps_decode_error(tmp_path, monkeypatch):
    bad = tmp_path / "provider_catalog.json"
    bad.write_bytes(b"\xff\xfe\x00not-utf8")
    monkeypatch.setenv("FREELLMPOOL_EXTERNAL_CATALOG_PATH", str(bad))
    with pytest.raises(ValueError, match="missing"):
        catalog_module.import_external_provider_to_user_catalog("whatever")


# ---------------------------------------------------------------------------
# #41 credential_store exists/read TOCTOU + OSError uncaught
# ---------------------------------------------------------------------------


def test_41_credential_store_unreadable_maps_to_valueerror(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    original = '[keys]\nOLD_KEY = "old"\n'
    path.write_text(original)
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(ValueError, match="unreadable|not changed"):
        save_key_values({"NEW_KEY": "v"}, path)
    assert real_read_text(path) == original


def test_41_credential_store_missing_file_race_treated_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[keys]\nOLD_KEY = "old"\n')
    real_read_text = Path.read_text
    calls = []

    def fake_read_text(self, *args, **kwargs):
        if self == path and not calls:
            calls.append(1)
            raise FileNotFoundError("vanished between exists() and read()")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    save_key_values({"NEW_KEY": "v"}, path)
    assert 'NEW_KEY = "v"' in real_read_text(path)


# ---------------------------------------------------------------------------
# #42 onboarding progress load maps ValueError only, OSError escapes
# ---------------------------------------------------------------------------


def test_42_onboarding_unreadable_progress_maps_to_valueerror(tmp_path, monkeypatch):
    progress = tmp_path / "progress.json"
    progress.write_text("{}")
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == progress:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(ValueError, match="unreadable"):
        run_onboarding(
            provider="alpha", registry=_onboarding_registry(), env={},
            progress_path=progress, input_fn=lambda _: "",
            secret_fn=lambda _: "x", check=lambda *_: {"status": "ok"},
            output=lambda _: None,
        )


def test_42_onboarding_missing_progress_race_treated_as_fresh(tmp_path, monkeypatch):
    progress = tmp_path / "progress.json"
    progress.write_text("{}")
    real_read_text = Path.read_text
    calls = []

    def fake_read_text(self, *args, **kwargs):
        if self == progress and not calls:
            calls.append(1)
            raise FileNotFoundError("vanished between exists() and read()")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    result = run_onboarding(
        provider="alpha", registry=_onboarding_registry(), env={},
        progress_path=progress, input_fn=lambda _: "",
        secret_fn=lambda _: pytest.fail("keyless setup must not ask for a key"),
        check=lambda *_: {"status": "ok"}, output=lambda _: None,
    )
    assert result == 0


# ---------------------------------------------------------------------------
# #43 proxy catches StopIteration: hides generator violation + retries
# ---------------------------------------------------------------------------


def test_43_proxy_stream_stopiteration_not_retried(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}),
                stream_post=make_stream_post({}))
    pool.stream_chat = lambda *args, **kwargs: iter([])  # noqa: E731
    chat_calls = []
    original_chat = pool.chat

    def chat_spy(*args, **kwargs):
        chat_calls.append(1)
        return original_chat(*args, **kwargs)

    pool.chat = chat_spy
    httpd = _serve_pool(pool)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        status, body = _post_raw(url, json.dumps({
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }).encode())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 500
    assert chat_calls == []
    assert b"StopIteration" in body


# ---------------------------------------------------------------------------
# #18 proxy OverflowError on inf max_tokens must be a 400, not a 500
# ---------------------------------------------------------------------------


def test_18_proxy_chat_rejects_infinite_max_tokens(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}),
                stream_post=make_stream_post({}))
    httpd = _serve_pool(pool)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        status, _ = _post_raw(url, json.dumps({
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": float("inf"),
        }).encode())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 400


# ---------------------------------------------------------------------------
# #19 _clamp_int must survive overflowing floats (mcp + panel twins)
# ---------------------------------------------------------------------------


def test_19_mcp_clamp_int_survives_infinite():
    from freellmpool.mcp_server import _clamp_int

    assert _clamp_int(float("inf"), 7, 1, 8192) == 8192
    assert _clamp_int(float("-inf"), 7, 1, 8192) == 1
    assert _clamp_int(100, 7, 1, 8192) == 100
    assert _clamp_int("junk", 7, 1, 8192) == 7


def test_19_panel_clamp_int_survives_infinite():
    from freellmpool.panel import _clamp_int, _int_or_default

    assert _clamp_int(float("inf"), 7, 1, 8192) == 8192
    assert _clamp_int(float("-inf"), 7, 1, 8192) == 1
    assert _int_or_default(float("inf"), 7) == 7
