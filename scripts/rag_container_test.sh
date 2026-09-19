#!/bin/sh
# G12 end-to-end: clean container (no keys, no mounts but sample docs),
# keyless discovery -> rag index -> rag ask -> correct cited $0 answer.
# Usage: sh scripts/rag_container_test.sh   (needs docker + network)
set -eu

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))
ROOT=$(cd "$ROOT" && pwd)
IMAGE=${RAG_E2E_IMAGE:-freellmpool:rag-e2e}
DOCS=$(mktemp -d)
trap 'rm -rf "$DOCS"' EXIT

printf 'The freellmpool gateway pools free-tier LLM routes behind one OpenAI-compatible proxy.\n' > "$DOCS/pool.txt"
printf 'Cuttlefish change color in milliseconds using pigment sacs called chromatophores.\n' > "$DOCS/fish.txt"
printf 'The Treaty of Tordesillas divided the New World between Spain and Portugal in 1494.\n' > "$DOCS/treaty.txt"
chmod 755 "$DOCS"
chmod 644 "$DOCS"/*

echo "== build $IMAGE =="
docker build -t "$IMAGE" "$ROOT" >/tmp/rag-e2e-build.log 2>&1
echo "build ok"

START=$(date +%s)
OUT=$(docker run --rm --entrypoint sh \
  -v "$DOCS:/docs:ro" \
  "$IMAGE" -c '
    set -e
    freellmpool update --provider ovh --provider opencode --provider llm7
    freellmpool rag index /docs --store /tmp/rag.sqlite3
    freellmpool rag ask "How do cuttlefish change color?" --store /tmp/rag.sqlite3
  ' 2>&1)
END=$(date +%s)

echo "$OUT"
echo "== elapsed: $((END - START))s =="
echo "$OUT" | grep -q "chromatophores" || { echo "E2E FAIL: answer missing expected fact"; exit 1; }
echo "$OUT" | grep -q "fish.txt" || { echo "E2E FAIL: answer missing citation"; exit 1; }
echo "$OUT" | grep -qE "Sources \(" || { echo "E2E FAIL: no Sources block"; exit 1; }
echo "RAG E2E PASS: correct cited \$0 answer in a clean container"
