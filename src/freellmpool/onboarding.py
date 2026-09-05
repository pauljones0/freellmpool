"""Resumable, private, one-provider-at-a-time free-access setup."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .client_setup import atomic_write
from .config import effective_env
from .key_inventory import default_config_path, redact_secrets

JSON = dict[str, Any]  # Validated catalog, account, and persisted progress schemas.
Input = Callable[[str], str]
Output = Callable[[str], None]
AccountSaver = Callable[[str, JSON, dict[str, str]], None]
ProviderCheck = Callable[[str, dict[str, str]], JSON]
BrowserOpener = Callable[[str], object]

_RECURRING = {"zero_price", "recurring_quota", "recurring_credit"}
_STATUS_TEXT = {
    "ok": "Provider check completed; listing access is not proof of a free allowance.",
    "auth_missing": "Required credential or account field is missing. Re-run this provider after adding it.",
    "auth_failed": "Authentication or permission was rejected. Check the key and the permissions above.",
    "rate_limited": "The catalog check hit a rate limit. Wait, then choose r to retry.",
    "unsupported": "This provider has no supported non-billable authentication/listing check. Your key is saved; it is not marked invalid.",
    "partial": "The catalog check was incomplete. Your key is saved; maintenance can retry.",
    "error": "The check could not complete. Your key is saved; follow the diagnostic and retry this provider.",
}


def _secret(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("enter one non-empty credential line without control characters")
    return value


def read_clipboard() -> str:
    """Capture a local clipboard without shell interpolation or secret output."""
    candidates = (
        ("wl-paste", "--no-newline"),
        ("xclip", "-selection", "clipboard", "-out"),
        ("pbpaste",),
        ("termux-clipboard-get",),
    )
    for command in candidates:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(list(command), check=True, capture_output=True, text=True, timeout=10)
            return _secret(result.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
    raise ValueError("clipboard unavailable; choose hidden keyboard input instead")


def _hidden_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise ValueError("hidden key input requires a terminal; use the clipboard helper or --stdin")
    return getpass.getpass(prompt)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(key)) + " = " + _toml_value(item) for key, item in value.items()) + " }"
    raise ValueError("unsupported existing TOML value; configuration was not changed")


@contextlib.contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl
        except ImportError:  # Windows still gets atomic replacement.
            pass
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_key_values(values: dict[str, str], path: Path | None = None) -> Path:
    """Atomically merge keys while preserving all existing TOML value types."""
    validated = {}
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid credential environment name")
        validated[name] = _secret(value)
    path = path or default_config_path()
    with _write_lock(path):
        try:
            data = tomllib.loads(path.read_text()) if path.exists() else {}
        except (ValueError, UnicodeError):
            raise ValueError("existing credential configuration is invalid; it was not changed") from None
        if not isinstance(data.get("keys", {}), dict):
            raise ValueError("existing keys table is invalid; it was not changed")
        data.setdefault("keys", {}).update(validated)
        rendered = "\n".join(json.dumps(key) + " = " + _toml_value(value) for key, value in data.items()) + "\n"
        # Validate generated syntax before replacing the original credential file.
        tomllib.loads(rendered)
        atomic_write(path, rendered)
    return path


def _progress_path(env: Mapping[str, str]) -> Path:
    override = env.get("FREELLMPOOL_SETUP_STATE_PATH")
    if override:
        return Path(override).expanduser()
    config = env.get("FREELLMPOOL_CONFIG_FILE")
    return (Path(config).expanduser().parent if config else Path.home() / ".config/freellmpool") / "setup-progress.json"


def _safe_note(value: object, secrets_to_hide: Iterable[str]) -> str:
    text = str(value or "")
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = redact_secrets(text)
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in text)[:300]


def _account_current(provider_id: str, grants: Sequence[JSON], env: dict[str, str],
                     key_env: str | None = None) -> bool:
    required = [grant for grant in grants if grant.get("requires_account_evidence") or grant.get("required_account_conditions")]
    if not required:
        return True
    from .free_policy import credential_fingerprint, fresh, load_accounts

    current = load_accounts(env).get(provider_id, {})
    credential_ref = credential_fingerprint(provider_id, env.get(key_env) if key_env else None)
    now = datetime.now(UTC)
    current_is_fresh = fresh(current.get("verified_at"), current.get("expires_at"), now.timestamp())
    tiers = list(dict.fromkeys(str(grant.get("required_account_tier") or "free") for grant in required))
    matching_current = [grant for grant in required if str(grant.get("required_account_tier") or "free") == current.get("tier")]
    conditions_match = any(
        all(current.get(key) == value and type(current.get(key)) is type(value) for key, value in grant.get("required_account_conditions", {}).items())
        for grant in matching_current
    )
    return bool(current_is_fresh and current.get("tier") in tiers and conditions_match and current.get("credential_ref") == credential_ref)


def _attest_account(provider_id: str, grants: Sequence[JSON], env: dict[str, str],
                    input_fn: Input, output: Output, account_saver: AccountSaver | None = None,
                    key_env: str | None = None) -> str:
    if _account_current(provider_id, grants, env, key_env):
        return "continue"
    from .free_policy import credential_fingerprint, save_account

    required = [grant for grant in grants if grant.get("requires_account_evidence") or grant.get("required_account_conditions")]
    tiers = list(dict.fromkeys(str(grant.get("required_account_tier") or "free") for grant in required))
    now = datetime.now(UTC)
    credential_ref = credential_fingerprint(provider_id, env.get(key_env) if key_env else None)
    output("Account check: confirm the actual plan shown in your provider dashboard.")
    for index, tier in enumerate(tiers, 1):
        output(f"  {index}. {tier}")
    answer = input_fn("Plan number (Enter confirms 1), s=leave unverified, q=quit: ").strip().lower()
    if answer in {"s", "q"}:
        return answer
    try:
        tier = tiers[int(answer or "1") - 1]
        if int(answer or "1") < 1:
            raise IndexError
    except (ValueError, IndexError):
        output("Plan was not confirmed. It remains unverified.")
        return "s"
    details: JSON = {"tier": tier, "account_ref": "primary", "verified_at": now.isoformat(), "expires_at": (now + timedelta(days=30)).isoformat(), "evidence_source": "operator", "credential_ref": credential_ref}
    selected = [grant for grant in required if str(grant.get("required_account_tier") or "free") == tier]
    conditions = selected[0].get("required_account_conditions", {})
    if conditions:
        output("This free allowance requires every condition below:")
        for name, value in conditions.items():
            shown = ("yes" if value else "no") if isinstance(value, bool) else str(value)
            output("• " + name.replace("_", " ") + ": " + shown)
        answer = input_fn("After checking all conditions in your account, type CONFIRM; otherwise Enter to leave unverified: ").strip()
        if answer != "CONFIRM":
            return "s"
        details.update(conditions)
    if any(grant.get("paid_overage_possible") and not grant.get("hard_free_boundary") for grant in selected):
        answer = input_fn("Only if paid overage is disabled/capped on this account, type FREE-ONLY; otherwise Enter: ").strip()
        details["no_paid_overage"] = answer == "FREE-ONLY"
    (account_saver or save_account)(provider_id, details, env)
    return "continue"


def _setup_ref(provider_id: str, entry: JSON, credentials: Mapping[str, str]) -> str:
    from .free_policy import credential_fingerprint

    names = set(entry.get("setup", {}).get("required_env", []))
    if entry.get("credential_env"):
        names.add(entry["credential_env"])
    return credential_fingerprint(provider_id, json.dumps({name: credentials.get(name, "") for name in sorted(names)}))


def _already_checked(provider_id: str, entry: JSON, grants: Sequence[JSON],
                     credentials: dict[str, str], progress: JSON) -> bool:
    from .free_policy import timestamp

    checked = timestamp(progress.get("updated_at"))
    now = datetime.now(UTC).timestamp()
    return bool(
        progress.get("status") == "checked" and checked is not None and 0 <= now - checked < 86400
        and progress.get("credential_ref") == _setup_ref(provider_id, entry, credentials)
        and _account_current(provider_id, grants, credentials, entry.get("credential_env"))
    )


def _open_setup_page(url: str | None, opener: BrowserOpener, output: Output) -> None:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        output("Use the official provider link printed above.")
        return
    try:
        # The HTTPS check above excludes a missing URL.
        if opener(cast(str, url)) is False:
            output("No browser opened. Click or copy the provider link above.")
    except Exception:
        output("No browser opened. Click or copy the provider link above.")


def run_onboarding(
    provider: str | None = None, *, resume: bool = True,
    registry: dict[str, JSON] | None = None, env: dict[str, str] | None = None,
    progress_path: Path | None = None, input_fn: Input = input, secret_fn: Input = _hidden_input,
    clipboard_fn: Callable[[], str] = read_clipboard, check: ProviderCheck | None = None,
    output: Output = print, account_saver: AccountSaver | None = None,
    eligibility: Callable[[str], bool] | None = None, probe: Callable[[str], object] | None = None,
    open_url: BrowserOpener = webbrowser.open,
) -> int:
    """Save/check credentials. Inference is optional and separately policy-gated."""
    if registry is None:
        from .provider_registry import load_registry
        registry = load_registry()
    if check is None:
        from .discovery import check_provider
        check = check_provider
    env = dict(os.environ if env is None else env)
    credentials = effective_env(env)
    progress_path = progress_path or _progress_path(env)
    try:
        state = json.loads(progress_path.read_text()) if progress_path.exists() else {"schema": 1, "providers": {}}
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("providers"), dict) or any(not isinstance(value, dict) for value in state["providers"].values()):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("setup progress is invalid; preserve it before resetting setup") from None
    selected = []
    for provider_id, entry in registry.items():
        if provider is not None and provider_id != provider:
            continue
        grants = [grant for grant in entry.get("grants", []) if grant.get("kind") in _RECURRING and grant.get("status") != "excluded"]
        if grants:
            selected.append((provider_id, entry, grants))
    if not selected:
        output("No documented recurring-free setup is available for this selection. Trial/paid-only providers are skipped.")
        return 2

    def save(provider_id: str, status: str, note: str = "") -> None:
        state["providers"][provider_id] = {"status": status, "note": note, "updated_at": datetime.now(UTC).isoformat(), "credential_ref": _setup_ref(provider_id, registry[provider_id], credentials)}
        atomic_write(progress_path, json.dumps(state, indent=2) + "\n")

    def collect(names: Iterable[str], replace: bool = False) -> str:
        values = {}
        for name in names:
            if credentials.get(name) and not replace:
                continue
            while True:
                answer = input_fn(f"{name}: Enter=hidden input, c=clipboard, s=skip, q=quit: ").strip().lower()
                if answer in {"q", "s"}:
                    return answer
                if answer not in {"", "c"}:
                    continue
                try:
                    values[name] = _secret(clipboard_fn() if answer == "c" else secret_fn(f"Paste {name} (hidden): "))
                    break
                except ValueError as exc:
                    output(str(exc))
        if values:
            path = Path(env["FREELLMPOOL_CONFIG_FILE"]).expanduser() if env.get("FREELLMPOOL_CONFIG_FILE") else default_config_path()
            save_key_values(values, path)
            credentials.update(values)
            output("Credential saved privately.")
            overrides = [name for name, value in values.items() if env.get(name) and env[name] != value]
            if overrides:
                output("This terminal still exports the previous value. After setup, run: unset " + " ".join(overrides))
        return "continue"

    try:
        for index, (provider_id, entry, grants) in enumerate(selected, 1):
            if resume and provider is None and _already_checked(provider_id, entry, grants, credentials, state["providers"].get(provider_id, {})):
                continue
            setup = entry.get("setup", {})
            output(f"\n{index}/{len(selected)} — {entry.get('display_name', provider_id)}")
            output(str(setup.get("signup_url") or setup.get("key_url") or "See the provider's official account page."))
            if setup.get("key_url") and setup.get("key_url") != setup.get("signup_url"):
                output("API keys: " + str(setup["key_url"]))
            for step in setup.get("steps", []):
                output("• " + str(step))
            while True:
                answer = input_fn("Enter=continue, o=open key page, s=skip, q=quit and resume later: ").strip().lower()
                if answer == "o":
                    _open_setup_page(setup.get("key_url") or setup.get("signup_url"), open_url, output)
                elif answer in {"", "s", "q"}:
                    break
            if answer == "q":
                return 1
            if answer == "s":
                save(provider_id, "skipped")
                continue
            names = setup.get("required_env", [entry["credential_env"]] if entry.get("credential_env") else [])
            collected = collect(names)
            if collected == "q":
                return 1
            if collected == "s":
                save(provider_id, "skipped")
                continue
            while True:
                save(provider_id, "key_saved")
                attested = _attest_account(provider_id, grants, credentials, input_fn, output, account_saver, entry.get("credential_env"))
                if attested == "q":
                    return 1
                if attested == "s":
                    output("Account tier remains unverified; inference stays subject to the gateway's free gate.")
                try:
                    result = check(provider_id, credentials)
                except Exception:  # Exceptions may contain request credentials.
                    result = {"status": "error", "note": "Check failed before a safe diagnostic was available."}
                status = str(result.get("status", "error"))
                if status not in _STATUS_TEXT:
                    status = "error"
                output(_STATUS_TEXT[status])
                note = _safe_note(result.get("note"), (credentials.get(name, "") for name in names))
                if note:
                    output(note)
                ready = bool(status == "ok" and eligibility is not None and eligibility(provider_id))
                if ready:
                    output("Free routes are admitted by the gateway. Inference has not been tested.")
                elif status == "ok" and eligibility is not None:
                    output("Catalog available. No free route is currently admitted; run freellmpool status for the reason.")
                if ready and probe is not None and input_fn("Run one bounded free test? [y/N]: ").strip().lower() == "y":
                    probe(provider_id)
                final_status = "checked" if status == "ok" else status
                if status == "ok" and attested == "s":
                    final_status = "account_unverified"
                save(provider_id, final_status, note)
                if status in {"ok", "unsupported"}:
                    break
                while True:
                    answer = input_fn("Enter=next provider, r=retry check, k=replace key, o=open key page, q=quit: ").strip().lower()
                    if answer == "o":
                        _open_setup_page(setup.get("key_url") or setup.get("signup_url"), open_url, output)
                    elif answer in {"", "s", "r", "k", "q"}:
                        break
                if answer == "q":
                    return 1
                if answer in {"", "s"}:
                    break
                if answer == "k":
                    replaced = collect(names, replace=True)
                    if replaced == "q":
                        return 1
                    if replaced == "s":
                        break
        output("\nSetup progress saved. Authentication/listing checks never enable paid or unverified routes.")
        output("Resume: freellmpool setup --resume   Retry one: freellmpool setup --provider PROVIDER")
        return 0
    except (EOFError, KeyboardInterrupt):
        output("\nStopped. Saved keys and progress are ready to resume.")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--clipboard", action="store_true", help="save this provider's key from the clipboard")
    parser.add_argument("--stdin", action="store_true", help="read a key privately from standard input")
    args = parser.parse_args(argv)
    if args.clipboard or args.stdin:
        if not args.provider:
            parser.error("key input requires --provider")
        from .provider_registry import load_registry
        record = load_registry().get(args.provider)
        if not record or not record.get("credential_env"):
            parser.error("this provider has no supported credential field")
        value = read_clipboard() if args.clipboard else _secret(sys.stdin.read(16385))
        save_key_values({record["credential_env"]: value})
        print("Credential saved privately. Verify account and permissions with: freellmpool setup --provider " + args.provider)
        return 0
    return run_onboarding(provider=args.provider, resume=args.resume)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
