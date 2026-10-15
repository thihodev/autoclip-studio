#!/usr/bin/env bash
# Start AutoClip Studio. On the first run (and whenever requirements.txt
# changes, e.g. after `git pull`) it sets up .venv and installs dependencies.
#   ./start.sh                 start and open the browser
#   ./start.sh --no-browser    start without opening a browser
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
  for p in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ]; then
  PY=$(find_python) || { echo "Python 3.11 or newer is required: https://www.python.org/downloads/" >&2; exit 1; }
  echo "→ Creating .venv with $($PY --version)"
  "$PY" -m venv .venv
fi

stamp=$(cksum requirements.txt | awk '{print $1}')
if [ "$(cat .venv/.requirements 2>/dev/null || true)" != "$stamp" ]; then
  echo "→ Installing dependencies (first run or requirements changed)"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -q -r requirements.txt
  else
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
  fi
  echo "$stamp" > .venv/.requirements
fi

exec .venv/bin/python -m app "$@"
