"""Local catalog validation used by CI and ``freellmpool doctor``."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config
from .capability import capability_table, model_capability
from .config import _safe_local_catalog_url, load_catalog, load_embedders, load_transcribers
from .models import Provider

_ADAPTERS = {"openai", "gemini", "cloudflare"}
_AUTH = {"bearer", "none"}
_MAX_MODEL_ID = 200
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._:/@+-]{0,199}$")


def normalize_model_listing(payload: Any) -> tuple[str, ...]:
    """Return bounded, safe canonical model names and aliases from common listings."""

    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("models")
    else:
        rows = payload
    if not isinstance(rows, list):
        return ()
    found: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            candidates: list[Any] = [row.get("id") or row.get("name")]
            aliases = row.get("aliases")
            if isinstance(aliases, list):
                candidates.extend(aliases)
        else:
            candidates = [row]
        for model_id in candidates:
            if (
                isinstance(model_id, str)
                and 1 <= len(model_id) <= _MAX_MODEL_ID
                and _SAFE_MODEL_ID.fullmatch(model_id)
            ):
                found.add(model_id)
    return tuple(sorted(found))


def _valid_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not any(ch in value for ch in "\r\n\t")
    ) or _safe_local_catalog_url(value)


def _check_group(name: str, providers: list[Provider]) -> list[str]:
    errors: list[str] = []
    ids = Counter(p.id for p in providers)
    for provider_id, count in ids.items():
        if count > 1:
            errors.append(f"{name}: duplicate provider id {provider_id!r}")
    for provider in providers:
        prefix = f"{name}:{provider.id}"
        if provider.adapter not in _ADAPTERS:
            errors.append(f"{prefix}: unsupported adapter {provider.adapter!r}")
        if provider.auth not in _AUTH:
            errors.append(f"{prefix}: unsupported auth {provider.auth!r}")
        if not _valid_url(provider.base_url):
            errors.append(
                f"{prefix}: base_url must be https or canonical literal loopback "
                "without control chars"
            )
        if not provider.models:
            errors.append(f"{prefix}: no models configured")
        model_names = Counter(model.name for model in provider.models)
        for model_name, count in model_names.items():
            if count > 1:
                errors.append(f"{prefix}: duplicate model {model_name!r}")
        for model in provider.models:
            if model.rpd < 0:
                errors.append(f"{prefix}/{model.name}: rpd must be non-negative")
            if model.context is not None and model.context <= 0:
                errors.append(f"{prefix}/{model.name}: context must be positive")
    return errors


def _raw_catalog_errors(path: Path) -> list[str]:
    """Diagnose malformed input before tolerant parsing discards or coerces it."""
    signature = config._path_signature(path)
    if not signature[1]:
        return [f"{path}: catalog file is missing or inaccessible"]
    data, error = config._read_toml_cached(signature)
    if error is not None:
        detail = "invalid TOML syntax" if error[0] == "toml_syntax" else "catalog could not be read"
        return [f"{path}: {detail}"]
    errors: list[str] = []
    for section in ("provider", "embedder", "transcriber"):
        rows = data.get(section, [])
        if not isinstance(rows, list):
            errors.append(f"{path}:{section}: provider rows must be an array")
            continue
        for index, row in enumerate(rows):
            prefix = f"{path}:{section}[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{prefix}: provider row must be a table")
                continue
            for field in ("id", "base_url"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    errors.append(f"{prefix}: {field} must be a non-empty string")
            for field in ("key_optional", "local"):
                if field in row and not isinstance(row[field], bool):
                    errors.append(f"{prefix}: {field} must be a boolean")
            models = row.get("models", [])
            if not isinstance(models, list):
                errors.append(f"{prefix}: models must be an array")
                continue
            for model_index, model in enumerate(models):
                model_prefix = f"{prefix}.models[{model_index}]"
                if not isinstance(model, dict):
                    errors.append(f"{model_prefix}: model row must be a table")
                    continue
                if not isinstance(model.get("name"), str) or not model["name"].strip():
                    errors.append(f"{model_prefix}: name must be a non-empty string")
                for field in ("enabled", "auto"):
                    if field in model and not isinstance(model[field], bool):
                        errors.append(f"{model_prefix}: {field} must be a boolean")
            if not config._parse_rows([row]):
                errors.append(f"{prefix}: provider row was rejected by the runtime parser")
    return errors


def validate_catalog(path: Path | None = None) -> list[str]:
    """Validate provider, embedder, and transcriber rows from ``path`` or bundled catalog."""
    paths = [path or config._PACKAGED_CATALOG]
    if path is None:
        user_path = config._user_catalog_path()
        if user_path is not None and user_path.exists():
            paths.append(user_path)
    providers = load_catalog(path)
    table = capability_table()
    errors = [
        *(error for source in paths for error in _raw_catalog_errors(source)),
        *_check_group("provider", providers),
        *_check_group("embedder", load_embedders(path)),
        *_check_group("transcriber", load_transcribers(path)),
    ]
    for provider in providers:
        for model in provider.models:
            score = model_capability(model.name, table)
            if not 0.0 <= score <= 1.0:
                errors.append(f"provider:{provider.id}/{model.name}: invalid capability score {score}")
    return errors
