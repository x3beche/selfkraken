"""The core loop on repositories that have several branches and merges.

Everything here runs the real thing: a real git repository built by
``tests/fixtures_git``, a real ``<sys.executable> -m pytest -q`` test command,
and the real CLI through ``typer.testing.CliRunner`` with an isolated
``SK_DATA_DIR`` and no model configured (manual ``--diff-file`` proposals).

What is pinned down:

* a proposal applied with ``--commit`` lands on the checked-out branch, never on
  ``main``, and a ``--no-ff`` merge of it shows up in ``graph --ascii`` and in
  ``branches``;
* a proposal made on one branch cannot be applied on another branch whose file
  differs: the CLI exits 2 with ``Error:``, the proposal stays ``proposed`` and
  the working tree is untouched;
* an uncommitted user edit in a touched file is never committed: ``--commit``
  refuses with exit 2 and the proposal stays ``proposed``, while an apply without
  ``--commit`` goes through and the edit is never lost;
* failing tests on a feature branch roll the change back byte-for-byte, create
  no commit, and leave ``propose -> apply -> test -> rollback`` in the audit log;
* reverting a proposal after its commit has been merged either works cleanly or
  refuses cleanly, and in both cases ``main``'s history is not rewritten;
* ``graph --ascii`` / ``tree`` work on octopus and criss-cross history, and
  ``graph --current-only`` on a branch that was never merged shows only its own
  history;
* ``export`` -> ``import`` into a fresh data dir shows the same proposals and
  the same audit log.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import pytest
from typer.testing import CliRunner, Result

from fixtures_git import make_criss_cross_repo, make_multibranch_repo, make_octopus_repo
from selfkraken.cli import app
from selfkraken.db import Store

#: File every patch in this module touches (it exists on every branch of the
#: multi-branch fixture, with different contents — which is the point).
CORE = "src/app/core.py"

#: Exactly what ``fixtures_git.make_multibranch_repo()`` has in ``src/app/core.py``
#: on ``main`` (asserted by the ``multibranch`` fixture, so a fixture change
#: breaks loudly here instead of silently making a patch apply on both branches).
CORE_ON_MAIN = '''"""Core utilities for the demo app."""


def greet(name: str) -> str:
    return f"hello {name}"


def compute(a: int, b: int) -> int:
    return a * 3 + b


def divide(a: int, b: int) -> float:
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''

#: A change that keeps the repository's own tests green.
CORE_WITH_SHOUT = CORE_ON_MAIN + "\n\ndef shout(name: str) -> str:\n    return f\"HELLO {name.upper()}\"\n"
#: A change that breaks ``tests/test_core.py::test_greet``.
CORE_WITH_HOLA = CORE_ON_MAIN.replace('    return f"hello {name}"', '    return f"hola {name}"')
#: An uncommitted user edit far away from the patch hunk (same file, no overlap).
USER_EDIT_FAR = CORE_ON_MAIN.replace(
    '"""Core utilities for the demo app."""', '"""Core utilities for the demo app (user WIP)."""'
)
#: An uncommitted user edit *inside* the context the patch hunk needs.
USER_EDIT_OVERLAP = CORE_ON_MAIN.replace(
    '        raise ValueError("division by zero")',
    '        raise ZeroDivisionError("user WIP")',
)


# --------------------------------------------------------------------------- helpers
def git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout; raise with stderr when it fails."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_rc(repo: Path, *args: str) -> int:
    """Run git in *repo* and return only its exit code (for reachability tests)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    ).returncode


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def head_sha(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).strip()


