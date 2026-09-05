#!/usr/bin/env bash
set -euo pipefail

QUICKSTART_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUICKSTART_WORKDIR="${FREELLMPOOL_QUICKSTART_WORKDIR:-}"
QUICKSTART_KEEP="${FREELLMPOOL_QUICKSTART_KEEP:-0}"
QUICKSTART_PACKAGE="${FREELLMPOOL_QUICKSTART_PACKAGE:-$QUICKSTART_ROOT}"
QUICKSTART_TIMEOUT="${FREELLMPOOL_QUICKSTART_MAX_SECONDS:-120}"
QUICKSTART_LIVE="${FREELLMPOOL_QUICKSTART_LIVE:-0}"

if [[ "$QUICKSTART_PACKAGE" == "." ]]; then
    QUICKSTART_PACKAGE="$QUICKSTART_ROOT"
fi
if [[ -z "$QUICKSTART_WORKDIR" ]]; then
    QUICKSTART_WORKDIR="$(mktemp -d)"
    if [[ "$QUICKSTART_KEEP" != "1" ]]; then
        trap 'rm -rf "$QUICKSTART_WORKDIR"' EXIT
    fi
else
    mkdir -p "$QUICKSTART_WORKDIR"
fi
QUICKSTART_WORKDIR="$(cd "$QUICKSTART_WORKDIR" && pwd)"
cd "$QUICKSTART_WORKDIR"
if command -v uv >/dev/null 2>&1; then
    uv venv --seed --python python3 .venv
else
    python3 -m venv .venv
fi
. .venv/bin/activate
python -m pip install --disable-pip-version-check "$QUICKSTART_PACKAGE"

# Every app state path is isolated explicitly. HOME is never repurposed, and
# no provider credentials are inherited by this credentialless check.
QUICKSTART_ENV=(
    env -i
    "PATH=$QUICKSTART_WORKDIR/.venv/bin:/usr/bin:/bin"
    "TERM=${TERM:-dumb}"
    "FREELLMPOOL_CONFIG_FILE=$QUICKSTART_WORKDIR/config.toml"
    "FREELLMPOOL_CONFIG=$QUICKSTART_WORKDIR/providers.toml"
    "FREELLMPOOL_DISCOVERY_FILE=$QUICKSTART_WORKDIR/discovery.json"
    "FREELLMPOOL_EVIDENCE_FILE=$QUICKSTART_WORKDIR/evidence.json"
    "FREELLMPOOL_ACCOUNTS_FILE=$QUICKSTART_WORKDIR/accounts.json"
    "FREELLMPOOL_ALLOWANCE_FILE=$QUICKSTART_WORKDIR/allowances.sqlite3"
    "FREELLMPOOL_KEYS_PATH=$QUICKSTART_WORKDIR/keys.toml"
    "FREELLMPOOL_QUOTA_PATH=$QUICKSTART_WORKDIR/quota.json"
    "FREELLMPOOL_STATS_PATH=$QUICKSTART_WORKDIR/stats.json"
    "FREELLMPOOL_CACHE_PATH=$QUICKSTART_WORKDIR/cache.db"
    "FREELLMPOOL_HEALTH_FILE=$QUICKSTART_WORKDIR/health.json"
    "FREELLMPOOL_CONFORMANCE_FILE=$QUICKSTART_WORKDIR/conformance.json"
    "FREELLMPOOL_CAPABILITY_FILE=$QUICKSTART_WORKDIR/capability.json"
    "FREELLMPOOL_EXTERNAL_CATALOG_PATH=$QUICKSTART_WORKDIR/external.json"
    "FREELLMPOOL_WAIT_SECONDS=0"
)
"${QUICKSTART_ENV[@]}" python -c 'import subprocess,sys; subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]), check=True)'     "$QUICKSTART_TIMEOUT" freellmpool update --public-only
"${QUICKSTART_ENV[@]}" freellmpool status --json > status.json
"${QUICKSTART_ENV[@]}" freellmpool setup --help >/dev/null
python -c 'import json; state=json.load(open("status.json")); assert state["strict_free"] is True; print("quickstart-test: installed, refreshed public catalogs, and verified strict-free status")'

# Live inference is an explicit optional bounded check, never a CI prerequisite.
# The normal admission gate still excludes unknown/paid/trial routes.
if [[ "$QUICKSTART_LIVE" == "1" ]]; then
    "${QUICKSTART_ENV[@]}" python -c 'import subprocess,sys; subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]), check=True)'         30 freellmpool ask --max-tokens 32 --timeout 20 "Reply with OK." > reply.txt
    test -s reply.txt
    echo "quickstart-test: bounded free canary returned a reply"
fi
