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
from selfkraken.graph import list_branches, layout, read_commits
from selfkraken.sample import (
    CALC_PY,
    CALC_PY_FIXED,
    MULTIBRANCH_DIFF,
    MULTIBRANCH_ORIGIN_DIRNAME,
    MULTIBRANCH_REPO_DIRNAME,
    MULTIBRANCH_REPO_NAME,
    SAMPLE_DIFF,
    SAMPLE_LABEL,
    SAMPLE_MARKER,
    SAMPLE_MODEL,
    SAMPLE_PROPOSAL_REQUEST,
    SAMPLE_REPO_DIRNAME,
    SAMPLE_REPO_NAME,
    SAMPLE_TEST_COMMAND,
    TEST_CALC_PY,
    load_multibranch_sample,
    load_sample,
    multibranch_origin_dir,
    multibranch_repo_dir,
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


# ----------------------------------------------------------- [SAMPLE] multibranch
#
# The demo the app ships: ``skraken sample load --kind multibranch`` builds a
# real multi-branch python project plus its bare remote, both inside the data
# directory, registers/indexes it and adds one [SAMPLE] proposal.

_MB_EXPECTED_BRANCHES = {
    "main",
    "feature/login",
    "feature/search",
    "feature/export",
    "hotfix/div-zero",
    "release/0.2",
    "experiment/cache",
}


def _ref_names(repo_dir: Path, section: str) -> set[str]:
    out = git(repo_dir, "for-each-ref", "--format=%(refname:short)", f"refs/{section}")
    return {line for line in out.stdout.splitlines() if line}


def _merge_subjects(repo_dir: Path) -> set[str]:
    out = git(repo_dir, "log", "--all", "--merges", "--pretty=%s")
    return {line for line in out.stdout.splitlines() if line}


def _porcelain(repo_dir: Path) -> str:
    return git(repo_dir, "status", "--porcelain", "--untracked-files=all").stdout.strip()


def test_multibranch_paths_are_both_inside_the_data_dir(settings: Settings) -> None:
    assert multibranch_repo_dir(settings) == settings.data_dir / MULTIBRANCH_REPO_DIRNAME
    assert multibranch_origin_dir(settings) == settings.data_dir / MULTIBRANCH_ORIGIN_DIRNAME


def test_load_multibranch_sample_builds_the_demo_and_records_it(
    store: Store, settings: Settings
) -> None:
    repo = load_multibranch_sample(store, settings)
    repo_dir = multibranch_repo_dir(settings)
    origin_dir = multibranch_origin_dir(settings)

    assert isinstance(repo, Repo)
    assert repo.is_sample is True
    assert repo.name == MULTIBRANCH_REPO_NAME
    assert repo.test_command == SAMPLE_TEST_COMMAND
    assert repo.path == str(repo_dir.resolve())

    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / SAMPLE_MARKER).is_file()
    assert origin_dir.is_dir() and (origin_dir / SAMPLE_MARKER).is_file()
    assert (repo_dir / "src" / "app" / "core.py").is_file()
    assert _porcelain(repo_dir) == "", "the demo must end on main with a clean tree"
    assert git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"

    branches = _ref_names(repo_dir, "heads")
    assert _MB_EXPECTED_BRANCHES <= branches
    assert len(branches) >= 6
    tags = _ref_names(repo_dir, "tags")
    assert {"v0.1.0", "v0.2.0"} <= tags
    assert len(tags) >= 2

    merges = _merge_subjects(repo_dir)
    assert "Merge feature/login into main" in merges
    assert "Merge feature/search into main" in merges
    assert "Merge hotfix/div-zero into main" in merges
    assert "Merge main into feature/search" in merges, "a feature must fold main in"

    commits = git(repo_dir, "rev-list", "--all", "--count").stdout.strip()
    assert 25 <= int(commits) <= 35, f"unexpected demo history size: {commits}"

    # the experiment is real but never merged into main
    assert git(repo_dir, "merge-base", "--is-ancestor", "experiment/cache", "main").returncode != 0

    # identity: every demo commit is the sample author
    authors = {
        line
        for line in git(repo_dir, "log", "--all", "--format=%an <%ae>").stdout.splitlines()
        if line
    }
    assert authors == {"selfkraken-sample <sample@localhost>"}

    proposals = store.list_proposals(repo_id=repo.id)
    assert len(proposals) == 1
    assert proposals[0].is_sample is True
    assert proposals[0].model == SAMPLE_MODEL
    assert proposals[0].status == "proposed"
    assert proposals[0].diff == MULTIBRANCH_DIFF
    assert SAMPLE_LABEL in proposals[0].request
    assert proposals[0].diff.startswith("diff --git a/src/app/core.py b/src/app/core.py\n")

    assert [item.kind for item in store.list_changes(repo_id=repo.id)] == ["sample_load"]
    assert store.count_files(repo.id) > 0, "loading the demo must index it"


def test_multibranch_demo_tests_pass_on_main(store: Store, settings: Settings) -> None:
    load_multibranch_sample(store, settings)
    result = run_pytest(multibranch_repo_dir(settings))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 error" not in result.stdout


