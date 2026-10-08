#!/usr/bin/env bash
# Start the watcher. Creates the virtualenv and installs dependencies when needed.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python -m app
