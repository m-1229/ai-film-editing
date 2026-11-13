#!/usr/bin/env bash
# ===========================================================
#  AI Film Editor - launcher (macOS / Linux)
#  Pass --debug to keep the traceback in the foreground.
# ===========================================================
set -u

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "[ERROR] The environment is not installed yet."
    echo "        Run bash install.sh first."
    exit 1
fi

"$PY" setup_env.py --check || {
    echo
    echo "[ERROR] Environment check failed. Run bash install.sh to repair it."
    exit 1
}

if [ "${1:-}" = "--debug" ] || [ "${1:-}" = "-d" ]; then
    shift
    "$PY" main.py "$@"
    echo
    read -r -p "Press Enter to close."
    exit 0
fi

exec "$PY" main.py "$@"
