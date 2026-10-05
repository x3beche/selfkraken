# selfkraken — shared build contract

A private, single-user, repository-local Git assistant (personal replacement for GitKraken's core loop):
**register repo → index it → ask ONE model for a change → get a unified diff proposal → review → apply → run tests →
keep or roll back → record every step in SQLite.**

Stack (mandatory): **Python 3.12 + Typer + SQLite (stdlib `sqlite3`)**. Runtime deps: `typer`, `python-dotenv`, `rich` (already a typer dependency) only.
HTTP to the model uses stdlib `urllib.request`. Dev deps: `pytest`, `build`. No analytics, telemetry, ads, accounts.

Package: `selfkraken/`. CLI entry point: `skraken = selfkraken.cli:app` (also `python -m selfkraken`).
Already present (do not rewrite): `pyproject.toml`, `selfkraken/__init__.py`, `selfkraken/__main__.py`,
`selfkraken/errors.py` (SKError, ValidationError, NotFoundError, GitError, LLMUnavailable, LLMError — all subclass SKError).

## File ownership (each file has exactly ONE owner; never create a file you do not own)

| Owner | Files |
|---|---|
| A data | `selfkraken/config.py`, `selfkraken/db.py`, `selfkraken/portability.py`, `selfkraken/sample.py`, `tests/test_config.py`, `tests/test_db.py`, `tests/test_portability.py`, `tests/test_sample.py` |
| B engine | `selfkraken/gitops.py`, `selfkraken/diffs.py`, `selfkraken/indexer.py`, `selfkraken/llm.py`, `selfkraken/runner.py`, `tests/test_gitops_diffs.py`, `tests/test_indexer.py`, `tests/test_llm.py`, `tests/test_runner.py` |
| C app | `selfkraken/workflow.py`, `selfkraken/cli.py`, `tests/conftest.py`, `tests/test_workflow.py`, `tests/test_cli.py`, `tests/test_e2e_smoke.py` |
| D docs | `README.md`, `.env.example`, `scripts/install.sh`, `scripts/dev.sh`, `scripts/test.sh`, `scripts/build.sh`, `scripts/run.sh`, `docs/ARCHITECTURE.md` |

## Conventions
- `from __future__ import annotations` at top of every module. Type hints everywhere. Dataclasses for records.
- Timestamps: UTC ISO-8601 strings via `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
- All subprocesses: `subprocess.run([...], shell=False, capture_output=True, text=True, timeout=...)`. Never `shell=True`.
- **Never log or print secrets or private file contents.** Only print diffs/output the user explicitly asks to see.
  The `logging` module may be used only for metadata (counts, ids, paths), never contents or keys.
- Raise the errors from `selfkraken/errors.py`; do not define new exception base classes.

## Environment / settings (`selfkraken/config.py`, owner A)
`.env` in the current directory is loaded with python-dotenv (`override=False`, real env wins).

| Var | Default | Meaning |
|---|---|---|
| `SK_DATA_DIR` | `$XDG_DATA_HOME/selfkraken` or `~/.local/share/selfkraken` | where `selfkraken.db` and sample repo live |
| `SK_LLM_BASE_URL` | unset | OpenAI-compatible base URL, e.g. `http://localhost:11434/v1` (Ollama) |
| `SK_LLM_API_KEY` | unset | optional bearer token (secret) |
| `SK_LLM_MODEL` | unset | model name |
| `SK_LLM_TIMEOUT` | `120` | seconds |
| `SK_MAX_CONTEXT_CHARS` | `12000` | max repo context sent to model |
| `SK_TEST_TIMEOUT` | `600` | seconds per test run |

```python
@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path                 # data_dir / "selfkraken.db"
    llm_base_url: str | None
    llm_api_key: str | None = field(repr=False)   # NEVER in repr/str/output
    llm_model: str | None
    llm_timeout: float
    max_context_chars: int
    test_timeout: int
    @property
    def llm_enabled(self) -> bool: ...   # base_url and model both set

def load_settings(env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True) -> Settings
    # env=None -> os.environ (after loading .env). Invalid ints/URLs -> ValidationError with var name (not value).
    # base_url must start with http:// or https://
def redact(text: str, settings: Settings) -> str   # replaces api key (if any, len>=4) with "***"
```

## Database (`selfkraken/db.py`, owner A) — SQLite at `settings.db_path`
PRAGMAs: `foreign_keys=ON`, `journal_mode=WAL`. Table `meta(key TEXT PRIMARY KEY, value TEXT)` holds `schema_version`=`1`.

```sql
CREATE TABLE repos(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  test_command TEXT, is_sample INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE files(id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(repo_id, path));
CREATE VIRTUAL TABLE chunks USING fts5(repo_id UNINDEXED, path UNINDEXED, start_line UNINDEXED, content);
  -- if FTS5 is unavailable, fall back to a plain table chunks(repo_id, path, start_line, content) + LIKE search
CREATE TABLE proposals(id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  request TEXT NOT NULL, model TEXT NOT NULL, diff TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, is_sample INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE test_runs(id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL, command TEXT NOT NULL, exit_code INTEGER NOT NULL,
  passed INTEGER NOT NULL, duration_ms INTEGER NOT NULL, output_tail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE changes(id INTEGER PRIMARY KEY, repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
  proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
  created_at TEXT NOT NULL);   -- append-only audit log
```

```python
PROPOSAL_STATUSES = ("proposed", "applied", "tests_passed", "tests_failed", "rejected", "reverted")
CHANGE_KINDS = ("register", "index", "propose", "propose_failed", "apply", "test", "rollback", "commit",
                "reject", "revert", "import", "export", "sample_load", "sample_remove")

@dataclass class Repo: id:int; path:str; name:str; test_command:str|None; is_sample:bool; created_at:str
@dataclass class Proposal: id:int; repo_id:int; request:str; model:str; diff:str; rationale:str; status:str;
                           is_sample:bool; created_at:str; updated_at:str
@dataclass class TestRun: id:int; repo_id:int; proposal_id:int|None; command:str; exit_code:int; passed:bool;
                          duration_ms:int; output_tail:str; created_at:str
@dataclass class Change: id:int; repo_id:int|None; proposal_id:int|None; kind:str; detail:str; created_at:str
@dataclass class Chunk: path:str; start_line:int; content:str; score:float

class Store:
    def __init__(self, db_path: Path): ...      # mkdir parents, connect, migrate. self.conn is public.
    def close(self) -> None; __enter__/__exit__
    # repos
    def add_repo(self, path: Path, name: str, test_command: str | None = None, is_sample: bool = False) -> Repo
        # path stored as str(path.resolve()); duplicate path -> ValidationError("already registered")
    def get_repo(self, repo_id: int) -> Repo                 # NotFoundError
    def find_repo_by_path(self, path: Path) -> Repo | None   # repo whose path == path or is an ancestor of it
    def list_repos(self) -> list[Repo]
    def set_test_command(self, repo_id: int, command: str | None) -> Repo
    def delete_repo(self, repo_id: int) -> None
    # index
    def get_file_shas(self, repo_id: int) -> dict[str, str]
    def replace_file_index(self, repo_id: int, path: str, sha: str, size: int,
                           chunks: list[tuple[int, str]]) -> None    # (start_line, content); replaces old rows
    def remove_files_not_in(self, repo_id: int, keep: set[str]) -> int
    def count_files(self, repo_id: int) -> int
    def search_chunks(self, repo_id: int, query: str, limit: int = 8) -> list[Chunk]
        # tokenises query to [A-Za-z0-9_]+ words, quotes each for FTS5 MATCH (OR), never passes raw input; [] on empty
    # proposals
    def create_proposal(self, repo_id: int, request: str, model: str, diff: str, rationale: str = "",
                        status: str = "proposed", is_sample: bool = False) -> Proposal
    def get_proposal(self, proposal_id: int) -> Proposal     # NotFoundError
    def list_proposals(self, repo_id: int | None = None, status: str | None = None) -> list[Proposal]  # newest first
    def set_proposal_status(self, proposal_id: int, status: str) -> Proposal   # bad status -> ValidationError
    # tests
    def add_test_run(self, repo_id: int, proposal_id: int | None, command: str, exit_code: int,
                     duration_ms: int, output_tail: str) -> TestRun     # passed = exit_code == 0
    def list_test_runs(self, repo_id: int | None = None, proposal_id: int | None = None) -> list[TestRun]
    # audit
    def record_change(self, kind: str, detail: str, repo_id: int | None = None,
                      proposal_id: int | None = None) -> Change      # bad kind -> ValidationError
    def list_changes(self, repo_id: int | None = None, limit: int = 50) -> list[Change]  # newest first
    # sample
    def delete_sample_data(self) -> int   # deletes repos with is_sample=1 (cascade) + sample proposals; returns rows
```

## Import / export (`selfkraken/portability.py`, owner A)
```python
EXPORT_FORMAT = "selfkraken-export"; EXPORT_VERSION = 1
def export_data(store: Store, path: Path) -> dict[str, int]   # writes JSON, returns counts per table
def import_data(store: Store, path: Path) -> dict[str, int]   # returns counts imported
```
JSON: `{"format","version","exported_at","repos":[...],"proposals":[...],"test_runs":[...],"changes":[...]}`,
each row a dict of its columns. The file index / chunks are NOT exported (rebuildable, private contents).
Import: max 50 MB, strict validation (format, version, types, statuses, kinds) → ValidationError naming the bad field.
Repos matched by path (existing kept, ids remapped); all other ids remapped; whole import in one transaction.
Records a `import`/`export` change.

## Sample data (`selfkraken/sample.py`, owner A)
```python
SAMPLE_LABEL = "[SAMPLE]"
def load_sample(store: Store, settings: Settings) -> Repo
def remove_sample(store: Store, settings: Settings) -> int
```
`load_sample` creates a git repo at `settings.data_dir / "sample-repo"` (use `subprocess` + `git` directly, set
`-c user.name=selfkraken-sample -c user.email=sample@localhost`), containing `calc.py` (add/sub/mul/div functions,
`div` without zero check) and `test_calc.py` (pytest-style tests using plain `assert`, runnable via
`python -m pytest -q`), commits it, registers it with name `"[SAMPLE] calculator"`, test_command
`"python -m pytest -q"`, is_sample=True, and creates one sample proposal (request `"[SAMPLE] Raise ValueError on
division by zero"`, model `"sample"`, a real valid unified diff against calc.py, is_sample=True). Idempotent:
if already loaded, return the existing repo. `remove_sample` deletes sample DB rows and the directory.

## Git + diffs (`selfkraken/gitops.py`, `selfkraken/diffs.py`, owner B)
```python
# gitops.py
def run_git(repo_path: Path, *args: str, input: str | None = None, check: bool = True,
            timeout: int = 60) -> subprocess.CompletedProcess[str]   # check fail -> GitError(stderr first line)
def is_git_repo(path: Path) -> bool
def repo_root(path: Path) -> Path                    # git rev-parse --show-toplevel; GitError if not a repo
def head_sha(repo_path: Path) -> str | None
def changed_paths(repo_path: Path) -> list[str]      # porcelain status paths
def commit_paths(repo_path: Path, paths: list[str], message: str) -> str   # git add -- paths; commit; return sha

# diffs.py
MAX_DIFF_BYTES = 200_000
def extract_diff(text: str) -> str
    # first ```diff / ```patch fenced block, else text starting at first "diff --git" or "--- " line;
    # ensures trailing newline; ValidationError("model output contained no unified diff") if none
def touched_paths(diff: str) -> list[str]   # from +++/--- headers, strips a/ b/, ignores /dev/null, unique, sorted
def validate_diff(diff: str, repo_path: Path) -> list[str]
    # size <= MAX_DIFF_BYTES, non-empty, no NUL, every path relative, no "..", not under .git/, resolves inside repo;
    # then `git apply --check` (stdin). Returns touched paths. ValidationError with reason on failure.
def apply_diff(repo_path: Path, diff: str) -> None      # git apply (stdin); GitError on failure
def reverse_diff(repo_path: Path, diff: str) -> None    # git apply -R (stdin); GitError on failure
def can_reverse(repo_path: Path, diff: str) -> bool     # git apply -R --check
```

## Indexer (`selfkraken/indexer.py`, owner B)
```python
MAX_FILE_BYTES = 200_000; CHUNK_LINES = 60
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.pfx", "secrets.*", "*.sqlite", "*.db")
def list_repo_files(repo_path: Path) -> list[str]   # git ls-files -z --cached --others --exclude-standard
def should_skip(rel_path: str) -> bool              # secret patterns (fnmatch on basename) — never indexed
def is_probably_binary(data: bytes) -> bool         # NUL byte in first 8 KiB or undecodable utf-8
def chunk_text(text: str, chunk_lines: int = CHUNK_LINES) -> list[tuple[int, str]]  # 1-based start lines
@dataclass class IndexStats: scanned: int; indexed: int; unchanged: int; skipped: int; removed: int
def index_repo(store: Store, repo: Repo, progress: Callable[[str], None] | None = None) -> IndexStats
    # incremental by sha256 of bytes; skips secrets, binaries, > MAX_FILE_BYTES; removes deleted files
def build_context(store: Store, repo: Repo, query: str, max_chars: int, extra_paths: Sequence[str] = ()) -> str
    # extra_paths (validated relative, inside repo, not secret) read in full first, then search_chunks hits;
    # each block formatted "### {path} (from line {n})\n```\n{content}\n```\n"; total <= max_chars
```

## Model (`selfkraken/llm.py`, owner B)
```python
class LLMClient(Protocol):
    name: str
    def complete(self, system: str, user: str) -> str: ...
class OpenAICompatibleClient:   # POST {base_url.rstrip('/')}/chat/completions, JSON body
    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 120): ...
    name -> the model name. temperature 0.2. Authorization: Bearer only when api_key.
    Errors (HTTP, timeout, bad JSON, missing choices) -> LLMError with message that NEVER contains the key.
def make_client(settings: Settings) -> LLMClient | None   # None when not settings.llm_enabled (degraded mode)
SYSTEM_PROMPT: str   # instructs: reply with a one-paragraph rationale then ONE ```diff fenced unified diff
                     # (git format, a/ b/ prefixes, paths relative to repo root), no other code blocks
def build_user_prompt(request: str, context: str, repo_name: str) -> str
def split_rationale(text: str) -> str    # text before the first ``` fence, stripped, max 2000 chars
```

## Test runner (`selfkraken/runner.py`, owner B)
```python
@dataclass class TestResult: command: str; exit_code: int; duration_ms: int; output_tail: str; timed_out: bool
    passed property -> exit_code == 0 and not timed_out
def run_tests(repo_path: Path, command: str, timeout: int, tail_chars: int = 4000) -> TestResult
    # shlex.split(command), shell=False, cwd=repo_path, stdout+stderr merged; timeout -> exit_code 124, timed_out
    # missing executable -> exit_code 127 with message; empty command -> ValidationError
```

## Workflow (`selfkraken/workflow.py`, owner C) — the core loop, UI-free
```python
def register_repo(store, path: Path, test_command: str | None, name: str | None = None) -> Repo
    # path must be a git repo (repo_root); records "register"
def resolve_repo(store, path: Path | None) -> Repo   # None -> cwd; find_repo_by_path; NotFoundError with hint
def propose(store, settings, repo, request: str, *, llm: LLMClient | None = None, diff_text: str | None = None,
            extra_paths: Sequence[str] = ()) -> Proposal
    # request: 3..4000 chars, else ValidationError. diff_text given -> manual mode, model "manual".
    # else llm (or make_client(settings)) required -> LLMUnavailable explaining degraded mode.
    # LLM path: build_context -> build_user_prompt -> complete -> extract_diff + split_rationale.
    # validate_diff in both modes. On failure record "propose_failed" (reason only, no contents) and re-raise.
    # On success create_proposal(status "proposed") + record "propose".
@dataclass class ApplyOutcome: proposal: Proposal; test_run: TestRun | None; rolled_back: bool; commit_sha: str | None
def apply_proposal(store, settings, proposal_id: int, *, run_tests: bool = True, commit: bool = False,
                   keep_on_failure: bool = False, commit_message: str | None = None) -> ApplyOutcome
    # only from status "proposed" (else ValidationError). validate_diff again, apply_diff, status "applied",
    # record "apply". If run_tests and repo.test_command: run_tests -> add_test_run -> record "test".
    #   pass -> "tests_passed"; fail -> "tests_failed" and unless keep_on_failure: reverse_diff, record "rollback",
    #   rolled_back=True, final status "tests_failed".
    # commit only if not rolled back (and tests passed or none configured): commit_paths(touched) -> record "commit".
def reject_proposal(store, proposal_id: int) -> Proposal          # from "proposed" only; record "reject"
def revert_proposal(store, proposal_id: int) -> Proposal
    # from applied/tests_passed/tests_failed-not-rolled-back; can_reverse else ValidationError; reverse_diff;
    # status "reverted"; record "revert". (Does not rewrite git history.)
def run_repo_tests(store, settings, repo) -> TestRun   # ValidationError if no test_command; records "test"
```

## CLI (`selfkraken/cli.py`, owner C) — Typer app `app`, command name `skraken`
Global option `--repo PATH` on repo commands (default cwd). Commands:
`init [PATH] --test-cmd TEXT --name TEXT` · `repos` · `set-test CMD` · `index` · `search QUERY` ·
`propose REQUEST [--diff-file FILE] [--file PATH ...] [--yes]` · `proposals [--status S]` · `show ID` ·
`apply ID [--no-test] [--commit] [--keep-on-failure] [--yes]` · `reject ID` · `revert ID` · `test` ·
`log [--limit N]` · `export FILE` · `import FILE` · `sample load|remove` · `doctor`.
UX states (every command): **empty** (e.g. "No proposals yet — try: skraken propose \"...\""), **loading**
(`rich` status spinner when stdout is a TTY, e.g. "Asking model…", "Running tests…"), **validation** (exit 2,
`Error:` prefix, which input and why), **success** (`OK` text label + green), **failure** (`FAILED`/`Error:` text
label + red, exit 1). Never rely on color alone (text labels always present); respect `NO_COLOR`. `doctor` shows
data path, DB status, git version, model mode ("model: configured (<model> @ <host>)" or "degraded mode: no model
configured — manual --diff-file proposals still work"), API key only as "set"/"not set". Catch `SKError` at the
top level → message, exit 1; never print tracebacks for expected errors. Interactive confirmation before apply
unless `--yes`.
