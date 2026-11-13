#!/usr/bin/env bash
# ===========================================================
#  AI Film Editor - one-click environment installer
#  macOS / Linux. Safe to re-run: it repairs the environment.
# ===========================================================
set -u

cd "$(dirname "$0")" || exit 1

for candidate in python3.11 python3.12 python3.10 python3.13 python3 python; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
        exec "$candidate" setup_env.py "$@"
    fi
done

echo "Python 3.10 or newer was not found."
echo
echo "  macOS:  brew install python@3.11"
echo "  Ubuntu: sudo apt install python3.11 python3.11-venv"
echo "  Fedora: sudo dnf install python3.11"
echo
echo "Then run bash install.sh again."
exit 1