def short_sha(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", "--short", "--verify", ref).strip()


def commit_count(repo: Path, ref: str = "HEAD") -> int:
    return int(git(repo, "rev-list", "--count", ref).strip())


def porcelain(repo: Path) -> str:
    """Tracked and untracked working-tree changes (the tests must see neither)."""
    return git(repo, "status", "--porcelain", "--untracked-files=all")


def core_text(repo: Path, ref: str | None = None) -> str:
    """``src/app/core.py`` from the working tree, or from *ref* when given."""
    if ref is None:
        return (repo / CORE).read_text(encoding="utf-8")
    return git(repo, "show", f"{ref}:{CORE}")


def change_kinds(store: Store, repo_id: int) -> list[str]:
    """Audit-log kinds of one repo in chronological order (oldest first)."""
    return [change.kind for change in reversed(store.list_changes(repo_id=repo_id, limit=200))]


def marked_branch_rows(output: str) -> list[str]:
    """Branch names the ``branches`` table marks as current."""
    marked: list[str] = []
    for line in output.splitlines():
        if "(current)" not in line:
            continue
        tokens = [token for token in line.replace("(current)", " ").split() if token != "*"]
        marked.append(tokens[0] if tokens else "")
    return marked


_SHA_TOKEN = re.compile(r"^[0-9a-f]{7,40}$")


def graph_rows(output: str) -> dict[str, str]:
    """Map every commit row of ``graph`` output to its short sha.

    The lane column is a variable number of glyphs (``*``, ``|``, ``\\``, ``/``,
    ``-``, ``+``), so the sha is recognised by shape rather than by position.
    """
    rows: dict[str, str] = {}
    for line in output.splitlines():
        for token in line.split():
            if _SHA_TOKEN.match(token):
                rows.setdefault(token, line)
                break
    return rows


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def stable_git_and_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic git for the CLI, the fixtures and the repo's own test runs.

    * a fixed identity, so commits work without depending on the developer's
      ``~/.gitconfig``;
    * empty global/system config, so no user alias, hook or signing option can
      change what the tests do;
    * no ``__pycache__`` / ``.pytest_cache`` inside the repository under test —
      the fixture repos have no ``.gitignore``, and "the working tree was not
      touched" only means something when it stays really empty.
    """
    empty_config = tmp_path / "gitconfig-empty"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "selfkraken-multibranch-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "multibranch@selfkraken.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "selfkraken-multibranch-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "multibranch@selfkraken.local")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")


@pytest.fixture
def multibranch(tmp_path: Path) -> Path:
    """``make_multibranch_repo`` in a temp dir, on ``main`` with a clean tree."""
    repo = make_multibranch_repo(tmp_path / "multibranch")
    assert current_branch(repo) == "main"
    assert porcelain(repo) == ""
    assert core_text(repo) == CORE_ON_MAIN, "fixtures_git core.py no longer matches this module"
    return repo


@pytest.fixture
def indexed(runner: CliRunner, cli_env: Path, multibranch: Path, test_command: str) -> Path:
    """The multi-branch repo, registered with ``--test-cmd`` and indexed."""
    init = runner.invoke(app, ["init", str(multibranch), "--test-cmd", test_command])
    assert init.exit_code == 0, init.output
    assert "OK" in init.output and "registered" in init.output.lower()

    index = runner.invoke(app, ["index", "--repo", str(multibranch)])
    assert index.exit_code == 0, index.output
    assert "OK" in index.output
    return multibranch


@pytest.fixture
def patch_file(tmp_path: Path, make_patch: Callable[[Path, dict[str, str]], str]) -> Callable[..., Path]:
    """Write a real git patch for *body* of ``src/app/core.py`` to a file."""

    def _write(body: str, name: str = "proposal.patch", repo: Path | None = None) -> Path:
        assert repo is not None, "the repository to patch against is required"
        diff = make_patch(repo, {CORE: body})
        path = tmp_path / name
        path.write_text(diff, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def cli_store(cli_env: Path) -> Iterator[Store]:
    """The database the CLI actually writes to.

    ``cli_env`` (``tmp_path/cli-data``) is the data dir the commands use; the
    ``settings``/``store`` fixtures of ``conftest.py`` point at
    ``tmp_path/data`` instead, so a CLI test must never read those.
    """
    opened = Store(cli_env / "selfkraken.db")
    try:
        yield opened
    finally:
        opened.close()


def propose(
    runner: CliRunner, repo: Path, patch: Path, request: str = "Add a shout helper"
) -> Result:
    """``skraken propose ... --diff-file ... --repo ... --yes`` (manual mode)."""
    return runner.invoke(
        app,
        ["propose", request, "--diff-file", str(patch), "--repo", str(repo), "--yes"],
    )


# --------------------------------------------------------------------------- 1
def test_apply_and_commit_land_on_the_feature_branch_and_merge_shows_up(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file
) -> None:
    """init/index -> branch -> propose --diff-file -> apply --yes --commit -> merge -> graph/branches."""
    repo = indexed
    main_before = head_sha(repo, "main")
    core_on_main_before = core_text(repo, "main")
    assert porcelain(repo) == ""

    # index actually indexed the checked-out tree
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "degraded mode" in doctor.output.lower()

    # "git checkout feature/login"-style: a new branch off main
    git(repo, "checkout", "-b", "feature/proposal")
    assert current_branch(repo) == "feature/proposal"
    assert porcelain(repo) == ""

    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    created = propose(runner, repo, patch)
    assert created.exit_code == 0, created.output
    assert "OK" in created.output and "proposal 1" in created.output
    assert "manual" in created.output, "no model is configured, so this must be manual mode"
    assert "diff --git" in created.output and "def shout" in created.output
    assert porcelain(repo) == "", "proposing must not touch the working tree"

    applied = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert applied.exit_code == 0, applied.output
    assert "OK" in applied.output
    assert "tests_passed" in applied.output
    assert "committed" in applied.output

    # the change is on disk, committed, and committed *on the feature branch*
    commit = head_sha(repo)
    assert "def shout" in core_text(repo)
    assert commit != main_before
    assert current_branch(repo) == "feature/proposal"
    assert git_rc(repo, "merge-base", "--is-ancestor", commit, "feature/proposal") == 0
    assert porcelain(repo) == "", "the commit did not leave the working tree clean"
    subject = git(repo, "log", "-1", "--pretty=%s")
    assert subject.startswith("selfkraken: apply proposal 1"), subject

    # ... and main is exactly where it was
    assert head_sha(repo, "main") == main_before
    assert core_text(repo, "main") == core_on_main_before
    assert git_rc(repo, "merge-base", "--is-ancestor", commit, "main") != 0
    assert commit_count(repo, "main..feature/proposal") == 1

    # while on the feature branch, ``branches`` marks that branch as current
    on_feature = runner.invoke(app, ["branches", "--repo", str(repo)])
    assert on_feature.exit_code == 0, on_feature.output
    assert marked_branch_rows(on_feature.output) == ["feature/proposal"]

    # merge it into main the way GitKraken's "merge" button does
    git(repo, "checkout", "main")
    merge_out = git(
        repo, "merge", "--no-ff", "-m", "Merge feature/proposal into main", "feature/proposal"
    )
    assert "Merge made by the" in merge_out, merge_out
    assert porcelain(repo) == ""

    graph = runner.invoke(app, ["graph", "--ascii", "--repo", str(repo)])
    assert graph.exit_code == 0, graph.output
    rows = graph_rows(graph.output)
    # every commit of the merged history is drawn exactly once, and nothing else
    shas = git(repo, "log", "--all", "--topo-order", "--pretty=%h").split()
    assert sorted(rows) == sorted(shas), (sorted(rows), sorted(shas))
    for sha in shas:
        assert graph.output.count(sha) == 1, f"{sha} x{graph.output.count(sha)}:\n{graph.output}"
    merge_row = rows[short_sha(repo, "main")]
    assert "[merge]" in merge_row, merge_row
    assert "Merge feature/proposal into main" in merge_row
    proposal_row = rows[short_sha(repo, commit)]
    assert "[feature/proposal]" in proposal_row, proposal_row
    assert "selfkraken: apply proposal 1" in proposal_row
    # the merge really joined two parents: main's old tip and the proposal commit
    parents = git(repo, "rev-list", "--parents", "-1", "main").split()[1:]
    assert parents == [main_before, commit], parents
    assert "[HEAD]" in merge_row and "[main]" in merge_row

    after_merge = runner.invoke(app, ["branches", "--repo", str(repo)])
    assert after_merge.exit_code == 0, after_merge.output
    marked = marked_branch_rows(after_merge.output)
    assert marked == ["main"], after_merge.output
    assert "feature/proposal" in after_merge.output

    tree = runner.invoke(app, ["tree", "--repo", str(repo), "--proposal", "1"])
    assert tree.exit_code == 0, tree.output
    assert "core.py" in tree.output

    log = runner.invoke(app, ["log", "--repo", str(repo)])
    assert log.exit_code == 0, log.output
    for kind in ("register", "index", "propose", "apply", "test", "commit"):
        assert kind in log.output, f"{kind} missing from:\n{log.output}"


# --------------------------------------------------------------------------- 2
def test_apply_after_switching_to_a_branch_with_a_different_file_fails_cleanly(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file, cli_store: Store
) -> None:
    """A proposal made on branch A cannot be applied on branch B where the file differs."""
    repo = indexed
    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    created = propose(runner, repo, patch, "Add a shout helper on main")
    assert created.exit_code == 0, created.output

    # branch B has the pre-hotfix core.py: same path, different contents
    git(repo, "checkout", "feature/login")
    assert current_branch(repo) == "feature/login"
    untouched_bytes = (repo / CORE).read_bytes()
    assert CORE_ON_MAIN not in untouched_bytes.decode(), "branch B must really differ from branch A"
    commits_before = commit_count(repo)

    refused = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert refused.exit_code == 2, refused.output
    assert "Error:" in refused.output, refused.output
    assert "traceback" not in refused.output.lower()
    validation_tail = refused.output.split("Error:")[-1].strip()
    assert CORE in validation_tail or "patch" in validation_tail.lower(), validation_tail

    # nothing was applied, nothing was committed, the tree is exactly as it was
    assert (repo / CORE).read_bytes() == untouched_bytes
    assert porcelain(repo) == ""
    assert commit_count(repo) == commits_before
    assert current_branch(repo) == "feature/login"

    kinds = change_kinds(cli_store, cli_store.list_repos()[0].id)
    assert "apply" not in kinds and "commit" not in kinds and "test" not in kinds
    assert cli_store.get_proposal(1).status == "proposed"

    listed = runner.invoke(app, ["proposals", "--repo", str(repo), "--status", "proposed"])
    assert listed.exit_code == 0, listed.output
    assert "proposed" in listed.output and "Add a shout helper on main" in listed.output

    # back on the branch it was made from, the very same proposal still applies
    git(repo, "checkout", "main")
    assert porcelain(repo) == ""
    allowed = runner.invoke(app, ["apply", "1", "--yes"])
    assert allowed.exit_code == 0, allowed.output
    assert "tests_passed" in allowed.output
    assert "def shout" in core_text(repo)


# --------------------------------------------------------------------------- 3
def test_apply_with_commit_refuses_dirty_touched_paths_and_never_loses_the_users_edit(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file, cli_store: Store
) -> None:
    """--commit refuses while a touched file is dirty; without it the apply keeps the WIP."""
    repo = indexed
    git(repo, "checkout", "-b", "feature/dirty")
    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    created = propose(runner, repo, patch, "Add shout while the user has work in progress")
    assert created.exit_code == 0, created.output

    (repo / CORE).write_text(USER_EDIT_FAR, encoding="utf-8")
    assert porcelain(repo) == f" M {CORE}\n", porcelain(repo)
    commits_before = commit_count(repo)

    refused = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert refused.exit_code == 2, refused.output
    assert "Error:" in refused.output
    assert "uncommitted" in refused.output
    assert CORE in refused.output

    # nothing was applied: the file is exactly the user's edit, no new commit,
    # the proposal is still proposed
    assert (repo / CORE).read_bytes() == USER_EDIT_FAR.encode("utf-8")
    assert porcelain(repo) == f" M {CORE}\n", porcelain(repo)
    assert commit_count(repo) == commits_before
    assert cli_store.get_proposal(1).status == "proposed"

    # without --commit a dirty tree may still be applied onto, and the user's
    # uncommitted edit survives on disk
    applied = runner.invoke(app, ["apply", "1", "--yes"])
    assert applied.exit_code == 0, applied.output
    on_disk = core_text(repo)
    assert "(user WIP)" in on_disk and "def shout" in on_disk
    assert commit_count(repo) == commits_before, "--commit was not asked for, none was made"
    assert cli_store.get_proposal(1).status == "tests_passed"


def test_apply_rejected_when_user_edit_overlaps_the_patch_context(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file, cli_store: Store
) -> None:
    """A dirty edit *inside* the hunk context makes validation refuse the apply."""
    repo = indexed
    git(repo, "checkout", "-b", "feature/dirty")
    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    assert propose(runner, repo, patch).exit_code == 0

    (repo / CORE).write_text(USER_EDIT_OVERLAP, encoding="utf-8")
    commits_before = commit_count(repo)

    refused = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert refused.exit_code == 2, refused.output
    assert "Error:" in refused.output

    # the user's conflicting edit is byte-identical to what they wrote, no commit
    assert (repo / CORE).read_text(encoding="utf-8") == USER_EDIT_OVERLAP
    assert porcelain(repo) == f" M {CORE}\n"
    assert commit_count(repo) == commits_before
    assert cli_store.get_proposal(1).status == "proposed"
    assert "rollback" not in change_kinds(cli_store, 1)


# --------------------------------------------------------------------------- 4
def test_failing_tests_on_a_feature_branch_roll_back_without_a_commit(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file, cli_store: Store
) -> None:
    """Tests fail -> exact bytes restored, no commit, apply/test/rollback logged."""
    repo = indexed
    git(repo, "checkout", "-b", "feature/broken")
    original_bytes = (repo / CORE).read_bytes()
    head_before = head_sha(repo)
    commits_before = commit_count(repo)

    patch = patch_file(CORE_WITH_HOLA, repo=repo, name="hola.patch")
    created = propose(runner, repo, patch, "Greet people in Spanish")
    assert created.exit_code == 0, created.output

    applied = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert applied.exit_code == 1, applied.output
    assert "FAILED" in applied.output
    assert "tests_failed" in applied.output
    assert "rolled back" in applied.output
    assert "test_greet" in applied.output, "the failing test should be shown in the tail"

    # exact same bytes as before, on the same commit, with nothing staged
    assert (repo / CORE).read_bytes() == original_bytes
    assert head_sha(repo) == head_before
    assert commit_count(repo) == commits_before
    assert current_branch(repo) == "feature/broken"
    assert porcelain(repo) == ""

    repo_row = cli_store.list_repos()[0]
    assert change_kinds(cli_store, repo_row.id) == [
        "register",
        "index",
        "propose",
        "apply",
        "test",
        "rollback",
    ]
    assert "commit" not in change_kinds(cli_store, repo_row.id)
    assert cli_store.get_proposal(1).status == "tests_failed"
    runs = cli_store.list_test_runs(repo_id=repo_row.id)
    assert len(runs) == 1 and runs[0].passed is False and runs[0].exit_code != 0

    # the repository is still healthy: its own tests pass again
    again = runner.invoke(app, ["test", "--repo", str(repo)])
    assert again.exit_code == 0, again.output
    assert "OK" in again.output

    listed = runner.invoke(app, ["proposals", "--repo", str(repo), "--status", "tests_failed"])
    assert listed.exit_code == 0, listed.output
    assert "tests_failed" in listed.output and "Greet people in Spanish" in listed.output


# --------------------------------------------------------------------------- 5
def test_revert_after_the_change_was_merged_into_main(
    runner: CliRunner, cli_env: Path, indexed: Path, patch_file, cli_store: Store
) -> None:
    """apply+commit on a branch, merge to main, then ``revert`` on main: clean or refused, recorded."""
    repo = indexed
    git(repo, "checkout", "-b", "feature/revert-me")
    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    assert propose(runner, repo, patch, "Add shout, then merge and revert").exit_code == 0

    applied = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert applied.exit_code == 0, applied.output
    proposal_commit = head_sha(repo)

    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "-m", "Merge feature/revert-me into main", "feature/revert-me")
    assert "def shout" in core_text(repo)
    main_history = head_sha(repo, "main")
    history_before = git(repo, "log", "--pretty=%H", "main")

    reverted = runner.invoke(app, ["revert", "1"])
    assert reverted.exit_code in (0, 2), reverted.output
    assert "traceback" not in reverted.output.lower()

    proposal = cli_store.get_proposal(1)
    kinds = change_kinds(cli_store, cli_store.list_repos()[0].id)

    if reverted.exit_code == 0:
        # selfkraken's revert is a working-tree reverse, by contract it never
        # rewrites git history — so the merge and the proposal commit stay put
        assert "OK" in reverted.output and "reverted" in reverted.output
        assert proposal.status == "reverted"
        assert "revert" in kinds
        assert "def shout" not in core_text(repo)
        assert git(repo, "log", "--pretty=%H", "main") == history_before
        assert head_sha(repo, "main") == main_history
        assert git_rc(repo, "merge-base", "--is-ancestor", proposal_commit, "main") == 0
        assert porcelain(repo) == f" M {CORE}\n", "the reverse is uncommitted work, not a new commit"
        # and the tree still builds and passes its own tests
        after = runner.invoke(app, ["test", "--repo", str(repo)])
        assert after.exit_code == 0, after.output
    else:
        assert "Error:" in reverted.output
        assert proposal.status in ("applied", "tests_passed", "tests_failed")
        assert "revert" not in kinds
        assert git(repo, "log", "--pretty=%H", "main") == history_before

    log = runner.invoke(app, ["log", "--repo", str(repo), "--limit", "40"])
    assert log.exit_code == 0, log.output
    assert "commit" in log.output


# --------------------------------------------------------------------------- 6
GRAPH_CASES = [
    (
        make_octopus_repo,
        "octopus",
        ["src/app/one.py", "src/app/three.py", "src/app/two.py", "src/app/__init__.py", "README.md"],
        "Merge topics one, two and three (octopus)",
    ),
    (
        make_criss_cross_repo,
        "criss-cross",
        ["src/app/left.py", "src/app/right.py", "src/app/__init__.py", "README.md"],
        "Merge right into main",
    ),
]


@pytest.mark.parametrize(
    "builder,dirname,files,top_subject",
    GRAPH_CASES,
    ids=[case[1] for case in GRAPH_CASES],
)
def test_graph_and_tree_on_merged_history(
    runner: CliRunner,
    cli_env: Path,
    tmp_path: Path,
    builder: Callable[[Path], Path],
    dirname: str,
    files: list[str],
    top_subject: str,
) -> None:
    """``graph --ascii`` prints each commit exactly once; ``tree`` lists the files."""
    repo = builder(tmp_path / dirname)
    assert porcelain(repo) == ""

    init = runner.invoke(app, ["init", str(repo)])
    assert init.exit_code == 0, init.output

    shas = git(repo, "log", "--all", "--topo-order", "--pretty=%h").split()
    assert shas, "the fixture built no commits?"

    graph = runner.invoke(app, ["graph", "--ascii", "--repo", str(repo)])
    assert graph.exit_code == 0, graph.output
    rows = graph_rows(graph.output)
    assert sorted(rows) == sorted(shas), (sorted(rows), sorted(shas))
    for sha in shas:
        assert graph.output.count(sha) == 1, f"{sha} appears {graph.output.count(sha)} times:\n{graph.output}"
    # the merge commits are labelled as merges, and the newest one is HEAD on main
    assert "[merge]" in graph.output
    assert top_subject in graph.output
    assert "[HEAD]" in graph.output and "[main]" in graph.output

    tree = runner.invoke(app, ["tree", "--repo", str(repo)])
    assert tree.exit_code == 0, tree.output
    for relative in files:
        assert Path(relative).name in tree.output, f"{relative} missing from:\n{tree.output}"
    tracked = git(repo, "ls-files").split()
    assert set(tracked) == set(files), tracked

    branches = runner.invoke(app, ["branches", "--repo", str(repo)])
    assert branches.exit_code == 0, branches.output
    assert marked_branch_rows(branches.output) == ["main"]


def test_graph_current_only_on_an_unmerged_branch(
    runner: CliRunner, cli_env: Path, multibranch: Path
) -> None:
    """``graph --current-only`` draws the checked-out branch, without main's tip."""
    repo = multibranch
    init = runner.invoke(app, ["init", str(repo)])
    assert init.exit_code == 0, init.output
    main_tip = short_sha(repo, "main")

    everything = runner.invoke(app, ["graph", "--ascii", "--repo", str(repo)])
    assert everything.exit_code == 0, everything.output
    assert "Bump release metadata" in everything.output
    assert main_tip in everything.output

    git(repo, "checkout", "experiment/cache")
    assert current_branch(repo) == "experiment/cache"
    assert porcelain(repo) == ""

    only_current = runner.invoke(app, ["graph", "--current-only", "--ascii", "--repo", str(repo)])
    assert only_current.exit_code == 0, only_current.output
    assert "Draft cache layer" in only_current.output
    assert "[experiment/cache]" in only_current.output
    # main moved on after this branch was cut: none of that is the branch's history
    assert "Bump release metadata" not in only_current.output
    assert main_tip not in only_current.output

    # ... while the all-refs view still shows both sides
    all_refs = runner.invoke(app, ["graph", "--ascii", "--repo", str(repo)])
    assert all_refs.exit_code == 0, all_refs.output
    assert "Bump release metadata" in all_refs.output and "Draft cache layer" in all_refs.output

    listed = runner.invoke(app, ["branches", "--repo", str(repo)])
    assert listed.exit_code == 0, listed.output
    assert marked_branch_rows(listed.output) == ["experiment/cache"]


# --------------------------------------------------------------------------- 7
def test_export_then_import_into_a_fresh_data_dir_keeps_proposals_and_log(
    runner: CliRunner,
    cli_env: Path,
    tmp_path: Path,
    indexed: Path,
    patch_file: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whole multi-branch session moves with ``export`` -> ``import``."""
    repo = indexed
    git(repo, "checkout", "-b", "feature/portable")
    patch = patch_file(CORE_WITH_SHOUT, repo=repo)
    assert propose(runner, repo, patch, "Add shout for the export test").exit_code == 0
    assert runner.invoke(app, ["apply", "1", "--yes", "--commit"]).exit_code == 0
    # a second proposal that is only ever proposed and rejected
    other = patch_file(CORE_WITH_HOLA, repo=repo, name="hola.patch")
    assert propose(runner, repo, other, "Greet in Spanish").exit_code == 0
    rejected = runner.invoke(app, ["reject", "2"])
    assert rejected.exit_code == 0, rejected.output

    before_proposals = runner.invoke(app, ["proposals", "--repo", str(repo)])
    before_log = runner.invoke(app, ["log", "--repo", str(repo), "--limit", "50"])
    assert before_proposals.exit_code == 0, before_proposals.output
    assert before_log.exit_code == 0, before_log.output
    assert "tests_passed" in before_proposals.output and "rejected" in before_proposals.output

    archive = tmp_path / "session.json"
    exported = runner.invoke(app, ["export", str(archive)])
    assert exported.exit_code == 0, exported.output
    payload = json.loads(archive.read_text(encoding="utf-8"))
    assert payload["format"] == "selfkraken-export"
    assert len(payload["repos"]) == 1
    assert [row["id"] for row in payload["proposals"]] == [1, 2]
    assert [row["status"] for row in payload["proposals"]] == ["tests_passed", "rejected"]
    assert len(payload["test_runs"]) == 1
    assert [row["kind"] for row in payload["changes"]] == [
        "register",
        "index",
        "propose",
        "apply",
        "test",
        "commit",
        "propose",
        "reject",
    ]

    # a completely fresh data dir, as if selfkraken had just been installed here
    fresh = tmp_path / "fresh-data"
    monkeypatch.setenv("SK_DATA_DIR", str(fresh))
    empty = runner.invoke(app, ["proposals"])
    assert empty.exit_code == 0, empty.output
    assert "Nothing to show" in empty.output

    imported = runner.invoke(app, ["import", str(archive)])
    assert imported.exit_code == 0, imported.output
    assert "OK" in imported.output

    after_proposals = runner.invoke(app, ["proposals", "--repo", str(repo)])
    after_log = runner.invoke(app, ["log", "--repo", str(repo), "--limit", "50"])
    assert after_proposals.exit_code == 0, after_proposals.output
    assert after_log.exit_code == 0, after_log.output
    assert after_proposals.output == before_proposals.output
    assert after_log.output == before_log.output

    # ... and the rows themselves match, not just their rendering
    with Store(cli_env / "selfkraken.db") as source, Store(fresh / "selfkraken.db") as target:
        source_rows = [(p.id, p.status, p.model, p.request, p.diff) for p in source.list_proposals()]
        target_rows = [(p.id, p.status, p.model, p.request, p.diff) for p in target.list_proposals()]
        assert target_rows == source_rows

        source_changes = [
            (c.kind, c.detail, c.repo_id, c.proposal_id)
            for c in reversed(source.list_changes(limit=200))
        ]
        target_changes = [
            (c.kind, c.detail, c.repo_id, c.proposal_id)
            for c in reversed(target.list_changes(limit=200))
        ]
        # the export row is the last thing the source recorded, the import row the
        # last thing the target recorded; neither is repo-scoped, so both ``log``
        # outputs above matched exactly
        assert [c for c in source_changes if c[0] != "export"] == [
            c for c in target_changes if c[0] != "import"
        ]
        assert source_changes[-1][0] == "export" and target_changes[-1][0] == "import"
        assert target.list_repos()[0].path == str(Path(repo).resolve())
        # the test run of the committed proposal travelled too
        assert len(target.list_test_runs()) == len(source.list_test_runs()) == 1

    # and the imported session still describes the real repository
    assert "def shout" in core_text(repo, "feature/portable")
    assert "def shout" not in core_text(repo, "main")
