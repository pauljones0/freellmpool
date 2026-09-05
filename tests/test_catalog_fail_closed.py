"""Regression coverage for catalog admission and malformed input handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freellmpool import catalog_validation, config
from freellmpool.catalog import import_external_provider_to_user_catalog


def _row(**fields):
    return {
        "id": "sample",
        "base_url": "https://sample.example/v1",
        "key_env": "SAMPLE_KEY",
        "models": [{"name": "model"}],
        **fields,
    }


@pytest.mark.parametrize("field", ["enabled", "auto"])
@pytest.mark.parametrize("value", ["false", "true", 1, [], {}])
def test_malformed_admission_flags_fail_closed(field, value):
    provider = config._parse_rows(
        [_row(models=[{"name": "model", field: value}])], allow_local=False
    )[0]
    assert getattr(provider.models[0], field) is False


@pytest.mark.parametrize(
    "enabled,auto", [(True, True), (True, False), (False, True), (False, False)]
)
def test_explicit_admission_booleans_are_preserved(enabled, auto):
    provider = config._parse_rows(
        [_row(models=[{"name": "model", "enabled": enabled, "auto": auto}])],
        allow_local=False,
    )[0]
    assert provider.models[0].enabled is enabled
    assert provider.models[0].auto is auto


def test_missing_admission_flags_preserve_existing_defaults():
    provider = config._parse_rows([_row()], allow_local=False)[0]
    assert provider.models[0].enabled is True
    assert provider.models[0].auto is True


@pytest.mark.parametrize("value", ["false", "true", 1, {}, []])
def test_malformed_optional_key_flag_does_not_bypass_authentication(value):
    provider = config._parse_rows([_row(key_optional=value)], allow_local=False)[0]
    assert provider.key_optional is False
    assert not provider.is_configured({})


@pytest.mark.parametrize("value", [None, 3, "model", {"name": "model"}])
def test_malformed_model_container_does_not_break_other_providers(value):
    providers = config._parse_rows([_row(models=value), _row(id="valid")], allow_local=False)
    # Retain the empty override so a typo cannot restore the bundled provider.
    assert providers[0].id == "sample"
    assert providers[0].models == ()
    assert providers[1].models[0].name == "model"


@pytest.mark.parametrize(
    "body,diagnostic",
    [
        ('models = [{ name = "model", enabled = "false" }]', "enabled must be a boolean"),
        ('models = [{ name = "model", auto = "false" }]', "auto must be a boolean"),
        ('key_optional = "false"\nmodels = [{ name = "model" }]', "key_optional must be a boolean"),
        ("models = 3", "models must be an array"),
        ("models = [3]", "model row must be a table"),
    ],
)
def test_validator_reports_raw_type_errors(tmp_path, monkeypatch, body, diagnostic):
    path = tmp_path / "providers.toml"
    path.write_text('[[provider]]\nid = "sample"\nbase_url = "https://sample.example/v1"\n' + body)
    monkeypatch.setattr(catalog_validation, "capability_table", lambda: {})
    errors = catalog_validation.validate_catalog(path)
    assert any(diagnostic in error for error in errors)


@pytest.mark.parametrize("content", [None, "[[provider]\n"])
def test_validator_rejects_missing_or_invalid_explicit_catalog(tmp_path, monkeypatch, content):
    path = tmp_path / "providers.toml"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(catalog_validation, "capability_table", lambda: {})
    assert catalog_validation.validate_catalog(path)


def test_external_import_is_manual_until_reviewed_and_preserves_existing_intent(
    tmp_path, monkeypatch
):
    cache = tmp_path / "external.json"
    destination = tmp_path / "providers.toml"
    cache.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "Sample",
                        "baseUrl": "https://sample.example/v1",
                        "models": [{"id": "model", "modality": "Text", "rateLimit": "20 RPD"}],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("FREELLMPOOL_EXTERNAL_CATALOG_PATH", str(cache))
    monkeypatch.setenv("FREELLMPOOL_CONFIG", str(destination))
    import_external_provider_to_user_catalog("Sample")
    model = config.load_catalog(destination)[0].models[0]
    assert model.enabled is True
    assert model.auto is False
    assert model.rpd == 20

    approved = destination.read_text().replace("auto = false", "auto = true")
    destination.write_text(approved)
    import_external_provider_to_user_catalog("Sample")
    assert destination.read_text() == approved


def test_catalog_cache_detects_content_change_when_stat_identity_is_unchanged(
    tmp_path, monkeypatch
):
    path = tmp_path / "providers.toml"
    first = '[[provider]]\nid = "sample"\nbase_url = "https://sample.example/v1"\nmodels = [{ name = "model-a" }]\n'
    path.write_text(first)
    frozen_stat = path.stat()
    original_stat = Path.stat

    def unchanged_stat(self, *args, **kwargs):
        return frozen_stat if self == path else original_stat(self, *args, **kwargs)

    # Model a filesystem with coarse timestamps; no sleeps or timing assumptions.
    monkeypatch.setattr(Path, "stat", unchanged_stat)
    assert config.load_catalog(path)[0].models[0].name == "model-a"
    path.write_text(first.replace("model-a", "model-b"))
    assert config.load_catalog(path)[0].models[0].name == "model-b"
