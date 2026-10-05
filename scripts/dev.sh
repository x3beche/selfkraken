#!/usr/bin/env bash
# Run skraken in dev mode: data goes to ./.data inside the repo (gitignored),
# so you never touch your real ~/.local/share/selfkraken database.
set -euo pipefail
cd "$(dirname "$0")/.."

# Keep dev data in the repo folder unless the user already set it.
export SK_DATA_DIR="${SK_DATA_DIR:-./.data}"

if [ ! -x .venv/bin/skraken ]; then
  echo "Error: .venv/bin/skraken not found. Run scripts/install.sh first." >&2
  exit 1
fi

# Health check first, then hand over to the requested command.
.venv/bin/skraken doctor

if [ "$#" -gt 0 ]; then
  exec .venv/bin/skraken "$@"
fi
