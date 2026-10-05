#!/usr/bin/env bash
# Production-style local run: install the built wheel into a separate venv
# (.venv-run) and execute the packaged CLI from it. Uses the DEFAULT user data
# dir (~/.local/share/selfkraken) — SK_DATA_DIR is not forced.
# Args pass to the CLI, e.g. scripts/run.sh repos
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Error: python3.12 not found on PATH. Run scripts/install.sh for setup hints." >&2
  exit 1
fi

# Pick the newest wheel in dist/; build first if there is none.
wheel="$(ls -t dist/*.whl 2>/dev/null | head -n 1 || true)"
if [ -z "$wheel" ]; then
  echo "No wheel in dist/ — building..."
  if [ ! -x .venv/bin/python ]; then
    python3.12 -m venv .venv
    .venv/bin/pip install -q -e '.[dev]'
  fi
  scripts/build.sh
  wheel="$(ls -t dist/*.whl 2>/dev/null | head -n 1 || true)"
  if [ -z "$wheel" ]; then
    echo "Error: build produced no wheel in dist/." >&2
    exit 1
  fi
fi

if [ ! -x .venv-run/bin/python ]; then
  echo "Creating .venv-run..."
  python3.12 -m venv .venv-run
fi

echo "Installing $(basename "$wheel") into .venv-run..."
.venv-run/bin/pip install -q --force-reinstall "$wheel"

exec .venv-run/bin/skraken "$@"
