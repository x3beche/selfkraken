#!/usr/bin/env bash
# Create the project virtualenv (.venv) and install selfkraken with dev deps.
set -euo pipefail
cd "$(dirname "$0")/.."

# python3.12 is mandatory (pyproject: requires-python >= 3.12).
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Error: python3.12 not found on PATH." >&2
  echo "Install Python 3.12 (e.g. your distro's python3.12 package or pyenv)" >&2
  echo "and re-run scripts/install.sh." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating .venv with $(python3.12 --version)"
  python3.12 -m venv .venv
fi

echo "Installing selfkraken (editable, with dev + GUI extras)..."
.venv/bin/pip install -e '.[dev,gui]'

# Config file: copy the example once, keep it private.
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example (chmod 600). Edit it to configure a model (optional)."
fi

echo
echo "Done. Try: .venv/bin/skraken doctor  (or scripts/dev.sh doctor)"
