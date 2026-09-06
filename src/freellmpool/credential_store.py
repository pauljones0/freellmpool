"""Shared private credential persistence for setup and compatibility commands."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tomllib
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path

from .client_setup import atomic_write


def _secret(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("enter one non-empty credential line without control characters")
    return value


def _toml_string(value: str) -> str:
    # JSON escapes C0 controls; TOML additionally forbids literal DEL. Keep
    # scalar Unicode intact because JSON surrogate escapes are invalid TOML.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(_toml_key(str(key)) + " = " + _toml_value(item) for key, item in value.items()) + " }"
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


def save_key_values(values: dict[str, str], path: Path) -> Path:
    """Atomically merge keys while preserving all existing TOML value types."""
    validated = {}
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid credential environment name")
        validated[name] = _secret(value)
    with _write_lock(path):
        try:
            data = tomllib.loads(path.read_text()) if path.exists() else {}
        except (ValueError, UnicodeError):
            raise ValueError("existing credential configuration is invalid; it was not changed") from None
        if not isinstance(data.get("keys", {}), dict):
            raise ValueError("existing keys table is invalid; it was not changed")
        data.setdefault("keys", {}).update(validated)
        rendered = "\n".join(_toml_key(key) + " = " + _toml_value(value) for key, value in data.items()) + "\n"
        # Validate generated syntax before replacing the original credential file.
        tomllib.loads(rendered)
        atomic_write(path, rendered)
    return path
