# $0 RAG quickstart: embed → retrieve → generate

Three free embedding routes are reviewed into the catalog: keyless
`ovh/Qwen3-Embedding-8B`, plus `mistral/mistral-embed` and Cloudflare
`@cf/baai/bge-small-en-v1.5` on free-tier keys. This page runs retrieval plus
generation for $0 through the local proxy. Stdlib Python only.

Terminal 1 — start the proxy (loopback only):

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

Prefer a keyed route? Point `EMBED_MODEL` at `mistral/mistral-embed`
(`MISTRAL_API_KEY`) or `cloudflare/@cf/baai/bge-small-en-v1.5`
(`CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`).
