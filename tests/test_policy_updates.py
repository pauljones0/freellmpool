"""Reviewed remote data cannot change credential destinations or erase accounting."""

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from freellmpool import policy_updates as policy


@pytest.fixture
def channel(tmp_path):
    document = json.loads(
        (Path(__file__).parents[1] / "src/freellmpool/provider_registry.json").read_text()
    )
    env = {
        "FREELLMPOOL_POLICY_BUNDLE_FILE": str(tmp_path / "bundle.json"),
        "FREELLMPOOL_POLICY_STATUS_FILE": str(tmp_path / "status.json"),
        "SECRET_API_KEY": "must-never-be-sent",
    }
    return document, env


def client_for(document, revision=1, *, digest=None, commit="a" * 40, minimum="0.13.0", status=200):
    data = json.dumps(document).encode()
    manifest = {
        "schema": 1,
        "revision": revision,
        "minimum_client_version": minimum,
        "registry_sha256": digest or hashlib.sha256(data).hexdigest(),
    }

    def respond(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        if request.url.host == "api.github.com":
            return httpx.Response(status, json={"sha": commit})
        assert request.url.host == "raw.githubusercontent.com"
        assert f"/{commit}/" in request.url.path
        if request.url.path.endswith("maintenance/policy-channel.json"):
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)


def test_valid_reviewed_capacity_update_is_atomic_private_and_read_without_network(channel):
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["limits"][0]["capacity"] = 3
    with client_for(changed) as client:
        result = policy.refresh_policy(env, client=client, packaged=packaged)
    assert result["status"] == "ok"
    assert policy.load_policy_document(env, packaged) == changed
    assert Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]).stat().st_mode & 0o777 == 0o600
    assert "must-never-be-sent" not in json.dumps(result)
    assert policy.load_policy_status(env)["revision"] == 1


def test_status_reports_incompatible_active_bundle_after_client_provider_removal(channel, monkeypatch):
    packaged, env = channel
    with client_for(packaged) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    current = copy.deepcopy(packaged)
    current["providers"] = current["providers"][1:]
    monkeypatch.setattr(policy, "_packaged", lambda: current)
    paths = [Path(env[key]) for key in ("FREELLMPOOL_POLICY_BUNDLE_FILE", "FREELLMPOOL_POLICY_STATUS_FILE")]
    previous = [path.read_bytes() for path in paths]
    assert policy.load_policy_document(env, current)["providers"] == []
    status = policy.load_policy_status(env)
    assert status["status"] == "error"
    assert "incompatible" in status["reason"]
    assert status["revision"] == 1
    assert [path.read_bytes() for path in paths] == previous
    with client_for(current, revision=2) as client:
        assert policy.refresh_policy(env, client=client, packaged=current)["status"] == "ok"
    assert policy.load_policy_status(env)["status"] == "ok"


@pytest.mark.parametrize(
    "mutation",
    [
        "endpoint",
        "credential",
        "auth",
        "scope",
        "window",
        "unit",
        "shared_membership",
        "new_provider",
        "tombstone",
        "grant",
        "duplicate",
    ],
)
def test_incompatible_or_ambiguous_policy_is_rejected(channel, mutation):
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    row = changed["providers"][0]
    if mutation == "endpoint":
        row["api_base_url"] = "https://attacker.invalid/v1"
    elif mutation == "credential":
        row["credential_env"] = "OTHER_KEY"
    elif mutation == "auth":
        row["inference_auth"] = "bearer"
    elif mutation == "scope":
        row["limits"][0]["scope"] = "model"
    elif mutation == "window":
        row["limits"][0]["window_seconds"] = 2
    elif mutation == "unit":
        row["limits"][0]["metric"] = "micro_usd"
    elif mutation == "shared_membership":
        row["limits"][0]["model_ids"] = ["only-one-model"]
    elif mutation == "new_provider":
        changed["providers"].append({"id": "new"})
    elif mutation == "tombstone":
        changed["tombstones"][0]["reason"] = "Unreviewed replacement"
    elif mutation == "grant":
        row["grants"][0]["hard_free_boundary"] = False
    else:
        changed["providers"].append(copy.deepcopy(row))
    with client_for(changed) as client:
        result = policy.refresh_policy(env, client=client, packaged=packaged)
    assert result["status"] in {"error", "requires_client_update"}
    assert policy.load_policy_document(env, packaged) == packaged


