"""[SAMPLE] demo data (see CONTRACT.md §Sample data).

Creates throwaway git repositories inside ``settings.data_dir`` so the whole
loop — register, index, propose, apply, test — can be tried without touching a
real project.  Everything it writes is labelled ``[SAMPLE]``, and everything it
writes stays inside the data directory: two demos live there,

* ``sample-repo`` — the calculator demo (one commit, one failing test, one
  proposal that turns it green), and
* ``sample-multibranch`` + ``sample-multibranch-origin.git`` — a richer demo
  project with a real multi-branch history (parallel features, ``--no-ff``
  merges, a hotfix, a release branch, two tags, an unmerged experiment and a
  bare remote that main has drifted away from in both directions), which is
  what ``skraken sample load --kind multibranch`` builds and
  ``skraken sample remove`` deletes.

This module talks to git through :mod:`subprocess` directly; it must not import
``selfkraken.gitops`` (owned by another agent).
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping

from .config import Settings
from .db import Repo, Store
from .errors import GitError, ValidationError

SAMPLE_LABEL: Final[str] = "[SAMPLE]"
SAMPLE_REPO_DIRNAME: Final[str] = "sample-repo"
SAMPLE_REPO_NAME: Final[str] = f"{SAMPLE_LABEL} calculator"
SAMPLE_TEST_COMMAND: Final[str] = "python -m pytest -q"
SAMPLE_MODEL: Final[str] = "sample"
SAMPLE_PROPOSAL_REQUEST: Final[str] = f"{SAMPLE_LABEL} Raise ValueError on division by zero"
SAMPLE_MARKER: Final[str] = ".selfkraken-sample"

_CALC_HEADER: Final[str] = '''"""[SAMPLE] Tiny calculator used by selfkraken's sample data.

``div`` deliberately has no zero check: the sample proposal adds one.
"""

from __future__ import annotations

'''

_CALC_BODY: Final[str] = '''

def add(a: int, b: int) -> int:
    return a + b


def sub(a: int, b: int) -> int:
    return a - b


def mul(a: int, b: int) -> int:
    return a * b


def div(a: int, b: int) -> float:
    return a / b
'''

CALC_PY: Final[str] = _CALC_HEADER + _CALC_BODY

CALC_PY_FIXED: Final[str] = _CALC_HEADER + _CALC_BODY.replace(
    "def div(a: int, b: int) -> float:\n    return a / b\n",
    'def div(a: int, b: int) -> float:\n'
    '    if b == 0:\n'
    '        raise ValueError("division by zero")\n'
    "    return a / b\n",
)

TEST_CALC_PY: Final[str] = '''"""[SAMPLE] pytest-style tests for calc.py (plain asserts, no fixtures)."""

from __future__ import annotations

import calc


def test_add() -> None:
    assert calc.add(2, 3) == 5


def test_sub() -> None:
    assert calc.sub(10, 4) == 6


def test_mul() -> None:
    assert calc.mul(6, 7) == 42


def test_div() -> None:
    assert calc.div(9, 3) == 3.0


def test_div_by_zero_raises_value_error() -> None:
    """Fails on the checked-in calc.py; passes once the sample patch is applied."""
    error: BaseException | None = None
    try:
        calc.div(4, 0)
    except BaseException as exc:  # captured so the assert below can name it
        error = exc
    assert isinstance(error, ValueError), f"div(4, 0) should raise ValueError, got {error!r}"
'''

SAMPLE_GITIGNORE: Final[str] = f"{SAMPLE_MARKER}\n__pycache__/\n*.py[cod]\n.pytest_cache/\n"

SAMPLE_RATIONALE: Final[str] = (
    f"{SAMPLE_LABEL} Example patch written by selfkraken itself (no model was used): "
    "guard ``div`` so a zero divisor raises ValueError instead of leaking "
    "ZeroDivisionError. Apply it and the sample test suite goes green."
)


def _git_style_diff(rel_path: str, before: str, after: str) -> str:
    """A real unified diff (git style) turning *before* into *after* at *rel_path*."""
    body = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
            lineterm="\n",
        )
    )
    diff = f"diff --git a/{rel_path} b/{rel_path}\n" + body
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


def _build_sample_diff() -> str:
    """A real unified diff (git style) turning CALC_PY into CALC_PY_FIXED."""
    return _git_style_diff("calc.py", CALC_PY, CALC_PY_FIXED)


SAMPLE_DIFF: Final[str] = _build_sample_diff()

_COMMIT_MESSAGE: Final[str] = f"{SAMPLE_LABEL} calculator demo repository"


def sample_repo_dir(settings: Settings) -> Path:
    """Where the calculator sample repository lives for these settings."""
    return Path(settings.data_dir) / SAMPLE_REPO_DIRNAME


def multibranch_repo_dir(settings: Settings) -> Path:
    """Where the multi-branch sample repository lives for these settings."""
    return Path(settings.data_dir) / MULTIBRANCH_REPO_DIRNAME


def multibranch_origin_dir(settings: Settings) -> Path:
    """Where the multi-branch sample's bare remote lives (next to the repo)."""
    return Path(settings.data_dir) / MULTIBRANCH_ORIGIN_DIRNAME


