"""Operational entry points must keep maintenance bounded and secrets local."""
import argparse
from pathlib import Path

from test_managed_runtime import make_pool

from freellmpool import managed_cli


def test_public_update_writes_separate_catalog(tmp_path, monkeypatch):
    calls = []
    def refresh(env, **kwargs):
        calls.append((env, kwargs))
        return {"providers": {}}
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", refresh)
    assert managed_cli.cmd_update(argparse.Namespace(public_only=True, provider=None)) == 0
    assert calls[0][0] == {}
    assert calls[0][1]["path"].name == "public-discovery.json"


def test_public_update_renews_only_public_evidence(monkeypatch):
    monkeypatch.setattr("freellmpool.discovery.refresh_catalog", lambda *a, **k: {"providers": {}})
    calls = []
    def evidence(env, **kwargs):
        calls.append((env, kwargs))
    monkeypatch.setattr("freellmpool.discovery.refresh_evidence", evidence)
    managed_cli.cmd_update(argparse.Namespace(public_only=True, provider=None, renew_evidence=True))
    assert calls[0][0] == {}
    assert calls[0][1]["public_only"] is True


def test_verification_uses_managed_transports_and_surfaces_failures(tmp_path, monkeypatch):
    pool = make_pool(tmp_path)
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)
    called = []
    def canaries(provider, model, **kwargs):
        called.append(provider.id)
        assert kwargs["call_fn"].__self__ is pool
        assert kwargs["stream_fn"].__self__ is pool
        return {"tools": {"status": "fail", "classification": "no_tool_call"}}
    monkeypatch.setattr("freellmpool.conformance.run_target_canaries", canaries)
    args = argparse.Namespace(provider=None, limit=2, features="tools", timeout=5, json=False)
    assert managed_cli.cmd_verify(args) == 3
    assert len(called) == 2
    assert len(set(called)) == 2


def test_maintenance_units_renew_reviewed_evidence_and_bound_free_canaries(tmp_path):
    units = managed_cli.install_maintenance(tmp_path)
    assert set(units) == {"freellmpool-update.timer", "freellmpool-review.timer", "freellmpool-verify.timer"}
    update = (tmp_path / "freellmpool-update.service").read_text()
    assert "freellmpool maintenance --refresh" in update
    review = (tmp_path / "freellmpool-review.service").read_text()
    assert "maintenance --public-only --refresh" in review
    verify = (tmp_path / "freellmpool-verify.service").read_text()
    assert "verify --limit 4" in verify
    assert "FREELLMPOOL_WAIT_SECONDS=0" in verify
    assert "UMask=0077" in verify
    assert "API_KEY" not in verify


def test_scheduled_repository_maintenance_never_loads_credentials_or_runs_inference():
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    assert not (workflows / "catalog-sentinel.yml").exists()
    workflow = (workflows / "provider-evidence-review.yml").read_text()
    assert "--public-only" in workflow
    assert "secrets." not in workflow
    import yaml
    jobs = yaml.safe_load(workflow)["jobs"]
    collectors = [job for job in jobs.values() if any("--public-only" in step.get("run", "") for step in job.get("steps", []))]
    assert collectors
    assert all(job.get("permissions", {}).get("issues") != "write" for job in collectors)
    assert any(job.get("permissions", {}).get("issues") == "write" for job in jobs.values())


def test_maintenance_is_exposed_by_main_parser():
    parser = argparse.ArgumentParser()
    managed_cli.add_commands(parser.add_subparsers())
    args = parser.parse_args(["maintenance", "--refresh"])
    assert args.refresh
    assert args.func.__name__ == "cmd_maintenance"
