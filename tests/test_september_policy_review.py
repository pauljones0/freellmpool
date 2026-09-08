"""Regression coverage for the September 8 upstream policy review."""
import json
from pathlib import Path


def test_opencode_pool_excludes_application_only_spark():
    registry = json.loads((Path(__file__).parents[1] / 'src/freellmpool/provider_registry.json').read_text())
    provider = next(p for p in registry['providers'] if p['id'] == 'opencode')
    models = provider['grants'][0]['model_selector']['models']
    assert 'muse-spark-1.3-contributor-free' not in models
    assert 'ling-3.0-flash-fin-free' in models
    assert 'ling-3-fin-free' not in models


def test_modelscope_does_not_claim_review_of_javascript_shell():
    registry = json.loads((Path(__file__).parents[1] / 'src/freellmpool/provider_registry.json').read_text())
    provider = next(p for p in registry['providers'] if p['id'] == 'modelscope')
    assert all('modelscope.cn/docs/' not in source['url'] for source in provider['evidence'])
