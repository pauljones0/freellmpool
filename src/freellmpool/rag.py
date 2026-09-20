"""G12 RAG-in-a-box: folder indexing + cited answers over free routes.

Embedded store is plain sqlite3 (stdlib) with float32 vector BLOBs and
brute-force cosine retrieval — no services, no new dependencies, no paid
path. Embeddings and chat both flow through the managed pool, so free
admission and allowance accounting apply unchanged.

Retrieval scores every chunk in Python per query (O(corpus)), streamed in
batches to bound peak memory. Corpora past MAX_SEARCH_CHUNKS are refused
with an honest error: re-index a smaller folder instead of paying for a
multi-GB scan on every question.
"""

from __future__ import annotations

import errno
import math
import os
import sqlite3
import stat
import struct
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
MAX_FILES = 500
MAX_SCAN_ENTRIES = 5000  # traversal budget per collect_files call (genuinely bounded scan)
MAX_FILE_BYTES = 200_000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
# Brute-force retrieval scores every chunk per query, so an unbounded corpus
# turns each question into a multi-GB scan (~100k chunks x 1.5KB vectors).
# search() refuses past this cap with an honest re-index-smaller error.
MAX_SEARCH_CHUNKS = 50_000
_SEARCH_BATCH = 2000
# G20 measured winner (2026-09-19 leaderboard, fixture v1: recall@3 1.000,
# MRR 1.000 — the only perfect score). `rag index` uses this unless
# --embed-model overrides it; re-run `rag leaderboard` to re-measure.
DEFAULT_EMBED_MODEL = "@cf/baai/bge-small-en-v1.5"


def default_rag_path() -> Path:
    override = os.environ.get("FREELLMPOOL_RAG_FILE")
    if override:
        return Path(override).expanduser()
    # Same rule as config.xdg_config_home (kept inline: this module is stdlib-only).
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    return base / "freellmpool" / "rag.sqlite3"


def chunk_text(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]
    step = max(1, size - overlap)
    return [cleaned[i:i + size] for i in range(0, len(cleaned), step) if cleaned[i:i + size].strip()]


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_MAX_LINK_DEPTH = 40  # symlink-resolution step budget (shared, nested links included)
_READ_CHUNK = 65536


class CollectedFiles(list[tuple[str, str]]):
    """Screened ``(relpath, text)`` pairs plus honest cap accounting.

    ``relpath`` is display-only (``/``-joined, relative to the collection
    root): callers must NEVER open it — the screened bytes are already
    carried in ``text``, so post-collection swaps of the target or its
    parents cannot redirect what gets embedded.
    """

    def __init__(
        self,
        items: list[tuple[str, str]] | None = None,
        *,
        truncated: bool = False,
        skipped_oversize: int = 0,
    ) -> None:
        super().__init__(items or [])
        self.truncated = truncated
        self.skipped_oversize = skipped_oversize


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _silent_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_chain(root_fd: int, rel: tuple[str, ...]) -> list[int] | None:
    """Open every level of ``rel`` from the pinned root; ``None`` on failure.

    Each component opens ``O_DIRECTORY | O_NOFOLLOW``, so a swapped-in
    symlink fails this branch closed instead of redirecting the walk.
    """
    stack: list[int] = []
    try:
        stack = [os.dup(root_fd)]
        for comp in rel:
            stack.append(os.open(comp, _DIR_FLAGS, dir_fd=stack[-1]))
    except OSError:
        _close_all(stack)
        return None
    return stack


