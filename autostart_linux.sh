#!/usr/bin/env bash
# Run Holdco as a systemd user service that starts on boot.
# To undo:  ./autostart_linux.sh uninstall
set -e
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python)"
exec "$PY" autostart.py "${1:-install}"
