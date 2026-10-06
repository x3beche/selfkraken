"""Shared fixtures for the app layer tests (workflow, CLI, end-to-end smoke).

Everything here is repository-local and throwaway: settings point at a temp
``SK_DATA_DIR``, and ``git_repo`` builds a real (tiny) git repository so the
diff/apply/test/rollback machinery is exercised for real.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator, Mapping

import pytest
from typer.testing import CliRunner

from selfkraken.config import Settings, load_settings
from selfkraken.db import Store

#: Repository contents: a one-function module plus a pytest-style test.
CALC_SRC = "def add(a, b):\n    return a + b\n"
TEST_CALC_SRC = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

#: Variants used to build patches.
CALC_WITH_SUB = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST_CALC_WITH_SUB = (
    "from calc import add\nfrom calc import sub\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_sub():\n    assert sub(5, 3) == 2\n"
)
#: Compiles, but breaks ``test_add``.
CALC_WRONG = "def add(a, b):\n    return a - b\n"
#: Does not even parse — breaks collection.
CALC_BROKEN = "def add(a, b):\n    return a +\n"

#: The suite's own interpreter runs the repo's tests, so the command works no
#: matter which venv pytest is running in.
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"


def git(repo_path: Path, *args: str) -> str:
    """Run git in *repo_path* and return stdout (raises with stderr on failure)."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture
def sources() -> dict[str, str]:
    """File bodies the tests turn into patches."""
    return {
        "calc": CALC_SRC,
        "test_calc": TEST_CALC_SRC,
        "calc_with_sub": CALC_WITH_SUB,
        "test_calc_with_sub": TEST_CALC_WITH_SUB,
        "calc_wrong": CALC_WRONG,
        "calc_broken": CALC_BROKEN,
    }


@pytest.fixture
def test_command() -> str:
    """A test command that really works in whatever interpreter runs the suite."""
    return TEST_COMMAND


@pytest.fixture(autouse=True)
def _isolated_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Never touch the user's real data dir: every test gets its own throwaway.

    A probe run once polluted ``~/.local/share/selfkraken`` because a path
    without ``SK_DATA_DIR`` fell back to the default.  Pinning both
    ``SK_DATA_DIR`` and ``XDG_DATA_HOME`` to this test's ``tmp_path`` means
    even that fallback lands in the temp dir — for Settings built straight
    from ``os.environ`` and for ``skraken`` subprocesses (which inherit it)
    alike.
    """
    monkeypatch.setenv("SK_DATA_DIR", str(tmp_path / "sk-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with the data dir (and therefore the DB) inside ``tmp_path``."""
    return load_settings(env={"SK_DATA_DIR": str(tmp_path / "data")}, load_dotenv_file=False)


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    """An open Store on the temp database, always closed again."""
    opened = Store(settings.db_path)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A committed git repo with ``calc.py`` + ``test_calc.py`` and a ``main`` branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC_SRC, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_CALC_SRC, encoding="utf-8")
    # So that running the repo's own tests cannot leave untracked noise behind.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "selfkraken-test")
    git(repo, "config", "user.email", "selfkraken-test@localhost")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-gpg-sign", "-m", "Initial commit")
    git(repo, "branch", "-M", "main")
    return repo


@pytest.fixture
def make_patch() -> Callable[[Path, Mapping[str, str]], str]:
    """Build a real ``git``-format unified diff by staging *files* and rolling back."""

    def _make(repo_path: Path, files: Mapping[str, str]) -> str:
        for relative, body in files.items():
            target = repo_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        git(repo_path, "add", "-A")
        diff = git(repo_path, "diff", "--cached", "--unified=3")
        git(repo_path, "reset", "-q", "--hard", "HEAD")
        git(repo_path, "clean", "-q", "-fd")
        if not diff.strip():
            raise AssertionError("make_patch produced an empty diff")
        return diff

    return _make


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated environment for CLI tests: temp data dir, no model, no stray .env."""
    data_dir = tmp_path / "cli-data"
    run_dir = tmp_path / "cli-cwd"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    monkeypatch.setenv("SK_DATA_DIR", str(data_dir))
    for variable in (
        "SK_LLM_BASE_URL",
        "SK_LLM_API_KEY",
        "SK_LLM_MODEL",
        "SK_LLM_TIMEOUT",
        "SK_MAX_CONTEXT_CHARS",
        "SK_TEST_TIMEOUT",
    ):
        monkeypatch.delenv(variable, raising=False)
    # rich must not hard-wrap the lines the tests assert on: pin the width at
    # render time (the CLI console resolves COLUMNS live, not at import).
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    return data_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
