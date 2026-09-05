"""The published data channel must not silently reuse a revision."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_policy_channel as checker


def write_channel(root, *, revision=1, minimum="0.13.0", version="0.13.0", document=None):
    document = document or {
        "schema": 1,
        "providers": [{"id": "groq", "credential_env": "GROQ_API_KEY", "limits": [{
            "id": "rpd", "metric": "requests", "scope": "organization",
            "window_seconds": 86400, "capacity": 100,
        }], "grants": [], "evidence": []}],
        "tombstones": [],
    }
    raw = (json.dumps(document, indent=2) + "\n").encode()
    manifest = {"schema": 1, "revision": revision, "minimum_client_version": minimum,
                "registry_sha256": hashlib.sha256(raw).hexdigest()}
    for relative, content in {
        "src/freellmpool/provider_registry.json": raw,
        "src/freellmpool/_version.py": f'__version__ = "{version}"\n'.encode(),
        "maintenance/policy-channel.json": (json.dumps(manifest) + "\n").encode(),
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return document, manifest


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def channel(tmp_path):
    write_channel(tmp_path)
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Policy Test", "-c", "user.email=policy@example.invalid",
        "commit", "--quiet", "-m", "Initial policy")
    return tmp_path


def test_initial_publish_checks_without_git_history(tmp_path):
    _, manifest = write_channel(tmp_path)
    assert checker.check_channel(tmp_path) == manifest


def test_unchanged_registry_can_keep_revision(channel):
    assert checker.check_channel(channel, base="HEAD")["revision"] == 1


@pytest.mark.parametrize("revision", [1, 0])
def test_registry_change_requires_strictly_increased_revision(channel, revision):
    document, _ = write_channel(channel)
    document["providers"][0]["limits"][0]["capacity"] = 90
    write_channel(channel, revision=revision, document=document)
    with pytest.raises(ValueError, match="revision"):
        checker.check_channel(channel, base="HEAD")


def test_whitespace_change_also_requires_revision_bump(channel):
    path = channel / "src/freellmpool/provider_registry.json"
    path.write_bytes(path.read_bytes() + b"\n")
    manifest_path = channel / "maintenance/policy-channel.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["registry_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="revision"):
        checker.check_channel(channel, base="HEAD")


def test_increased_revision_allows_reviewed_capacity_update(channel):
    document, _ = write_channel(channel)
    document["providers"][0]["limits"][0]["capacity"] = 90
    write_channel(channel, revision=2, document=document)
    assert checker.check_channel(channel, base="HEAD")["revision"] == 2


def test_unchanged_content_cannot_roll_revision_back(channel):
    write_channel(channel, revision=3)
    git(channel, "add", ".")
    git(channel, "-c", "user.name=Policy Test", "-c", "user.email=policy@example.invalid",
        "commit", "--quiet", "-m", "Revision three")
    write_channel(channel, revision=2)
    with pytest.raises(ValueError, match="revision"):
        checker.check_channel(channel, base="HEAD")


@pytest.mark.parametrize("mutation", ["digest", "minimum", "revision_bool", "schema_bool", "extra", "duplicate"])
def test_malformed_or_incompatible_manifest_is_rejected(channel, mutation):
    path = channel / "maintenance/policy-channel.json"
    manifest = json.loads(path.read_text())
    if mutation == "digest":
        manifest["registry_sha256"] = "0" * 64
    elif mutation == "minimum":
        manifest["minimum_client_version"] = "0.14.0"
    elif mutation == "revision_bool":
        manifest["revision"] = True
    elif mutation == "schema_bool":
        manifest["schema"] = True
    else:
        manifest["extra"] = "unrecognized"
    text = json.dumps(manifest)
    if mutation == "duplicate":
        text = text[:-1] + ', "revision": 2}'
    path.write_text(text)
    with pytest.raises(ValueError):
        checker.check_channel(channel)


def test_unknown_base_is_an_error_not_initial_publish(channel):
    with pytest.raises(ValueError, match="base"):
        checker.check_channel(channel, base="missing-ref")


def test_existing_commit_without_channel_is_initial_publish(channel):
    git(channel, "rm", "maintenance/policy-channel.json")
    git(channel, "-c", "user.name=Policy Test", "-c", "user.email=policy@example.invalid",
        "commit", "--quiet", "-m", "Before channel exists")
    write_channel(channel)
    assert checker.check_channel(channel, base="HEAD")["revision"] == 1


def test_protected_change_requires_a_new_client_minimum(channel):
    document, _ = write_channel(channel)
    document["providers"][0]["credential_env"] = "NEW_KEY"
    write_channel(channel, revision=2, document=document)
    with pytest.raises(ValueError, match="minimum_client_version"):
        checker.check_channel(channel, base="HEAD")
    write_channel(channel, revision=2, version="0.14.0", minimum="0.14.0", document=document)
    assert checker.check_channel(channel, base="HEAD")["minimum_client_version"] == "0.14.0"


def test_prepare_updates_only_manifest_and_preserves_deliberate_higher_revision(channel):
    document, _ = write_channel(channel)
    document["providers"][0]["limits"][0]["capacity"] = 90
    registry = channel / "src/freellmpool/provider_registry.json"
    registry.write_text(json.dumps(document))
    before = registry.read_bytes()
    result = checker.check_channel(channel, base="HEAD", prepare=True)
    assert result["revision"] == 2
    assert registry.read_bytes() == before
    assert checker.check_channel(channel, base="HEAD") == result
    write_channel(channel, revision=7, document=document)
    assert checker.check_channel(channel, base="HEAD", prepare=True)["revision"] == 7


def test_prepare_does_not_write_when_validation_fails(channel):
    document, _ = write_channel(channel)
    changed = copy.deepcopy(document)
    changed["providers"][0]["limits"][0]["capacity"] = -1
    (channel / "src/freellmpool/provider_registry.json").write_text(json.dumps(changed))
    manifest_path = channel / "maintenance/policy-channel.json"
    before = manifest_path.read_bytes()
    with pytest.raises(ValueError):
        checker.check_channel(channel, base="HEAD", prepare=True)
    assert manifest_path.read_bytes() == before


def test_cli_reports_validation_errors_without_traceback(channel, capsys):
    assert checker.main(["--root", str(channel), "--base", "HEAD"]) == 0
    assert "revision 1" in capsys.readouterr().out
    assert checker.main(["--root", str(channel), "--base", "missing"]) == 1
    output = capsys.readouterr()
    assert "base" in output.err
    assert "Traceback" not in output.err


def test_checked_in_channel_validates():
    checker.check_channel(Path(__file__).parents[1])
