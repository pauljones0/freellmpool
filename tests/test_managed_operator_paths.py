"""Every supported catalog path must apply operator routing restrictions."""

from pathlib import Path

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool.errors import AllProvidersExhausted


@pytest.mark.parametrize("path_kind", ["home_relative", "relative", "absolute"])
@pytest.mark.parametrize("models", ['[{name="free",enabled=false}]', "[]"])
def test_catalog_path_does_not_bypass_disabled_chat_routes(tmp_path, monkeypatch, path_kind, models):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "providers.toml"
    path.write_text(
        '[[provider]]\nid="alpha"\nlabel="alpha"\nadapter="openai"\n'
        'base_url="https://alpha.test/v1"\nauth="none"\nmodels=' + models + "\n")
    setting = {"home_relative": "~/providers.toml", "relative": "providers.toml", "absolute": str(path)}[path_kind]
    calls = []
    pool = make_pool(tmp_path, ids=("alpha",), post=lambda *args: calls.append(args) or successful())
    pool._catalog_override = None
    pool._base_env["FREELLMPOOL_CONFIG"] = setting
    assert not [route for route in pool.snapshot().routes if route.modality == "chat"]
    with pytest.raises(AllProvidersExhausted):
        pool.ask("synthetic fixture", model="free", providers=["alpha"])
    assert calls == []


def test_malformed_home_relative_catalog_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "providers.toml").write_text("[[provider]\n")
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._catalog_override = None
    pool._base_env["FREELLMPOOL_CONFIG"] = "~/providers.toml"
    snapshot = pool.snapshot()
    assert snapshot.generation == "invalid-config"
    assert snapshot.routes == ()


def test_unresolvable_home_catalog_fails_closed(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._catalog_override = None
    pool._base_env["FREELLMPOOL_CONFIG"] = "~missing-user/providers.toml"
    original = Path.expanduser
    def expand(path):
        if str(path) == "~missing-user/providers.toml":
            raise RuntimeError("Could not determine home directory.")
        return original(path)
    monkeypatch.setattr(Path, "expanduser", expand)
    snapshot = pool.snapshot()
    assert snapshot.generation == "invalid-config"
    assert snapshot.routes == ()


def test_exported_unresolvable_catalog_is_reported_without_crashing(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, ids=("alpha",))
    pool._catalog_override = None
    monkeypatch.setenv("FREELLMPOOL_CONFIG", "~missing-user/providers.toml")
    original = Path.expanduser
    def expand(path):
        if str(path) == "~missing-user/providers.toml":
            raise RuntimeError("Could not determine home directory.")
        return original(path)
    monkeypatch.setattr(Path, "expanduser", expand)
    snapshot = pool.snapshot()
    assert snapshot.generation == "invalid-config"
    assert snapshot.routes == ()
