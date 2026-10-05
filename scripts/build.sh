#!/usr/bin/env bash
# Build sdist + wheel into dist/ and list the artifacts.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Error: .venv not found. Run scripts/install.sh first." >&2
  exit 1
fi

echo "Building sdist and wheel..."
.venv/bin/python -m build

echo
echo "dist/ contents:"
ls -l dist/
