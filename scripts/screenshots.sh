#!/usr/bin/env bash
# Regenerate the README screenshots (SVG) into docs/screenshots.
# Extra args pass through to scripts/screenshots.py.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Error: .venv not found. Run scripts/install.sh first." >&2
  exit 1
fi

exec .venv/bin/python scripts/screenshots.py --out docs/screenshots "$@"