def _resolve_link(
    root_fd: int,
    dir_fd: int,
    rel: tuple[str, ...],
    text: str,
    budget: int = _MAX_LINK_DEPTH,
) -> tuple[int, tuple[str, ...]] | None:
    """Open a relative symlink target stepwise from pinned fds.

    ``..`` can never climb above the collection root (fail-closed), and
    nested links share one depth budget. Absolute targets (top-level or
    nested) delegate to :func:`_resolve_absolute_link` with the leftover
    budget. Returns ``(fd, rel-inside-root)`` or ``None``. All opens are
    non-blocking: a link to a FIFO can never hang the walk.
    """
    if text.startswith("/"):
        return _resolve_absolute_link(root_fd, text, budget)
    try:
        stack = _open_chain(root_fd, rel)
        if stack is None:
            return None
        cur_rel = rel
    except OSError:
        return None
    transfer: tuple[int, tuple[str, ...]] | None = None
    try:
        pending = [p for p in text.split("/") if p not in ("", ".")]
        steps = 0
        while pending:
            steps += 1
            if steps > budget:
                return None
            comp = pending.pop(0)
            if comp == "..":
                if len(stack) <= 1 or not cur_rel:
                    return None
                _silent_close(stack.pop())
                cur_rel = cur_rel[:-1]
                continue
            try:
                nxt = os.open(
                    comp,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=stack[-1],
                )
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    return None
                try:
                    sub = os.readlink(comp, dir_fd=stack[-1])
                except OSError:
                    return None
                if sub.startswith("/"):
                    rest = "/".join(pending)
                    new_text = sub if not rest else sub.rstrip("/") + "/" + rest
                    _close_all(stack)
                    stack = []
                    return _resolve_absolute_link(root_fd, new_text, budget - steps)
                pending = [p for p in sub.split("/") if p not in ("", ".")] + pending
                continue
            try:
                mode = os.fstat(nxt).st_mode
            except OSError:
                _silent_close(nxt)
                return None
            if stat.S_ISDIR(mode):
                stack.append(nxt)
                cur_rel = cur_rel + (comp,)
            elif not pending and stat.S_ISREG(mode):
                transfer = (nxt, cur_rel + (comp,))
                break
            else:
                _silent_close(nxt)
                return None
        return transfer
    finally:
        _close_all(stack)


def _resolve_absolute_link(
    root_fd: int, text: str, budget: int = _MAX_LINK_DEPTH
) -> tuple[int, tuple[str, ...]] | None:
    """Open an absolute symlink target from the filesystem root, confined.

    Resolution walks physically from ``/`` (every component pinned,
    non-blocking, never followed); the result is accepted only when the
    walk passes THROUGH the collection root's ``(st_dev, st_ino)`` identity
    and ends below it. Returns ``(fd, rel-inside-root)`` or ``None``.
    """
    try:
        root_stat = os.fstat(root_fd)
        root_key = (root_stat.st_dev, root_stat.st_ino)
        stack = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
        fs_stat = os.fstat(stack[0])
        inside = (fs_stat.st_dev, fs_stat.st_ino) == root_key
    except OSError:
        return None
    transfer: tuple[int, tuple[str, ...]] | None = None
    try:
        pending = [p for p in text.split("/") if p not in ("", ".")]
        cur_rel: tuple[str, ...] = ()
        steps = 0
        while pending:
            steps += 1
            if steps > budget:
                return None
            comp = pending.pop(0)
            if comp == "..":
                if len(stack) <= 1:
                    return None
                _silent_close(stack.pop())
                if cur_rel:
                    cur_rel = cur_rel[:-1]
                else:
                    inside = False
                continue
            try:
                nxt = os.open(
                    comp,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=stack[-1],
                )
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    return None
                try:
                    sub = os.readlink(comp, dir_fd=stack[-1])
                except OSError:
                    return None
                if sub.startswith("/"):
                    rest = "/".join(pending)
                    new_text = sub if not rest else sub.rstrip("/") + "/" + rest
                    _close_all(stack)
                    stack = []
                    return _resolve_absolute_link(root_fd, new_text, budget - steps)
                pending = [p for p in sub.split("/") if p not in ("", ".")] + pending
                continue
            try:
                st = os.fstat(nxt)
            except OSError:
                _silent_close(nxt)
                return None
            if (st.st_dev, st.st_ino) == root_key:
                inside = True
                cur_rel = ()
            elif inside:
                cur_rel = cur_rel + (comp,)
            if stat.S_ISDIR(st.st_mode):
                stack.append(nxt)
            elif not pending and stat.S_ISREG(st.st_mode) and inside:
                transfer = (nxt, cur_rel)
                break
            else:
                _silent_close(nxt)
                return None
        return transfer
    finally:
        _close_all(stack)


