#!/usr/bin/env python3
"""Validate policy publication, or prepare its manifest after a reviewed edit.

CI should supply its trusted comparison commit with ``--base``. Omitting it is
useful for the first public commit and checks the current files only.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from freellmpool.client_setup import atomic_write  # noqa: E402
from freellmpool.policy_updates import IncompatiblePolicy, validate_document  # noqa: E402

JSON = dict[str, Any]
_MANIFEST = "maintenance/policy-channel.json"
_REGISTRY = "src/freellmpool/provider_registry.json"
_VERSION = "src/freellmpool/_version.py"
_MAX_BYTES = 2_000_000
_MAX_REVISION = 2**53


def _unique(pairs: list[tuple[str, Any]]) -> JSON:
    result: JSON = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _decode(raw: bytes) -> JSON:
    if len(raw) > _MAX_BYTES:
        raise ValueError("policy file exceeds size limit")
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("policy JSON must be an object")
    return value


def _read(path: Path) -> bytes:
    if path.is_symlink() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("invalid policy file")
    return path.read_bytes()


def _version(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("client version must have three numeric components")
    return tuple(int(part) for part in value.split("."))


def _client_version(raw: bytes) -> tuple[int, ...]:
    # Read a literal rather than executing a file from the comparison commit.
    tree = ast.parse(raw)
    versions = [node.value.value for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)]
    if len(versions) != 1:
        raise ValueError("missing or ambiguous literal client version")
    return _version(versions[0])


def _manifest(raw: bytes) -> JSON:
    value = _decode(raw)
    if set(value) != {"schema", "revision", "minimum_client_version", "registry_sha256"}:
        raise ValueError("invalid policy manifest fields")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("unsupported policy manifest schema")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= _MAX_REVISION:
        raise ValueError("invalid policy revision")
    _version(value["minimum_client_version"])
    if not isinstance(value["registry_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["registry_sha256"]):
        raise ValueError("invalid registry digest")
    return value


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=root, check=False, capture_output=True,
                                timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("could not read Git comparison base") from exc
    if result.returncode:
        raise ValueError("could not read Git comparison base; fetch the requested commit")
    if len(result.stdout) > _MAX_BYTES:
        raise ValueError("Git comparison data exceeds size limit")
    return result.stdout


def _baseline(root: Path, base: str | None) -> tuple[JSON, bytes, tuple[int, ...]] | None:
    if base is None:
        return None
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("invalid Git comparison base")
    files = _git(root, "ls-tree", "--name-only", commit, "--", _MANIFEST).decode().splitlines()
    if _MANIFEST not in files:
        # Existing projects can introduce their first policy channel later.
        return None
    manifest = _manifest(_git(root, "show", f"{commit}:{_MANIFEST}"))
    raw = _git(root, "show", f"{commit}:{_REGISTRY}")
    if hashlib.sha256(raw).hexdigest() != manifest["registry_sha256"]:
        raise ValueError("comparison base has a mismatched registry digest")
    version = _client_version(_git(root, "show", f"{commit}:{_VERSION}"))
    if _version(manifest["minimum_client_version"]) > version:
        raise ValueError("comparison base requires an unavailable client version")
    return manifest, raw, version


def check_channel(root: Path, *, base: str | None = None, prepare: bool = False) -> JSON:
    """Return the validated manifest; preparation changes only that manifest."""
    manifest = _manifest(_read(root / _MANIFEST))
    raw = _read(root / _REGISTRY)
    document = _decode(raw)
    digest = hashlib.sha256(raw).hexdigest()
    version = _client_version(_read(root / _VERSION))
    if _version(manifest["minimum_client_version"]) > version:
        raise ValueError("minimum_client_version exceeds the current client version")
    validate_document(document, document)
    previous = _baseline(root, base)
    if prepare:
        manifest["registry_sha256"] = digest
        if previous is not None:
            old, old_raw, _ = previous
            manifest["revision"] = max(manifest["revision"], old["revision"] + (raw != old_raw))
            if manifest["revision"] > _MAX_REVISION:
                raise ValueError("policy revision exceeds supported range")
    if manifest["registry_sha256"] != digest:
        raise ValueError("registry digest mismatch; review changes and run --prepare")
    if previous is not None:
        old, old_raw, old_version = previous
        if manifest["revision"] < old["revision"] or (raw != old_raw and manifest["revision"] <= old["revision"]):
            raise ValueError("registry changes require a strictly increased policy revision")
        try:
            validate_document(document, _decode(old_raw))
        except IncompatiblePolicy as exc:
            if _version(manifest["minimum_client_version"]) <= old_version:
                raise ValueError("protected policy changes require a newer minimum_client_version and client release") from exc
    if prepare:
        atomic_write(root / _MANIFEST, json.dumps(manifest, indent=2) + "\n", mode=0o644)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_ROOT, help="checkout to validate")
    parser.add_argument("--base", help="trusted Git commit/ref to compare; omit for initial publication")
    parser.add_argument("--prepare", action="store_true", help="write a matching manifest digest and required revision bump after review")
    args = parser.parse_args(argv)
    try:
        manifest = check_channel(args.root, base=args.base, prepare=args.prepare)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError, SyntaxError) as exc:
        print(f"Policy channel invalid: {exc}", file=sys.stderr)
        return 1
    action = "Prepared" if args.prepare else "Validated"
    print(f"{action} policy revision {manifest['revision']} (minimum client {manifest['minimum_client_version']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
