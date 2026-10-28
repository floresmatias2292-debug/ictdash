#!/usr/bin/env bash
# One-command setup on a new Linux computer:
#   git clone https://github.com/floresmatias2292-debug/ictdash.git ~/dashboard/ictdash
#   bash ~/dashboard/ictdash/deploy/install.sh
# Creates the Python environment, installs the packages and runs the dashboard as a user service that
# restarts on failure and starts with your session. Open http://localhost:8050 afterwards.
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
echo "Project: $APP"

if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is needed (found: $("$PY" --version 2>&1)). Install it, or run with PYTHON=python3.12." >&2
  exit 1
fi
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
  echo "The Python venv module is missing. Install it first: sudo apt install python3-venv" >&2
  exit 1
fi

[ -x "$APP/.venv/bin/python" ] || "$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"
chmod +x "$APP/run.sh"
mkdir -p "$APP/data"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s#^WorkingDirectory=.*#WorkingDirectory=$APP#" -e "s#^ExecStart=.*#ExecStart=$APP/run.sh#" \
  "$APP/deploy/ictdash.service" > "$UNIT_DIR/ictdash.service"
systemctl --user daemon-reload
systemctl --user enable --now ictdash.service

echo "Waiting for the dashboard (the first start downloads the price history, a few minutes)…"
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null http://127.0.0.1:8050/; then
    echo "Dashboard running: http://localhost:8050"
    exit 0
  fi
  sleep 5
done
echo "Still starting. Check progress with: journalctl --user -u ictdash.service -f"
