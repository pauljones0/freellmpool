"""G12: RAG-in-a-box — chunk, index, retrieve, cited ask, zero new deps."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
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
    assert [rel for rel, _ in rag.collect_files(tmp_path)] == ["a.md", "b.txt"]


def test_collect_files_rejects_symlink_escape(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "ok.md").write_text("x")
    (tmp_path / "secret.md").write_text("s3cret")
    (folder / "evil.md").symlink_to(tmp_path / "secret.md")
    sub = folder / "sub"
    sub.mkdir()
    (sub / "inner.md").write_text("x")
    (folder / "linkdir").symlink_to(sub, target_is_directory=True)
    names = [rel for rel, _ in rag.collect_files(folder)]
    assert "evil.md" not in names
    assert names.count("sub/inner.md") == 1
    assert "ok.md" in names


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
    stdlib = {"argparse", "contextlib", "errno", "json", "math", "os", "pathlib", "sqlite3", "stat", "struct", "sys", "typing"}
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


def test_rag_connections_use_wal_and_busy_timeout(tmp_path):
    """Mirror cache.py: WAL journal + explicit busy timeout so a concurrent
    index write txn cannot SQLITE_BUSY a reader."""
    import sqlite3 as _sqlite3

    store = rag.RagStore(tmp_path / "r.sqlite3")
    with store._connect() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    con = _sqlite3.connect(tmp_path / "r.sqlite3")
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_search_refuses_corpus_over_documented_cap(tmp_path, monkeypatch):
    """Brute-force scoring is O(corpus) per query: fail honestly past the cap
    instead of running a multi-GB scan."""
    monkeypatch.setattr(rag, "MAX_SEARCH_CHUNKS", 2)
    store = rag.RagStore(tmp_path / "r.sqlite3")
    store.index(
        [(f"{i}.md", 0, "some indexable text") for i in range(3)],
        [_vec(i) for i in range(3)],
        model="m",
    )
    with pytest.raises(ValueError, match="[Cc]ap"):
        store.search(_vec(1), k=1)


def test_collect_files_excludes_symlink_to_hidden_target(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / ".env").write_text("K=v")
    (folder / "public.txt").symlink_to(folder / ".env")
    (folder / "ok.md").write_text("x")
    names = [rel for rel, _ in rag.collect_files(folder)]
    assert "public.txt" not in names
    assert names == ["ok.md"]


def test_index_ignores_post_collect_target_swap(tmp_path, monkeypatch):
    """G23 HIGH#1 reopen (supervisor repro): swapping a collected target for an
    outside symlink after collect must not redirect what gets embedded."""
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "public.md"
    target.write_text("allowed fixture")
    outside = tmp_path / "outside.md"
    outside.write_text("outside harmless fixture")
    seen: list[str] = []
    original = rag.collect_files

    def swap_after_collection(folder):
        found = original(folder)
        target.unlink()
        target.symlink_to(outside)
        return found

    def embed(texts, **kwargs):
        texts = [texts] if isinstance(texts, str) else list(texts)
        seen.extend(texts)
        return SimpleNamespace(vectors=[[1.0, 0.0] for _ in texts])

    monkeypatch.setattr(rag, "collect_files", swap_after_collection)
    stats = rag.index_folder(
        SimpleNamespace(embed=embed), tmp_path / "index.sqlite", root
    )
    assert stats["files"] == 1
    assert "allowed fixture" in seen
    assert "outside harmless fixture" not in seen


def test_index_ignores_post_collect_parent_swap(tmp_path, monkeypatch):
    """G23 HIGH#1 reopen: replacing a collected parent dir after collect must
    not redirect what gets embedded either."""
    import shutil

    root = tmp_path / "docs"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "note.md").write_text("nested fixture")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "evil.md").write_text("outside nested fixture")
    seen: list[str] = []
    original = rag.collect_files

    def swap_after_collection(folder):
        found = original(folder)
        shutil.rmtree(sub)
        sub.symlink_to(outside_dir, target_is_directory=True)
        return found

    def embed(texts, **kwargs):
        texts = [texts] if isinstance(texts, str) else list(texts)
        seen.extend(texts)
        return SimpleNamespace(vectors=[[1.0, 0.0] for _ in texts])

    monkeypatch.setattr(rag, "collect_files", swap_after_collection)
    stats = rag.index_folder(
        SimpleNamespace(embed=embed), tmp_path / "index.sqlite", root
    )
    assert stats["files"] == 1
    assert "nested fixture" in seen
    assert "outside nested fixture" not in seen


def test_index_reports_oversize_and_truncation(tmp_path, monkeypatch):
    """G23 HIGH#1: oversize files are skipped (counted) and caps are reported,
    never silently claimed as full coverage."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "ok.md").write_text("fine")
    (root / "big.md").write_bytes(b"x" * (rag.MAX_FILE_BYTES + 100))
    stats = rag.index_folder(_pool(), tmp_path / "r1.sqlite3", root)
    assert stats["files"] == 1
    assert stats["skipped_oversize"] == 1
    assert stats["truncated"] is False
    (root / "ok2.md").write_text("fine too")
    monkeypatch.setattr(rag, "MAX_FILES", 1)
    stats = rag.index_folder(_pool(), tmp_path / "r2.sqlite3", root)
    assert stats["files"] == 1
    assert stats["truncated"] is True


