"""Shared fixtures: fake providers, env, and a fixed-clock quota store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freellmpool.models import Model, Provider
from freellmpool.quota import QuotaStore


@pytest.fixture(autouse=True)
def isolate_operator_state(tmp_path, monkeypatch):
    """Keep default state and credential discovery inside each test's sandbox.

    Explicit env={} callers bypass environment overrides, so their Path.home()
    fallback must also be temporary. HOME itself remains unchanged. Individual
    tests can still override paths through their own monkeypatch calls.
    """
    temporary_home = tmp_path / "_operator_home"
    temporary_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: temporary_home))
    state = temporary_home / ".config" / "freellmpool"
    paths = {
        "FREELLMPOOL_CONFIG_FILE": "config.toml",
        "FREELLMPOOL_CONFIG": "providers.toml",
        "FREELLMPOOL_KEYS_PATH": "keys.toml",
        "FREELLMPOOL_QUOTA_PATH": "quota.json",
        "FREELLMPOOL_STATS_PATH": "stats.json",
        "FREELLMPOOL_CACHE_PATH": "cache.db",
        "FREELLMPOOL_HEALTH_FILE": "route_health.json",
        "FREELLMPOOL_CONFORMANCE_FILE": "conformance.json",
        "FREELLMPOOL_DISCOVERY_FILE": "discovery.json",
        "FREELLMPOOL_ACCOUNTS_FILE": "accounts.json",
        "FREELLMPOOL_ALLOWANCE_FILE": "allowances.sqlite3",
        "FREELLMPOOL_EVIDENCE_FILE": "provider-evidence.json",
        "FREELLMPOOL_SETUP_STATE_PATH": "setup-progress.json",
        "FREELLMPOOL_CAPABILITY_FILE": "capability_scores.json",
        "FREELLMPOOL_TASK_EVIDENCE_FILE": "task_evidence.json",
        "FREELLMPOOL_EXTERNAL_CATALOG_PATH": "provider_catalog.json",
        "FREELLMPOOL_JOBS_PATH": "jobs.jsonl",
        "FREELLMPOOL_RUN_RECORDS_PATH": "run_records.jsonl",
    }
    # Let reports follow DATA_DIR so a test's explicit data-root override wins.
    monkeypatch.delenv("FREELLMPOOL_REPORT_DIR", raising=False)
    monkeypatch.delenv("FREELLMPOOL_REPORTS_DIR", raising=False)
    monkeypatch.setenv("FREELLMPOOL_DATA_DIR", str(state))
    for name, relative in paths.items():
        monkeypatch.setenv(name, str(state / relative))


@pytest.fixture
def providers() -> list[Provider]:
    return [
        Provider(
            id="alpha",
            label="Alpha",
            adapter="openai",
            base_url="https://alpha.test/v1",
            key_env="ALPHA_KEY",
            models=(Model("alpha-small", rpd=2), Model("alpha-big", rpd=0)),
        ),
        Provider(
            id="beta",
            label="Beta",
            adapter="openai",
            base_url="https://beta.test/v1",
            key_env="BETA_KEY",
            models=(Model("beta-1", rpd=0),),
        ),
        Provider(
            id="gee",
            label="Gee",
            adapter="gemini",
            base_url="https://gee.test/v1beta",
            key_env="GEE_KEY",
            models=(Model("gee-flash", rpd=0),),
        ),
        Provider(
            id="free",
            label="Keyless",
            adapter="openai",
            base_url="https://free.test/v1",
            auth="none",
            models=(Model("free-1", rpd=0),),
        ),
    ]


@pytest.fixture
def env() -> dict[str, str]:
    return {"ALPHA_KEY": "a", "BETA_KEY": "b", "GEE_KEY": "g"}


@pytest.fixture
def quota(tmp_path) -> QuotaStore:
    clock = lambda: datetime(2026, 6, 2, 12, 0, tzinfo=UTC)  # noqa: E731
    return QuotaStore(path=tmp_path / "quota.json", clock=clock)
