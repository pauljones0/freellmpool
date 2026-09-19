"""G20 free-embedding leaderboard: score every free embedding route on a
fixed retrieval fixture through the gateway, rank by recall@k then MRR."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).with_name("rag_bench_fixture.json")


@dataclass(frozen=True)
class RouteScore:
    route: str
    recall_at_k: float
    mrr: float
    dims: int
    elapsed_ms: float
    error: str | None = None


def load_fixture() -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
    assert fixture.get("fixture_version") == 1, "unsupported fixture version"
    return fixture


def _cosine(a: list[float], b: list[float]) -> float:
    denom = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    if denom == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / denom


def rank_documents(doc_ids: list[str], doc_vecs: list[list[float]],
                   query_vec: list[float], k: int) -> list[tuple[str, float]]:
    scored = sorted(
        ((doc_id, _cosine(vec, query_vec)) for doc_id, vec in zip(doc_ids, doc_vecs, strict=True)),
        key=lambda item: item[1],
        reverse=True,
    )
    return scored[: max(1, k)]


def score_route(doc_ids: list[str], doc_vecs: list[list[float]],
                queries: list[dict[str, Any]], query_vecs: list[list[float]],
                k: int) -> tuple[float, float]:
    """Mean recall@k and MRR over queries (full ranking for reciprocal rank)."""
    if not queries:
        return 0.0, 0.0
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    for query, query_vec in zip(queries, query_vecs, strict=True):
        relevant = set(query.get("relevant") or [])
        if not relevant:
            recalls.append(0.0)
            reciprocal_ranks.append(0.0)
            continue
        top = rank_documents(doc_ids, doc_vecs, query_vec, k)
        recalls.append(len(relevant.intersection(doc_id for doc_id, _ in top)) / len(relevant))
        full = rank_documents(doc_ids, doc_vecs, query_vec, len(doc_ids))
        rank = next((i for i, (doc_id, _) in enumerate(full, 1) if doc_id in relevant), None)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
    return sum(recalls) / len(recalls), sum(reciprocal_ranks) / len(reciprocal_ranks)


def run_leaderboard(pool: Any, *, providers: list[str] | None = None,
                    k: int = 3, fixture: dict[str, Any] | None = None) -> list[RouteScore]:
    """Embed the fixture through each route; failures score 0, never abort."""
    fixture = fixture if fixture is not None else load_fixture()
    documents = fixture["documents"]
    queries = fixture["queries"]
    doc_ids = [doc["id"] for doc in documents]
    doc_texts = [doc["text"] for doc in documents]
    query_texts = [query["text"] for query in queries]
    include = set(providers) if providers else None
    scores: list[RouteScore] = []
    for emb in getattr(pool, "embedders", []) or []:
        if include is not None and emb.id not in include:
            continue
        for model in emb.models:
            if not model.enabled:
                continue
            route = f"{emb.id}/{model.name}"
            started = time.monotonic()
            try:
                doc_reply = pool.embed(doc_texts, providers=[emb.id], model=model.name)
                query_reply = pool.embed(query_texts, providers=[emb.id], model=model.name)
                doc_vecs = [list(map(float, row)) for row in doc_reply.vectors]
                query_vecs = [list(map(float, row)) for row in query_reply.vectors]
                recall, mrr = score_route(doc_ids, doc_vecs, queries, query_vecs, k)
                dims = len(doc_vecs[0]) if doc_vecs else 0
                scores.append(RouteScore(route, recall, mrr, dims,
                                         (time.monotonic() - started) * 1000.0))
            except Exception as exc:  # noqa: BLE001 — a dead route scores 0
                scores.append(RouteScore(
                    route, 0.0, 0.0, 0,
                    (time.monotonic() - started) * 1000.0, error=f"{exc}"))
    # Accuracy first (recall, then MRR); failed routes last; among exact
    # ties the faster route wins, with the route name as final stabilizer.
    scores.sort(key=lambda s: (s.error is not None, -s.recall_at_k, -s.mrr,
                               s.elapsed_ms, s.route))
    return scores


def render_table(scores: list[RouteScore], k: int) -> str:
    """Fixed-width ranking table for CLI output."""
    if not scores:
        return "No embedding routes to rank."
    width = max(len(s.route) for s in scores)
    lines = [f"  {'route':<{width}}  recall@{k}    mrr   dims  time/error"]
    for rank, score in enumerate(scores, 1):
        if score.error:
            detail = f"ERROR {score.error}"[:60]
        else:
            detail = (f"{score.recall_at_k:>7.3f}  {score.mrr:>5.3f}  "
                      f"{score.dims:>4}  {score.elapsed_ms:>7,.0f} ms")
        lines.append(f"{rank}. {score.route:<{width}}  {detail}")
    return "\n".join(lines)
