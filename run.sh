#!/usr/bin/env bash
# Start the dashboard at http://127.0.0.1:8050 (sets up .venv on first run)
cd "$(dirname "$0")"
for py in python3.14 python3.13 python3.12 python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    exec "$py" start.py
  fi
done
echo "[ictdash] ERROR: Python 3.12+ not found. Ubuntu/Debian: sudo apt install python3.12 python3.12-venv" >&2
exit 1
