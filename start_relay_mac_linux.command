#!/usr/bin/env bash
# Relay CRM launcher for macOS / Linux. Double-click (macOS) or run ./start_relay_mac_linux.command
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3.10+ is required. Install from https://www.python.org/downloads/ and run again."
  read -r -p "Press Enter to close"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "First run: setting up Relay CRM (about 1 minute)..."
  "$PY" -m venv .venv || { echo "venv failed (on Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q || { echo "Dependency install failed."; exit 1; }
fi

if ! cmp -s requirements.txt .venv/req.stamp; then
  .venv/bin/python -m pip install -r requirements.txt -q && cp requirements.txt .venv/req.stamp
fi

exec .venv/bin/python run.py "$@"