@pytest.mark.parametrize(
    "revision,digest,minimum,status",
    [
        (True, None, "0.13.0", 200),
        (0, None, "0.13.0", 200),
        (1, "0" * 64, "0.13.0", 200),
        (1, None, "999.0.0", 200),
        (1, None, "0.13.0", 503),
    ],
)
def test_invalid_manifest_or_network_failure_retains_last_good(
    channel, revision, digest, minimum, status
):
    packaged, env = channel
    with client_for(packaged, revision=2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    before = Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]).read_bytes()
    with client_for(packaged, revision, digest=digest, minimum=minimum, status=status) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] in {
            "error",
            "requires_client_update",
        }
    assert Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]).read_bytes() == before
    assert policy.load_policy_status(env)["revision"] == 2


def test_revision_cannot_be_reused_with_different_data_or_rolled_back(channel):
    packaged, env = channel
    with client_for(packaged, 3) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["limits"][0]["capacity"] = 4
    for revision in (2, 3):
        with client_for(changed, revision) as client:
            assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    with client_for(changed, 4) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"


def test_disabled_channel_uses_packaged_rules_and_no_network(channel):
    packaged, env = channel
    env["FREELLMPOOL_POLICY_UPDATES"] = "0"
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("network"))) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "disabled"
    assert policy.load_policy_document(env, packaged) == packaged


def test_corrupt_active_bundle_fails_closed_instead_of_restoring_looser_rules(channel):
    packaged, env = channel
    path = Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"])
    path.write_text('{"schema":1,"revision":4,"registry":')
    assert policy.load_policy_document(env, packaged)["providers"] == []
    assert policy.load_policy_status(env)["status"] == "error"


def test_failed_atomic_activation_does_not_advance_active_revision(channel, monkeypatch):
    packaged, env = channel
    with client_for(packaged, 1) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    original = policy.atomic_write

    def fail_bundle(path, content, **kwargs):
        if Path(path) == Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]):
            raise OSError("disk unavailable")
        return original(path, content, **kwargs)

    monkeypatch.setattr(policy, "atomic_write", fail_bundle)
    with client_for(packaged, 2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    assert policy.load_policy_status(env)["revision"] == 1


def test_status_write_failure_cannot_mix_active_revision_and_digest(channel, monkeypatch):
    packaged, env = channel
    with client_for(packaged, 1) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["limits"][0]["capacity"] = 2
    original = policy.atomic_write
    def fail_status(path, content, **kwargs):
        if Path(path) == Path(env["FREELLMPOOL_POLICY_STATUS_FILE"]):
            raise OSError("disk unavailable")
        return original(path, content, **kwargs)
    monkeypatch.setattr(policy, "atomic_write", fail_status)
    with client_for(changed, 2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    status = policy.load_policy_status(env)
    active = json.loads(Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]).read_text())
    for name in ("revision", "commit", "source_sha256", "repository", "checked_at"):
        assert status[name] == active[name]


def test_manifest_digest_matches_packaged_registry():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "maintenance/policy-channel.json").read_text())
    assert (
        manifest["registry_sha256"]
        == hashlib.sha256(
            (root / "src/freellmpool/provider_registry.json").read_bytes()
        ).hexdigest()
    )


def test_registry_loader_uses_reviewed_bundle_only_when_environment_is_supplied(channel):
    from freellmpool.provider_registry import load_registry
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["limits"][0]["capacity"] = 3
    with client_for(changed) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    pid = changed["providers"][0]["id"]
    assert load_registry(env)[pid]["limits"][0]["capacity"] == 3
    assert load_registry()[pid]["limits"][0]["capacity"] != 3


