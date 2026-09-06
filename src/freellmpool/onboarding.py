"""Resumable, private, one-provider-at-a-time free-access setup."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .client_setup import atomic_write
from .config import effective_env
from .credential_store import _secret
from .credential_store import _write_lock as _write_lock
from .credential_store import save_key_values as _save_key_values
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


def save_key_values(values: dict[str, str], path: Path | None = None) -> Path:
    """Save setup credentials through the shared atomic configuration writer."""
    return _save_key_values(values, path or default_config_path())


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
    from .free_policy import _valid_account, credential_fingerprint, default_accounts_path, fresh

    try:
        current = _read_account_document(default_accounts_path(env))["providers"].get(provider_id, {})
    except ValueError:
        return False
    if not _valid_account(current):
        return False
    credential_ref = credential_fingerprint(provider_id, env.get(key_env) if key_env else None)
    current_is_fresh = fresh(current.get("verified_at"), current.get("expires_at"), datetime.now(UTC).timestamp())
    for grant in grants:
        if grant.get("requires_account_evidence") and (
            not current_is_fresh or current.get("tier") not in _grant_tiers(grant)
            or current.get("credential_ref") != credential_ref
        ):
            continue
        conditions = grant.get("required_account_conditions", {})
        if not isinstance(conditions, Mapping):
            continue
        if conditions:
            matches = current_is_fresh
            for key, expected in conditions.items():
                value = current.get(key, current.get("tier") if key == "plan" else None)
                matches = matches and (value is expected if isinstance(expected, bool) else value == expected)
            if not matches:
                continue
        return True
    return not grants


def _grant_tiers(grant: JSON) -> list[str]:
    raw = grant.get("required_account_tier", "free")
    values = [raw] if isinstance(raw, str) else raw
    return list(dict.fromkeys(values)) if isinstance(values, list) and all(isinstance(tier, str) and tier for tier in values) else []


def _read_account_document(path: Path) -> JSON:
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            raise ValueError
        document = json.loads(path.read_text()) if path.exists() else {"schema": 1, "providers": {}}
        if (not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1
                or not isinstance(document.get("providers"), dict)):
            raise ValueError
        return cast(JSON, document)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("existing account file is invalid; preserve and repair it before continuing setup") from None


def _save_account_confirmation(provider_id: str, details: JSON, env: dict[str, str]) -> None:
    """Repair one account under its writer lock, preserving actual exclusions."""
    from .free_policy import _account_lock, _valid_account, default_accounts_path

    path = default_accounts_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _account_lock(path):
        document = _read_account_document(path)
        previous = document["providers"].get(provider_id, {})
        if not isinstance(previous, dict):
            raise ValueError("existing account record is invalid; preserve and repair it before continuing setup")
        # Invalid billing fields can be replaced by the new confirmation. Invalid
        # exclusions are ambiguous operator intent and must not be silently erased.
        if any(not _valid_account({field: previous[field]}) for field in ("disabled", "disabled_models", "manual_models") if field in previous):
            raise ValueError("existing account exclusions are invalid; preserve and repair them before continuing setup")
        if "account_ref" in previous and not _valid_account({"account_ref": previous["account_ref"]}):
            raise ValueError("existing quota identity is invalid; preserve and repair it before continuing setup")
        repaired = {field: value for field, value in previous.items() if _valid_account({field: value})}
        repaired.update(details)
        if not _valid_account(repaired):
            raise ValueError("account confirmation is invalid; account file was not changed")
        document["providers"][provider_id] = repaired
        atomic_write(path, json.dumps(document, indent=2) + "\n")


def _attest_account(provider_id: str, grants: Sequence[JSON], env: dict[str, str],
                    input_fn: Input, output: Output, account_saver: AccountSaver | None = None,
                    key_env: str | None = None) -> str:
    if _account_current(provider_id, grants, env, key_env):
        return "continue"
    from .free_policy import credential_fingerprint

    required = [grant for grant in grants if grant.get("requires_account_evidence") or grant.get("required_account_conditions")]
    if len(required) < len(grants):
        # An account-independent grant needs no new billing claim, but malformed
        # metadata still blocks admission and must not be marked resumably done.
        (account_saver or _save_account_confirmation)(provider_id, {}, env)
        output("Account metadata repaired; existing exclusions and quota identity were preserved.")
        return "continue"
    options = [(tier, grant) for grant in required for tier in _grant_tiers(grant)]
    if not options:
        output("No supported account confirmation is available for this grant. It remains unverified.")
        return "s"
    now = datetime.now(UTC)
    credential_ref = credential_fingerprint(provider_id, env.get(key_env) if key_env else None)
    output("Account check: confirm the actual plan shown in your provider dashboard.")
    for index, (tier, grant) in enumerate(options, 1):
        label = tier
        if sum(other_tier == tier for other_tier, _ in options) > 1:
            label += " — " + ", ".join(name.replace("_", " ") + ": " + str(value)
                                       for name, value in grant.get("required_account_conditions", {}).items())
        output(f"  {index}. {label}")
    answer = input_fn("Plan number (Enter confirms 1), s=leave unverified, q=quit: ").strip().lower()
    if answer in {"s", "q"}:
        return answer
    try:
        tier, selected = options[int(answer or "1") - 1]
        if int(answer or "1") < 1:
            raise IndexError
    except (ValueError, IndexError):
        output("Plan was not confirmed. It remains unverified.")
        return "s"
    details: JSON = {"tier": tier, "verified_at": now.isoformat(), "expires_at": (now + timedelta(days=30)).isoformat(), "evidence_source": "operator", "credential_ref": credential_ref}
    conditions = selected.get("required_account_conditions", {})
    if conditions:
        output("This free allowance requires every condition below:")
        for name, value in conditions.items():
            shown = ("yes" if value else "no") if isinstance(value, bool) else str(value)
            output("• " + name.replace("_", " ") + ": " + shown)
        answer = input_fn("After checking all conditions in your account, type CONFIRM; otherwise Enter to leave unverified: ").strip()
        if answer != "CONFIRM":
            return "s"
        details.update(conditions)
    if selected.get("paid_overage_possible") and not selected.get("hard_free_boundary"):
        answer = input_fn("Only if paid overage is disabled/capped on this account, type FREE-ONLY; otherwise Enter: ").strip()
        details["no_paid_overage"] = answer == "FREE-ONLY"
    (account_saver or _save_account_confirmation)(provider_id, details, env)
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
    env = dict(os.environ if env is None else env)
    credentials = effective_env(env)
    if registry is None:
        from .provider_registry import load_registry
        registry = load_registry(credentials)
    if check is None:
        from .discovery import check_provider
        check = check_provider
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
        grants = [grant for grant in entry.get("grants", []) if grant.get("kind") in _RECURRING and grant.get("status") in {"verified", "conditional"}]
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
        record = load_registry(effective_env()).get(args.provider)
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
