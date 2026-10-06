# selfkraken

A private, single-user, repository-local Git assistant — a personal replacement
for GitKraken's core loop:

**register a repo → index it → ask one model for a change → get a unified diff
proposal → review → apply → run tests → keep or roll back → every step recorded
in SQLite.**

Python 3.12 + Typer + SQLite. Small, local, honest.

## What it is — and what it is not

It **is**: a command-line tool that keeps an index of your repo, asks one
configurable OpenAI-compatible model for a change as a unified diff, applies
that diff with `git apply`, runs your test command, and records every proposal,
apply, test run and rollback in a local database.

It is **not**:

- **not frontier-model quality.** Output quality is whatever your chosen model
  gives you, with the same limits it has.
- **not large-context infrastructure.** It sends at most `SK_MAX_CONTEXT_CHARS`
  of repo snippets per request. There is no embedding pipeline, no RAG platform.
- **not an IDE.** No editor, no plugins, no sub-100ms autocomplete loop.
  There is a native desktop GUI (`skraken gui`, see below) for the core loop —
  review, apply, test, keep or roll back — but you still type your git
  commands elsewhere.

## Quick start

```bash
git clone <your-repo-url> selfkraken && cd selfkraken
scripts/install.sh            # creates .venv (needs python3.12), installs, makes .env

.venv/bin/skraken doctor      # shows data path, DB status, git, model mode
```

Try the whole loop on labelled sample data (safe, removable):

```bash
.venv/bin/skraken sample load          # a [SAMPLE] git repo + one sample proposal
```

On your own repo, the core loop is:

```bash
cd /path/to/your/project
skraken init --test-cmd "python -m pytest -q"   # register this repo (PATH optional)
skraken index                                   # index tracked+untracked (non-secret) files
skraken propose "fix the off-by-one in parse_line"   # ask the model (or use --diff-file, see below)
skraken show 3                                  # read the diff + rationale
skraken apply 3                                 # git apply, run tests, roll back on failure
skraken log                                     # the audit trail of everything above
skraken graph                                   # GitKraken-style commit graph in the terminal
skraken branches                                # branch table: current marker, ahead/behind
skraken tree --proposal 3                       # file tree; touched files marked "✎ changed"
```

Other commands: `skraken repos`, `set-test`, `search`, `proposals`, `reject`,
`revert`, `test`, `export`, `import`, `sample remove`.

## Screenshots

Rendered from the sample repo — regenerate anytime with
`python scripts/screenshots.py` (or `scripts/screenshots.sh`).

Commit graph (`skraken graph`):

![Commit graph](docs/screenshots/graph.svg)

Branches (`skraken branches`):

![Branches](docs/screenshots/branches.svg)

File tree with a proposal (`skraken tree --proposal`):

![File tree](docs/screenshots/tree.svg)

Propose and apply (`skraken propose` / `skraken apply`):

![Apply flow](docs/screenshots/cli-apply.svg)

## Degraded mode (no model)

Leave `SK_LLM_BASE_URL` / `SK_LLM_MODEL` unset and everything still works
except model-generated proposals. You create proposals from a unified diff you
wrote or got elsewhere yourself:

```bash
skraken propose "guard against empty input" --diff-file my.patch
skraken show 7
skraken apply 7          # same pipeline: validate, apply, test, record
```

Indexing, search, apply, tests, revert, log, export/import, sample data — all
function without any model configured. `skraken doctor` tells you which mode
you are in.

## Model setup

selfkraken talks to **one OpenAI-compatible endpoint** (`POST
{SK_LLM_BASE_URL}/chat/completions`). Local Ollama is the typical choice; any
compatible server works. In `.env`:

```bash
SK_LLM_BASE_URL=http://localhost:11434/v1
SK_LLM_MODEL=qwen2.5-coder:7b
```

**What is sent to the model:** your request text plus up to `SK_MAX_CONTEXT_CHARS`
(default 12000) characters of repo file snippets selected from the index. So —
if you point `SK_LLM_BASE_URL` at a remote endpoint, **your code leaves the
machine** for that request. Keep it local if that matters to you.

Secret-like files (`.env`, `*.pem`, `*.key`, `id_rsa*`, `secrets.*`, `*.db`,
etc.) are **never indexed**, never sent, and never read as context. The API key
(when set) is only sent as a bearer token to your configured URL; it is never
printed, logged, or stored in the database.

## Commands