def test_corrupt_bundle_recovers_from_verified_channel_without_rolling_back(channel):
    packaged, env = channel
    with client_for(packaged, 3) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    path = Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"])
    path.write_text("broken")
    with client_for(packaged, 2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    assert policy.load_policy_document(env, packaged)["providers"] == []
    with client_for(packaged, 3) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    assert policy.load_policy_document(env, packaged) == packaged


def test_missing_activated_bundle_fails_closed_and_retains_recovery_floor(channel):
    packaged, env = channel
    with client_for(packaged, 3) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"]).unlink()
    assert policy.load_policy_document(env, packaged)["providers"] == []
    with client_for(packaged, 2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    with client_for(packaged, 3) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    assert policy.load_policy_document(env, packaged) == packaged


def test_restoring_older_valid_bundle_cannot_override_saved_revision_floor(channel):
    packaged, env = channel
    path = Path(env["FREELLMPOOL_POLICY_BUNDLE_FILE"])
    with client_for(packaged, 1) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    old = path.read_bytes()
    with client_for(packaged, 3) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    path.write_bytes(old)
    assert policy.load_policy_status(env)["revision"] == 3
    assert policy.load_policy_document(env, packaged)["providers"] == []
    with client_for(packaged, 2) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"
    with client_for(packaged, 3) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"


@pytest.mark.parametrize("value", [["official"], True, "nonsense"])
def test_malformed_evidence_status_is_rejected_before_activation(channel, value):
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["evidence"][0]["status"] = value
    with client_for(changed) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"


def test_reviewed_provider_and_tombstone_deletion_cannot_add_an_identity(channel):
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    changed["providers"] = changed["providers"][1:]
    changed["tombstones"] = []
    with client_for(changed, 4) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    assert policy.load_policy_document(env, packaged) == changed
    # A client whose packaged registry removed a provider cannot adopt a stale
    # bundle containing that identity, even if its digest is internally valid.
    with client_for(packaged, 5) as client:
        assert policy.refresh_policy(env, client=client, packaged=changed)["status"] == "requires_client_update"


def test_switching_trusted_repository_cannot_reuse_another_repository_bundle(channel):
    packaged, env = channel
    with client_for(packaged) as client:
        policy.refresh_policy(env, client=client, packaged=packaged)
    env["FREELLMPOOL_POLICY_REPOSITORY"] = "different/repository"
    assert policy.load_policy_document(env, packaged)["providers"] == []


def test_policy_evidence_cannot_claim_indefinite_freshness(channel):
    packaged, env = channel
    changed = copy.deepcopy(packaged)
    changed["providers"][0]["evidence"][0]["expires_at"] = "2099-01-01T00:00:00+00:00"
    with client_for(changed) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "error"


def test_active_policy_sources_renew_repeatedly_but_public_checks_use_packaged(channel, monkeypatch, tmp_path):
    from freellmpool import discovery
    from freellmpool.provider_registry import load_registry
    packaged, env = channel
    env["FREELLMPOOL_EVIDENCE_FILE"] = str(tmp_path / "evidence.json")
    changed = copy.deepcopy(packaged)
    provider = changed["providers"][0]
    content = b"<main>Reviewed updated allowance.</main>"
    provider["evidence"][0]["source_hash"] = {"algorithm": "visible_text_v1", "sha256": discovery.source_digest(content, "text/html")}
    with client_for(changed) as client:
        assert policy.refresh_policy(env, client=client, packaged=packaged)["status"] == "ok"
    monkeypatch.setattr(discovery, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"content-type": "text/html"}))))
    pid, sid = provider["id"], provider["evidence"][0]["id"]
    for _ in range(2):
        row = discovery.refresh_evidence(env, [pid])["providers"][pid][sid]
        assert row["status"] == "unchanged"
        assert load_registry(env)[pid]["evidence"][0]["checked_at"] == row["checked_at"]
    public = discovery.refresh_evidence({}, [pid], public_only=True, path=tmp_path / "public-evidence.json")
    assert public["providers"][pid][sid]["status"] == "review_required"
