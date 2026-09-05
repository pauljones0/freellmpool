"""Generated free client profiles keep all model traffic inside one gateway."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from freellmpool.client_setup import (
    atomic_write,
    build_launch_environment,
    generate_client_files,
    install_client_setup,
    main,
    validate_client_files,
)


def test_generated_profiles_contain_all_inference_and_no_credentials(tmp_path):
    files = generate_client_files(tmp_path, "http://127.0.0.1:8080/v1")
    validate_client_files(files)
    opencode = json.loads(files["opencode/opencode.json"])
    assert opencode["enabled_providers"] == ["freellmpool"]
    assert opencode["model"] == opencode["small_model"] == "freellmpool/auto"
    assert opencode["plugin"] == []
    assert opencode["provider"]["freellmpool"]["options"]["apiKey"].startswith("{file:")
    hermes = json.loads(files["hermes/config.yaml"])
    assert hermes["model"]["provider"] == "custom"
    assert hermes["fallback_providers"] == []
    for slot in [*hermes["auxiliary"].values(), hermes["delegation"]]:
        assert slot["provider"] == "custom"
        assert slot["base_url"] == "http://127.0.0.1:8080/v1"
        assert slot.get("fallback_chain", []) == []


def test_launch_environment_drops_upstream_keys_and_conflicting_configs(tmp_path):
    inherited = {
        "HOME": "/unchanged/home",
        "PATH": "/usr/bin",
        "OPENROUTER_API_KEY": "must-not-pass",
        "ANTHROPIC_API_KEY": "must-not-pass",
        "OPENCODE_CONFIG_CONTENT": "untrusted",
        "CUSTOM_BASE_URL": "https://paid.example",
    }
    for client in ("opencode", "hermes"):
        env = build_launch_environment(client, tmp_path, "synthetic-local-key", inherited)
        assert env["HOME"] == inherited["HOME"]
        assert "OPENROUTER_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("CUSTOM_BASE_URL") != "https://paid.example"
        if client == "opencode":
            assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
            assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
            assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["enabled_providers"] == ["freellmpool"]
        else:
            assert env["HERMES_HOME"] == str(tmp_path / "hermes")
            assert env["OPENAI_API_KEY"] == "synthetic-local-key"
            assert env["PYTHON_DOTENV_DISABLED"] == "1"


@pytest.mark.parametrize("url", ["http://0.0.0.0:8080/v1", "https://paid.example/v1", "http://key@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1?key=secret"])
def test_free_presets_reject_nonlocal_or_credential_urls(tmp_path, url):
    with pytest.raises(ValueError):
        generate_client_files(tmp_path, url)


def test_atomic_private_write_retains_original_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    path.write_text("old")
    before = set(tmp_path.iterdir())
    def fail(*args):
        raise OSError("synthetic failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert set(tmp_path.iterdir()) == before


def test_install_is_idempotent_preserves_t3_settings_and_uses_private_keys(tmp_path):
    root = tmp_path / "clients"
    t3 = tmp_path / "t3-settings.json"
    t3.write_text(json.dumps({"theme": "dark", "providers": {"codex": {"enabled": True}}}))
    kwargs = dict(root=root, bin_dir=tmp_path / "bin", unit_dir=tmp_path / "units", t3_settings=t3, binaries={"opencode": "/usr/bin/opencode", "hermes": "/usr/bin/hermes"})
    first = install_client_setup(**kwargs)
    key = (root.parent / "proxy.key").read_text()
    second = install_client_setup(**kwargs)
    assert (root.parent / "proxy.key").read_text() == key
    assert len(key.strip()) >= 40
    assert (root.parent / "proxy.key").stat().st_mode & 0o777 == 0o600
    settings = json.loads(t3.read_text())
    assert settings["theme"] == "dark"
    assert settings["providers"]["codex"] == {"enabled": True}
    assert settings["providers"]["opencode"]["binaryPath"] == str(tmp_path / "bin" / "opencode-free")
    selection = {"instanceId": "opencode", "model": "freellmpool/auto", "options": []}
    assert settings["textGenerationModelSelection"] == selection
    assert settings["sourceControlWriterModelSelection"] == selection
    assert key not in json.dumps(first) + json.dumps(second)
    assert key.strip() not in (tmp_path / "units" / "freellmpool.service").read_text()
    assert str(Path(root)) in (tmp_path / "bin" / "opencode-free").read_text()


def test_existing_t3_instance_cannot_shadow_the_free_adapter(tmp_path):
    t3 = tmp_path / "t3.json"
    t3.write_text(json.dumps({"providerInstances": {"opencode": {"driver": "opencode", "enabled": False, "config": {"serverUrl": "https://paid.example"}}}}))
    install_client_setup(root=tmp_path / "clients", bin_dir=tmp_path / "bin", unit_dir=tmp_path / "units", t3_settings=t3, binaries={"opencode": "/bin/opencode"})
    settings = json.loads(t3.read_text())
    instance = settings["providerInstances"]["opencode"]
    assert instance["enabled"] is True
    assert instance["config"]["serverUrl"] == ""
    assert instance["config"]["binaryPath"] == str(tmp_path / "bin" / "opencode-free")


def test_service_uses_actual_proxy_command_and_never_key_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("FREELLMPOOL_PROXY_KEY", "")
    monkeypatch.setenv("FREELLMPOOL_LEGACY_ROUTER", "1")
    root = tmp_path / "clients"
    install_client_setup(root=root, bin_dir=tmp_path / "bin", unit_dir=tmp_path / "units", binaries={})
    captured = []
    monkeypatch.setattr("freellmpool.cli.main", lambda argv: captured.append(argv) or 0)
    assert main(["service", "--root", str(root)]) == 0
    assert captured == [["proxy", "--host", "127.0.0.1", "--port", "8080"]]
    assert os.environ["FREELLMPOOL_PROXY_KEY"] not in repr(captured)
    assert os.environ["FREELLMPOOL_LEGACY_ROUTER"] == "0"


def test_bootstrap_installs_this_checkout_and_preserves_setup_arguments(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "calls"
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/sh\nif [ "$1 $2 $3" = "tool dir --bin" ]; then dirname "$0"; else printf "%s\\n" "$@" >> "$BOOTSTRAP_TEST_LOG"; fi\n')
    cli = fake_bin / "freellmpool"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$BOOTSTRAP_TEST_LOG"\n')
    uv.chmod(0o755)
    cli.chmod(0o755)
    source = Path(__file__).resolve().parents[1]
    subprocess.run(["/bin/sh", str(source / "integrations/setup/bootstrap.sh"), "--provider", "groq"], env={"PATH": str(fake_bin) + ":/usr/bin:/bin", "BOOTSTRAP_TEST_LOG": str(log)}, check=True, capture_output=True, text=True)
    assert log.read_text().splitlines() == ["tool", "install", "--force", str(source), "setup", "--provider", "groq"]