| Command | What it does |
|---|---|
| `skraken init [PATH]` | Register a git repo (`--test-cmd`, `--name`) |
| `skraken repos` | List registered repos |
| `skraken set-test CMD` | Set the test command for a repo |
| `skraken index` | (Re)index repo files; incremental by content hash |
| `skraken search QUERY` | Keyword search the index (FTS5) |
| `skraken propose REQUEST` | Ask the model for a diff; `--diff-file FILE` for manual proposals; `--file PATH` to include specific files as context; `--yes` skips confirmation |
| `skraken proposals [--status S]` | List proposals |
| `skraken show ID` | Show a proposal's diff and rationale |
| `skraken apply ID` | Apply with `git apply`, run tests, auto-rollback on failure (`--no-test`, `--commit`, `--keep-on-failure`) |
| `skraken reject ID` | Reject a proposed diff |
| `skraken revert ID` | Reverse an applied diff (`git apply -R`); does not rewrite git history |
| `skraken test` | Run the repo's test command and record the result |
| `skraken graph [--limit N] [--current-only] [--ascii]` | Commit graph with branch lanes, merges and tags — GitKraken-style, in the terminal (`--current-only` limits to the current branch, `--ascii` a plain-ASCII rendering) |
| `skraken branches` | Branch table with the current branch marked and ahead/behind counts |
| `skraken tree [--depth N] [--proposal ID]` | File tree (`--depth` bounds it); with `--proposal`, files that diff touches are marked `✎ changed` |
| `skraken log [--limit N]` | Audit log: every register/index/propose/apply/test/rollback/commit/reject/revert |
| `skraken export FILE` / `import FILE` | JSON portability (index not included; rebuild with `index`) |
| `skraken sample load` / `sample remove` | Labelled demo repo and proposal |
| `skraken gui [--theme auto\|dark\|light] [--skip-checks]` | Open the native Qt desktop window (needs the `gui` extra + one system library — see *Desktop GUI (Qt)*) |
| `skraken doctor` | Environment check: data path, DB, git, model mode |

Repo commands accept `--repo PATH` (default: the current directory, resolved to
the nearest registered repo).

## Permissions — what selfkraken reads and writes

- **Reads:** files in repos you register (tracked + untracked, minus
  secret-like and binary files) for indexing; the diff you apply.
- **Writes to your working tree:** only via `git apply` / `git apply -R` when you
  run `apply` / `revert`; `git add`/`commit` only if you pass `--commit`.
- **Runs:** your configured test command, as your user, in the repo directory
  (`shlex`-split, no shell).
- **Writes:** only to its data directory (SQLite DB, sample repo) and files you
  explicitly name in `export`.
- **Network:** only to the configured `SK_LLM_BASE_URL`, plus whatever your own
  git remote does when you use git yourself — selfkraken never pushes/fetches.
- It never sends telemetry anywhere. It is the only client.

## Architecture

Modules: `config` (settings/.env), `db` (SQLite store), `gitops`/`diffs`
(git subprocess + diff validation), `indexer` (incremental file index +
context building), `llm` (OpenAI-compatible client), `runner` (test execution),
`workflow` (the UI-free core loop), `cli` (Typer front end), `portability`
(export/import), `sample` (demo data). See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map, sequence
diagram, proposal state machine and security boundaries.

## Data location

Default: `~/.local/share/selfkraken/selfkraken.db` (or `$XDG_DATA_HOME/selfkraken`).
Override with `SK_DATA_DIR`. The `scripts/dev.sh` helper sets `SK_DATA_DIR=./.data`
so development data stays in the repo folder and is gitignored.

## Backup & restore

- Copy the database file while selfkraken is **not running**:
  `cp ~/.local/share/selfkraken/selfkraken.db backup/`
- Or use JSON portability:
  ```bash
  skraken export ~/backups/skraken-$(date +%F).json
  skraken import ~/backups/skraken-2026-10-05.json
  ```
  Export contains repos, proposals, test runs and the audit log. The file index
  is **not** exported (it is rebuildable, and it holds your file contents) —
  rebuild it after a restore with `skraken index`.

## Sample data

`skraken sample load` creates a small git repo (`sample-repo` in the data
dir) and one proposal, both labelled `[SAMPLE]`. Nothing of yours is touched.
Remove everything with `skraken sample remove`.

## Desktop GUI (Qt)

A native PySide6 desktop window for the same loop — no web server, no
browser, no QtWebEngine. It is optional: the core install never requires
PySide6, and `skraken gui` tells you exactly how to get it if it is missing.

```bash
scripts/install.sh                 # installs '.[dev,gui]' (includes PySide6)
# or:  pip install -e '.[gui]'
.venv/bin/skraken gui              # dark or light, follows the system theme
.venv/bin/skraken gui --theme light
```

What you get: a repo selector, **Add Repo** (folder picker + test command),
**Index**, **Run Tests**, **Export/Import**, clearly labelled **[SAMPLE]**
demo data, and a model status label that explains degraded mode when no
`SK_LLM_BASE_URL` / `SK_LLM_MODEL` is configured. The center is a
GitKraken-style **commit graph** (painted lanes, merge rings, ref badges like
`HEAD`, `main ↑2`, `tag: v0.1.0`, `feature/login ⇄ origin`) and a
**proposals** table with coloured status badges. The right pane holds the
**New proposal** form (request with live character counter and inline
validation, optional context files, optional *paste a unified diff* box for
degraded mode) and the **proposal detail**: rationale, highlighted diff
(`+` green, `-` red, `@@` cyan), Apply with *Run tests / Commit on success /
Keep on failure* checkboxes and a confirmation dialog, Reject, Revert, and a
collapsible test-output tail. A bottom **activity log** shows every step,
newest first. Everything slow runs in worker threads; the window never
freezes, and errors come back as plain `Error:` text — never tracebacks.

