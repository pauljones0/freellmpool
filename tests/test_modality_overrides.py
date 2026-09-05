"""One catalog override must restrict every modality consistently."""

import pytest

from freellmpool import config


@pytest.mark.parametrize("section,loader", [
    ("embedder", config.load_embedders), ("transcriber", config.load_transcribers),
])
def test_local_modality_override_can_disable_bundled_routes(tmp_path, monkeypatch, section, loader):
    bundled = tmp_path / "bundled.toml"
    bundled.write_text(f'''
[[{section}]]
id = "alpha"
base_url = "https://alpha.test/v1"
models = [{{ name = "primary" }}]
[[{section}]]
id = "beta"
base_url = "https://beta.test/v1"
models = [{{ name = "fallback" }}]
''')
    monkeypatch.setattr(config, "_PACKAGED_CATALOG", bundled)
    original = loader()
    assert [p.id for p in original] == ["alpha", "beta"]
    provider = original[0]
    path = tmp_path / "providers.toml"
    path.write_text(f'[[{section}]]\nid = "{provider.id}"\nbase_url = "{provider.base_url}"\nmodels = []\n')
    monkeypatch.setenv("FREELLMPOOL_CONFIG", str(path))
    merged = {p.id: p for p in loader()}
    assert merged[provider.id].models == ()
    assert set(merged) == {p.id for p in original}
    assert merged["beta"] == original[1]


@pytest.mark.parametrize("section,loader", [
    ("embedder", config.load_embedders), ("transcriber", config.load_transcribers),
])
def test_explicit_modality_catalog_is_not_merged_with_user_file(tmp_path, monkeypatch, section, loader):
    user = tmp_path / "user.toml"
    user.write_text(f'[[{section}]]\nid="other"\nbase_url="https://other.test/v1"\nmodels=[]\n')
    selected = tmp_path / "selected.toml"
    selected.write_text(f'[[{section}]]\nid="selected"\nbase_url="https://selected.test/v1"\nmodels=[]\n')
    monkeypatch.setenv("FREELLMPOOL_CONFIG", str(user))
    assert [p.id for p in loader(selected)] == ["selected"]
