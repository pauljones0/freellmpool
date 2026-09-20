"""First-run discovery bootstrap: `main()` refreshes the catalog once when state is missing."""
import argparse
import json
import os
from pathlib import Path

import pytest

from freellmpool.cli import _needs_bootstrap, main


def _del_opt_out(monkeypatch):
    monkeypatch.delenv("FREELLMPOOL_NO_AUTO_DISCOVERY", raising=False)


def _stub_refresh(monkeypatch, *, fail=False):
    calls = []

    def fake(env, **kwargs):
        calls.append((dict(env), kwargs))
        if fail:
            raise RuntimeError("simulated offline first run")
        return {}

    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", fake)
    return calls


def _ask_args():
    return ["ask", "hi", "--max-tokens", "1", "--timeout", "5"]


@pytest.fixture(autouse=True)
def _no_stdin(monkeypatch):
    monkeypatch.setattr("freellmpool.cli._read_stdin", lambda: "")


def test_first_run_ask_bootstraps_discovery(monkeypatch, capsys):
    _del_opt_out(monkeypatch)
    calls = _stub_refresh(monkeypatch)
    assert not Path(os.environ["FREELLMPOOL_DISCOVERY_FILE"]).exists()
    assert main(_ask_args()) == 4
    assert len(calls) == 1
    assert "freellmpool update" in capsys.readouterr().err


def test_existing_discovery_skips_bootstrap(monkeypatch):
    _del_opt_out(monkeypatch)
    calls = _stub_refresh(monkeypatch)
    discovery_file = Path(os.environ["FREELLMPOOL_DISCOVERY_FILE"])
    discovery_file.parent.mkdir(parents=True, exist_ok=True)
    discovery_file.write_text(json.dumps({"schema": 1, "providers": {}}))
    assert main(_ask_args()) == 4
    assert calls == []


def test_opt_out_skips_bootstrap(monkeypatch):
    assert os.environ["FREELLMPOOL_NO_AUTO_DISCOVERY"] == "1"
    calls = _stub_refresh(monkeypatch)
    assert main(_ask_args()) == 4
    assert calls == []


def test_bootstrap_failure_degrades_gracefully(monkeypatch, capsys):
    _del_opt_out(monkeypatch)
    calls = _stub_refresh(monkeypatch, fail=True)
    assert main(_ask_args()) == 4
    assert len(calls) == 1
    assert "freellmpool update" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["ask", "battle", "tokenmax", "proxy", "mcp"])
def test_bootstrap_command_set(command):
    assert _needs_bootstrap(argparse.Namespace(command=command)) is True


@pytest.mark.parametrize("command", ["models", "keys", "status", "update", "init", "doctor", "version"])
def test_non_bootstrap_commands(command):
    assert _needs_bootstrap(argparse.Namespace(command=command)) is False


def test_bootstrap_nested_run_commands():
    assert _needs_bootstrap(argparse.Namespace(command="jobs", jobs_command="run")) is True
    assert _needs_bootstrap(argparse.Namespace(command="jobs", jobs_command="list")) is False
    assert _needs_bootstrap(argparse.Namespace(command="recipe", recipe_command="run")) is True
    assert _needs_bootstrap(argparse.Namespace(command="recipe", recipe_command="list")) is False
