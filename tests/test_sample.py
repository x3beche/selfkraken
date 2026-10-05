"""Tests for selfkraken.sample (owner A).

Everything here lives under pytest's ``tmp_path``; the real user data directory is
never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from selfkraken.config import ENV_DATA_DIR, Settings, load_settings
from selfkraken.db import Repo, Store
from selfkraken.errors import ValidationError
from selfkraken.sample import (
    CALC_PY,
    CALC_PY_FIXED,
    SAMPLE_DIFF,
    SAMPLE_LABEL,
    SAMPLE_MARKER,
    SAMPLE_MODEL,
    SAMPLE_PROPOSAL_REQUEST,
    SAMPLE_REPO_DIRNAME,
    SAMPLE_REPO_NAME,
    SAMPLE_TEST_COMMAND,
    TEST_CALC_PY,
    load_sample,
    remove_sample,
    sample_repo_dir,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        {ENV_DATA_DIR: str(tmp_path / "data")},
        load_dotenv_file=False,
    )


@pytest.fixture()
def store(settings: Settings) -> Store:
    with Store(settings.db_path) as instance:
        yield instance


def git(repo_dir: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        shell=False,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=60,
    )


def run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        shell=False,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=180,
    )


# ------------------------------------------------------------------- contents


def test_sample_diff_is_a_real_git_patch() -> None:
    assert SAMPLE_DIFF.startswith("diff --git a/calc.py b/calc.py\n")
    assert "--- a/calc.py" in SAMPLE_DIFF
    assert "+++ b/calc.py" in SAMPLE_DIFF
    assert "@@ -" in SAMPLE_DIFF
    assert SAMPLE_DIFF.endswith("\n")
    assert '+        raise ValueError("division by zero")' in SAMPLE_DIFF
    assert '+    if b == 0:' in SAMPLE_DIFF
    assert CALC_PY != CALC_PY_FIXED
    assert "def add" in CALC_PY and "def sub" in CALC_PY and "def mul" in CALC_PY


def test_sample_source_files_are_self_consistent() -> None:
    assert SAMPLE_LABEL in CALC_PY
    assert SAMPLE_LABEL in TEST_CALC_PY
    assert "assert" in TEST_CALC_PY
    assert "import calc" in TEST_CALC_PY


# ----------------------------------------------------------------- load_sample


def test_load_sample_creates_git_repo_and_records(store: Store, settings: Settings) -> None:
    repo = load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)

    assert isinstance(repo, Repo)
    assert repo.is_sample is True
    assert repo.name == SAMPLE_REPO_NAME
    assert repo.test_command == SAMPLE_TEST_COMMAND
    assert repo.path == str(repo_dir.resolve())
    assert repo_dir.name == SAMPLE_REPO_DIRNAME

    assert repo_dir.is_dir()
    assert (repo_dir / "calc.py").read_text(encoding="utf-8") == CALC_PY
    assert (repo_dir / "test_calc.py").is_file()
    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / SAMPLE_MARKER).is_file()

    status = git(repo_dir, "status", "--porcelain")
    assert status.returncode == 0 and status.stdout.strip() == ""
    log = git(repo_dir, "log", "--format=%an %ae %s")
    lines = log.stdout.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("selfkraken-sample sample@localhost ")
    assert SAMPLE_LABEL in lines[0]

    proposals = store.list_proposals(repo_id=repo.id)
    assert len(proposals) == 1
    assert proposals[0].is_sample is True
    assert proposals[0].model == SAMPLE_MODEL
    assert proposals[0].request == SAMPLE_PROPOSAL_REQUEST
    assert proposals[0].status == "proposed"
    assert proposals[0].diff == SAMPLE_DIFF
    assert SAMPLE_LABEL in proposals[0].rationale

    assert [item.kind for item in store.list_changes(repo_id=repo.id)] == ["sample_load"]


def test_sample_diff_passes_git_apply_check(store: Store, settings: Settings) -> None:
    load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)
    result = git(repo_dir, "apply", "--check", "-", input_text=SAMPLE_DIFF)
    assert result.returncode == 0, result.stderr


def test_sample_diff_makes_the_sample_tests_pass(store: Store, settings: Settings) -> None:
    load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)

    before = run_pytest(repo_dir)
    assert before.returncode != 0  # the missing zero check is a real failing test

    applied = git(repo_dir, "apply", "-", input_text=SAMPLE_DIFF)
    assert applied.returncode == 0, applied.stderr
    assert (repo_dir / "calc.py").read_text(encoding="utf-8") == CALC_PY_FIXED
    try:
        after = run_pytest(repo_dir)
        assert after.returncode == 0, after.stdout + after.stderr
    finally:
        git(repo_dir, "apply", "-R", "-", input_text=SAMPLE_DIFF)
    assert git(repo_dir, "status", "--porcelain").stdout.strip() == ""


def test_load_sample_is_idempotent(store: Store, settings: Settings) -> None:
    first = load_sample(store, settings)
    second = load_sample(store, settings)
    assert second.id == first.id
    assert len(store.list_repos()) == 1
    assert len(store.list_proposals()) == 1
    assert (sample_repo_dir(settings) / ".git").is_dir()
    assert len(store.list_changes()) == 1


def test_load_sample_recreates_a_deleted_directory(store: Store, settings: Settings) -> None:
    first = load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)
    shutil.rmtree(repo_dir)
    second = load_sample(store, settings)
    assert second.id == first.id
    assert len(store.list_repos()) == 1
    assert repo_dir.is_dir() and (repo_dir / ".git").is_dir()


def test_load_sample_does_not_clobber_user_edits(store: Store, settings: Settings) -> None:
    repo = load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)
    assert git(repo_dir, "apply", "-", input_text=SAMPLE_DIFF).returncode == 0

    again = load_sample(store, settings)
    assert again.id == repo.id
    assert len(store.list_repos()) == 1
    # the applied change survives: sample files are only created when missing
    assert (repo_dir / "calc.py").read_text(encoding="utf-8") == CALC_PY_FIXED
    log = git(repo_dir, "log", "--oneline")
    assert len(log.stdout.strip().splitlines()) == 1


def test_load_sample_does_not_register_other_repos(store: Store, settings: Settings, tmp_path: Path) -> None:
    other = store.add_repo(tmp_path / "other", "other project")
    sample = load_sample(store, settings)
    assert sample.id != other.id
    assert len(store.list_repos()) == 2
    assert store.get_repo(other.id).is_sample is False


# ---------------------------------------------------------------- remove_sample


def test_remove_sample_deletes_rows_and_directory(store: Store, settings: Settings) -> None:
    load_sample(store, settings)
    repo_dir = sample_repo_dir(settings)
    assert repo_dir.is_dir()

    removed = remove_sample(store, settings)
    assert isinstance(removed, int) and removed > 0
    assert not repo_dir.exists()
    assert store.list_repos() == []
    assert store.list_proposals() == []
    kinds = [item.kind for item in store.list_changes()]
    assert "sample_load" not in kinds
    assert "sample_remove" in kinds


def test_remove_sample_keeps_real_repos(store: Store, settings: Settings) -> None:
    real_dir = settings.data_dir / "real-project"
    real_dir.mkdir(parents=True)
    real = store.add_repo(real_dir, "real project")
    store.create_proposal(real.id, "keep me", "model", "--- a\n+++ b\n")
    load_sample(store, settings)

    remove_sample(store, settings)
    assert [item.id for item in store.list_repos()] == [real.id]
    assert real_dir.is_dir()
    assert store.list_proposals()[0].request == "keep me"


def test_remove_sample_without_sample_loaded_is_a_no_op(store: Store, settings: Settings) -> None:
    assert remove_sample(store, settings) == 0
    assert not sample_repo_dir(settings).exists()
    assert [item.kind for item in store.list_changes()] == ["sample_remove"]


def test_remove_sample_refuses_an_unmarked_directory(store: Store, settings: Settings) -> None:
    fake = sample_repo_dir(settings)
    fake.mkdir(parents=True)
    (fake / "important.txt").write_text("do not delete me\n", encoding="utf-8")

    with pytest.raises(ValidationError) as excinfo:
        remove_sample(store, settings)
    assert SAMPLE_LABEL in str(excinfo.value)
    assert fake.is_dir()
    assert (fake / "important.txt").is_file()


def test_remove_sample_refuses_a_file_in_place_of_the_directory(
    store: Store, settings: Settings
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    sample_repo_dir(settings).write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        remove_sample(store, settings)


def test_sample_directory_must_stay_inside_the_data_dir(settings: Settings) -> None:
    from selfkraken import sample

    expected = settings.data_dir / SAMPLE_REPO_DIRNAME
    sample._check_sample_dir(settings, expected)  # no raise
    for wrong in (
        settings.data_dir / "other-name",
        settings.data_dir.parent / SAMPLE_REPO_DIRNAME,
        Path(expected).resolve().parent.parent / SAMPLE_REPO_DIRNAME,
    ):
        with pytest.raises(ValidationError):
            sample._check_sample_dir(settings, wrong)


def test_sample_dir_path_is_under_data_dir(settings: Settings) -> None:
    assert sample_repo_dir(settings) == settings.data_dir / SAMPLE_REPO_DIRNAME
