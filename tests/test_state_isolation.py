"""The test suite must not read credentials or mutate an operator's state."""

from __future__ import annotations

import os
from pathlib import Path

from freellmpool.cache import default_cache_path
from freellmpool.config import _config_file_path, _user_catalog_path, load_config_file
from freellmpool.conformance import default_conformance_path
from freellmpool.key_inventory import default_inventory_path
from freellmpool.quota import default_quota_path
from freellmpool.route_health import default_route_health_path
from freellmpool.stats import default_stats_path

_ORIGINAL_HOME = os.environ.get("HOME")


def test_default_state_paths_are_temporary_even_with_explicit_empty_env(tmp_path):
    assert Path.home().is_relative_to(tmp_path)
    assert os.environ.get("HOME") == _ORIGINAL_HOME
    for path in (
        default_cache_path(),
        default_inventory_path(),
        default_quota_path(),
        default_stats_path(),
        default_conformance_path(),
        default_conformance_path({}),
        default_route_health_path(),
        default_route_health_path({}),
        _user_catalog_path(),
    ):
        assert path.is_relative_to(tmp_path)
    assert not _user_catalog_path().exists()
    assert _config_file_path({}) is None
    assert load_config_file() == {}
    assert load_config_file({}) == {}


def test_individual_path_overrides_still_win(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "quota.json"
    monkeypatch.setenv("FREELLMPOOL_QUOTA_PATH", str(custom))
    assert default_quota_path() == custom
