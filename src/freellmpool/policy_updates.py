"""Activate reviewed policy data from one trusted repository, never remote code."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from ._version import __version__
from .client_setup import atomic_write
from .http_read import ACCEPT_ENCODING, bounded_response_bytes

JSON = dict[str, Any]
DEFAULT_REPOSITORY = "pauljones0/freellmpool"
_MAX_BYTES = 2_000_000
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class IncompatiblePolicy(ValueError):
    """The new data needs a reviewed client/schema change."""


def _path(env: Mapping[str, str], name: str, variable: str) -> Path:
    root = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Path(env.get(variable) or root / "freellmpool" / name).expanduser()


def bundle_path(env: Mapping[str, str]) -> Path:
    return _path(env, "policy-bundle.json", "FREELLMPOOL_POLICY_BUNDLE_FILE")


def _status_path(env: Mapping[str, str]) -> Path:
    return _path(env, "policy-update.json", "FREELLMPOOL_POLICY_STATUS_FILE")


def _unique(pairs: list[tuple[str, Any]]) -> JSON:
    result: JSON = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous policy JSON")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite policy number")


def _decode(data: bytes) -> JSON:
    if len(data) > _MAX_BYTES:
        raise ValueError("policy data is too large")
    value = json.loads(data, object_pairs_hook=_unique, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("policy data must be an object")
    return value


def _read(path: Path) -> JSON:
    if path.is_symlink() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("invalid policy file")
    return _decode(path.read_bytes())


def _packaged() -> JSON:
    return _read(Path(__file__).with_name("provider_registry.json"))


def _version(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("invalid policy client version")
    return tuple(int(part) for part in value.split("."))


def _number(value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)) or not 0 <= cast(float, value) <= 10**18:
        raise ValueError("invalid policy capacity")


def _strings(value: object) -> None:
    if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(x, str) or not x or len(x) > 1024 for x in value):
        raise ValueError("invalid policy model list")


def _rows(value: object) -> dict[str, JSON]:
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError("invalid policy rows")
    result: dict[str, JSON] = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in result:
            raise ValueError("invalid or duplicate policy ID")
        result[row["id"]] = row
    return result


def _unchanged_except(before: JSON, after: JSON, mutable: set[str]) -> None:
    if {k: v for k, v in before.items() if k not in mutable} != {k: v for k, v in after.items() if k not in mutable}:
        raise IncompatiblePolicy("policy changes protected fields; update the client after review")


def validate_document(document: JSON, packaged: JSON) -> None:
    """Keep credentials, quota identities and no-charge mechanisms client-reviewed."""
    if document.get("schema") != 1 or type(document.get("schema")) is not int:
        raise IncompatiblePolicy("unsupported policy schema")
    if set(document) - {"schema", "reviewed_at", "providers", "tombstones"}:
        raise ValueError("unknown policy document fields")
    old, new = _rows(packaged.get("providers")), _rows(document.get("providers"))
    old_tombstones, new_tombstones = _rows(packaged.get("tombstones", [])), _rows(document.get("tombstones", []))
    if (not new.keys() <= old.keys() or not new_tombstones.keys() <= old_tombstones.keys()
            or any(row != old_tombstones[ident] for ident, row in new_tombstones.items())):
        raise IncompatiblePolicy("new provider identities or changed tombstones require a client update")
    for pid, before in old.items():
        if pid not in new:
            continue  # Deactivation cannot enlarge the set of free routes.
        after = new[pid]
        _unchanged_except(before, after, {"limits", "evidence", "grants", "model_costs", "blocked_models", "paid_required_models"})
        for name in ("blocked_models", "paid_required_models"):
            if name in after:
                _strings(after[name])
        previous_limits, limits = _rows(before.get("limits", [])), _rows(after.get("limits", []))
        if previous_limits.keys() != limits.keys():
            raise IncompatiblePolicy("new or removed quota definitions require a client update")
        for ident, rule in limits.items():
            mutable = {"capacity", "maximum_documented", "model_capacities", "capacity_note", "accounting_note", "evidence_ids"}
            if previous_limits[ident].get("scope") == "model":
                mutable.add("model_ids")
            _unchanged_except(previous_limits[ident], rule, mutable)
            for field in ("capacity", "maximum_documented"):
                if field in rule:
                    _number(rule[field], nullable=True)
            if "model_ids" in rule:
                _strings(rule["model_ids"])
            capacities = rule.get("model_capacities", {})
            if not isinstance(capacities, dict) or len(capacities) > 10000:
                raise ValueError("invalid model quota capacities")
            for model_id, amount in capacities.items():
                if not isinstance(model_id, str) or not model_id or len(model_id) > 1024:
                    raise ValueError("invalid model quota identity")
                _number(amount)
        previous_grants, grants = _rows(before.get("grants", [])), _rows(after.get("grants", []))
        if previous_grants.keys() != grants.keys():
            raise IncompatiblePolicy("new free grants require a client update")
        for ident, grant in grants.items():
            _unchanged_except(previous_grants[ident], grant, {"model_selector", "evidence_ids", "status"})
            if grant.get("status") not in {"verified", "conditional", "excluded", "unverified", "unknown", "unsupported"}:
                raise ValueError("invalid grant verification status")
            selector = grant.get("model_selector", {})
            if not isinstance(selector, dict):
                raise ValueError("invalid grant selector")
            _unchanged_except(previous_grants[ident].get("model_selector", {}), selector, {"models", "exclude", "suffix"})
            for name in ("models", "exclude"):
                if name in selector:
                    _strings(selector[name])
            if "suffix" in selector and (not isinstance(selector["suffix"], str) or not selector["suffix"] or len(selector["suffix"]) > 64):
                raise ValueError("invalid free selector suffix")
        previous_evidence, evidence = _rows(before.get("evidence", [])), _rows(after.get("evidence", []))
        if previous_evidence.keys() != evidence.keys():
            raise IncompatiblePolicy("new policy evidence identities require a client update")
        for ident, source in evidence.items():
            _unchanged_except(previous_evidence[ident], source, {"url", "status", "checked_at", "expires_at", "source_hash", "claim"})
            if not isinstance(source.get("status"), str) or source["status"] not in {"verified", "official", "observed", "unverified", "unknown", "expired"}:
                raise ValueError("invalid evidence verification status")
            url = urlsplit(str(source.get("url", "")))
            old_url = urlsplit(str(previous_evidence[ident].get("url", "")))
            if url.scheme != "https" or url.hostname != old_url.hostname or url.username or url.password or url.port not in (None, 443):
                raise IncompatiblePolicy("new policy source origins require a client update")
            for field in ("checked_at", "expires_at"):
                value = source.get(field)
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError("invalid policy evidence timestamp")
            lifetime = (datetime.fromisoformat(source["expires_at"]) - datetime.fromisoformat(source["checked_at"])).total_seconds()
            if not 0 < lifetime <= 7 * 86400:
                raise ValueError("policy evidence freshness exceeds review interval")
            digest = source.get("source_hash")
            if digest is not None and (not isinstance(digest, dict) or digest.get("algorithm") not in {"visible_text_v1", "raw_body_v1", "modelscope_article_v1", "discourse_first_post_v1"} or not _DIGEST.fullmatch(str(digest.get("sha256", "")))):
                raise ValueError("invalid policy source digest")
        for row in [*grants.values(), *limits.values()]:
            refs = row.get("evidence_ids", [])
            _strings(refs)
            if any(ref not in evidence for ref in refs):
                raise ValueError("policy references missing evidence")
        costs = after.get("model_costs", {})
        if not isinstance(costs, dict) or len(costs) > 10000:
            raise ValueError("invalid model costs")
        for cost in costs.values():
            if not isinstance(cost, dict):
                raise ValueError("invalid model cost row")
            for field, value in cost.items():
                if field in {"neurons_per_input_token", "neurons_per_cached_input_token", "neurons_per_output_token", "minimum_audio_seconds"}:
                    try:
                        amount = Decimal(str(value))
                        if isinstance(value, bool) or not amount.is_finite() or not 0 <= amount <= 10**18:
                            raise ValueError("invalid model cost")
                    except InvalidOperation as exc:
                        raise ValueError("invalid model cost") from exc
                elif field != "evidence_ids":
                    raise IncompatiblePolicy("new cost units require a client update")
            _strings(cost.get("evidence_ids", []))
            if any(ref not in evidence for ref in cost.get("evidence_ids", [])):
                raise ValueError("model cost references missing evidence")


def _bundle(env: Mapping[str, str]) -> JSON:
    data = _read(bundle_path(env))
    if data.get("repository") != env.get("FREELLMPOOL_POLICY_REPOSITORY", DEFAULT_REPOSITORY):
        raise ValueError("cached policy belongs to another repository")
    if data.get("schema") != 1 or type(data.get("revision")) is not int or data["revision"] < 1 or not _SHA.fullmatch(str(data.get("commit", ""))):
        raise ValueError("invalid cached policy bundle")
    raw = json.dumps(data.get("registry"), sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(raw).hexdigest() != data.get("canonical_sha256") or not isinstance(data.get("registry"), dict):
        raise ValueError("cached policy digest mismatch")
    try:
        floor = _read(_status_path(env))
    except (OSError, ValueError, TypeError, RecursionError):
        floor = {}
    if floor.get("repository") == data["repository"] and type(floor.get("revision")) is int:
        if data["revision"] < floor["revision"] or (data["revision"] == floor["revision"]
                and floor.get("source_sha256") != data.get("source_sha256")):
            raise ValueError("restored policy is older than the accepted revision")
    return data


def load_policy_document(env: Mapping[str, str], packaged: JSON) -> JSON:
    if env.get("FREELLMPOOL_POLICY_UPDATES") == "0":
        return packaged
    if not bundle_path(env).exists() and not _status_path(env).exists():
        return packaged
    try:
        if not bundle_path(env).exists() and "revision" not in _read(_status_path(env)):
            return packaged
        data = _bundle(env)
        validate_document(data["registry"], packaged)
        return cast(JSON, data["registry"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        # Restoring the packaged policy could undo a remotely reviewed restriction.
        return {"schema": 1, "providers": [], "tombstones": packaged.get("tombstones", [])}


def load_policy_status(env: Mapping[str, str]) -> JSON:
    if env.get("FREELLMPOOL_POLICY_UPDATES") == "0":
        return {"status": "disabled", "reason": "Policy channel disabled; using packaged rules."}
    try:
        status = _read(_status_path(env))
        result: JSON = {k: status[k] for k in ("status", "reason", "revision", "commit", "checked_at", "last_attempt_at", "source_sha256", "repository") if k in status}
    except (OSError, ValueError, TypeError, RecursionError):
        result = {"status": "not_checked", "reason": "Run freellmpool maintenance --refresh."}
    if bundle_path(env).exists():
        try:
            active = _bundle(env)
            for field in ("revision", "commit", "source_sha256", "repository", "checked_at"):
                result[field] = active[field]
            validate_document(active["registry"], _packaged())
        except IncompatiblePolicy:
            result.update(status="error", reason="Cached policy is incompatible with the installed client; run freellmpool maintenance --refresh.")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            result.update(status="error", reason="Cached policy is invalid; run freellmpool maintenance --refresh.")
    elif "revision" in result:
        result.update(status="error", reason="Active policy is missing; run freellmpool maintenance --refresh.")
    return result


def _fetch(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": ACCEPT_ENCODING}, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f"policy source HTTP {response.status_code}")
        return bounded_response_bytes(response, _MAX_BYTES)


def refresh_policy(env: Mapping[str, str], *, client: httpx.Client | None = None,
                   packaged: JSON | None = None) -> JSON:
    if env.get("FREELLMPOOL_POLICY_UPDATES") == "0":
        return load_policy_status(env)
    packaged = _packaged() if packaged is None else copy.deepcopy(packaged)
    path = bundle_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    repository = env.get("FREELLMPOOL_POLICY_REPOSITORY", DEFAULT_REPOSITORY)
    now = datetime.now(UTC).isoformat()
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    owned = client is None
    session = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    try:
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = load_policy_status(env)
            result: JSON = {"status": "error", "last_attempt_at": now,
                            **{k: previous[k] for k in ("revision", "commit", "checked_at", "source_sha256", "repository") if k in previous}}
            try:
                if not _REPOSITORY.fullmatch(repository) or any(part in {".", ".."} for part in repository.split("/")):
                    raise ValueError("invalid trusted policy repository")
                repair = False
                try:
                    if not path.exists() and "revision" in previous:
                        raise ValueError("active policy is missing")
                    current = _bundle(env) if path.exists() else {}
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    # The last successful status is a recovery floor. Without it we
                    # cannot prove that repairing a corrupt bundle avoids rollback.
                    if (previous.get("repository") != repository or type(previous.get("revision")) is not int
                            or not _DIGEST.fullmatch(str(previous.get("source_sha256", "")))):
                        raise ValueError("cached policy needs explicit operator recovery") from None
                    current = previous
                    repair = True
                commit = _decode(_fetch(session, f"https://api.github.com/repos/{repository}/commits/HEAD")).get("sha")
                if not isinstance(commit, str) or not _SHA.fullmatch(commit):
                    raise ValueError("invalid policy source commit")
                base = f"https://raw.githubusercontent.com/{repository}/{commit}"
                manifest = _decode(_fetch(session, base + "/maintenance/policy-channel.json"))
                revision = manifest.get("revision")
                if manifest.get("schema") != 1 or type(manifest.get("schema")) is not int or type(revision) is not int or revision < 1 or revision > 2**53:
                    raise ValueError("invalid policy channel manifest")
                if _version(manifest.get("minimum_client_version")) > _version(__version__):
                    raise IncompatiblePolicy("reviewed policy requires a newer client")
                raw = _fetch(session, base + "/src/freellmpool/provider_registry.json")
                digest = hashlib.sha256(raw).hexdigest()
                if digest != manifest.get("registry_sha256"):
                    raise ValueError("policy source digest mismatch")
                if revision < current.get("revision", 0) or (revision == current.get("revision") and digest != current.get("source_sha256")):
                    raise ValueError("policy rollback or reused revision rejected")
                document = _decode(raw)
                validate_document(document, packaged)
                canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                bundle = {"schema": 1, "revision": revision, "commit": commit, "repository": repository,
                          "source_sha256": digest, "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
                          "checked_at": now, "registry": document}
                atomic_write(path, json.dumps(bundle, indent=2) + "\n")
                result.update(status="unchanged" if not repair and revision == current.get("revision") else "ok", revision=revision,
                              commit=commit, checked_at=now, source_sha256=digest, repository=repository,
                              reason="Reviewed policy data checked.")
            except IncompatiblePolicy as exc:
                result.update(status="requires_client_update", reason=str(exc))
            except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError, httpx.HTTPError):
                result.update(status="error", reason="Policy update failed validation or could not be fetched; prior rules retained.")
            try:
                atomic_write(_status_path(env), json.dumps(result, indent=2) + "\n")
            except (OSError, ValueError):
                result.update(status="error", reason="Policy status could not be saved.")
            return result
    finally:
        if owned:
            session.close()
