#!/usr/bin/env bash
# G39 wheel cold-start job (gate phase): build the wheel, install it into a
# fresh venv, and run the hermetic acceptance against the INSTALLED artifact.
set -euo pipefail
TOP="$(git rev-parse --show-toplevel)"
cd "$TOP"
rm -rf /tmp/g39venv
rm -f dist/*.whl
python -m pip wheel --no-deps -w dist .
python -m venv /tmp/g39venv
shopt -s nullglob
_whl=(dist/*.whl)
test "${#_whl[@]}" -eq 1
/tmp/g39venv/bin/pip install "${_whl[0]}"
/tmp/g39venv/bin/pip install pytest
FREELLMPOOL_WHEEL_ASSERT=1 /tmp/g39venv/bin/python -m pytest tests/test_agent_coldstart.py -q
/tmp/g39venv/bin/freellmpool --version
