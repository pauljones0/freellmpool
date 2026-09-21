"""G37 provider-literal validation on the remaining 9 surfaces (P1-P52).

Pool-anchored accept-if-known-anywhere for the 7 pool-union surfaces
(ask/models/health/publish/benchmark/conf/board); strict registry for
rag-ask + discovery.main. Unknown literals exit 2 naming the literal
(keys-check shape), stdout untouched, no network/probes/writes/embed
before validation. Offline: fixture pools + catalog mocks + booms.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from freellmpool.models import Model, Provider


@pytest.fixture(autouse=True)
def _no_stdin(monkeypatch):
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")


class _EmptyQuota:
    def snapshot(self):
        return {}


class _FakeAskPool:
    quota = _EmptyQuota()

    def __init__(self, captured, env=None):
        self.captured = captured
        self.env = {} if env is None else env
        self.providers = []

    def rank_targets(self, messages, **kwargs):
        self.captured["rank"] = kwargs
        return []

    def ask(self, prompt, **kwargs):
        self.captured["prompt"] = prompt
        self.captured.update(kwargs)
        from freellmpool.models import Reply
        return Reply(text="ok", provider_id="fake", model="fake-model", raw={})


def _patch_ask_pool(monkeypatch, pool):
    from freellmpool.router import Pool
    monkeypatch.setattr(Pool, "from_default_config",
                        classmethod(lambda cls, **kwargs: pool))


def _provider(pid):
    return Provider(id=pid, label=pid.title(), adapter="openai",
                    base_url=f"https://{pid}.test/v1",
                    models=(Model(f"{pid}-model"),))


# --- ask (P1-P11, P46) ---

def test_ask_unknown_names_literal(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "Known registry ids:" in captured.err
    assert captured.out == ""


def test_ask_unknown_json_keeps_stdout_empty(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", "NOSUCH", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown provider 'NOSUCH'" in captured.err


def test_ask_unknown_probes_nothing(monkeypatch, capsys):
    from freellmpool.cli import main

    pool = _FakeAskPool({})
    pool.ask = _boom("pool.ask")
    pool.rank_targets = _boom("rank_targets")
    _patch_ask_pool(monkeypatch, pool)
    monkeypatch.setattr("freellmpool.cli.run_panel", _boom("run_panel"))
    assert main(["ask", "hi", "-p", "NOSUCH"]) == 2


def _boom(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"{name} must not run for an unknown literal")
    return _raise


def test_ask_mixed_known_unknown_rejects(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", "GROQ,NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


def test_ask_canonical_case_forwards_lowercase(monkeypatch, capsys):
    from freellmpool.cli import main
    captured = {}
    _patch_ask_pool(monkeypatch, _FakeAskPool(captured))
    assert main(["ask", "hi", "-p", " groq "]) == 0
    assert captured.get("providers") == ["groq"]


def test_ask_slash_overwrite_ignores_raw_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    captured = {}
    _patch_ask_pool(monkeypatch, _FakeAskPool(captured))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    rc = main(["ask", "hi", "-p", "NOSUCH", "--model", "groq/llama-3.1-8b"])
    assert rc == 0
    assert captured.get("providers") == ["groq"]
    assert "unknown provider" not in capsys.readouterr().err


def test_ask_non_provider_slash_prefix_validates_raw_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    captured = {}
    _patch_ask_pool(monkeypatch, _FakeAskPool(captured))
    argv = ["ask", "hi", "-p", "NOSUCH", "--model", "Qwen/Qwen2-7B"]
    assert main(argv) == 2
    captured_out = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured_out.err
    assert captured_out.out == ""
    assert "providers" not in captured


def test_ask_empty_prompt_beats_bad_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "-p", "NOSUCH"]) == 3
    captured = capsys.readouterr()
    assert "no prompt provided" in captured.err
    assert "unknown provider" not in captured.err


def test_ask_unknown_role_beats_bad_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", "NOSUCH", "--role", "NOSUCHROLE"]) == 2
    captured = capsys.readouterr()
    assert "unknown role" in captured.err
    assert "unknown provider" not in captured.err


def test_ask_literal_beats_second_opinion_json_rejection(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    monkeypatch.setattr("freellmpool.cli.run_panel", _boom("run_panel"))
    argv = ["ask", "hi", "-p", "NOSUCH", "--json", "--second-opinion"]
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "--json is not supported with --second-opinion" not in captured.err


@pytest.mark.parametrize("literal", ["", " "])
def test_ask_empty_literal_is_unknown(monkeypatch, capsys, literal):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", literal]) == 2
    assert "unknown provider" in capsys.readouterr().err


def test_ask_trailing_comma_is_unknown(monkeypatch, capsys):
    from freellmpool.cli import main
    _patch_ask_pool(monkeypatch, _FakeAskPool({}))
    assert main(["ask", "hi", "-p", "groq,"]) == 2
    assert "unknown provider ''" in capsys.readouterr().err


def test_ask_dead_registry_skips_validation(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool

    def _boom_registry(*args, **kwargs):
        raise OSError("simulated dead registry")
    # Implementer note (deviates v1.4 Q2): validation lives in the shared
    # _resolve_cli_filter, so the dead-registry seam is managed_cli's read.
    monkeypatch.setattr("freellmpool.managed_cli.load_registry", _boom_registry)
    monkeypatch.setattr(Pool, "from_default_config",
                        classmethod(lambda cls, **kwargs: Pool([], env={})))
    assert main(["ask", "hi", "-p", "NOSUCH"]) == 3
    captured = capsys.readouterr()
    # Implementer note (deviates v1.3 O5): cmd_ask gates empty pools with the
    # no-key message before pool.ask; the skip is proven by reaching old flow
    # (rc3, no traceback, no literal error) despite the dead registry.
    assert "no provider has an API key set" in captured.err
    assert "unknown provider" not in captured.err
    assert "Traceback" not in captured.err


# --- models (P12-P14, P49) ---

def _legacy_pool(monkeypatch):
    from freellmpool.router import Pool
    pool = Pool([], env={})
    monkeypatch.setattr(Pool, "from_default_config", lambda **kwargs: pool)
    return pool


def test_models_unknown_names_literal(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["models", "-p", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_models_unknown_json_keeps_stdout_empty(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["models", "-p", "NOSUCH", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown provider 'NOSUCH'" in captured.err


def test_models_mixed_rejects_canonical_forwards(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["models", "-p", "groq,NOSUCH"]) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err
    # Canonical case passes validation (zero catalog rows, honest exit 0).
    assert main(["models", "-p", "GROQ"]) == 0


def test_models_plugin_only_id_passes_validation(monkeypatch, capsys):
    from freellmpool import plugins
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    monkeypatch.setattr(plugins, "registered_providers",
                        lambda: [_provider("plugx")])
    assert main(["models", "-p", "plugx"]) == 0
    captured = capsys.readouterr()
    assert "unknown provider" not in captured.err
    assert "plugx" in captured.out


# --- health (P15-P17) ---

def test_health_unknown_names_literal(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["providers", "health", "-p", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_health_unknown_probes_nothing(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    monkeypatch.setattr("freellmpool.healthcheck.run_healthcheck",
                        _boom("run_healthcheck"))
    assert main(["providers", "health", "-p", "NOSUCH"]) == 2


def test_health_mixed_rejects(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["providers", "health", "-p", "groq,NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


# --- publish (P18-P20) ---

def test_publish_unknown_writes_nothing(tmp_path, monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    docs = tmp_path / "docs"
    argv = ["status-page", "publish", "-p", "NOSUCH", "--docs-dir", str(docs)]
    assert main(argv) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err
    assert not docs.exists()


def test_publish_rows_file_ignores_filter(tmp_path, monkeypatch, capsys):
    from freellmpool.cli import main
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"target": "a/m", "status": "pass",
                                 "latency_ms": 1, "note": ""}]))
    docs = tmp_path / "docs"
    argv = ["status-page", "publish", "--rows-file", str(rows),
           "-p", "NOSUCH", "--docs-dir", str(docs)]
    assert main(argv) == 0
    assert "unknown provider" not in capsys.readouterr().err


def test_publish_mixed_rejects(tmp_path, monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    docs = tmp_path / "docs"
    argv = ["status-page", "publish", "-p", "groq,NOSUCH",
           "--docs-dir", str(docs)]
    assert main(argv) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err
    assert not docs.exists()


# --- benchmark (P21-P23, P50) ---

def test_benchmark_unknown_beats_empty_pool_gate(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["benchmark", "-p", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "no providers configured" not in captured.err


def test_benchmark_unknown_skips_banner(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool
    pool = Pool([_provider("builtin")], env={})
    monkeypatch.setattr(Pool, "from_default_config", lambda **kwargs: pool)
    assert main(["benchmark", "-p", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "Benchmarking" not in captured.err


def test_benchmark_known_empty_pool_hits_gate(monkeypatch, capsys):
    from freellmpool.cli import main
    _legacy_pool(monkeypatch)
    assert main(["benchmark", "-p", "groq"]) == 3
    captured = capsys.readouterr()
    assert "no providers configured" in captured.err
    assert "unknown provider" not in captured.err


def test_benchmark_mixed_rejects(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool
    pool = Pool([_provider("builtin")], env={})
    monkeypatch.setattr(Pool, "from_default_config", lambda **kwargs: pool)
    assert main(["benchmark", "-p", "groq,NOSUCH"]) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err


# --- G40 T6 bench banner counts the probed set ---

def _g40_bench_pool(monkeypatch):
    from freellmpool.router import Pool
    pool = Pool([_provider("groq"), _provider("cerebras"), _provider("openrouter")], env={})
    monkeypatch.setattr(Pool, "from_default_config", lambda **kwargs: pool)
    monkeypatch.setattr("freellmpool.benchmark.benchmark", lambda *a, **k: [])


def test_g40_bench_banner_counts_filtered_providers(monkeypatch, capsys):
    from freellmpool.cli import main
    _g40_bench_pool(monkeypatch)
    assert main(["benchmark", "-p", "groq"]) == 0
    assert "Benchmarking 1 providers" in capsys.readouterr().err


def test_g40_bench_banner_counts_all_without_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    _g40_bench_pool(monkeypatch)
    assert main(["benchmark"]) == 0
    assert "Benchmarking 3 providers" in capsys.readouterr().err


# --- conf run (P24-P28, P51) ---

def _conf_args(*extra):
    return ["conformance", "run", "--features", "chat", *extra]


def test_conf_unknown_probes_nothing(monkeypatch, capsys):
    from freellmpool.cli import main
    monkeypatch.setattr("freellmpool.cli.run_target_canaries",
                        _boom("run_target_canaries"))
    assert main(_conf_args("--providers", "NOSUCH")) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_conf_disabled_rejection_beats_validation(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool
    monkeypatch.setattr(Pool, "from_default_config",
                        lambda **kwargs: SimpleNamespace(managed=False))
    argv = _conf_args("--include-disabled", "--providers", "NOSUCH")
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "--include-disabled requires one exact --provider and --model" in captured.err
    assert "unknown provider" not in captured.err


def test_conf_features_error_beats_bad_filter(monkeypatch, capsys):
    from freellmpool.cli import main
    argv = ["conformance", "run", "--features", "bogus",
           "--providers", "NOSUCH"]
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "invalid conformance feature selection" in captured.err
    assert "unknown provider" not in captured.err


def test_conf_mixed_rejects_and_canonical_overwrites(monkeypatch, capsys, tmp_path):
    from freellmpool.cli import main
    from freellmpool.conformance import ConformanceStore
    from freellmpool.router import Pool

    assert main(_conf_args("--providers", "GROQ,NOSUCH")) == 2
    assert "unknown provider 'NOSUCH'" in capsys.readouterr().err

    seen = {}

    def _capture(provider, model, **kwargs):
        seen.setdefault("targets", []).append(provider.id)
        return {"chat": {"status": "pass", "classification": "verified"}}
    # Legacy path: configured comes from the keyed catalog (managed admission
    # needs evidence); the GROQ literal must still canonicalize to groq.
    monkeypatch.setattr(Pool, "from_default_config",
                        lambda **kwargs: Pool([_provider("groq")], env={}))
    monkeypatch.setattr("freellmpool.cli.run_target_canaries", _capture)
    monkeypatch.setattr("freellmpool.cli.ConformanceStore",
                        lambda *a, **k: ConformanceStore(tmp_path / "c.json"))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    assert main(_conf_args("--providers", "GROQ")) == 0
    assert seen.get("targets") == ["groq"]


def test_conf_managed_disabled_path_never_validates(monkeypatch, capsys):
    from freellmpool.cli import main
    from freellmpool.router import Pool
    pool = SimpleNamespace(managed=True, snapshot=lambda: None, providers=[])
    monkeypatch.setattr(Pool, "from_default_config", lambda **kwargs: pool)
    argv = _conf_args("--include-disabled", "--providers", "NOSUCH")
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "disabled routes cannot bypass free admission" in captured.err
    assert "unknown provider" not in captured.err


def test_conf_unknown_json_keeps_stdout_empty(monkeypatch, capsys):
    from freellmpool.cli import main
    assert main(_conf_args("--providers", "NOSUCH", "--json")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown provider 'NOSUCH'" in captured.err


# --- rag ask (P29-P35, P47) ---

def _rag_ask_ns(tmp_path, provider):
    return SimpleNamespace(question="color?", store=str(tmp_path / "r.sqlite3"),
                           k=4, model=None, provider=provider)


def test_rag_ask_unknown_skips_pool_and_embed(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config",
                        _boom("pool load"))
    monkeypatch.setattr("freellmpool.rag._embed_texts", _boom("embed"))
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["NOSUCH"])) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_rag_ask_user_catalog_only_id_rejected(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    monkeypatch.setattr("freellmpool.config.load_catalog",
                        lambda *a, **k: [_provider("customx")])
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["customx"])) == 2
    assert "unknown provider 'customx'" in capsys.readouterr().err


def test_rag_ask_external_only_id_rejected(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    from freellmpool.catalog import ExternalProvider
    ext = ExternalProvider(name="Ext X", slug="extx", category=None, url=None,
                           base_url=None, description="", model_count=0,
                           best_rpd=0, best_rpm=0, best_tpd=0, generous_score=0)
    monkeypatch.setattr("freellmpool.catalog.load_external_catalog",
                        lambda *a, **k: [ext])
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["extx"])) == 2
    assert "unknown provider 'extx'" in capsys.readouterr().err


def test_rag_ask_plugin_only_id_rejected(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli, plugins
    monkeypatch.setattr(plugins, "registered_providers",
                        lambda: [_provider("plugx")])
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["plugx"])) == 2
    assert "unknown provider 'plugx'" in capsys.readouterr().err


def test_rag_ask_literal_beats_empty_store(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    monkeypatch.setattr("freellmpool.rag._embed_texts", _boom("embed"))
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["NOSUCH"])) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "vector store is empty" not in captured.err


def test_rag_ask_mixed_rejects(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    ns = _rag_ask_ns(tmp_path, ["groq", "NOSUCH"])
    assert managed_cli.cmd_rag_ask(ns) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


def test_rag_ask_empty_literal_is_unknown(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, [""])) == 2
    assert "unknown provider ''" in capsys.readouterr().err


def test_rag_ask_dead_registry_is_clean_exit_2(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    def _boom_registry(*args, **kwargs):
        raise OSError("simulated dead registry")
    monkeypatch.setattr(managed_cli, "load_registry", _boom_registry)
    assert managed_cli.cmd_rag_ask(_rag_ask_ns(tmp_path, ["groq"])) == 2
    captured = capsys.readouterr()
    assert "provider registry is unavailable" in captured.err
    assert captured.out == ""


# --- rag leaderboard (P36-P39) ---

def _empty_pool(monkeypatch):
    from freellmpool import managed_cli
    pool = SimpleNamespace(providers=[], embedders=[])
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config",
                        lambda **kwargs: pool)
    return pool


def test_board_unknown_names_literal(monkeypatch, capsys):
    from freellmpool import managed_cli
    _empty_pool(monkeypatch)
    ns = SimpleNamespace(providers="NOSUCH", k=3)
    assert managed_cli.cmd_rag_leaderboard(ns) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_board_chat_only_known_id_keeps_empty_message(monkeypatch, capsys):
    from freellmpool import managed_cli
    _empty_pool(monkeypatch)
    ns = SimpleNamespace(providers="groq", k=3)
    assert managed_cli.cmd_rag_leaderboard(ns) == 0
    captured = capsys.readouterr()
    assert "No embedding routes to rank." in captured.out
    assert "unknown provider" not in captured.err


def test_board_custom_only_id_keeps_empty_message(monkeypatch, capsys):
    from freellmpool import managed_cli
    _empty_pool(monkeypatch)
    monkeypatch.setattr("freellmpool.config.load_catalog",
                        lambda *a, **k: [_provider("customx")])
    ns = SimpleNamespace(providers="customx", k=3)
    assert managed_cli.cmd_rag_leaderboard(ns) == 0
    captured = capsys.readouterr()
    assert "No embedding routes to rank." in captured.out
    assert "unknown provider" not in captured.err


def test_board_mixed_rejects(monkeypatch, capsys):
    from freellmpool import managed_cli
    _empty_pool(monkeypatch)
    ns = SimpleNamespace(providers="groq,NOSUCH", k=3)
    assert managed_cli.cmd_rag_leaderboard(ns) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


# --- discovery.main (P40-P45, P48) ---

def _discovery_env(monkeypatch, tmp_path):
    path = tmp_path / "state" / "discovery.json"
    monkeypatch.setenv("FREELLMPOOL_DISCOVERY_FILE", str(path))
    return path


def test_discovery_refresh_unknown_skips_mkdir(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    path = _discovery_env(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "refresh_catalog", _boom("refresh_catalog"))
    assert d.main(["--provider", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""
    assert not path.parent.exists()


def test_discovery_check_sources_unknown_writes_nothing(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    state = _discovery_env(monkeypatch, tmp_path)
    target = tmp_path / "hashes.json"
    monkeypatch.setattr(d, "refresh_catalog", _boom("refresh_catalog"))
    monkeypatch.setattr(d, "check_public_sources", _boom("check_public_sources"))
    assert d.main(["--provider", "NOSUCH", "--check-sources", str(target)]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""
    assert not target.exists()
    assert not state.parent.exists()


def test_discovery_renew_unknown_writes_nothing(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    state = _discovery_env(monkeypatch, tmp_path)
    evidence = tmp_path / "evidence.json"
    monkeypatch.setenv("FREELLMPOOL_EVIDENCE_FILE", str(evidence))
    monkeypatch.setattr(d, "refresh_catalog", _boom("refresh_catalog"))
    monkeypatch.setattr(d, "refresh_evidence", _boom("refresh_evidence"))
    assert d.main(["--provider", "NOSUCH", "--renew-evidence"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""
    assert not evidence.exists()
    assert not evidence.with_name(evidence.name + ".lock").exists()
    assert not state.parent.exists()


def test_discovery_public_only_fork_loads_bare_registry(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    _discovery_env(monkeypatch, tmp_path)
    calls = []
    real = d.load_registry

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(d, "load_registry", _spy)
    monkeypatch.setattr(d, "refresh_catalog",
                        lambda *a, **k: {"generation": 1, "providers": {}})
    assert d.main(["--public-only", "--provider", "groq"]) == 0
    assert d.main(["--provider", "groq"]) == 0
    assert calls[0] == ((), {})
    assert len(calls[1][0]) == 1 and isinstance(calls[1][0][0], dict)


def test_discovery_mixed_rejects(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    _discovery_env(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "refresh_catalog", _boom("refresh_catalog"))
    assert d.main(["--provider", "groq", "--provider", "NOSUCH"]) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert "'groq'" not in captured.err


def test_discovery_empty_literal_is_unknown(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    _discovery_env(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "refresh_catalog", _boom("refresh_catalog"))
    assert d.main(["--provider", ""]) == 2
    assert "unknown provider ''" in capsys.readouterr().err


def test_discovery_dead_registry_is_clean_exit_2(tmp_path, monkeypatch, capsys):
    from freellmpool import discovery as d
    _discovery_env(monkeypatch, tmp_path)

    def _boom_registry(*args, **kwargs):
        raise OSError("simulated dead registry")
    monkeypatch.setattr(d, "load_registry", _boom_registry)
    assert d.main(["--provider", "groq"]) == 2
    captured = capsys.readouterr()
    assert "provider registry is unavailable" in captured.err
    assert captured.out == ""
