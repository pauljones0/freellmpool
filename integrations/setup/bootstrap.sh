#!/bin/sh
# Install this reviewed checkout, then walk through one provider at a time.
set -eu
bootstrap_source=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$bootstrap_source"
    bootstrap_bin=$(uv tool dir --bin)
    exec "$bootstrap_bin/freellmpool" setup "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3.11+ is required. In Termux run: pkg install python' >&2
    exit 127
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
bootstrap_venv=${XDG_DATA_HOME:-"$HOME/.local/share"}/freellmpool/venv
python3 -m venv "$bootstrap_venv"
"$bootstrap_venv/bin/python" -m pip install "$bootstrap_source"
"$bootstrap_venv/bin/python" -m freellmpool.client_setup install-command --binary "$bootstrap_venv/bin/freellmpool"
PATH="$HOME/.local/bin:$PATH"
export PATH
exec "$bootstrap_venv/bin/freellmpool" setup "$@"