Commit graph with lanes, merges and refs:

![GUI commit graph](docs/screenshots/gui-graph.png)

Proposal review: highlighted diff, apply options, test result:

![GUI proposal detail](docs/screenshots/gui-proposal.png)

Empty state with guidance:

![GUI empty state](docs/screenshots/gui-empty.png)

Light theme:

![GUI light theme](docs/screenshots/gui-light.png)

Regenerate the screenshots anytime with
`.venv/bin/python scripts/gui_screenshots.py` (offscreen Qt, throwaway data).

### System requirements for the GUI

One thing `pip` cannot install: from Qt 6.5 on, the xcb (X11) platform plugin
needs the **system** library `libxcb-cursor0`. Without it the window never
opens — Qt aborts the process with a core dump:

```
qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb platform plugin.
qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" even though it was found.
Aborted (core dumped)
```

`skraken gui` now checks the environment **before** importing Qt and prints
the fix instead of crashing: it refuses to start when no display is available
(tell you to run it from a desktop session), when `libxcb-cursor0` cannot be
loaded, and — as a back-stop for anything the checks cannot see — a throwaway
subprocess first proves a `QApplication` really comes up on your display.
Install the library once per machine:

```bash
sudo apt install libxcb-cursor0     # Debian / Ubuntu
sudo dnf install xcb-util-cursor    # Fedora
sudo pacman -S xcb-util-cursor      # Arch
```

On a Wayland session Qt is automatically told to prefer the Wayland plugin;
`--skip-checks` bypasses the guards if you know better than they do. The
**CLI needs none of this**: every `skraken` command except `gui` works
headless with no system packages, and the test suite runs Qt offscreen.

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+1` | Show the Graph tab |
| `Ctrl+2` | Show the Proposals tab |
| `Ctrl+N` | New proposal (focus and clear the request form) |
| `Ctrl+Enter` | Submit the proposal request |
| `Ctrl+I` | Index the selected repository |
| `Ctrl+T` | Run the repository's tests |
| `F5` | Refresh everything from the database |
| `F1` | Keyboard-shortcuts help |
| `Arrow keys` | Navigate the graph and tables |
| `Ctrl+Q` | Quit |

Accessibility: every control has an accessible name, form labels use buddies
and mnemonics, status is always a text label (`OK:`, `Error:`, `PASSED`,
`✎ changed`) and never colour alone, both palettes are WCAG-AA contrast,
and the graph is fully keyboard-navigable.

## Accessibility

- Keyboard-only by design — it is a CLI.
- Status is always a **text label** (`OK`, `FAILED`, `Error:`, `[SAMPLE]`),
  never conveyed by color alone.
- Respects `NO_COLOR` (and non-TTY output loses color automatically).
- Output is plain text suitable for screen readers; loading spinners appear
  only on an interactive TTY.

## Privacy

Single-user and local. No telemetry, no analytics, no ads, no accounts, no
background services. The only outbound request is the one you trigger, to the
model URL you configured. Your `.env` key stays on your machine.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/install.sh` | Create `.venv` (python3.12), install `.[dev,gui]`, make `.env` (600) |
| `scripts/dev.sh ...` | Dev mode: `SK_DATA_DIR=./.data`, runs `doctor` then passes args to `skraken` |
| `scripts/test.sh ...` | Run pytest in `.venv` |
| `scripts/build.sh` | Build sdist + wheel into `dist/` |
| `scripts/run.sh ...` | Install the built wheel into `.venv-run` and run it (default data dir) |
| `scripts/screenshots.sh ...` | Regenerate the README screenshots (`python scripts/screenshots.py --out docs/screenshots`) |

## Testing

Unit tests cover config, DB, portability, sample data, git/diff handling,
indexing, the LLM client and the runner; plus a workflow, CLI and end-to-end
smoke test that exercises the full loop (init → index → propose → apply →
log) against a **local HTTP stub** of an OpenAI-compatible endpoint — no real
model, no network.

```bash
scripts/test.sh            # everything
```

## Limitations (honest list)

- The test command is split with `shlex` and run without a shell — no pipes,
  `&&`, globs or redirects in `--test-cmd`. Wrap those in a script.
- Search is keyword/FTS5, not embeddings or semantic.
- Diff quality depends entirely on your model and context size; a bad model
  gives bad diffs, and `apply` only guarantees the diff applies and your tests
  decide the rest.
- `revert` reverses the diff in the working tree; it does not rewrite git
  history (if you used `--commit`, the commit stays; revert makes an inverse
  change to undo its effect).
- One model at a time, one endpoint, configured by environment variables.
- No merge-conflict UI, no rebase UI — the GUI covers the core loop, not all
  of git.
- Context is truncated at `SK_MAX_CONTEXT_CHARS`; big refactors across many
  files will not fit in one request.
