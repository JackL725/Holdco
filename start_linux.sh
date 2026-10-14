#!/usr/bin/env bash
# Start Holdco and open the dashboard. Keep this terminal open while agents work.
# For a background service that survives reboots, run:  ./autostart_linux.sh
set -e
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Holdco needs Python 3.10 or newer (e.g. sudo apt install python3)."
  exit 1
fi
exec "$PY" run.py "$@"
