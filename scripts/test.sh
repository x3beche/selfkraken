#!/usr/bin/env bash
# Run the test suite in the project virtualenv. Args pass to pytest,
# e.g. scripts/test.sh -k indexer or scripts/test.sh tests/test_cli.py
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Error: .venv not found. Run scripts/install.sh first." >&2
  exit 1
fi

exec .venv/bin/python -m pytest "$@"
