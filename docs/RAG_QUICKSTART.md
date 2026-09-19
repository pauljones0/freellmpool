# $0 RAG in a box: `rag index` + `rag ask`

Two commands index a folder and answer questions over it, entirely on
free routes with an embedded SQLite vector store. No backend, no vector
DB, no bill — and no new dependencies (stdlib `sqlite3` only).

```sh
# one-time keyless discovery (no API keys needed)
freellmpool update --provider ovh --provider opencode --provider llm7

# index a folder of .md/.txt/.rst files
freellmpool rag index ./docs

# ask — answer cites its sources
freellmpool rag ask "How do cuttlefish change color?"
```

Expected output:

```
Cuttlefish change color in milliseconds using pigment sacs called chromatophores [1].

Sources (llm7/codestral-latest):
  [1] fish.txt (chunk 0, score 0.86)
  ...
```

Options: `--store PATH` (default `~/.config/freellmpool/rag.sqlite3`,
override with `FREELLMPOOL_RAG_FILE`), `--embed-model` for index,
`--k`, `--model`, `--provider` for ask. Answers are grounded: the
gateway instructs the model to cite every claim as `[1]`, and prints
the retrieved sources with cosine scores regardless.

Honest failure modes: an empty store tells you to index first; a thin
embedding or chat bench surfaces the normal exhaustion error naming
the gap (`freellmpool status` / `verify` to investigate).

`rag index` embeds with the leaderboard winner by default (Cloudflare
bge-small, measured 2026-09-19 — see
[free-embedding-leaderboard](https://0xzr.github.io/freellmpool/free-embedding-leaderboard.html),
re-measure with `freellmpool rag leaderboard`). Prefer another route?
`rag index --embed-model mistral/mistral-embed` (`MISTRAL_API_KEY`) or
any other configured embedder.

## Clean-container proof

`scripts/rag_container_test.sh` builds the stock image, runs discovery
→ index → ask with no keys and no state, and asserts a correct cited
answer. Verified 2026-09-19: PASS in 11s.

## Manual recipe (appendix)

The same flow through the local proxy, stdlib Python only. Terminal 1:

```sh
freellmpool proxy --port 8080
```

Terminal 2 — save as `rag_quickstart.py` and run it:

```python
"""Minimal $0 RAG: embed docs + query, cosine retrieval, grounded answer."""

import json
import math
import time
import urllib.request

BASE = "http://localhost:8080/v1"
EMBED_MODEL = "ovh/Qwen3-Embedding-8B"  # keyless
CHAT_MODEL = "auto"

DOCS = [
    "The freellmpool gateway pools free-tier LLM routes behind one OpenAI-compatible proxy.",
    "Cuttlefish change color in milliseconds using pigment sacs called chromatophores.",
    "The Treaty of Tordesillas divided the New World between Spain and Portugal in 1494.",
]
QUERY = "How do cuttlefish change color?"


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(70)  # free allowances refill; one polite retry window


def embed(texts):
    out = post("/embeddings", {"model": EMBED_MODEL, "input": texts})
    return [row["embedding"] for row in out["data"]]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


vecs = embed(DOCS + [QUERY])  # one request: docs first, query last
doc_vecs, qvec = vecs[:-1], vecs[-1]
print(f"embedded {len(DOCS)} docs + query, dim={len(qvec)}")
ranked = sorted(range(len(DOCS)), key=lambda i: cosine(qvec, doc_vecs[i]), reverse=True)
for i in ranked:
    print(f"score={cosine(qvec, doc_vecs[i]):.4f} doc{i}: {DOCS[i][:60]}...")
top = DOCS[ranked[0]]
chat = post(
    "/chat/completions",
    {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "Answer using only the provided context."},
            {"role": "user", "content": f"Context: {top}\n\nQuestion: {QUERY}"},
        ],
        "max_tokens": 64,
    },
)
print("answer:", chat["choices"][0]["message"]["content"].strip())
print("served_by:", chat.get("model"), "| usage:", chat.get("usage"))
```

```sh
python3 rag_quickstart.py
```

Expected: the cuttlefish doc ranks first by a wide margin and the answer
repeats its sentence. Batch texts into as few `/embeddings` calls as you
can — keyless routes pace anonymous callers (hence the one polite retry).