def test_multibranch_sample_is_reachable_from_the_real_bare_remote(
    store: Store, settings: Settings
) -> None:
    load_multibranch_sample(store, settings)
    repo_dir = multibranch_repo_dir(settings)
    origin_dir = multibranch_origin_dir(settings)

    url = git(repo_dir, "remote", "get-url", "origin").stdout.strip()
    assert Path(url).name == MULTIBRANCH_ORIGIN_DIRNAME
    assert Path(url).parent == settings.data_dir

    listing = subprocess.run(
        ["git", "--git-dir", str(origin_dir), "for-each-ref", "--format=%(refname:short)"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert listing.returncode == 0, listing.stderr
    pushed = {line for line in listing.stdout.splitlines() if line}
    assert {"main", "experiment/cache", "v0.1.0", "v0.2.0"} <= pushed
    assert _ref_names(repo_dir, "remotes") >= {
        "origin/main",
        "origin/feature/search",
        "origin/experiment/cache",
    }


def test_multibranch_sample_shows_ahead_and_behind(store: Store, settings: Settings) -> None:
    load_multibranch_sample(store, settings)
    branches = {branch.name: branch for branch in list_branches(multibranch_repo_dir(settings))}
    main = branches["main"]
    assert main.ahead and main.ahead > 0, "main must be ahead of the pushed state"
    assert main.behind and main.behind > 0, "the remote must hold a commit main does not have"
    assert branches["origin/main"].is_remote is True
    # branches pushed without tracking stay silent about ahead/behind
    login = branches["feature/login"]
    assert login.ahead is None and login.behind is None


def test_multibranch_sample_graph_data_lays_out(store: Store, settings: Settings) -> None:
    load_multibranch_sample(store, settings)
    commits = read_commits(multibranch_repo_dir(settings), limit=500)
    rows = layout(commits)
    assert len(commits) >= 25
    assert [row.commit.sha for row in rows] == [commit.sha for commit in commits]
    merges = [row for row in rows if len(row.commit.parents) >= 2]
    assert len(merges) >= 5
    for row in merges:
        for parent in row.commit.parents:
            assert parent in row.lanes_out, f"{row.commit.subject}: parent lane missing"
    for before, after in zip(rows, rows[1:]):
        assert before.lanes_out == after.lanes_in
    assert any(any(ref.startswith("tag:") for ref in commit.refs) for commit in commits)


def test_multibranch_proposal_is_a_real_patch_that_keeps_the_demo_green(
    store: Store, settings: Settings
) -> None:
    load_multibranch_sample(store, settings)
    repo_dir = multibranch_repo_dir(settings)

    check = git(repo_dir, "apply", "--check", "-", input_text=MULTIBRANCH_DIFF)
    assert check.returncode == 0, check.stderr
    assert "diff --git a/tests/test_core.py b/tests/test_core.py\n" in MULTIBRANCH_DIFF

    applied = git(repo_dir, "apply", "-", input_text=MULTIBRANCH_DIFF)
    assert applied.returncode == 0, applied.stderr
    try:
        result = run_pytest(repo_dir)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "def slug" in (repo_dir / "src" / "app" / "core.py").read_text(encoding="utf-8")
    finally:
        git(repo_dir, "apply", "-R", "-", input_text=MULTIBRANCH_DIFF)
    assert _porcelain(repo_dir) == ""


def test_load_multibranch_sample_is_idempotent(store: Store, settings: Settings) -> None:
    first = load_multibranch_sample(store, settings)
    second = load_multibranch_sample(store, settings)
    assert second.id == first.id
    assert len(store.list_repos()) == 1
    assert len(store.list_proposals()) == 1
    assert len(store.list_changes()) == 1
    assert (multibranch_repo_dir(settings) / ".git").is_dir()


def test_load_multibranch_sample_recreates_a_deleted_directory(
    store: Store, settings: Settings
) -> None:
    first = load_multibranch_sample(store, settings)
    repo_dir = multibranch_repo_dir(settings)
    shutil.rmtree(repo_dir)
    second = load_multibranch_sample(store, settings)
    assert second.id == first.id
    assert len(store.list_repos()) == 1
    assert (repo_dir / ".git").is_dir()
    assert _porcelain(repo_dir) == ""


def test_load_multibranch_sample_does_not_clobber_user_edits(
    store: Store, settings: Settings
) -> None:
    repo = load_multibranch_sample(store, settings)
    repo_dir = multibranch_repo_dir(settings)
    (repo_dir / "README.md").write_text("# mine now\n", encoding="utf-8")

    again = load_multibranch_sample(store, settings)
    assert again.id == repo.id
    assert (repo_dir / "README.md").read_text(encoding="utf-8") == "# mine now\n"


def test_load_multibranch_sample_is_deterministic(tmp_path: Path) -> None:
    """Two builds in two data dirs must produce the very same history."""
    heads: list[dict[str, str]] = []
    for name in ("first", "second"):
        settings = load_settings({ENV_DATA_DIR: str(tmp_path / name)}, load_dotenv_file=False)
        with Store(settings.db_path) as store:
            load_multibranch_sample(store, settings)
        repo_dir = multibranch_repo_dir(settings)
        heads.append(
            {
                parts[0]: parts[1]
                for line in git(
                    repo_dir,
                    "for-each-ref",
                    "--format=%(refname:short) %(objectname)",
                    "refs/heads",
                ).stdout.splitlines()
                if (parts := line.split())
            }
        )
    assert heads[0] == heads[1]
    assert heads[0]["main"]


def test_load_multibranch_sample_creates_nothing_outside_the_data_dir(
    tmp_path: Path,
) -> None:
    """The old fixture put ``origin.git`` next to the repo — never again."""
    data_dir = tmp_path / "data"
    settings = load_settings({ENV_DATA_DIR: str(data_dir)}, load_dotenv_file=False)
    data_dir.mkdir(parents=True)
    before = sorted(path.name for path in tmp_path.iterdir())
    with Store(settings.db_path) as store:
        load_multibranch_sample(store, settings)
    assert sorted(path.name for path in tmp_path.iterdir()) == before == ["data"]
    assert not (tmp_path / "origin.git").exists()
    assert not (data_dir / "origin.git").exists()
    created = sorted(
        path.name for path in data_dir.iterdir() if not path.name.startswith("selfkraken.db")
    )
    assert created == sorted([MULTIBRANCH_REPO_DIRNAME, MULTIBRANCH_ORIGIN_DIRNAME])


def test_load_both_samples_side_by_side(store: Store, settings: Settings) -> None:
    calculator = load_sample(store, settings)
    demo = load_multibranch_sample(store, settings)
    assert calculator.id != demo.id
    assert {calculator.name, demo.name} == {SAMPLE_REPO_NAME, MULTIBRANCH_REPO_NAME}
    assert len(store.list_repos()) == 2
    assert len(store.list_proposals()) == 2


# ------------------------------------------------------------------ multibranch removal


def test_remove_sample_deletes_both_demos_and_all_rows(store: Store, settings: Settings) -> None:
    load_sample(store, settings)
    load_multibranch_sample(store, settings)
    dirs = (
        sample_repo_dir(settings),
        multibranch_repo_dir(settings),
        multibranch_origin_dir(settings),
    )
    assert all(path.is_dir() for path in dirs)

    removed = remove_sample(store, settings)
    assert removed > 0
    for path in dirs:
        assert not path.exists()
    assert store.list_repos() == []
    assert store.list_proposals() == []
    kinds = [item.kind for item in store.list_changes()]
    assert "sample_load" not in kinds
    assert kinds == ["sample_remove"]


def test_remove_sample_refuses_an_unmarked_multibranch_directory(
    store: Store, settings: Settings
) -> None:
    load_multibranch_sample(store, settings)
    fake = multibranch_repo_dir(settings)
    (fake / SAMPLE_MARKER).unlink()

    with pytest.raises(ValidationError) as excinfo:
        remove_sample(store, settings)
    assert SAMPLE_LABEL in str(excinfo.value)
    assert fake.is_dir()
    assert multibranch_origin_dir(settings).is_dir()


def test_multibranch_directories_must_stay_inside_the_data_dir(settings: Settings) -> None:
    from selfkraken import sample

    inside_repo = settings.data_dir / MULTIBRANCH_REPO_DIRNAME
    inside_origin = settings.data_dir / MULTIBRANCH_ORIGIN_DIRNAME
    sample._check_sample_dir(settings, inside_repo, MULTIBRANCH_REPO_DIRNAME)
    sample._check_sample_dir(settings, inside_origin, MULTIBRANCH_ORIGIN_DIRNAME)
    for wrong in (
        settings.data_dir / "origin.git",
        settings.data_dir / SAMPLE_REPO_DIRNAME,
        settings.data_dir.parent / MULTIBRANCH_REPO_DIRNAME,
        settings.data_dir / MULTIBRANCH_ORIGIN_DIRNAME,
    ):
        with pytest.raises(ValidationError):
            sample._check_sample_dir(settings, wrong, MULTIBRANCH_REPO_DIRNAME)


# ------------------------------------------------------- fixture parity (task 1)


def test_make_multibranch_repo_keeps_its_fake_remote_outside_the_caller_dir(
    tmp_path: Path,
) -> None:
    """``fixtures_git.make_multibranch_repo`` may not litter ``<dir>/origin.git``."""
    from fixtures_git import make_multibranch_repo

    repo = make_multibranch_repo(tmp_path / "demo")
    assert (tmp_path / "demo-origin.git").is_dir()
    assert not (tmp_path / "origin.git").exists()
    assert git(repo, "remote", "get-url", "origin").stdout.strip() == str(
        tmp_path / "demo-origin.git"
    )

    elsewhere = tmp_path / "nested" / "upstream.git"
    second = make_multibranch_repo(tmp_path / "second", origin_path=elsewhere)
    assert elsewhere.is_dir()
    assert not (tmp_path / "second-origin.git").exists()
    assert git(second, "remote", "get-url", "origin").stdout.strip() == str(elsewhere)

