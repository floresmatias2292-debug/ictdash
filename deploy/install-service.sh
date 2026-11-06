#!/usr/bin/env bash
# Install the systemd user service for THIS folder (works on any computer / any path).
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p ~/.config/systemd/user
sed "s#__DIR__#$DIR#g" "$DIR/deploy/ictdash.service" > ~/.config/systemd/user/ictdash.service
systemctl --user daemon-reload
systemctl --user enable --now ictdash.service
loginctl enable-linger "$USER" 2>/dev/null || true
echo "Installed for $DIR -> http://localhost:8050   (logs: journalctl --user -u ictdash.service -f)"
