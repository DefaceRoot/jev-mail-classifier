#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first: https://www.python.org/downloads/"
  exit 1
fi

echo "Setting up jev-mail-classifier in ${here}/.venv ..."
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
chmod +x "$here/jev-mail"

echo ""
echo "Installed. Copy config.example.yaml to config.yaml and .env.example to .env,"
echo "fill them in, then run ./jev-mail run --dry-run"
