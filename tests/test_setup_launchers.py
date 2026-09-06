"""Installers give usable commands and distinguish files from running services."""

import argparse
import io
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from freellmpool import client_setup, managed_cli


def setup_result(tmp_path, wrappers=()):
    return {"root": str(tmp_path / "client settings"), "wrappers": list(wrappers),
            "unit": str(tmp_path / "units/freellmpool.service"), "base_url": "http://127.0.0.1:8080/v1", "status": "configured"}


def run_clients(monkeypatch, result, *, manager=None, no_start=False, fail=False):
    monkeypatch.setattr(client_setup, "install_client_setup", lambda: result)
    monkeypatch.setattr(managed_cli, "install_maintenance", lambda: ["freellmpool-update.timer"])
    monkeypatch.setattr(managed_cli.shutil, "which", lambda name: manager)
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if fail:
            raise subprocess.CalledProcessError(1, command, stderr="private-system-error")
    monkeypatch.setattr(managed_cli.subprocess, "run", execute)
    output = io.StringIO()
    with redirect_stdout(output):
        result = managed_cli.cmd_setup_clients(argparse.Namespace(no_start=no_start))
    return result, output.getvalue(), calls


def test_no_service_manager_has_exact_foreground_recovery(monkeypatch, tmp_path):
    result = setup_result(tmp_path)
    code, output, calls = run_clients(monkeypatch, result)
    assert code == 0 and not calls
    assert "not started" in output.lower()
    command = next(line.removeprefix("Run: ") for line in output.splitlines() if line.startswith("Run: "))
    assert shlex.split(command) == [sys.executable, "-m", "freellmpool.client_setup", "service", "--root", result["root"]]
    assert "maintenance --refresh" in output


def test_absent_clients_are_not_advertised_as_installed(monkeypatch, tmp_path):
    _, output, _ = run_clients(monkeypatch, setup_result(tmp_path))
    assert "No supported coding client" in output
    assert "Start with opencode-free or hermes-free" not in output
    assert "setup-clients" in output


def test_only_installed_launcher_is_suggested(monkeypatch, tmp_path):
    wrapper = str(tmp_path / "bin/opencode-free")
    _, output, _ = run_clients(monkeypatch, setup_result(tmp_path, [wrapper]), manager="/bin/systemctl")
    assert wrapper in output
    assert "hermes-free" not in output


def test_no_start_is_distinguished_from_running_gateway(monkeypatch, tmp_path):
    _, output, calls = run_clients(monkeypatch, setup_result(tmp_path), manager="/bin/systemctl", no_start=True)
    assert not calls and "not started" in output.lower()
    assert "freellmpool-update.timer" in output


def test_unavailable_user_service_bus_has_safe_recovery(monkeypatch, tmp_path):
    code, output, calls = run_clients(monkeypatch, setup_result(tmp_path), manager="/bin/systemctl", fail=True)
    assert code != 0 and calls
    assert "not confirmed" in output.lower() and "freellmpool.client_setup" in output
    assert "private-system-error" not in output


def test_persistent_command_launcher_quotes_paths_and_preserves_arguments(tmp_path):
    binary = tmp_path / "installation with spaces/cli"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    binary.chmod(0o700)
    bin_dir = tmp_path / "bin with spaces"
    launcher = client_setup.install_command_launcher(binary, bin_dir=bin_dir)
    result = subprocess.run([str(launcher), "setup", "argument with spaces"], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["setup", "argument with spaces"]
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_non_uv_bootstrap_installs_persistent_resume_command(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text('''#!/bin/sh
set -eu
if [ "$1" = '-c' ]; then exit 0; fi
[ "$1 $2" = '-m venv' ] || exit 9
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'EOF'
#!/bin/sh
if [ "$1 $2" = '-m pip' ]; then exit 0; fi
exec "$SETUP_TEST_PYTHON" "$@"
EOF
cat > "$3/bin/freellmpool" <<'EOF'
#!/bin/sh
printf '%s\\n' "$@" > "$SETUP_TEST_ARGS"
EOF
chmod 700 "$3/bin/python" "$3/bin/freellmpool"
''')
    fake_python.chmod(0o700)
    env = {"HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "data"),
           "PATH": str(fake_bin) + ":/usr/bin:/bin", "SETUP_TEST_PYTHON": sys.executable,
           "SETUP_TEST_ARGS": str(tmp_path / "arguments"), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(["/bin/sh", str(Path(__file__).parents[1] / "integrations/setup/bootstrap.sh"), "--no-start"],
                            env=env, capture_output=True, text=True, check=True)
    launcher = Path(env["HOME"]) / ".local/bin/freellmpool"
    assert launcher.exists()
    assert str(launcher) in result.stdout
    assert "PATH" in result.stdout
    assert (tmp_path / "arguments").read_text().splitlines() == ["setup", "--no-start"]
    subprocess.run([str(launcher), "setup", "--resume"], env=env, check=True)
    assert (tmp_path / "arguments").read_text().splitlines() == ["setup", "--resume"]
