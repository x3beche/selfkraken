# Changelog

All notable changes to selfkraken. Dates are UTC; versions follow
[semver](https://semver.org).

## v0.1.1 — 2026-10-23

- **The desktop build is also the CLI.** `skraken-gui` opens the window as
  before; `skraken-gui -c <command>` (or `--cli`) runs any CLI command, so one
  download covers both. On Windows the windowed build attaches to the calling
  console, because `stdout` does not exist there otherwise.
- Version bumped so the release assets and the wheel agree.

## v0.1.0 — 2026-10-06

First public release.

**Core loop**

- `skraken init` registers a repository with its own test command; the state
  lives in SQLite under `$SK_DATA_DIR` (default `~/.local/share/selfkraken`).
- `skraken index` builds a repository-local index of tracked and untracked
  non-secret files, skipping binaries and dotdirs.
- `skraken propose "<instruction>"` asks one OpenAI-compatible model for a
  unified diff; `--diff-file` keeps it useful with no model at all (degraded
  mode).
- `skraken apply <n>` runs `git apply`, then the test command, then keeps or
  rolls back on failure. Every proposal, apply, test run and rollback is
  recorded in `skraken log`.

**Reading the repo**

- `skraken graph` — GitKraken-style commit graph in the terminal.
- `skraken branches` — branch table with current marker and ahead/behind.
- `skraken tree` — file tree with touched files marked.
- `skraken show <n>`, `skraken diff` — proposal and working-tree diffs.

**Desktop GUI** (`skraken gui`, optional `[gui]` extra)

- PySide6 window for review → apply → test → keep/roll back, with a commit
  graph widget and a read-only Monaco editor (vendored, no network access).
- Offscreen-capable: `QT_QPA_PLATFORM=offscreen` is what the screenshots use.
- Falls back to a plain text viewer when QtWebEngine is unavailable.

**Samples and safety**

- `skraken sample load [--kind multibranch]` creates removable demo repos.
- Repository-local, single-user by design: no telemetry, no cloud, no uploads.

**Packaging**

- Wheel and sdist carry the vendored Monaco build.
- Standalone CLI (PyInstaller one-file) and desktop builds for Linux x86_64,
  macOS arm64/x86_64 and Windows x86_64, published with `SHA256SUMS`.
