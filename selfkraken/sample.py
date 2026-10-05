"""[SAMPLE] demo data (see CONTRACT.md §Sample data).

Creates a throwaway git repository inside ``settings.data_dir`` so the whole
loop — register, index, propose, apply, test — can be tried without touching a
real project.  Everything it writes is labelled ``[SAMPLE]``.

This module talks to git through :mod:`subprocess` directly; it must not import
``selfkraken.gitops`` (owned by another agent).
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from pathlib import Path
from typing import Final

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


def _build_sample_diff() -> str:
    """A real unified diff (git style) turning CALC_PY into CALC_PY_FIXED."""
    body = "".join(
        difflib.unified_diff(
            CALC_PY.splitlines(keepends=True),
            CALC_PY_FIXED.splitlines(keepends=True),
            fromfile="a/calc.py",
            tofile="b/calc.py",
            n=3,
            lineterm="\n",
        )
    )
    header = "diff --git a/calc.py b/calc.py\n"
    diff = header + body
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


SAMPLE_DIFF: Final[str] = _build_sample_diff()

_COMMIT_MESSAGE: Final[str] = f"{SAMPLE_LABEL} calculator demo repository"


def sample_repo_dir(settings: Settings) -> Path:
    """Where the sample repository lives for these settings."""
    return Path(settings.data_dir) / SAMPLE_REPO_DIRNAME


def _check_sample_dir(settings: Settings, target: Path) -> None:
    """The sample directory must be exactly ``<data_dir>/sample-repo``."""
    base = Path(settings.data_dir).resolve()
    candidate = Path(target).resolve()
    if candidate.name != SAMPLE_REPO_DIRNAME or candidate.parent != base:
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


def _run_git(repo_dir: Path, *args: str, timeout: int = 60, check: bool = True) -> str:
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
        "core.autocrlf=false",
        "-c",
        "init.defaultBranch=main",
        "-C",
        str(repo_dir),
        *args,
    ]
    try:
        result = subprocess.run(
            command, shell=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git {name}") from exc
    if result.returncode != 0:
        if not check:
            return ""
        raise GitError(f"git {name} failed: {_first_line(result.stderr or result.stdout)}")
    return result.stdout


def _write_sample_files(repo_dir: Path) -> None:
    """Create the sample files, never overwriting files already there."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        "calc.py": CALC_PY,
        "test_calc.py": TEST_CALC_PY,
        ".gitignore": SAMPLE_GITIGNORE,
        SAMPLE_MARKER: (
            f"{SAMPLE_LABEL} selfkraken sample repository —"
            " safe to delete with 'skraken sample remove'\n"
        ),
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


def _find_sample_repo(store: Store, expected_path: str) -> Repo | None:
    for repo in store.list_repos():
        if repo.is_sample and (repo.path == expected_path or repo.name == SAMPLE_REPO_NAME):
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


def remove_sample(store: Store, settings: Settings) -> int:
    """Delete sample DB rows and the sample repository directory.

    Only ``<data_dir>/sample-repo`` is ever removed, and only when it carries the
    sample marker file — an unmarked directory with the same name is left alone.
    """
    repo_dir = sample_repo_dir(settings)
    _check_sample_dir(settings, repo_dir)
    if repo_dir.is_dir():
        if not (repo_dir / SAMPLE_MARKER).is_file():
            raise ValidationError(
                f"{SAMPLE_LABEL} refusing to delete {repo_dir.name}: "
                f"no {SAMPLE_MARKER} marker, so it may not be ours"
            )
        shutil.rmtree(repo_dir)
    elif repo_dir.exists():
        raise ValidationError(
            f"{SAMPLE_LABEL} refusing to delete {repo_dir.name}: it is not a directory"
        )
    removed = store.delete_sample_data()
    store.record_change("sample_remove", f"{SAMPLE_LABEL} removed sample data ({removed} rows)")
    return removed
