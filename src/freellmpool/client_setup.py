"""Private, reversible gateway service and contained OpenCode/Hermes profiles."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit

_AUXILIARY_TASKS = (
    "vision", "web_extract", "compression", "session_search", "skills_hub",
    "approval", "mcp", "title_generation", "triage_specifier",
    "kanban_decomposer", "profile_describer", "curator",
)
_INHERITED_ENV = (
    "HOME", "PATH", "LANG", "LC_ALL", "TERM", "COLORTERM", "DISPLAY",
    "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK", "USER", "LOGNAME",
    "SHELL", "TMPDIR", "OPENCODE_SERVER_PASSWORD",
)


class InstallResult(TypedDict):
    root: str
    wrappers: list[str]
    unit: str
    base_url: str
    status: str


def atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Replace one file without exposing partial contents or widening its mode."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("refusing to replace a symlink with generated configuration")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:
                os.chmod(name, mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _local_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http" or not parsed.hostname
            or not ipaddress.ip_address(parsed.hostname).is_loopback
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError("free client presets require a literal loopback HTTP /v1 endpoint") from None
    return value.rstrip("/")


def generate_client_files(root: Path, base_url: str = "http://127.0.0.1:8080/v1") -> dict[str, str]:
    root = Path(root).resolve()
    base_url = _local_base_url(base_url)
    gateway = "freellmpool/auto"
    opencode = {
        "$schema": "https://opencode.ai/config.json",
        "model": gateway,
        "small_model": gateway,
        "enabled_providers": ["freellmpool"],
        "plugin": [],
        "mcp": {},
        "provider": {
            "freellmpool": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Free pool (strict free access)",
                "options": {"baseURL": base_url, "apiKey": "{file:" + str(root.parent / "proxy.key") + "}"},
                "models": {"auto": {"name": "Available free coding models", "limit": {"context": 32768, "output": 4096}}},
            }
        },
    }
    route = {"provider": "custom", "model": "auto", "base_url": base_url, "fallback_chain": []}
    hermes = {
        "model": {"provider": "custom", "default": "auto", "base_url": base_url, "api_mode": "chat_completions"},
        "providers": {"freellmpool": {"api": base_url, "models": ["auto"], "discover_models": False}},
        "fallback_providers": [],
        "fallback_model": None,
        "auxiliary": {task: dict(route) for task in _AUXILIARY_TASKS},
        "delegation": {**route, "api_mode": "chat_completions", "inherit_mcp_toolsets": False},
        "honcho": {"enabled": False},
        "mcp_servers": {},
    }
    files = {
        "opencode/opencode.json": json.dumps(opencode, indent=2) + "\n",
        # JSON is a YAML subset and avoids an optional runtime YAML dependency.
        "hermes/config.yaml": json.dumps(hermes, indent=2) + "\n",
        "client-manifest.json": json.dumps({"schema": 1, "base_url": base_url}, indent=2) + "\n",
    }
    validate_client_files(files)
    return files


def validate_client_files(files: dict[str, str]) -> None:
    """Refuse profiles that could silently fall back to an unrelated provider."""
    opencode = json.loads(files["opencode/opencode.json"])
    if (
        opencode.get("enabled_providers") != ["freellmpool"]
        or opencode.get("model") != "freellmpool/auto"
        or opencode.get("small_model") != "freellmpool/auto"
        or opencode.get("plugin") != [] or opencode.get("mcp") != {}
        or set(opencode.get("provider", {})) != {"freellmpool"}
    ):
        raise ValueError("OpenCode free profile permits an external model path")
    base_url = _local_base_url(opencode["provider"]["freellmpool"]["options"]["baseURL"])
    hermes = json.loads(files["hermes/config.yaml"])
    if hermes.get("fallback_providers") != [] or hermes.get("fallback_model"):
        raise ValueError("Hermes free profile permits an external fallback")
    routes = [hermes["model"], hermes["delegation"], *hermes["auxiliary"].values()]
    if not set(_AUXILIARY_TASKS).issubset(hermes["auxiliary"]):
        raise ValueError("Hermes free profile is missing an auxiliary route")
    for route in routes:
        if route.get("provider") != "custom" or route.get("base_url") != base_url or route.get("fallback_chain"):
            raise ValueError("Hermes free profile permits an external model path")


def build_launch_environment(client: str, root: Path, proxy_key: str,
                             inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if inherited is None else inherited
    env = {name: inherited[name] for name in _INHERITED_ENV if name in inherited}
    root = Path(root).resolve()
    files = generate_client_files(root)
    config_path = root / "client-manifest.json"
    if config_path.exists():
        base_url = json.loads(config_path.read_text())["base_url"]
        files = generate_client_files(root, base_url)
    if client == "opencode":
        env.update({
            "XDG_CONFIG_HOME": str(root / "opencode" / "xdg-config"),
            "XDG_DATA_HOME": str(root / "opencode" / "xdg-data"),
            "XDG_CACHE_HOME": str(root / "opencode" / "xdg-cache"),
            "XDG_STATE_HOME": str(root / "opencode" / "xdg-state"),
            "OPENCODE_CONFIG": str(root / "opencode" / "opencode.json"),
            "OPENCODE_CONFIG_DIR": str(root / "opencode"),
            "OPENCODE_CONFIG_CONTENT": files["opencode/opencode.json"],
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        })
    elif client == "hermes":
        env.update({
            # Outside ~/.hermes: prevents shared root profile auth discovery.
            "HERMES_HOME": str(root / "hermes"),
            "OPENAI_API_KEY": proxy_key,
            "FREELLMPOOL_PROXY_KEY": proxy_key,
            "CUSTOM_BASE_URL": json.loads(files["client-manifest.json"])["base_url"],
            # Hermes loads the installation checkout's .env even with an
            # isolated profile. Never restore upstream keys through dotenv.
            "PYTHON_DOTENV_DISABLED": "1",
        })
    else:
        raise ValueError("unsupported free client")
    return env


def _ensure_key(path: Path) -> str:
    if path.exists():
        value = path.read_text().strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("invalid local proxy credential file")
        path.chmod(0o600)
        return value
    value = secrets.token_urlsafe(32)
    atomic_write(path, value + "\n")
    return value


def _write_with_backup(path: Path, content: str, *, mode: int = 0o600) -> None:
    if path.exists() and path.read_text() != content:
        backup = path.with_name(path.name + ".before-freellmpool")
        if not backup.exists():
            atomic_write(backup, path.read_text())
    atomic_write(path, content, mode=mode)


def install_command_launcher(binary: Path, *, bin_dir: Path | None = None) -> Path:
    """Expose a private-venv installation without requiring shell activation."""
    binary = binary.expanduser().resolve()
    launcher = (bin_dir or Path.home() / ".local/bin") / "freellmpool"
    if not binary.is_file() or not os.access(binary, os.X_OK) or binary == launcher.absolute():
        raise ValueError("the installed freellmpool executable is missing or invalid")
    _write_with_backup(launcher, "#!/bin/sh\nexec " + shlex.quote(str(binary)) + ' "$@"\n', mode=0o755)
    return launcher


def install_client_setup(
    *, root: Path | None = None, bin_dir: Path | None = None,
    unit_dir: Path | None = None, t3_settings: Path | None = None,
    binaries: dict[str, str] | None = None, base_url: str = "http://127.0.0.1:8080/v1",
) -> InstallResult:
    """Write validated local setup. Starting/restarting services is separate."""
    root = (root or Path.home() / ".config/freellmpool/clients").resolve()
    bin_dir = bin_dir or Path.home() / ".local/bin"
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    t3_settings = t3_settings or Path.home() / ".t3/userdata/settings.json"
    binaries = binaries if binaries is not None else {name: path for name in ("opencode", "hermes") if (path := shutil.which(name))}
    files = generate_client_files(root, base_url)
    # Decode existing settings before touching anything; never overwrite corruption.
    t3 = json.loads(t3_settings.read_text()) if t3_settings.exists() else {}
    if not isinstance(t3, dict) or not isinstance(t3.get("providers", {}), dict):
        raise ValueError("unsupported T3 settings structure")
    if not isinstance(t3.get("providerInstances", {}), dict):
        raise ValueError("unsupported T3 provider instance structure")
    existing_instance = t3.get("providerInstances", {}).get("opencode")
    if existing_instance is not None and (not isinstance(existing_instance, dict) or existing_instance.get("driver") != "opencode" or not isinstance(existing_instance.get("config", {}), dict)):
        raise ValueError("unsupported T3 OpenCode provider instance")
    if not isinstance(t3.get("providers", {}).get("opencode", {}), dict):
        raise ValueError("unsupported T3 OpenCode settings structure")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_key(root.parent / "proxy.key")
    server_key = _ensure_key(root.parent / "opencode-server.key")
    for name, content in files.items():
        _write_with_backup(root / name, content)
    wrappers = []
    clipboard_wrapper = bin_dir / "freellmpool-key-from-clipboard"
    clipboard_command = shlex.join([sys.executable, "-m", "freellmpool.onboarding"])
    _write_with_backup(
        clipboard_wrapper,
        '#!/bin/sh\nset -eu\n[ "$#" -eq 1 ] || { echo "Usage: freellmpool-key-from-clipboard PROVIDER" >&2; exit 2; }\n'
        + "exec " + clipboard_command + ' --provider "$1" --clipboard\n',
        mode=0o755,
    )
    for name, binary in binaries.items():
        if name not in ("opencode", "hermes"):
            continue
        wrapper = bin_dir / (name + "-free")
        command = [sys.executable, "-m", "freellmpool.client_setup", "launch", "--root", str(root), "--client", name, "--binary", binary]
        _write_with_backup(wrapper, "#!/bin/sh\nexec " + shlex.join(command) + ' -- "$@"\n', mode=0o755)
        wrappers.append(str(wrapper))
    if "opencode" in binaries and t3_settings.exists():
        providers = t3.setdefault("providers", {})
        provider = providers.setdefault("opencode", {})
        if not isinstance(provider, dict):
            raise ValueError("unsupported T3 OpenCode settings structure")
        provider.update({"enabled": True, "binaryPath": str(bin_dir / "opencode-free"), "serverUrl": "", "serverPassword": server_key, "customModels": ["freellmpool/auto"]})
        if existing_instance is not None:
            existing_instance["enabled"] = True
            existing_instance.setdefault("config", {}).update(provider)
        # T3 titles, summaries, commit messages and PR text use these separate
        # model selections, whose installed default is otherwise Codex.
        selection = {"instanceId": "opencode", "model": "freellmpool/auto", "options": []}
        t3["textGenerationModelSelection"] = dict(selection)
        t3["sourceControlWriterModelSelection"] = dict(selection)
        _write_with_backup(t3_settings, json.dumps(t3, indent=2) + "\n")
    command = [sys.executable, "-m", "freellmpool.client_setup", "service", "--root", str(root)]
    unit = (
        "[Unit]\nDescription=Authenticated strict-free LLM gateway\nAfter=network-online.target\n"
        "\n[Service]\nType=simple\nUMask=0077\nExecStart=" + shlex.join(command).replace("%", "%%")
        + "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nNoNewPrivileges=true\n"
        "\n[Install]\nWantedBy=default.target\n"
    )
    _write_with_backup(unit_dir / "freellmpool.service", unit, mode=0o644)
    return {"root": str(root), "wrappers": wrappers, "unit": str(unit_dir / "freellmpool.service"), "base_url": base_url, "status": "configured"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "install-command", "launch", "service"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".config/freellmpool/clients")
    parser.add_argument("--client", choices=("opencode", "hermes"))
    parser.add_argument("--binary")
    args, remainder = parser.parse_known_args(argv)
    if args.action == "install-command":
        if not args.binary:
            parser.error("install-command requires --binary")
        launcher = install_command_launcher(Path(args.binary))
        print("Resume setup: " + shlex.join([str(launcher), "setup", "--resume"]))
        if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            print("To use the short command in this terminal: export PATH=" + shlex.quote(str(launcher.parent)) + ':"$PATH"')
        return 0
    if args.action == "install":
        print(json.dumps(install_client_setup(root=args.root), indent=2))
        return 0
    key = (args.root.parent / "proxy.key").read_text().strip()
    if args.action == "service":
        from .cli import main as cli_main
        url = json.loads((args.root / "client-manifest.json").read_text())["base_url"]
        parsed = urlsplit(_local_base_url(url))
        os.environ["FREELLMPOOL_PROXY_KEY"] = key
        os.environ["FREELLMPOOL_LEGACY_ROUTER"] = "0"
        # _local_base_url requires a literal loopback hostname before this point.
        return cli_main(["proxy", "--host", cast(str, parsed.hostname), "--port", str(parsed.port or 80)])
    if not args.client or not args.binary:
        parser.error("launch requires --client and --binary")
    files = {name: (args.root / name).read_text() for name in ("opencode/opencode.json", "hermes/config.yaml")}
    validate_client_files(files)
    environment = build_launch_environment(args.client, args.root, key)
    if args.client == "opencode":
        environment.setdefault("OPENCODE_SERVER_PASSWORD", (args.root.parent / "opencode-server.key").read_text().strip())
    if remainder[:1] == ["--"]:
        remainder = remainder[1:]
    os.execvpe(args.binary, [args.binary, *remainder], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
