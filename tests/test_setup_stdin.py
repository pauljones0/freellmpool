"""G36 setup --stdin: piped keys on the real CLI, privately and honestly.

Requires --provider; unknown literals and TTY stdin refuse with exit 2;
decode/read/save failures never echo the key and never traceback. Offline:
tmp config + stubbed stdin + monkeypatched save/registry.
"""

from __future__ import annotations

import argparse
import io
import tomllib

import pytest

from freellmpool import managed_cli

SENTINEL = "G36-SENTINEL-SECRET-KEY"


def _setup_args(**kwargs):
    base = {"provider": None, "resume": True, "no_clients": True,
            "no_start": True, "stdin": True}
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.fixture()
def _config(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("FREELLMPOOL_CONFIG_FILE", str(path))
    state = tmp_path / "setup-progress.json"
    monkeypatch.setenv("FREELLMPOOL_SETUP_STATE_PATH", str(state))
    return path


def _read_keys(path):
    return tomllib.loads(path.read_bytes().decode("utf-8")).get("keys", {})


class _TtyStdin(io.StringIO):
    def isatty(self):
        return True


class _ClosedStdin:
    def isatty(self):
        raise ValueError("I/O operation on closed file.")

    def read(self, *args):
        raise ValueError("I/O operation on closed file.")


class _DecodeErrorStdin(io.StringIO):
    def read(self, *args):
        raise UnicodeDecodeError("utf-8", b"\xff" + SENTINEL.encode(),
                                 0, 1, "invalid start byte")


# --- flag contract (B16-B18, B20, B27) ---

def test_stdin_without_provider_is_usage_error(_config, capsys):
    assert managed_cli.cmd_setup(_setup_args(provider=None)) == 2
    captured = capsys.readouterr()
    assert "freellmpool: key input requires --provider" in captured.err
    assert captured.out == ""


def test_stdin_unknown_provider_names_literal(_config, capsys):
    assert managed_cli.cmd_setup(_setup_args(provider="NOSUCH")) == 2
    captured = capsys.readouterr()
    assert "unknown provider 'NOSUCH'" in captured.err
    assert captured.out == ""


def test_stdin_provider_without_credential_field(_config, capsys):
    assert managed_cli.cmd_setup(_setup_args(provider="kilo")) == 2
    captured = capsys.readouterr()
    assert "this provider has no supported credential field" in captured.err
    assert captured.out == ""


def test_stdin_success_canonicalizes_guidance(_config, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="GROQ")) == 0
    assert ("freellmpool setup --provider groq"
            in capsys.readouterr().out)


def test_stdin_ignores_other_setup_flags(_config, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    args = _setup_args(provider="groq", resume=False, no_clients=False,
                       no_start=False)
    assert managed_cli.cmd_setup(args) == 0
    state = tmp_path / "setup-progress.json"
    assert not state.exists()  # save-a-key never touches setup progress


# --- success + secrecy (B19, B26) ---

def test_stdin_piped_key_saved_privately(_config, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("setup-clients must not run for --stdin")
    monkeypatch.setattr(managed_cli, "cmd_setup_clients", _boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 0
    captured = capsys.readouterr()
    assert "Credential saved privately." in captured.out
    assert _read_keys(_config) == {"GROQ_API_KEY": SENTINEL}
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_stdin_save_failure_never_echoes_key(_config, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise OSError("simulated save failure")
    monkeypatch.setattr(managed_cli, "save_key_values", _boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    captured = capsys.readouterr()
    assert "simulated save failure" in captured.err
    assert SENTINEL not in captured.err
    assert captured.out == ""


# --- input edges (B21-B25, B28, B30-B31) ---

def test_stdin_empty_leaves_config_untouched(_config, monkeypatch, capsys):
    _config.write_bytes(b"[keys]\nkeep = true\n")
    before = _config.read_bytes()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    assert "enter one non-empty credential line" in capsys.readouterr().err
    assert _config.read_bytes() == before


def test_stdin_over_cap_rejected(_config, monkeypatch, capsys):
    _config.write_bytes(b"[keys]\n")
    before = _config.read_bytes()
    monkeypatch.setattr("sys.stdin", io.StringIO("k" * 16385))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    assert _config.read_bytes() == before


def test_stdin_max_length_accepted(_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("k" * 16384))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 0
    assert _read_keys(_config) == {"GROQ_API_KEY": "k" * 16384}


@pytest.mark.parametrize("payload", ["a\nb", "   \n", "a\x00b", "a\x7fb"])
def test_stdin_malformed_lines_rejected(_config, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    assert "enter one non-empty credential line" in capsys.readouterr().err


def test_stdin_tty_refuses_without_reading(_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _TtyStdin(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    captured = capsys.readouterr()
    assert "--stdin reads a piped key" in captured.err
    assert SENTINEL not in captured.err
    assert captured.out == ""


def test_stdin_decode_error_sanitized(_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _DecodeErrorStdin())
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    captured = capsys.readouterr()
    assert "could not decode key from standard input" in captured.err
    assert SENTINEL not in captured.err
    assert captured.out == ""


def test_stdin_closed_stream_is_read_failure(_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _ClosedStdin())
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    captured = capsys.readouterr()
    assert "could not read key from standard input" in captured.err
    assert captured.out == ""


def test_stdin_padded_over_cap_rejected(_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(" " + "k" * 16384))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    assert "enter one non-empty credential line" in capsys.readouterr().err


# --- registry failure + trial-only (B29, B32) ---

def test_stdin_registry_load_failure_is_clean_exit_2(_config, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise OSError("simulated corrupt install")
    monkeypatch.setattr(managed_cli, "load_registry", _boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="groq")) == 2
    captured = capsys.readouterr()
    assert "provider registry is unavailable" in captured.err
    assert SENTINEL not in captured.err
    assert captured.out == ""


def test_stdin_trial_only_id_saves_key(_config, monkeypatch, capsys, tmp_path):
    from freellmpool.onboarding import run_onboarding

    registry = {"trialx": {"credential_env": "TRIALX_KEY",
                           "grants": [{"kind": "one_time_credit",
                                       "status": "verified"}]}}
    monkeypatch.setattr(managed_cli, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL + "\n"))
    assert managed_cli.cmd_setup(_setup_args(provider="trialx")) == 0
    assert _read_keys(_config) == {"TRIALX_KEY": SENTINEL}
    # Self-proof: the same row selection-skips in interactive setup.
    output = []
    assert run_onboarding(provider="trialx", registry=registry, env={},
                          progress_path=tmp_path / "p.json",
                          output=output.append) == 2
    assert any("Trial/paid-only providers are skipped" in line for line in output)
