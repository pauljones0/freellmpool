import json
import threading
import urllib.error
import urllib.request

import pytest
from test_managed_runtime import make_pool

from freellmpool import cli
from freellmpool.benchmark import benchmark
from freellmpool.conformance import ConformanceStore, classify_canary_exception, run_target_canaries
from freellmpool.errors import AllProvidersExhausted
from freellmpool.healthcheck import run_healthcheck
from freellmpool.mcp_server import _call_tool, _quota_summary
from freellmpool.proxy import _openai_models_payload, _readiness_snapshot, _status_payload, serve


def test_benchmark_respects_managed_ledger_and_never_discovers_raw_routes(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=1)
    monkeypatch.setattr("freellmpool.benchmark.discover_openai_models", lambda *a, **kw: pytest.fail("raw discovery"))
    assert benchmark(pool)[0].ok
    assert not benchmark(pool)[0].ok
    assert pool.ledger.summary()["reservations"] == 1
    assert pool.metrics.get("alpha/free").ok == 1
    assert pool.metrics.get("alpha/free").fail == 0


def test_managed_health_reports_rate_limits_after_local_exhaustion(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=1)
    assert run_healthcheck(pool)[0].ok
    assert run_healthcheck(pool)[0].status == "rate_limited"


def test_health_explains_why_a_configured_provider_has_no_free_routes(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._discovery_override["providers"]["alpha"]["checked_at"] = "2020-01-01T00:00:00Z"
    rows = benchmark(pool, providers=["alpha"])
    assert len(rows) == 1
    assert rows[0].skipped
    assert "expired" in rows[0].error
    assert pool.ledger.summary()["reservations"] == 0


def test_managed_listings_follow_refresh_and_expose_allowances(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=1)
    assert "alpha/free" in [row["id"] for row in _openai_models_payload(pool)["data"]]
    pool.ask("hi")
    assert not _readiness_snapshot(pool).ready_providers
    assert _status_payload(pool, [])["strict_free"] is True
    assert "allowances" in _status_payload(pool, [])
    assert "remaining=0" in _quota_summary(pool)
    pool._discovery_override["providers"]["alpha"]["models"][0]["pricing"]["output"] = "1"
    assert "alpha/free" not in [row["id"] for row in _openai_models_payload(pool)["data"]]
    assert "alpha/free" not in str(_call_tool(pool, {"name": "free_llm_models"}))


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
    ("/v1/responses", {"model": "auto", "input": "hi"}),
    ("/v1/messages", {"model": "auto", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/embeddings", {"model": "auto", "input": "hi"}),
])
def test_exhaustion_retry_after_reaches_http_clients(tmp_path, path, body):
    pool = make_pool(tmp_path)
    def exhausted(*args, **kwargs):
        raise AllProvidersExhausted([], client_status=429, client_message="Free capacity exhausted", retry_after=3.2)
    pool.chat = pool.stream_chat = pool.embed = exhausted
    httpd = serve(pool, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 429
        assert error.value.headers["Retry-After"] == "4"
        error.value.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_conformance_uses_guarded_probe_and_refuses_disabled_override(tmp_path, monkeypatch, capsys):
    pool = make_pool(tmp_path, ids=("alpha",), capacity=1)
    monkeypatch.setattr(cli.Pool, "from_default_config", lambda **kwargs: pool)
    monkeypatch.setattr(cli, "_runtime_catalog", lambda: pool.providers)
    assert cli.main(["conformance", "run", "--features", "chat", "--json"]) == 0
    assert pool.ledger.summary()["reservations"] == 1
    capsys.readouterr()
    assert cli.main(["conformance", "run", "--include-disabled", "--provider", "alpha", "--model", "paid"]) != 0


def test_cli_models_uses_current_managed_routes(tmp_path, monkeypatch, capsys):
    pool = make_pool(tmp_path, ids=("alpha",))
    monkeypatch.setattr(cli.Pool, "from_default_config", lambda **kwargs: pool)
    assert cli.main(["models", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["model"] for row in rows} == {"free"}
    assert rows[0]["eligible"] is True


def test_historic_single_call_tools_proof_cannot_admit_agent_requests(tmp_path):
    pool = make_pool(tmp_path, ids=("alpha",))
    provider = pool.providers[0]
    store = ConformanceStore(tmp_path / "proof.json")
    for feature in ("chat", "tools"):
        store.record(provider, "free", feature, status="pass", classification="verified")
    state = json.loads(store.path.read_text())
    state["targets"]["alpha/free"]["features"]["tools"].pop("probe_version", None)
    store.path.write_text(json.dumps(state))
    assert store.passes(provider, "free", ["chat"])
    assert not store.passes(provider, "free", ["tools"])
    store.record(provider, "free", "tools", status="pass", classification="verified")
    assert store.passes(provider, "free", ["tools"])


@pytest.mark.parametrize("client_status,upstream_status,classification", [
    (429, None, "rate_limit"), (503, 404, "model_not_found"),
    (503, 401, "auth"), (503, 403, "auth"), (429, 402, "billing"),
    (403, None, "unavailable_free_policy"), (413, None, "context_limit"),
])
def test_canaries_classify_managed_exhaustion_without_disclosing_errors(tmp_path, client_status, upstream_status, classification):
    error = AllProvidersExhausted([], client_status=client_status, upstream_status=upstream_status,
                                  client_message="sensitive-upstream-value")
    assert classify_canary_exception(error) == classification
    pool = make_pool(tmp_path)
    def fail(*args, **kwargs):
        raise error
    result = run_target_canaries(pool.providers[0], "free", env={}, features=["chat"], call_fn=fail)
    assert result["chat"] == {"status": "unavailable", "classification": classification}
    assert "sensitive" not in json.dumps(result)