def _read_bounded(fd: int) -> bytes | None:
    """Read up to ``MAX_FILE_BYTES``; ``None`` when the file exceeds the cap.

    The cap is enforced DURING the read (not via a pre-stat), so growth
    between screening and reading cannot smuggle in an over-cap file.
    """
    chunks: list[bytes] = []
    remaining = MAX_FILE_BYTES + 1
    while remaining > 0:
        try:
            block = os.read(fd, min(_READ_CHUNK, remaining))
        except OSError:
            return None
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    data = b"".join(chunks)
    return None if len(data) > MAX_FILE_BYTES else data


def _suffix_ok(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in TEXT_SUFFIXES


def collect_files(folder: str | Path) -> CollectedFiles:
    """Indexable text files under ``folder`` as screened ``(relpath, text)``.

    Every open is relative to a pinned directory fd (``O_NOFOLLOW``,
    non-blocking); the screened bytes are read ONCE here and carried to
    the caller, so neither a later target swap nor a later
    parent-directory replacement can redirect what gets embedded. Two
    names for one file (hardlink, valid in-root symlink) are indexed once,
    by ``(st_dev, st_ino)`` identity. Relative links resolve inside the
    root (``..`` past the root fails closed); absolute links resolve from
    the filesystem root and must pass through the root. The alias name
    carries the suffix gate; targets are screened by confinement plus
    hidden parts, and bytes by the size cap plus strict UTF-8. Linked
    directories are never descended; escaping/hidden targets, non-regular
    files, and unreadable/over-cap/non-UTF-8 files are skipped.
    Directories stream lazily: at most ``MAX_SCAN_ENTRIES`` entries are
    consumed and at most ``MAX_FILES`` matches kept, so a huge directory
    cannot blow the memory bound — under truncation the retained set is
    readdir-order-first (returned sorted) and ``truncated`` is set
    (reported, not silent); over-cap files count into ``skipped_oversize``.
    """
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"not a folder: {folder}")
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError:
        return CollectedFiles()
    found: list[tuple[str, str]] = []
    seen: set[tuple[int, int]] = set()
    skipped_oversize = 0
    truncated = False
    scanned = 0
    try:
        stack: list[tuple[str, ...]] = [()]
        while stack:
            if len(found) >= MAX_FILES:
                truncated = True
                break
            rel = stack.pop()
            chain = _open_chain(root_fd, rel)
            if chain is None:
                continue
            dir_fd, ancestors = chain[-1], chain[:-1]
            _close_all(ancestors)
            it = None
            try:
                it = os.scandir(dir_fd)
                for entry in it:
                    if len(found) >= MAX_FILES or scanned >= MAX_SCAN_ENTRIES:
                        # This entry and everything after it is unexplored.
                        truncated = True
                        stack.clear()
                        break
                    scanned += 1
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(rel + (name,))
                            continue
                        is_link = entry.is_symlink()
                        if not is_link and not entry.is_file(follow_symlinks=False):
                            continue  # fifo/socket/device: never opened
                    except OSError:
                        continue
                    if not _suffix_ok(name):
                        continue
                    fd: int | None
                    try:
                        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
                    except OSError as exc:
                        if exc.errno != errno.ELOOP:
                            continue
                        fd = None
                    if fd is None:
                        try:
                            target = os.readlink(name, dir_fd=dir_fd)
                        except OSError:
                            continue
                        resolved = _resolve_link(root_fd, dir_fd, rel, target)
                        if resolved is None:
                            continue
                        fd, res_rel = resolved
                        if not res_rel or any(p.startswith(".") for p in res_rel):
                            _silent_close(fd)
                            continue
                    try:
                        try:
                            st = os.fstat(fd)
                        except OSError:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        data = _read_bounded(fd)
                        if data is None:
                            skipped_oversize += 1
                            continue
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                        seen.add(key)
                        found.append(("/".join(rel + (name,)), text))
                    finally:
                        _silent_close(fd)
            except OSError:
                continue
            finally:
                if it is not None:
                    it.close()
                _silent_close(dir_fd)
    finally:
        _silent_close(root_fd)
    found.sort(key=lambda item: item[0])
    return CollectedFiles(found, truncated=truncated, skipped_oversize=skipped_oversize)


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
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("CREATE TABLE IF NOT EXISTS chunks "
                       "(id INTEGER PRIMARY KEY, path TEXT NOT NULL, ord INTEGER NOT NULL, text TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS vectors "
                       "(chunk_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        return con

    @contextmanager
    def _connect(self):
        # `with sqlite3.connect()` commits but does NOT close; pair it with
        # closing (same idiom as cache.py) so every connection is committed
        # AND closed deterministically (CI runs with -W error::ResourceWarning).
        with closing(self._conn()) as db, db:
            yield db

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
        total = len(self)
        if total == 0:
            raise ValueError("vector store is empty — run: freellmpool rag index <folder>")
        if total > MAX_SEARCH_CHUNKS:
            raise ValueError(
                f"vector store holds {total} chunks (cap {MAX_SEARCH_CHUNKS}) — "
                "re-index a smaller folder: freellmpool rag index <folder>"
            )
        scored = []
        seen = 0
        with self._connect() as db:
            cursor = db.execute("SELECT c.path, c.ord, c.text, v.vec, v.dim FROM chunks c "
                                "JOIN vectors v ON v.chunk_id = c.id")
            while batch := cursor.fetchmany(_SEARCH_BATCH):
                for path, ord_, text, blob, dim in batch:
                    seen += 1
                    if dim != len(vec):
                        raise ValueError(
                            f"query dimension {len(vec)} does not match indexed vectors"
                        )
                    stored = list(struct.unpack(f"<{dim}f", blob))
                    scored.append(
                        {"path": path, "chunk": ord_, "text": text,
                         "score": _cosine(vec, stored)}
                    )
        if not seen:
            raise ValueError("vector store is empty — run: freellmpool rag index <folder>")
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[: max(1, k)]


def _embed_texts(pool: Any, texts: list[str], model: str | None) -> tuple[list[list[float]], str]:
    kwargs = {"model": model} if model else {}
    reply = pool.embed(texts, **kwargs)
    used = getattr(reply, "model", None) or model or "auto"
    rows = getattr(reply, "vectors", None) or []
    if not rows:
        raise ValueError("embed backend returned no vectors")
    return [list(map(float, row)) for row in rows], str(used)


def index_folder(pool: Any, store_path: str | Path, folder: str | Path,
                 *, embed_model: str | None = None) -> dict[str, Any]:
    embed_model = embed_model or DEFAULT_EMBED_MODEL
    files = collect_files(folder)
    if not files:
        raise ValueError(f"no indexable text files in {folder} (want {sorted(TEXT_SUFFIXES)})")
    docs: list[tuple[str, int, str]] = []
    for rel, text in files:
        for ord_, chunk in enumerate(chunk_text(text)):
            docs.append((rel, ord_, chunk))
    if not docs:
        raise ValueError(f"no indexable text in {len(files)} file(s) under {folder}")
    vectors, used = _embed_texts(pool, [text for _, _, text in docs], embed_model)
    count = RagStore(store_path).index(docs, vectors, model=used)
    return {
        "files": len(files),
        "chunks": count,
        "model": used,
        "truncated": bool(getattr(files, "truncated", False)),
        "skipped_oversize": int(getattr(files, "skipped_oversize", 0)),
    }


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