def test_collect_reports_scan_budget_truncation(tmp_path, monkeypatch):
    """G23 HIGH#1: exhausting MAX_SCAN_ENTRIES with work left is flagged."""
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text("x")
    monkeypatch.setattr(rag, "MAX_SCAN_ENTRIES", 2)
    found = rag.collect_files(root)
    assert len(found) == 2
    assert found.truncated is True


def test_collect_indexes_valid_symlink_once(tmp_path):
    """G23 HIGH#1: an in-root symlink is still indexed, deduped by identity."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "real.md").write_text("shared content")
    (root / "alias.md").symlink_to(root / "real.md")
    found = rag.collect_files(root)
    assert len(found) == 1
    assert found[0][1] == "shared content"


def test_collect_skips_special_files_without_hanging(tmp_path):
    """G23 HIGH#1: fifos/sockets are never opened for reads (no blocking)."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "ok.md").write_text("x")
    os.mkfifo(root / "pipe.md")
    assert [rel for rel, _ in rag.collect_files(root)] == ["ok.md"]


def test_collect_files_caps_matches_and_sorts(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    for i in range(rag.MAX_FILES + 50):
        (folder / f"f{i:04d}.md").write_text("x")
    found = rag.collect_files(folder)
    assert len(found) == rag.MAX_FILES
    assert [rel for rel, _ in found] == sorted(rel for rel, _ in found)
    assert found.truncated is True


def test_ask_question_rejects_empty_embedding_reply(tmp_path):
    import pytest

    from freellmpool.rag import RagStore, ask_question

    store = RagStore(tmp_path / "r.sqlite3")
    store.index([("a.md", 0, "hello world")], [[0.1] * 8], model="m")

    class _EmptyEmbed:
        def embed(self, texts, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(vectors=[], model="m")

    with pytest.raises(ValueError, match="no vectors"):
        ask_question(_EmptyEmbed(), tmp_path / "r.sqlite3", "hi?")


def test_chunk_text_rejects_nonpositive_size():
    """G23 #39: size<=0 must fail loud, not silently drop/return garbage."""
    with pytest.raises(ValueError, match="size"):
        rag.chunk_text("hello world", size=0)
    with pytest.raises(ValueError, match="size"):
        rag.chunk_text("hello world", size=-1)


def test_default_paths_ignore_empty_xdg_config_home(tmp_path, monkeypatch):
    """G23 #14: empty XDG_CONFIG_HOME falls back to ~/.config, never cwd-relative."""
    from freellmpool import drift, run_checkpoint

    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    # conftest mocks Path.home(); the fallback must follow it, never cwd.
    base = Path.home() / ".config" / "freellmpool"
    assert rag.default_rag_path() == base / "rag.sqlite3"
    assert drift.default_drift_dir() == base / "drift"
    assert run_checkpoint.default_runs_dir() == base / "runs"
    import os

    assert not os.path.exists("freellmpool")  # nothing cwd-relative


def test_default_rag_path_expands_xdg_tilde(tmp_path, monkeypatch):
    """G23 #14: a ~/ XDG_CONFIG_HOME is expanded against the real home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/xdg-test")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert rag.default_rag_path() == tmp_path / "xdg-test" / "freellmpool" / "rag.sqlite3"


def test_collect_symlink_to_fifo_does_not_hang(tmp_path):
    """G23 HIGH#1 follow-on: a symlink to a FIFO must be skipped, never block.

    Runs in a subprocess so a regression fails by timeout instead of hanging
    the suite; fixture-only, no providers involved.
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "ok.md").write_text("x")
    os.mkfifo(root / "pipe")
    (root / "alias.md").symlink_to(root / "pipe")
    src = str(Path(rag.__file__).resolve().parent.parent)
    script = (
        "import json, sys; from freellmpool import rag; "
        "print(json.dumps(sorted(rel for rel, _ in rag.collect_files(sys.argv[1]))))"
    )
    env = dict(os.environ, PYTHONPATH=src)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["ok.md"]


def test_collect_consumes_bounded_directory_iteration(tmp_path, monkeypatch):
    """G23 HIGH#1 follow-on: the scan budget must bound iterator consumption,
    not just the returned count (no eager whole-dir materialization)."""
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md"):
        (root / name).write_text("x")
    real_scandir = os.scandir
    calls: list[int] = []

    class Counting:
        def __init__(self, it):
            self._it = it

        def __iter__(self):
            return self

        def __next__(self):
            calls.append(1)
            return next(self._it)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._it.close()

        def close(self):
            self._it.close()

    monkeypatch.setattr(os, "scandir", lambda fd: Counting(real_scandir(fd)))
    monkeypatch.setattr(rag, "MAX_SCAN_ENTRIES", 1)
    found = rag.collect_files(root)
    assert len(found) == 1
    assert found.truncated is True
    assert len(calls) <= 2  # budget + the break-triggering fetch, not dir size


def test_collect_follows_relative_symlink_alias(tmp_path):
    """G23 HIGH#1 follow-on: a relative alias is PROVEN followed — the target
    (bad suffix) is uncollectible directly, so only the alias yields bytes."""
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "data.dat").write_text("linked payload")
    (root / "alias.md").symlink_to("sub/data.dat")
    assert rag.collect_files(root) == [("alias.md", "linked payload")]


def test_collect_follows_absolute_in_root_symlink(tmp_path):
    """G23 HIGH#1 follow-on: absolute links to in-root targets resolve against
    the filesystem root and stay confined to the collection root."""
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "data.dat"
    target.write_text("abs payload")
    (root / "abs.md").symlink_to(str(target.resolve()))
    assert rag.collect_files(root) == [("abs.md", "abs payload")]


def test_collect_skips_absolute_escape_symlink(tmp_path):
    """G23 HIGH#1 follow-on guard: absolute links outside the root stay out."""
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("shh")
    (root / "evil.md").symlink_to(str(outside.resolve()))
    (root / "ok.md").write_text("x")
    assert [rel for rel, _ in rag.collect_files(root)] == ["ok.md"]


def test_collect_skips_dotdot_past_root_symlink(tmp_path):
    """G23 HIGH#1 follow-on guard: relative links climbing past the root fail closed."""
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("shh")
    (root / "sub" / "a.md").symlink_to("../../secret.md")
    (root / "ok.md").write_text("x")
    assert [rel for rel, _ in rag.collect_files(root)] == ["ok.md"]