def _check_sample_dir(
    settings: Settings, target: Path, expected_name: str = SAMPLE_REPO_DIRNAME
) -> None:
    """A sample directory must be exactly ``<data_dir>/<expected_name>``.

    This is what keeps ``sample remove`` from ever deleting something that is
    not ours: the name must match and the parent must be the data directory
    itself, and the marker file has to agree afterwards.
    """
    base = Path(settings.data_dir).resolve()
    candidate = Path(target).resolve()
    if candidate.name != expected_name or candidate.parent != base:
        raise ValidationError(
            f"{SAMPLE_LABEL} refusing to touch {candidate.name}: "
            "the sample directory must sit directly inside the data directory"
        )


def _git_present() -> None:
    if shutil.which("git") is None:
        raise GitError("git executable not found — sample data needs git")


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no output"


def _run_git(
    repo_dir: Path,
    *args: str,
    timeout: int = 60,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> str:
    name = args[0] if args else "git"
    command = [
        "git",
        "-c",
        "user.name=selfkraken-sample",
        "-c",
        "user.email=sample@localhost",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "init.defaultBranch=main",
        "-C",
        str(repo_dir),
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git {name}") from exc
    if result.returncode != 0:
        if not check:
            return ""
        raise GitError(f"git {name} failed: {_first_line(result.stderr or result.stdout)}")
    return result.stdout


def _write_sample_marker(target: Path) -> None:
    """Drop the ownership marker that lets ``sample remove`` trust this directory."""
    marker = target / SAMPLE_MARKER
    if not marker.exists():
        marker.write_text(
            f"{SAMPLE_LABEL} selfkraken sample data — safe to delete with"
            " 'skraken sample remove'\n",
            encoding="utf-8",
        )


def _write_sample_files(repo_dir: Path) -> None:
    """Create the sample files, never overwriting files already there."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_sample_marker(repo_dir)
    contents = {
        "calc.py": CALC_PY,
        "test_calc.py": TEST_CALC_PY,
        ".gitignore": SAMPLE_GITIGNORE,
    }
    for name, text in contents.items():
        target = repo_dir / name
        if not target.exists():
            target.write_text(text, encoding="utf-8")


def _init_sample_repo(repo_dir: Path) -> None:
    """git init + one starter commit; existing history and edits are left alone."""
    _git_present()
    if not (repo_dir / ".git").is_dir():
        _run_git(repo_dir, "init", "--quiet", ".")
    if _run_git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
        return
    _run_git(repo_dir, "add", "--", "calc.py", "test_calc.py", ".gitignore")
    _run_git(repo_dir, "commit", "--quiet", "-m", _COMMIT_MESSAGE)


def _find_sample_repo(
    store: Store, expected_path: str, name: str = SAMPLE_REPO_NAME
) -> Repo | None:
    for repo in store.list_repos():
        if repo.is_sample and (repo.path == expected_path or repo.name == name):
            return repo
    return None


def load_sample(store: Store, settings: Settings) -> Repo:
    """Create (or reuse) the ``[SAMPLE]`` repository, registration and proposal."""
    repo_dir = sample_repo_dir(settings)
    _check_sample_dir(settings, repo_dir)
    _write_sample_files(repo_dir)
    _init_sample_repo(repo_dir)

    resolved = str(repo_dir.resolve())
    existing = _find_sample_repo(store, resolved)
    if existing is not None:
        return existing

    repo = store.add_repo(
        Path(resolved),
        name=SAMPLE_REPO_NAME,
        test_command=SAMPLE_TEST_COMMAND,
        is_sample=True,
    )
    proposal = store.create_proposal(
        repo.id,
        request=SAMPLE_PROPOSAL_REQUEST,
        model=SAMPLE_MODEL,
        diff=SAMPLE_DIFF,
        rationale=SAMPLE_RATIONALE,
        status="proposed",
        is_sample=True,
    )
    store.record_change(
        "sample_load",
        f"{SAMPLE_LABEL} loaded calculator sample (repo {repo.id}, proposal {proposal.id})",
        repo_id=repo.id,
    )
    return repo


# ------------------------------------------------------------------ [SAMPLE] multi-branch
#
# The demo that ships with the app: a small but real python project whose
# history has parallel features, ``--no-ff`` merges, a hotfix, a release branch,
# two tags, an experiment that never landed and a bare remote that main has
# drifted away from in *both* directions.  Everything is scripted — fixed
# identity, fixed dates, no user config, no network — so ``skraken graph`` shows
# the same shape on every machine, and nothing is written outside the data dir.

MULTIBRANCH_REPO_DIRNAME: Final[str] = "sample-multibranch"
MULTIBRANCH_ORIGIN_DIRNAME: Final[str] = "sample-multibranch-origin.git"
MULTIBRANCH_REPO_NAME: Final[str] = f"{SAMPLE_LABEL} multibranch demo"
MULTIBRANCH_PROPOSAL_REQUEST: Final[str] = (
    f"{SAMPLE_LABEL} Add a slug helper to src/app/core.py and cover it with a test"
)
MULTIBRANCH_RATIONALE: Final[str] = (
    f"{SAMPLE_LABEL} Example patch written by selfkraken itself (no model was used): "
    "add ``slug()``, the text helper the export module ends up wanting, together "
    "with a test for it. The demo's own suite is green before *and* after "
    "applying it, so ``skraken apply`` reports tests_passed."
)

#: The identity every demo commit carries, and the fixed clock: 2025-03-03
#: 09:00 UTC with four hours of work between steps (stable newest-first order).
SAMPLE_IDENTITY_NAME: Final[str] = "selfkraken-sample"
SAMPLE_IDENTITY_EMAIL: Final[str] = "sample@localhost"
_MB_START: Final[datetime] = datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc)
_MB_STEP_SECONDS: Final[int] = 4 * 3600


def _replace_once(text: str, old: str, new: str) -> str:
    """Replace *old* with *new*, exactly once — sample drift must fail loudly."""
    if text.count(old) != 1:
        raise ValidationError(f"{SAMPLE_LABEL} sample data drift: expected {old!r} once")
    return text.replace(old, new, 1)


_MB_README_V0: Final[str] = """# Demo App

A tiny Python project that ships with selfkraken as the ``[SAMPLE]``
multi-branch demo: parallel features, ``--no-ff`` merges, a hotfix, a release
branch and one experiment that never landed.

Run the tests with ``python -m pytest -q``.
"""

_MB_README_ROADMAP: Final[str] = """
## Roadmap

- login
- search
- export
- cache
"""

_MB_README_V1: Final[str] = _MB_README_V0 + _MB_README_ROADMAP
_MB_README_V2: Final[str] = _replace_once(
    _MB_README_V1, "- search\n", "- search (case-insensitive, ranked)\n"
)

_MB_GITIGNORE_V0: Final[str] = f"{SAMPLE_MARKER}\n__pycache__/\n*.py[cod]\n.pytest_cache/\n"
_MB_GITIGNORE_V1: Final[str] = _MB_GITIGNORE_V0 + "build/\ndist/\n"

_MB_CONFTEST: Final[str] = '''"""Make the repository root importable when the suite runs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
'''

_MB_PACKAGE_V0: Final[str] = '"""Demo application package."""\n\n__version__ = "0.1.0"\n'
_MB_PACKAGE_V1: Final[str] = _replace_once(_MB_PACKAGE_V0, '"0.1.0"', '"0.2.0"')

_MB_DOC_TESTING: Final[str] = """# Running the tests

From the repository root:

```console
$ python -m pytest -q
```

No markers, no plugins, no network: every test is a plain ``assert`` over a
function in ``src/app``.
"""

_MB_CORE_V0: Final[str] = '''"""Core helpers for the demo app."""

from __future__ import annotations


def greet(name: str) -> str:
    return f"hello {name}"
'''

_MB_CORE_V1: Final[str] = _MB_CORE_V0 + '''

def compute(a: int, b: int) -> int:
    return a + b
'''

_MB_COMPUTE_V1: Final[str] = "def compute(a: int, b: int) -> int:\n    return a + b\n"
_MB_COMPUTE_V2: Final[str] = (
    "def compute(a: int, b: int) -> int:\n"
    "    return a * 3 + b\n"
    "\n"
    "\n"
    "def divide(a: int, b: int) -> float:\n"
    "    return a / b\n"
)
_MB_CORE_V2: Final[str] = _replace_once(_MB_CORE_V1, _MB_COMPUTE_V1, _MB_COMPUTE_V2)

_MB_DIVIDE_V2: Final[str] = "def divide(a: int, b: int) -> float:\n    return a / b\n"
_MB_DIVIDE_V3: Final[str] = (
    "def divide(a: int, b: int) -> float:\n"
    "    if b == 0:\n"
    '        raise ValueError("division by zero")\n'
    "    return a / b\n"
)
_MB_CORE_V3: Final[str] = _replace_once(_MB_CORE_V2, _MB_DIVIDE_V2, _MB_DIVIDE_V3)

_MB_CORE_V4: Final[str] = _MB_CORE_V3 + '''

def summarise(items: list[int]) -> int:
    return sum(items) * 2
'''

_MB_CORE_PROPOSED: Final[str] = _MB_CORE_V4 + '''

def slug(text: str) -> str:
    """Turn *text* into a lowercase, hyphen-joined slug."""
    kept = [c for c in text.lower() if c.isalnum() or c.isspace()]
    return "-".join("".join(kept).split())
'''

_MB_TEST_CORE_V0: Final[str] = '''"""Tests for src.app.core."""

from __future__ import annotations

from src.app.core import greet


def test_greet() -> None:
    assert greet("ada") == "hello ada"
'''

_MB_TEST_CORE_V1: Final[str] = _replace_once(
    _MB_TEST_CORE_V0,
    "from src.app.core import greet\n",
    "from src.app.core import compute, greet\n",
) + '''

def test_compute() -> None:
    assert compute(2, 3) == 5
'''

_MB_TEST_CORE_V2: Final[str] = _replace_once(
    _MB_TEST_CORE_V1, "compute(2, 3) == 5", "compute(2, 3) == 9"
)

_MB_TEST_CORE_V3: Final[str] = _replace_once(
    _MB_TEST_CORE_V2,
    "from src.app.core import compute, greet\n",
    "import pytest\n\nfrom src.app.core import compute, divide, greet\n",
) + '''

def test_divide() -> None:
    assert divide(9, 3) == 3.0


def test_divide_rejects_zero() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
'''

_MB_TEST_CORE_V4: Final[str] = _replace_once(
    _MB_TEST_CORE_V3,
    "from src.app.core import compute, divide, greet\n",
    "from src.app.core import compute, divide, greet, summarise\n",
) + '''

def test_summarise() -> None:
    assert summarise([1, 2, 3]) == 12
'''

_MB_TEST_CORE_PROPOSED: Final[str] = _replace_once(
    _MB_TEST_CORE_V4,
    "from src.app.core import compute, divide, greet, summarise\n",
    "from src.app.core import compute, divide, greet, slug, summarise\n",
) + '''

def test_slug() -> None:
    assert slug("Hello, World!") == "hello-world"
    assert slug("  Kraken   Sample ") == "kraken-sample"
'''

_MB_AUTH_V0: Final[str] = '''"""Login helpers."""

from __future__ import annotations


def login(user: str, password: str) -> bool:
    return bool(user) and bool(password)
'''

_MB_AUTH_V1: Final[str] = '''"""Login helpers."""

from __future__ import annotations


def login(user: str, password: str) -> bool:
    """Both halves of the credentials have to carry something."""
    if not user.strip() or not password:
        return False
    return True
'''

_MB_TEST_AUTH: Final[str] = '''"""Tests for src.app.auth."""

from __future__ import annotations

from src.app.auth import login


def test_login_accepts_full_credentials() -> None:
    assert login("ada", "secret") is True


def test_login_rejects_an_empty_password() -> None:
    assert login("ada", "") is False


def test_login_rejects_a_blank_user() -> None:
    assert login("   ", "secret") is False
'''

_MB_DOC_LOGIN: Final[str] = """# Login

`login(user, password)` is deliberately strict: a blank user name or an empty
password is refused. Real authentication is out of scope for a demo project.
"""

_MB_SEARCH_V0: Final[str] = '''"""Search prototype."""

from __future__ import annotations


def find(items: list[str], needle: str) -> list[str]:
    return [item for item in items if needle in item]
'''

_MB_SEARCH_V1: Final[str] = '''"""Search prototype with simple ranking."""

from __future__ import annotations


def find(items: list[str], needle: str) -> list[str]:
    scored = [(item.count(needle), item) for item in items if needle in item]
    return [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]
'''

_MB_SEARCH_V2: Final[str] = '''"""Substring search with simple ranking."""

from __future__ import annotations


def find(items: list[str], needle: str, limit: int | None = None) -> list[str]:
    """Items containing *needle*, case-insensitively, most matches first."""
    lowered = needle.lower()
    scored = [
        (item.lower().count(lowered), item) for item in items if lowered in item.lower()
    ]
    ranked = [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]
    return ranked[:limit] if limit is not None else ranked
'''

_MB_TEST_SEARCH_V1: Final[str] = '''"""Tests for src.app.search."""

from __future__ import annotations

from src.app.search import find


def test_find_ranks_by_hits() -> None:
    assert find(["aa", "a"], "a") == ["aa", "a"]


def test_find_skips_missing_items() -> None:
    assert find(["beta"], "zz") == []
'''

_MB_TEST_SEARCH_V2: Final[str] = _MB_TEST_SEARCH_V1 + '''

def test_find_ignores_case() -> None:
    assert find(["Beta", "gamma"], "bet") == ["Beta"]


def test_find_respects_the_limit() -> None:
    assert find(["aa", "a"], "a", limit=1) == ["aa"]
'''

_MB_EXPORT_V0: Final[str] = '''"""CSV export helpers."""

from __future__ import annotations

DELIMITER = ","


def row(values: list[object]) -> str:
    """Join *values* into one CSV row."""
    return DELIMITER.join(str(value) for value in values)
'''

_MB_EXPORT_V1: Final[str] = _MB_EXPORT_V0 + '''

def table(headers: list[str], rows: list[list[object]]) -> str:
    """Render *headers* and *rows* as CSV text with a trailing newline."""
    lines = [row(list(headers))]
    lines.extend(row(values) for values in rows)
    return "\\n".join(lines) + "\\n"
'''

_MB_TEST_EXPORT: Final[str] = '''"""Tests for src.app.export."""

from __future__ import annotations

from src.app.export import row, table


def test_row_joins_values() -> None:
    assert row(["a", 1, True]) == "a,1,True"


def test_table_writes_a_header_and_rows() -> None:
    assert table(["x", "y"], [[1, 2], [3, 4]]) == "x,y\\n1,2\\n3,4\\n"
'''

_MB_DOC_EXPORT: Final[str] = """# Export format

`table(headers, rows)` writes plain comma-separated text: a header row, one row
per record, a trailing newline. Values are stringified with ``str()``.
"""

_MB_CHANGELOG_V0: Final[str] = """# Changelog

## 0.1.0

- login, search and the compute helpers.
"""

_MB_CHANGELOG_V1: Final[str] = _MB_CHANGELOG_V0 + """
## 0.2.0

- CSV export.
- ``divide`` now refuses a zero divisor.
"""

_MB_CHANGELOG_V2: Final[str] = _MB_CHANGELOG_V1 + """
## Unreleased

- an in-memory cache is being experimented with.
"""

#: Written on a throwaway branch and pushed straight to ``origin/main``: the
#: commit exists on the remote only, so main is *behind* as well as *ahead*.
_MB_PIPELINE: Final[str] = """# CI for the demo app (added on the remote, never pulled locally)
jobs:
  test:
    script:
      - python -m pytest -q
"""

_MB_CACHE_V0: Final[str] = '''"""Naive in-memory cache (experimental)."""

from __future__ import annotations

_CACHE: dict[str, str] = {}


def remember(key: str, value: str) -> None:
    _CACHE[key] = value
'''

_MB_CACHE_V1: Final[str] = _replace_once(
    _MB_CACHE_V0,
    '"""Naive in-memory cache (experimental)."""',
    '"""In-memory cache with a size cap (experimental)."""',
) + '''

def forget(key: str) -> None:
    _CACHE.pop(key, None)
'''

_MB_TEST_CACHE_V0: Final[str] = '''"""Tests for src.app.cache."""

from __future__ import annotations

from src.app.cache import _CACHE, remember


def test_remember_stores_a_value() -> None:
    remember("k", "v")
    assert _CACHE["k"] == "v"
'''

_MB_TEST_CACHE_V1: Final[str] = _replace_once(
    _MB_TEST_CACHE_V0,
    "from src.app.cache import _CACHE, remember\n",
    "from src.app.cache import _CACHE, forget, remember\n",
) + '''

def test_forget_drops_a_value() -> None:
    remember("gone", "1")
    forget("gone")
    assert "gone" not in _CACHE
'''


def _build_multibranch_diff() -> str:
    """A real two-file patch against the demo's ``main`` (core module + its test)."""
    return _git_style_diff("src/app/core.py", _MB_CORE_V4, _MB_CORE_PROPOSED) + _git_style_diff(
        "tests/test_core.py", _MB_TEST_CORE_V4, _MB_TEST_CORE_PROPOSED
    )


MULTIBRANCH_DIFF: Final[str] = _build_multibranch_diff()


class _DemoGit:
    """Scripted git for the shipped demo: pinned identity, pinned dates.

    :meth:`tick` moves the demo clock forward before every command that creates
    an object, so the history is byte-for-byte reproducible and never depends
    on the developer's ``git config``, hooks or ``GIT_*`` environment.
    """

    def __init__(self, repo_dir: Path) -> None:
        self.path = Path(repo_dir)
        self._step = 0

    def _env(self) -> dict[str, str]:
        stamp = f"@{int(_MB_START.timestamp()) + self._step * _MB_STEP_SECONDS} +0000"
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "HOME": str(self.path.parent),
                "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_DATE": stamp,
                "GIT_AUTHOR_NAME": SAMPLE_IDENTITY_NAME,
                "GIT_AUTHOR_EMAIL": SAMPLE_IDENTITY_EMAIL,
                "GIT_COMMITTER_NAME": SAMPLE_IDENTITY_NAME,
                "GIT_COMMITTER_EMAIL": SAMPLE_IDENTITY_EMAIL,
                # no user alias, hook, template or signing key may reshape the demo
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "LC_ALL": "C",
            }
        )
        return env

    def run(self, *args: str, timeout: int = 60) -> str:
        """One git command at the current clock position."""
        return _run_git(self.path, *args, timeout=timeout, env=self._env())

    def tick(self) -> None:
        """Advance the demo clock: the next object gets a newer date."""
        self._step += 1

    def write(self, rel_path: str, content: str) -> None:
        target = self.path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def init(self) -> None:
        self.tick()
        self.run("init", "--quiet", "-b", "main", ".")

    def commit(self, message: str, *paths: str) -> str:
        self.tick()
        self.run("add", "--", *paths)
        self.run("commit", "--quiet", "-m", message)
        return self.rev("HEAD")

    def checkout(self, name: str, *, create: bool = False, start: str | None = None) -> None:
        if not create:
            self.run("checkout", "--quiet", name)
            return
        self.run("checkout", "--quiet", "-b", name, *([start] if start else []))

    def merge(self, ref: str, message: str) -> str:
        self.tick()
        self.run("merge", "--no-ff", "--quiet", "-m", message, ref)
        return self.rev("HEAD")

    def tag(self, name: str, message: str) -> None:
        self.tick()
        self.run("tag", "-a", name, "-m", message)

    def rev(self, ref: str) -> str:
        return self.run("rev-parse", ref).strip()


def _mb_scaffold(demo: _DemoGit) -> None:
    """The ``main`` commits that exist before any branch was cut."""
    demo.write("README.md", _MB_README_V0)
    demo.write(".gitignore", _MB_GITIGNORE_V0)
    demo.write("conftest.py", _MB_CONFTEST)
    demo.write("src/app/__init__.py", _MB_PACKAGE_V0)
    demo.write("src/app/core.py", _MB_CORE_V0)
    demo.write("tests/test_core.py", _MB_TEST_CORE_V0)
    demo.commit(
        "Initial project scaffold",
        "README.md",
        ".gitignore",
        "conftest.py",
        "src/app/__init__.py",
        "src/app/core.py",
        "tests/test_core.py",
    )

    demo.write("src/app/core.py", _MB_CORE_V1)
    demo.write("tests/test_core.py", _MB_TEST_CORE_V1)
    demo.commit("Add compute helper", "src/app/core.py", "tests/test_core.py")

    demo.write("docs/testing.md", _MB_DOC_TESTING)
    demo.commit("Document how to run the tests", "docs/testing.md")

    demo.write("README.md", _MB_README_V1)
    demo.commit("Draft the roadmap", "README.md")


def _mb_features(demo: _DemoGit) -> None:
    """feature/login and feature/search in parallel; search folds main in."""
    demo.checkout("feature/login", create=True)
    demo.write("src/app/auth.py", _MB_AUTH_V0)
    demo.commit("Add login helper", "src/app/auth.py")
    demo.write("src/app/auth.py", _MB_AUTH_V1)
    demo.write("tests/test_auth.py", _MB_TEST_AUTH)
    demo.commit("Reject blank credentials", "src/app/auth.py", "tests/test_auth.py")
    demo.write("docs/login.md", _MB_DOC_LOGIN)
    demo.commit("Document the login rules", "docs/login.md")

    # cut from the same main commit, so the two features really are parallel
    demo.checkout("main")
    demo.checkout("feature/search", create=True)
    demo.write("src/app/search.py", _MB_SEARCH_V0)
    demo.commit("Add search prototype", "src/app/search.py")
    demo.write("src/app/search.py", _MB_SEARCH_V1)
    demo.write("tests/test_search.py", _MB_TEST_SEARCH_V1)
    demo.commit("Rank search results", "src/app/search.py", "tests/test_search.py")

    # main moves on underneath the search branch, which then folds it in
    demo.checkout("main")
    demo.write("src/app/core.py", _MB_CORE_V2)
    demo.write("tests/test_core.py", _MB_TEST_CORE_V2)
    demo.commit("Tune compute and add divide", "src/app/core.py", "tests/test_core.py")

    demo.checkout("feature/search")
    demo.merge("main", "Merge main into feature/search")
    demo.write("src/app/search.py", _MB_SEARCH_V2)
    demo.write("tests/test_search.py", _MB_TEST_SEARCH_V2)
    demo.commit(
        "Match search case-insensitively", "src/app/search.py", "tests/test_search.py"
    )

    demo.checkout("main")
    demo.write("README.md", _MB_README_V2)
    demo.commit("Note the search state in the roadmap", "README.md")
    demo.merge("feature/login", "Merge feature/login into main")
    demo.merge("feature/search", "Merge feature/search into main")


def _mb_export_and_releases(demo: _DemoGit) -> None:
    """feature/export, the hotfix, v0.1.0, release/0.2 and v0.2.0."""
    demo.checkout("feature/export", create=True)
    demo.write("src/app/export.py", _MB_EXPORT_V0)
    demo.commit("Add CSV row helper", "src/app/export.py")
    demo.write("src/app/export.py", _MB_EXPORT_V1)
    demo.commit("Export whole tables", "src/app/export.py")
    demo.write("tests/test_export.py", _MB_TEST_EXPORT)
    demo.commit("Cover export with tests", "tests/test_export.py")
    demo.write("docs/export.md", _MB_DOC_EXPORT)
    demo.commit("Document the export format", "docs/export.md")

    demo.checkout("main")
    demo.checkout("hotfix/div-zero", create=True)
    demo.write("src/app/core.py", _MB_CORE_V3)
    demo.write("tests/test_core.py", _MB_TEST_CORE_V3)
    demo.commit("Guard against division by zero", "src/app/core.py", "tests/test_core.py")

    demo.checkout("main")
    demo.merge("hotfix/div-zero", "Merge hotfix/div-zero into main")
    demo.write("CHANGELOG.md", _MB_CHANGELOG_V0)
    demo.commit("Add the 0.1.0 release notes", "CHANGELOG.md")
    demo.tag("v0.1.0", "Release 0.1.0")

    demo.write("src/app/core.py", _MB_CORE_V4)
    demo.write("tests/test_core.py", _MB_TEST_CORE_V4)
    demo.commit("Add summarise helper", "src/app/core.py", "tests/test_core.py")
    demo.merge("feature/export", "Merge feature/export into main")

    demo.checkout("release/0.2", create=True)
    demo.write("src/app/__init__.py", _MB_PACKAGE_V1)
    demo.write("CHANGELOG.md", _MB_CHANGELOG_V1)
    demo.commit("Start release 0.2", "src/app/__init__.py", "CHANGELOG.md")
    demo.tag("v0.2.0", "Release 0.2.0")

    demo.checkout("main")
    demo.merge("release/0.2", "Merge release/0.2 into main")


def _mb_experiment(demo: _DemoGit) -> None:
    """experiment/cache: two commits that are never merged."""
    demo.checkout("experiment/cache", create=True)
    demo.write("src/app/cache.py", _MB_CACHE_V0)
    demo.write("tests/test_cache.py", _MB_TEST_CACHE_V0)
    demo.commit("Draft in-memory cache", "src/app/cache.py", "tests/test_cache.py")
    demo.write("src/app/cache.py", _MB_CACHE_V1)
    demo.write("tests/test_cache.py", _MB_TEST_CACHE_V1)
    demo.commit(
        "Cap the cache and allow forgetting", "src/app/cache.py", "tests/test_cache.py"
    )
    demo.checkout("main")


def _mb_add_origin(demo: _DemoGit, origin: Path) -> None:
    """Create the bare remote inside the data dir and push everything to it.

    A left-over remote from an earlier build is wiped and rebuilt (it is ours:
    the marker file proves it), so rebuilding a deleted demo can never be
    refused as a non-fast-forward against the previous history.
    """
    origin = Path(origin)
    if origin.exists():
        if origin.is_dir() and (origin / SAMPLE_MARKER).is_file():
            shutil.rmtree(origin)
        elif origin.is_dir() and not any(origin.iterdir()):
            pass  # empty directory from a half-finished run: fill it
        else:
            raise ValidationError(
                f"{SAMPLE_LABEL} refusing to use {origin.name} as the demo remote: "
                f"it exists and carries no {SAMPLE_MARKER} marker"
            )
    origin.mkdir(parents=True, exist_ok=True)
    _write_sample_marker(origin)
    demo.tick()
    demo.run("init", "--bare", "--quiet", "-b", "main", str(origin))
    demo.run("remote", "add", "origin", str(origin))
    demo.run("push", "--quiet", "--all", "origin")
    demo.run("push", "--quiet", "--tags", "origin")
    demo.run("push", "--quiet", "--set-upstream", "origin", "main")


def _mb_diverge_from_origin(demo: _DemoGit) -> None:
    """One local-only commit and one remote-only commit: ahead *and* behind."""
    pushed = demo.rev("main")

    demo.write(".gitignore", _MB_GITIGNORE_V1)
    demo.commit("Ignore build output as well", ".gitignore")
    demo.write("CHANGELOG.md", _MB_CHANGELOG_V2)
    demo.commit("Note the cache experiment in the changelog", "CHANGELOG.md")

    demo.checkout("tmp/upstream-notes", create=True, start=pushed)
    demo.write(".ci/pipeline.yml", _MB_PIPELINE)
    demo.commit("Add the CI pipeline the release desk pushed", ".ci/pipeline.yml")
    demo.run("push", "--quiet", "origin", "tmp/upstream-notes:main")
    demo.checkout("main")
    demo.run("branch", "-D", "tmp/upstream-notes")
    demo.run("fetch", "--quiet", "--prune", "origin")


def _has_history(repo_dir: Path) -> bool:
    """True when *repo_dir* is a git repository with at least one commit."""
    if not (repo_dir / ".git").is_dir():
        return False
    return bool(_run_git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip())


def _build_multibranch(repo: Path, origin: Path) -> None:
    """Build the demo repository at *repo*, with its bare remote at *origin*.

    Deterministic and offline: fixed dates, fixed identity, no user config and
    no hooks, and nothing is written outside the two directories handed in.
    Ends on ``main`` with a clean working tree whose own tests pass.
    """
    _git_present()
    repo_dir = Path(repo)
    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_sample_marker(repo_dir)

    demo = _DemoGit(repo_dir)
    demo.init()
    _mb_scaffold(demo)
    _mb_features(demo)
    _mb_export_and_releases(demo)
    _mb_experiment(demo)
    _mb_add_origin(demo, Path(origin))
    _mb_diverge_from_origin(demo)


def _index_sample_repo(store: Store, repo: Repo) -> None:
    """Index a freshly created sample (late import: the indexer is another layer)."""
    try:
        from .indexer import index_repo
    except ImportError:  # pragma: no cover - indexer unavailable in odd installs
        return
    index_repo(store, repo)


def load_multibranch_sample(store: Store, settings: Settings) -> Repo:
    """Create (or reuse) the ``[SAMPLE]`` multi-branch demo, remote and proposal.

    Idempotent like :func:`load_sample`: an existing demo directory is never
    rebuilt and an existing registration is returned as it is.
    """
    repo_dir = multibranch_repo_dir(settings)
    origin_dir = multibranch_origin_dir(settings)
    _check_sample_dir(settings, repo_dir, MULTIBRANCH_REPO_DIRNAME)
    _check_sample_dir(settings, origin_dir, MULTIBRANCH_ORIGIN_DIRNAME)

    if not _has_history(repo_dir):
        _build_multibranch(repo_dir, origin_dir)

    resolved = str(repo_dir.resolve())
    existing = _find_sample_repo(store, resolved, MULTIBRANCH_REPO_NAME)
    if existing is not None:
        return existing

    repo = store.add_repo(
        Path(resolved),
        name=MULTIBRANCH_REPO_NAME,
        test_command=SAMPLE_TEST_COMMAND,
        is_sample=True,
    )
    proposal = store.create_proposal(
        repo.id,
        request=MULTIBRANCH_PROPOSAL_REQUEST,
        model=SAMPLE_MODEL,
        diff=MULTIBRANCH_DIFF,
        rationale=MULTIBRANCH_RATIONALE,
        status="proposed",
        is_sample=True,
    )
    _index_sample_repo(store, repo)
    store.record_change(
        "sample_load",
        f"{SAMPLE_LABEL} loaded multibranch sample (repo {repo.id}, proposal {proposal.id})",
        repo_id=repo.id,
    )
    return repo


def _remove_sample_dir(target: Path) -> None:
    """Delete one sample directory — only when the ownership marker says it is ours."""
    if target.is_dir():
        if not (target / SAMPLE_MARKER).is_file():
            raise ValidationError(
                f"{SAMPLE_LABEL} refusing to delete {target.name}: "
                f"no {SAMPLE_MARKER} marker, so it may not be ours"
            )
        shutil.rmtree(target)
    elif target.exists():
        raise ValidationError(
            f"{SAMPLE_LABEL} refusing to delete {target.name}: it is not a directory"
        )


def remove_sample(store: Store, settings: Settings) -> int:
    """Delete sample DB rows and every sample directory this module can create.

    Three directories are considered: the calculator repository, the
    multi-branch demo and the demo's bare remote. Each one must sit directly
    inside the data directory under its own expected name *and* carry the
    :data:`SAMPLE_MARKER` file — anything else (an unmarked directory of the
    same name, a file, a path outside the data dir) is refused and left alone.
    """
    for target, expected_name in (
        (sample_repo_dir(settings), SAMPLE_REPO_DIRNAME),
        (multibranch_repo_dir(settings), MULTIBRANCH_REPO_DIRNAME),
        (multibranch_origin_dir(settings), MULTIBRANCH_ORIGIN_DIRNAME),
    ):
        _check_sample_dir(settings, target, expected_name)
        _remove_sample_dir(target)
    removed = store.delete_sample_data()
    store.record_change("sample_remove", f"{SAMPLE_LABEL} removed sample data ({removed} rows)")
    return removed
