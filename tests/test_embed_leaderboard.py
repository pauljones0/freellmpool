"""G20: free-embedding leaderboard — fixture-scored retrieval, never live."""

from __future__ import annotations

from types import SimpleNamespace

from freellmpool import embed_leaderboard as lb
from freellmpool import rag


def test_cosine_matches_known_values() -> None:
    assert lb._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert lb._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert lb._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert abs(lb._cosine([1.0, 1.0], [1.0, 0.0]) - 0.70710678) < 1e-6


def test_perfect_vectors_score_full_marks() -> None:
    doc_ids = ["d1", "d2"]
    doc_vecs = [[1.0, 0.0], [0.0, 1.0]]
    queries = [{"id": "q1", "text": "x", "relevant": ["d1"]},
               {"id": "q2", "text": "y", "relevant": ["d2"]}]
    query_vecs = [[1.0, 0.0], [0.0, 1.0]]
    recall, mrr = lb.score_route(doc_ids, doc_vecs, queries, query_vecs, k=2)
    assert recall == 1.0
    assert mrr == 1.0


def test_partial_ranking_scores_known_fractions() -> None:
    doc_ids = ["d1", "d2", "d3"]
    doc_vecs = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]
    queries = [{"id": "q1", "text": "x", "relevant": ["d1"]},
               {"id": "q2", "text": "y", "relevant": ["d1", "d2"]}]
    # q1 ranks d1 first (recall 1, rr 1); q2 ranks d3,d1,d2 (recall 1/2, rr 1/2).
    query_vecs = [[1.0, 0.0], [0.7, 0.7]]
    recall, mrr = lb.score_route(doc_ids, doc_vecs, queries, query_vecs, k=2)
    assert recall == (1.0 + 0.5) / 2
    assert mrr == (1.0 + 0.5) / 2


def test_empty_relevant_list_scores_zero_without_crashing() -> None:
    recall, mrr = lb.score_route(["d1"], [[1.0]],
                                 [{"id": "q", "text": "x", "relevant": []}],
                                 [[1.0]], k=3)
    assert (recall, mrr) == (0.0, 0.0)


def _tiny_fixture() -> dict:
    return {
        "fixture_version": 1,
        "documents": [{"id": "d1", "text": "AAA"}, {"id": "d2", "text": "BBB"}],
        "queries": [{"id": "q1", "text": "QQQ", "relevant": ["d1"]}],
    }


def test_run_ranks_routes_and_isolates_failures() -> None:
    calls: list[str] = []
    tables = {
        "good/m": {"AAA": [1.0, 0.0], "BBB": [0.0, 1.0], "QQQ": [1.0, 0.0]},
        "mid/m": {"AAA": [1.0, 0.0], "BBB": [0.0, 1.0], "QQQ": [0.0, 1.0]},
    }

    def embed(texts, **kwargs):
        route = f"{kwargs['providers'][0]}/{kwargs['model']}"
        calls.append(route)
        if route == "bad/m":
            raise RuntimeError("boom")
        table = tables[route]
        return SimpleNamespace(vectors=[table[t] for t in texts], model="m")

    emb = lambda pid: SimpleNamespace(  # noqa: E731
        id=pid, models=[SimpleNamespace(name="m", enabled=True)])
    pool = SimpleNamespace(embed=embed, embedders=[emb("good"), emb("mid"), emb("bad")])
    scores = lb.run_leaderboard(pool, fixture=_tiny_fixture(), k=1)
    assert [s.route for s in scores] == ["good/m", "mid/m", "bad/m"]
    assert scores[0].recall_at_k == 1.0
    assert (scores[1].recall_at_k, scores[1].mrr) == (0.0, 0.5)
    assert scores[2].error == "boom"
    assert scores[2].recall_at_k == 0.0
    assert {s.route for s in scores} <= set(calls)


def test_exact_tie_broken_by_latency_then_name(monkeypatch) -> None:
    # Route "a" is slower (30ms) than "b" (10ms) with identical accuracy.
    times = iter([10.0, 40.0, 50.0, 60.0])
    monkeypatch.setattr(lb.time, "monotonic", lambda: next(times))

    def embed(texts, **kwargs):
        return SimpleNamespace(vectors=[[1.0] for _ in texts], model="m")

    emb = lambda pid: SimpleNamespace(  # noqa: E731
        id=pid, models=[SimpleNamespace(name="m", enabled=True)])
    pool = SimpleNamespace(embed=embed, embedders=[emb("a"), emb("b")])
    scores = lb.run_leaderboard(pool, fixture=_tiny_fixture(), k=1)
    assert [(s.route, s.recall_at_k) for s in scores] == [("b/m", 1.0), ("a/m", 1.0)]


def test_run_supports_provider_filter() -> None:
    seen: list = []

    def embed(texts, **kwargs):
        seen.append(kwargs.get("providers"))
        return SimpleNamespace(vectors=[[1.0] for _ in texts], model="m")

    emb = lambda pid: SimpleNamespace(  # noqa: E731
        id=pid, models=[SimpleNamespace(name="m", enabled=True)])
    pool = SimpleNamespace(embed=embed, embedders=[emb("a"), emb("b")])
    scores = lb.run_leaderboard(pool, providers=["b"], k=3)
    assert [s.route for s in scores] == ["b/m"]
    assert seen and all(p == ["b"] for p in seen)


def test_fixture_is_fixed_and_versioned() -> None:
    fixture = lb.load_fixture()
    assert fixture["fixture_version"] == 1
    assert len(fixture["documents"]) == 24
    assert len(fixture["queries"]) == 10
    doc_ids = {d["id"] for d in fixture["documents"]}
    for query in fixture["queries"]:
        assert query["relevant"], query["id"]
        assert set(query["relevant"]) <= doc_ids


def test_rag_index_defaults_to_measured_winner(tmp_path) -> None:
    """`rag index` without --embed-model uses the leaderboard winner."""
    seen: dict = {}

    def embed(texts, **kwargs):
        seen.update(kwargs)
        if isinstance(texts, str):
            texts = [texts]
        return SimpleNamespace(vectors=[[0.5] for _ in texts], model=kwargs.get("model"))

    pool = SimpleNamespace(embed=embed)
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("hello world")
    result = rag.index_folder(pool, tmp_path / "r.sqlite3", folder)
    assert seen.get("model") == rag.DEFAULT_EMBED_MODEL
    assert result["model"] == rag.DEFAULT_EMBED_MODEL


def test_rag_index_override_flag_wins(tmp_path) -> None:
    seen: dict = {}

    def embed(texts, **kwargs):
        seen.update(kwargs)
        if isinstance(texts, str):
            texts = [texts]
        return SimpleNamespace(vectors=[[0.5] for _ in texts], model=kwargs.get("model"))

    pool = SimpleNamespace(embed=embed)
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("hello world")
    rag.index_folder(pool, tmp_path / "r.sqlite3", folder, embed_model="other/model")
    assert seen.get("model") == "other/model"
