"""G12: RAG-in-a-box — chunk, index, retrieve, cited ask, zero new deps."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from freellmpool import rag


def _vec(seed):
    return [float((seed >> i) & 1) or 0.1 for i in range(8)]


def _pool():
    def embed(texts, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        return SimpleNamespace(vectors=[_vec(abs(hash(t)) % 256) for t in texts])

    def chat(messages, **kwargs):
        joined = json.dumps(messages)
        return SimpleNamespace(text="ANS " + joined[:50], provider_id="p", model="m", attempts=1)

    return SimpleNamespace(embed=embed, chat=chat)


def test_chunk_text_splits_with_overlap():
    chunks = rag.chunk_text("abcdefghij", size=4, overlap=2)
    assert chunks == ["abcd", "cdef", "efgh", "ghij", "ij"]
    assert rag.chunk_text("  hi  ") == ["hi"]


def test_collect_files_picks_text_skips_hidden(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "c.bin").write_bytes(b"\x00\x01")
    (tmp_path / ".hidden.md").write_text("x")
    sub = tmp_path / ".git"
    sub.mkdir()
    (sub / "d.md").write_text("x")
    assert [p.name for p in rag.collect_files(tmp_path)] == ["a.md", "b.txt"]


def test_store_round_trip_ranks_best_first(tmp_path):
    store = rag.RagStore(tmp_path / "r.sqlite3")
    docs = [("a.md", 0, "cuttlefish chromatophores"), ("b.md", 0, "treaty tordesillas 1494")]
    store.index(docs, [_vec(1), _vec(128)], model="m")
    assert len(store) == 2
    hits = store.search(_vec(1), k=2)
    assert [(h["path"], h["chunk"]) for h in hits] == [("a.md", 0), ("b.md", 0)]
    assert hits[0]["score"] >= hits[1]["score"]
    assert store.embed_model == "m"


def test_search_empty_store_is_honest_error(tmp_path):
    store = rag.RagStore(tmp_path / "r.sqlite3")
    with pytest.raises(ValueError, match="rag index"):
        store.search(_vec(1), k=2)


def test_search_rejects_dimension_mismatch(tmp_path):
    store = rag.RagStore(tmp_path / "r.sqlite3")
    store.index([("a.md", 0, "x")], [_vec(1)], model="m")
    with pytest.raises(ValueError, match="dimension"):
        store.search([1.0, 2.0], k=1)


def test_index_folder_then_ask_cites_sources(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pool.md").write_text("The freellmpool gateway pools free-tier LLM routes.")
    (docs / "fish.md").write_text("Cuttlefish change color via chromatophores.")
    store_path = tmp_path / "r.sqlite3"
    stats = rag.index_folder(_pool(), store_path, docs)
    assert stats["files"] == 2 and stats["chunks"] == 2
    result = rag.ask_question(_pool(), store_path, "How do cuttlefish change color?")
    assert result["answer"].startswith("ANS")
    assert len(result["citations"]) == 2
    assert {c["path"] for c in result["citations"]} == {"pool.md", "fish.md"}
    assert all("score" in c for c in result["citations"])


def test_ask_grounding_prompt_carries_chunks_and_citation_rule():
    seen = {}

    def chat(messages, **kwargs):
        seen["messages"] = messages
        return SimpleNamespace(text="ANS", provider_id="p", model="m", attempts=1)

    pool = SimpleNamespace(embed=_pool().embed, chat=chat)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store_path = str(Path(td) / "r.sqlite3")
        docs = Path(td) / "docs"
        docs.mkdir()
        (docs / "f.md").write_text("Chromatophores change cuttlefish color.")
        rag.index_folder(pool, store_path, docs)
        rag.ask_question(pool, store_path, "color?", k=1)
    blob = json.dumps(seen["messages"])
    assert "Chromatophores" in blob and "[1]" in blob and "cite" in blob.lower()


def test_cli_index_then_ask(tmp_path, monkeypatch, capsys):
    from freellmpool import managed_cli

    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: _pool())
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "f.md").write_text("Cuttlefish change color via chromatophores.")
    store = str(tmp_path / "r.sqlite3")
    ns = SimpleNamespace(path=str(docs), store=store, embed_model=None)
    assert managed_cli.cmd_rag_index(ns) == 0
    assert "chunks" in capsys.readouterr().out
    ns = SimpleNamespace(question="color?", store=store, k=4, model=None, provider=None)
    assert managed_cli.cmd_rag_ask(ns) == 0
    out = capsys.readouterr().out
    assert "Sources" in out and "f.md" in out


def test_rag_imports_stdlib_only():
    """The store must add no services and no paid path: stdlib + sibling only."""
    import freellmpool.rag as module

    tree = ast.parse(Path(module.__file__).read_text())
    allowed = {"__future__", "freellmpool", "freellmpool.errors"}
    stdlib = {"argparse", "contextlib", "json", "math", "os", "pathlib", "sqlite3", "struct", "sys", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib | {"freellmpool"}, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "") in allowed | stdlib, node.module


def test_store_closes_every_connection(tmp_path, monkeypatch):
    """CI runs with -W error::ResourceWarning: every sqlite connection the
    store opens must be closed, not left for GC finalization."""
    import sqlite3 as _sqlite3

    opened = []
    real_connect = _sqlite3.connect

    class _Tracked:
        def __init__(self, con):
            self._con = con
            self.closed = False

        def close(self):
            self.closed = True
            return self._con.close()

        def __enter__(self):
            self._con.__enter__()
            return self

        def __exit__(self, *exc):
            return self._con.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._con, name)

    def _tracking_connect(*args, **kwargs):
        proxy = _Tracked(real_connect(*args, **kwargs))
        opened.append(proxy)
        return proxy

    monkeypatch.setattr(_sqlite3, "connect", _tracking_connect)
    store = rag.RagStore(tmp_path / "v.sqlite3")
    store.index([("a.md", 0, "hello world")], [_vec(1)], model="m")
    assert len(store) == 1
    assert store.search(_vec(1), k=1)[0]["path"] == "a.md"
    assert opened, "expected connections to be tracked"
    assert all(p.closed for p in opened), "leaked sqlite connection"
