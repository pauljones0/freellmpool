"""Benchmark outages must not demote durable measured capability evidence."""

import json

import pytest

from freellmpool import capability as c
from freellmpool.models import Model, Provider


@pytest.fixture
def catalog(monkeypatch):
    from freellmpool import config

    monkeypatch.setattr(config, "load_catalog", lambda: [Provider(
        id="test", label="Test", adapter="openai", base_url="https://test.invalid", auth="none",
        models=(Model("frontier-100b"), Model("other-20b"), Model("new-40b")),
    )])
    monkeypatch.setattr(c, "fetch_aa_scores", lambda **kw: {})
    monkeypatch.setattr(c, "fetch_arena_scores", lambda **kw: {})
    monkeypatch.setattr(c, "fetch_aider_scores", lambda **kw: {})


def cache(tmp_path):
    path = tmp_path / "capabilities.json"
    entries = {
        "frontier-100b": {"score": .95, "source": "aa", "verified_at": "2026-08-01T00:00:00Z"},
        "other-20b": {"score": .75, "source": "arena", "verified_at": "2026-08-02T00:00:00Z"},
    }
    path.write_text(json.dumps({"scores": entries}))
    return path, entries


def test_independent_failure_preserves_stronger_and_unreturned_rows(catalog, monkeypatch, tmp_path):
    path, entries = cache(tmp_path)

    def fail(**kwargs):
        raise OSError("private failure text must not persist")

    monkeypatch.setattr(c, "fetch_arena_scores", fail)
    monkeypatch.setattr(c, "fetch_aider_scores", lambda **kw: {"frontier-100b": 10, "new-40b": 20})
    _, stats = c.sync_capability_table(path=path)
    payload = json.loads(path.read_text())
    assert payload["scores"]["frontier-100b"] == entries["frontier-100b"]
    assert payload["scores"]["other-20b"] == entries["other-20b"]
    assert payload["scores"]["new-40b"]["source"] == "aider"
    assert payload["scores"]["new-40b"]["verified_at"]
    assert stats["source_status"]["arena"] == "unavailable"
    assert "private failure" not in path.read_text()


def test_empty_refresh_preserves_entire_last_good_table(catalog, tmp_path):
    path, entries = cache(tmp_path)
    c.sync_capability_table(path=path)
    assert json.loads(path.read_text())["scores"] == entries


def test_partial_refresh_does_not_repercentile_or_downgrade_old_source(catalog, monkeypatch, tmp_path):
    path, entries = cache(tmp_path)
    monkeypatch.setattr(c, "fetch_arena_scores", lambda **kw: c.BenchmarkScores(
        {"other-20b": 5}, complete=False))
    _, stats = c.sync_capability_table(path=path)
    assert json.loads(path.read_text())["scores"] == entries
    assert stats["source_status"]["arena"] == "partial"


def test_successful_same_source_refresh_updates_score_and_provenance(catalog, monkeypatch, tmp_path):
    path, entries = cache(tmp_path)
    monkeypatch.setattr(c, "fetch_arena_scores", lambda **kw: {"other-20b": 1, "new-40b": 2})
    c.sync_capability_table(path=path)
    rows = json.loads(path.read_text())["scores"]
    assert rows["other-20b"]["score"] == .25
    assert rows["other-20b"]["verified_at"] != entries["other-20b"]["verified_at"]
    assert rows["frontier-100b"] == entries["frontier-100b"]


def test_failed_atomic_replace_keeps_previous_cache_and_cleans_temporary_file(catalog, monkeypatch, tmp_path):
    path, _ = cache(tmp_path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(c.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        c.sync_capability_table(path=path)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".*.tmp")) == []


def test_fetch_arena_detects_missing_page_instead_of_accepting_partial_population(monkeypatch):
    pages = iter([
        {"rows": [{"row": {"Model": "other-20b", "Arena Score": 1000}}], "num_rows_total": 101},
        {"rows": [], "num_rows_total": 101},
    ])
    monkeypatch.setattr(c, "_get_json", lambda *a, **kw: next(pages))
    scores = c.fetch_arena_scores()
    assert scores == {"other-20b": 1000}
    assert scores.complete is False


def test_normalization_rejects_nonfinite_and_boolean_measurements():
    assert c.normalize_scores({"good": 5, "nan": float("nan"), "infinite": float("inf"),
                               "boolean": True}) == {"good": .5}


@pytest.mark.parametrize("first,second", [
    ({"rows": [{"row": {"Model": "a", "Arena Score": 1}}], "num_rows_total": 2},
     {"rows": [{"row": {"Model": "a", "Arena Score": 1}}], "num_rows_total": 2}),
    ({"rows": [{"row": {"Model": "a", "Arena Score": 1}}], "num_rows_total": 2},
     {"rows": [{"row": {"Model": "b", "Arena Score": 2}}], "num_rows_total": 3}),
    ({"rows": [{"row": None}], "num_rows_total": 1}, None),
    ({"rows": "schema changed"}, None),
])
def test_arena_repeated_changed_or_malformed_population_is_partial(monkeypatch, first, second):
    pages = iter([first, second])
    monkeypatch.setattr(c, "_get_json", lambda *a, **kw: next(pages))
    assert c.fetch_arena_scores().complete is False


def test_aider_partial_source_is_identified(monkeypatch):
    calls = 0

    def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("unavailable")
        return "model: model-1\npass_rate_2: 75.0\n"

    monkeypatch.setattr(c, "_get_text", fetch)
    scores = c.fetch_aider_scores()
    assert scores == {"model-1": 75}
    assert scores.complete is False


def test_incomplete_aa_response_cannot_replace_prior_measurement(catalog, monkeypatch, tmp_path):
    path, entries = cache(tmp_path)
    monkeypatch.setattr(c, "_get_json", lambda *a, **kw: {
        "data": [{"name": "frontier-100b", "evaluations": {
            "artificial_analysis_intelligence_index": 90}}], "has_more": True,
    })
    monkeypatch.setattr(c, "fetch_aa_scores", ORIGINAL_AA_FETCH)
    _, stats = c.sync_capability_table(path=path, aa_api_key="synthetic-test-key")
    assert json.loads(path.read_text())["scores"] == entries
    assert stats["source_status"]["aa"] == "partial"


ORIGINAL_AA_FETCH = c.fetch_aa_scores
