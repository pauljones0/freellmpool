"""G12 RAG-in-a-box: folder indexing + cited answers over free routes.

Embedded store is plain sqlite3 (stdlib) with float32 vector BLOBs and
brute-force cosine retrieval — no services, no new dependencies, no paid
path. Embeddings and chat both flow through the managed pool, so free
admission and allowance accounting apply unchanged.
"""

from __future__ import annotations

import math
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
MAX_FILES = 500
MAX_FILE_BYTES = 200_000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100


def default_rag_path() -> Path:
    override = os.environ.get("FREELLMPOOL_RAG_FILE")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "freellmpool" / "rag.sqlite3"


def chunk_text(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]
    step = max(1, size - overlap)
    return [cleaned[i:i + size] for i in range(0, len(cleaned), step) if cleaned[i:i + size].strip()]


def collect_files(folder: str | Path) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"not a folder: {folder}")
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= MAX_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.append(path)
    return found


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class RagStore:
    """SQLite-backed chunk + vector store; one file, no server."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS chunks "
                       "(id INTEGER PRIMARY KEY, path TEXT NOT NULL, ord INTEGER NOT NULL, text TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS vectors "
                       "(chunk_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def __len__(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return int(row[0])

    @property
    def embed_model(self) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
            return row[0] if row else None

    def index(self, docs: list[tuple[str, int, str]], vectors: list[list[float]], *, model: str) -> int:
        if len(docs) != len(vectors):
            raise ValueError(f"{len(docs)} chunks but {len(vectors)} vectors")
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise ValueError(f"mixed vector dimensions: {sorted(dims)}")
        with self._connect() as db:
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM vectors")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
            for (path, ord_, text), vec in zip(docs, vectors, strict=True):
                cur = db.execute("INSERT INTO chunks (path, ord, text) VALUES (?, ?, ?)", (path, ord_, text))
                blob = struct.pack(f"<{len(vec)}f", *vec)
                db.execute("INSERT INTO vectors VALUES (?, ?, ?, ?)", (cur.lastrowid, model, len(vec), blob))
        return len(docs)

    def search(self, vec: list[float], *, k: int = 4) -> list[dict[str, Any]]:
        if len(self) == 0:
            raise ValueError("vector store is empty — run: freellmpool rag index <folder>")
        with self._connect() as db:
            rows = db.execute("SELECT c.path, c.ord, c.text, v.vec, v.dim FROM chunks c "
                              "JOIN vectors v ON v.chunk_id = c.id").fetchall()
        if not rows:
            raise ValueError("vector store is empty — run: freellmpool rag index <folder>")
        if any(dim != len(vec) for _, _, _, _, dim in rows):
            raise ValueError(f"query dimension {len(vec)} does not match indexed vectors")
        scored = []
        for path, ord_, text, blob, dim in rows:
            stored = list(struct.unpack(f"<{dim}f", blob))
            scored.append({"path": path, "chunk": ord_, "text": text, "score": _cosine(vec, stored)})
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[: max(1, k)]


def _embed_texts(pool: Any, texts: list[str], model: str | None) -> tuple[list[list[float]], str]:
    kwargs = {"model": model} if model else {}
    reply = pool.embed(texts, **kwargs)
    used = getattr(reply, "model", None) or model or "auto"
    return [list(map(float, row)) for row in reply.vectors], str(used)


def index_folder(pool: Any, store_path: str | Path, folder: str | Path,
                 *, embed_model: str | None = None) -> dict[str, Any]:
    root = Path(folder)
    files = collect_files(root)
    if not files:
        raise ValueError(f"no indexable text files in {folder} (want {sorted(TEXT_SUFFIXES)})")
    docs: list[tuple[str, int, str]] = []
    for path in files:
        for ord_, chunk in enumerate(chunk_text(path.read_text(encoding="utf-8"))):
            docs.append((str(path.relative_to(root)), ord_, chunk))
    if not docs:
        raise ValueError(f"no indexable text in {len(files)} file(s) under {folder}")
    vectors, used = _embed_texts(pool, [text for _, _, text in docs], embed_model)
    count = RagStore(store_path).index(docs, vectors, model=used)
    return {"files": len(files), "chunks": count, "model": used}


def ask_question(pool: Any, store_path: str | Path, question: str, *, k: int = 4,
                 model: str | None = None, providers: Any = None) -> dict[str, Any]:
    store = RagStore(store_path)
    if len(store) == 0:
        raise ValueError("vector store is empty — run: freellmpool rag index <folder>")
    vectors, _used = _embed_texts(pool, [question], store.embed_model)
    hits = store.search(vectors[0], k=k)
    context = "\n\n".join(f"[{i + 1}] {hit['path']}:\n{hit['text']}" for i, hit in enumerate(hits))
    messages = [
        {"role": "system", "content": (
            "Answer only from the numbered sources below. Cite every factual claim "
            "with its source number like [1]. If the sources do not answer the "
            "question, say so plainly and cite nothing.")},
        {"role": "user", "content": f"Sources:\n{context}\n\nQuestion: {question}"},
    ]
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    if providers:
        kwargs["providers"] = providers
    reply = pool.chat(messages, **kwargs)
    return {
        "answer": reply.text,
        "citations": [{"path": h["path"], "chunk": h["chunk"], "score": round(h["score"], 4)} for h in hits],
        "model": reply.model,
        "provider": reply.provider_id,
    }
