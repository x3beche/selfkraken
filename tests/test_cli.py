"""CLI tests: exit codes, the four UX states, degraded mode, and the visualisations.

The CLI is driven through ``typer.testing.CliRunner``, so no real terminal is
involved: spinners are skipped and prompts are answered from ``input=``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from selfkraken import gitops
from selfkraken.cli import app
from typer.testing import CliRunner

SECRET = "sk-super-secret-value-123"


@pytest.fixture
def registered_repo(runner: CliRunner, cli_env: Path, git_repo: Path, test_command: str) -> Path:
    """A repo registered through ``skraken init``."""
    result = runner.invoke(app, ["init", str(git_repo), "--test-cmd", test_command])
    assert result.exit_code == 0, result.output
    return git_repo


@pytest.fixture
def manual_proposal(
    runner: CliRunner,
    cli_env: Path,
    registered_repo: Path,
    make_patch,
    sources,
    tmp_path: Path,
) -> Path:
    """A stored proposal (id 1) created from a patch file, no model involved."""
    diff = make_patch(
        registered_repo,
        {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]},
    )
    patch = tmp_path / "add-sub.patch"
    patch.write_text(diff, encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "propose",
            "Add a sub function",
            "--diff-file",
            str(patch),
            "--repo",
            str(registered_repo),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    return registered_repo


# --------------------------------------------------------------------------- #
# help / registration
# --------------------------------------------------------------------------- #
def test_help_lists_every_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for name in (
        "init",
        "repos",
        "index",
        "propose",
        "proposals",
        "show",
        "apply",
        "reject",
        "revert",
        "test",
        "log",
        "export",
        "import",
        "sample",
        "doctor",
        "graph",
        "branches",
        "tree",
    ):
        assert name in result.output


def test_init_then_repos_then_duplicate(
    runner: CliRunner, cli_env: Path, git_repo: Path, test_command: str
) -> None:
    result = runner.invoke(app, ["init", str(git_repo), "--test-cmd", test_command])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "registered" in result.output.lower()

    listing = runner.invoke(app, ["repos"])
    assert listing.exit_code == 0, listing.output
    assert git_repo.name in listing.output
    assert "repository record" in listing.output.lower()

    duplicate = runner.invoke(app, ["init", str(git_repo)])
    assert duplicate.exit_code == 2
    assert "Error:" in duplicate.output
    assert "registered" in duplicate.output.lower()


def test_repos_empty_state(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["repos"])

    assert result.exit_code == 0, result.output
    assert "no repositories registered" in result.output.lower()
    assert "skraken init" in result.output


def test_init_outside_a_git_repo_is_a_validation_error(runner: CliRunner, cli_env: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()

    result = runner.invoke(app, ["init", str(plain)])

    assert result.exit_code == 2
    assert "Error:" in result.output
    assert "git repository" in result.output


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def test_doctor_reports_degraded_mode_and_how_to_leave_it(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "degraded mode: no model configured" in lowered
    assert "--diff-file" in lowered
    assert "sk_llm_base_url" in lowered and "sk_llm_model" in lowered
    assert "api key" in lowered and "not set" in lowered
    assert "git" in lowered


def test_doctor_never_prints_the_api_key(
    runner: CliRunner, cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SK_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("SK_LLM_MODEL", "stub-model")
    monkeypatch.setenv("SK_LLM_API_KEY", SECRET)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    lowered = result.output.lower()
    assert "api key" in lowered
    assert "configured (stub-model @ 127.0.0.1:1)" in lowered


# --------------------------------------------------------------------------- #
# proposals: empty state, degraded mode, unknown ids
# --------------------------------------------------------------------------- #
def test_proposals_empty_state(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["proposals"])

    assert result.exit_code == 0, result.output
    assert "no proposals" in result.output.lower()
    assert "skraken propose" in result.output


def test_propose_without_model_explains_degraded_mode_and_exits_1(
    runner: CliRunner, registered_repo: Path
) -> None:
    result = runner.invoke(
        app, ["propose", "Add a sub function", "--repo", str(registered_repo)]
    )

    assert result.exit_code == 1
    lowered = result.output.lower()
    assert "degraded mode" in lowered
    assert "sk_llm_base_url" in lowered and "sk_llm_model" in lowered
    assert "--diff-file" in lowered
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    "args",
    [
        ["show", "4242"],
        ["apply", "4242", "--yes"],
        ["reject", "4242"],
        ["revert", "4242"],
    ],
    ids=["show", "apply", "reject", "revert"],
)
def test_unknown_proposal_id_is_nonzero(
    runner: CliRunner, cli_env: Path, args: list[str]
) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert "Error:" in result.output


def test_bad_status_filter_is_a_validation_error(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["proposals", "--status", "wontfix"])

    assert result.exit_code == 2
    assert "Error:" in result.output
    assert "--status" in result.output


def test_bad_log_limit_is_a_validation_error(runner: CliRunner, cli_env: Path) -> None:
    assert runner.invoke(app, ["log", "--limit", "0"]).exit_code == 2
    assert runner.invoke(app, ["log", "--limit", "5000"]).exit_code == 2


# --------------------------------------------------------------------------- #
# the manual loop through the CLI
# --------------------------------------------------------------------------- #
def test_manual_propose_shows_diff_then_apply_runs_tests(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, sources
) -> None:
    listing = runner.invoke(app, ["proposals"])
    assert listing.exit_code == 0, listing.output
    assert "proposed" in listing.output
    assert "Add a sub function" in listing.output

    shown = runner.invoke(app, ["show", "1"])
    assert shown.exit_code == 0, shown.output
    assert "diff --git" in shown.output
    assert "manual" in shown.output

    applied = runner.invoke(app, ["apply", "1", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert "OK" in applied.output
    assert "tests_passed" in applied.output
    assert "def sub" in (manual_proposal / "calc.py").read_text(encoding="utf-8")

    log = runner.invoke(app, ["log", "--repo", str(manual_proposal)])
    assert log.exit_code == 0, log.output
    for kind in ("register", "propose", "apply", "test"):
        assert kind in log.output


def test_apply_without_yes_does_not_touch_the_files(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, sources
) -> None:
    result = runner.invoke(app, ["apply", "1"], input="\n")

    assert result.exit_code != 0
    assert "cancelled" in result.output.lower() or "aborted" in result.output.lower()
    assert (manual_proposal / "calc.py").read_text(encoding="utf-8") == sources["calc"]


def test_apply_declined_at_the_prompt_does_not_touch_the_files(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, sources
) -> None:
    result = runner.invoke(app, ["apply", "1"], input="n\n")

    assert result.exit_code != 0
    assert (manual_proposal / "calc.py").read_text(encoding="utf-8") == sources["calc"]


def test_apply_accepting_the_prompt_works(
    runner: CliRunner, cli_env: Path, manual_proposal: Path
) -> None:
    result = runner.invoke(app, ["apply", "1"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "tests_passed" in result.output


def test_propose_from_patch_then_reject(
    runner: CliRunner, cli_env: Path, manual_proposal: Path
) -> None:
    rejected = runner.invoke(app, ["reject", "1"])

    assert rejected.exit_code == 0, rejected.output
    assert "rejected" in rejected.output
    assert runner.invoke(app, ["apply", "1", "--yes"]).exit_code == 2


def test_apply_commit_creates_a_commit(
    runner: CliRunner, cli_env: Path, manual_proposal: Path
) -> None:
    before = gitops.head_sha(manual_proposal)

    applied = runner.invoke(app, ["apply", "1", "--yes", "--commit"])

    assert applied.exit_code == 0, applied.output
    assert "commit" in applied.output.lower()
    assert gitops.head_sha(manual_proposal) != before
    assert gitops.changed_paths(manual_proposal) == []


def test_apply_with_failing_tests_reports_failed_and_restores(
    runner: CliRunner,
    cli_env: Path,
    registered_repo: Path,
    make_patch,
    sources,
    tmp_path: Path,
) -> None:
    diff = make_patch(registered_repo, {"calc.py": sources["calc_wrong"]})
    patch = tmp_path / "wrong.patch"
    patch.write_text(diff, encoding="utf-8")
    proposed = runner.invoke(
        app,
        ["propose", "Break add", "--diff-file", str(patch), "--repo", str(registered_repo), "--yes"],
    )
    assert proposed.exit_code == 0, proposed.output

    applied = runner.invoke(app, ["apply", "1", "--yes"])

    assert applied.exit_code == 1
    assert "FAILED" in applied.output
    assert "rolled back" in applied.output.lower()
    assert (registered_repo / "calc.py").read_text(encoding="utf-8") == sources["calc"]


def test_revert_command_restores_the_file(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, sources
) -> None:
    assert runner.invoke(app, ["apply", "1", "--yes", "--no-test"]).exit_code == 0
    assert "def sub" in (manual_proposal / "calc.py").read_text(encoding="utf-8")

    reverted = runner.invoke(app, ["revert", "1"])

    assert reverted.exit_code == 0, reverted.output
    assert "reverted" in reverted.output
    assert (manual_proposal / "calc.py").read_text(encoding="utf-8") == sources["calc"]


def test_index_search_and_set_test(
    runner: CliRunner, cli_env: Path, registered_repo: Path, test_command: str
) -> None:
    indexed = runner.invoke(app, ["index", "--repo", str(registered_repo)])
    assert indexed.exit_code == 0, indexed.output
    assert "OK" in indexed.output
    assert "indexed" in indexed.output.lower()

    searched = runner.invoke(app, ["search", "add", "--repo", str(registered_repo)])
    assert searched.exit_code == 0, searched.output

    set_test = runner.invoke(app, ["set-test", test_command, "--repo", str(registered_repo)])
    assert set_test.exit_code == 0, set_test.output
    assert "pytest" in set_test.output

    tested = runner.invoke(app, ["test", "--repo", str(registered_repo)])
    assert tested.exit_code == 0, tested.output
    assert "tests passed" in tested.output.lower()

    log = runner.invoke(app, ["log", "--repo", str(registered_repo)])
    assert "index" in log.output


def test_search_reports_no_matches(runner: CliRunner, registered_repo: Path) -> None:
    assert runner.invoke(app, ["index", "--repo", str(registered_repo)]).exit_code == 0

    result = runner.invoke(app, ["search", "zzznotathing", "--repo", str(registered_repo)])

    assert result.exit_code == 0, result.output
    assert "no chunks match" in result.output.lower()


def test_export_and_import_archive(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "archive.json"

    exported = runner.invoke(app, ["export", str(archive)])
    assert exported.exit_code == 0, exported.output
    assert "OK" in exported.output
    assert archive.exists() and archive.stat().st_size > 0

    monkeypatch.setenv("SK_DATA_DIR", str(tmp_path / "second-data"))
    imported = runner.invoke(app, ["import", str(archive)])
    assert imported.exit_code == 0, imported.output
    assert "imported" in imported.output.lower()

    listing = runner.invoke(app, ["repos"])
    assert listing.exit_code == 0, listing.output
    assert manual_proposal.name in listing.output


# --------------------------------------------------------------------------- #
# visualisation commands
# --------------------------------------------------------------------------- #
@pytest.fixture
def merged_repo(runner: CliRunner, registered_repo: Path) -> Path:
    """The sample repo plus a feature branch folded in with ``git merge --no-ff``."""
    gitops.run_git(registered_repo, "checkout", "-q", "-b", "experiment")
    (registered_repo / "notes.md").write_text("# notes\n", encoding="utf-8")
    gitops.run_git(registered_repo, "add", "-A")
    gitops.run_git(registered_repo, "commit", "-q", "--no-gpg-sign", "-m", "Document the plan")
    gitops.run_git(registered_repo, "checkout", "-q", "main")
    gitops.run_git(
        registered_repo,
        "merge",
        "-q",
        "--no-ff",
        "--no-gpg-sign",
        "-m",
        "Merge branch 'experiment'",
        "experiment",
    )
    return registered_repo


def test_graph_shows_the_merged_branch(runner: CliRunner, cli_env: Path, merged_repo: Path) -> None:
    result = runner.invoke(app, ["graph", "--repo", str(merged_repo), "--ascii"])

    assert result.exit_code == 0, result.output
    assert "experiment" in result.output
    assert "merge" in result.output.lower()


def test_graph_current_only_and_limit_are_accepted(
    runner: CliRunner, cli_env: Path, merged_repo: Path
) -> None:
    result = runner.invoke(
        app, ["graph", "--repo", str(merged_repo), "--current-only", "--limit", "5"]
    )

    assert result.exit_code == 0, result.output


def test_graph_validates_its_limit(runner: CliRunner, cli_env: Path, registered_repo: Path) -> None:
    for bad in ("0", "-1", "5000"):
        result = runner.invoke(app, ["graph", "--repo", str(registered_repo), "--limit", bad])
        assert result.exit_code == 2
        assert "Error:" in result.output


def test_branches_lists_local_and_feature_branches(
    runner: CliRunner, cli_env: Path, merged_repo: Path
) -> None:
    result = runner.invoke(app, ["branches", "--repo", str(merged_repo)])

    assert result.exit_code == 0, result.output
    assert "main" in result.output
    assert "experiment" in result.output


def test_tree_lists_files_and_marks_proposal_changes(
    runner: CliRunner, cli_env: Path, manual_proposal: Path
) -> None:
    plain = runner.invoke(app, ["tree", "--repo", str(manual_proposal)])
    assert plain.exit_code == 0, plain.output
    assert "calc.py" in plain.output
    assert "changed" not in plain.output.lower()

    highlighted = runner.invoke(
        app, ["tree", "--repo", str(manual_proposal), "--proposal", "1"]
    )
    assert highlighted.exit_code == 0, highlighted.output
    assert "calc.py" in highlighted.output
    assert "changed" in highlighted.output.lower()


def test_tree_accepts_a_depth_limit(runner: CliRunner, cli_env: Path, manual_proposal: Path) -> None:
    result = runner.invoke(app, ["tree", "--repo", str(manual_proposal), "--depth", "1"])

    assert result.exit_code == 0, result.output


def test_tree_validates_depth(runner: CliRunner, cli_env: Path, registered_repo: Path) -> None:
    result = runner.invoke(app, ["tree", "--repo", str(registered_repo), "--depth", "0"])

    assert result.exit_code == 2
    assert "Error:" in result.output


def test_tree_rejects_a_proposal_from_another_repo(
    runner: CliRunner, cli_env: Path, manual_proposal: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    gitops.run_git(other, "init", "-q")
    gitops.run_git(other, "config", "user.name", "selfkraken-test")
    gitops.run_git(other, "config", "user.email", "selfkraken-test@localhost")
    assert runner.invoke(app, ["init", str(other)]).exit_code == 0

    result = runner.invoke(app, ["tree", "--repo", str(other), "--proposal", "1"])

    assert result.exit_code == 2
    assert "Error:" in result.output


def test_sample_subtyper_load_and_remove(runner: CliRunner, cli_env: Path) -> None:
    loaded = runner.invoke(app, ["sample", "load"])
    assert loaded.exit_code == 0, loaded.output
    assert "OK" in loaded.output
    assert "[SAMPLE]" in loaded.output or "sample" in loaded.output.lower()

    listed = runner.invoke(app, ["repos"])
    assert listed.exit_code == 0, listed.output
    assert "sample" in listed.output.lower()

    removed = runner.invoke(app, ["sample", "remove"])
    assert removed.exit_code == 0, removed.output
    assert "OK" in removed.output


def test_visualisation_commands_need_a_registered_repo(
    runner: CliRunner, cli_env: Path, tmp_path: Path
) -> None:
    nowhere = tmp_path / "unregistered"
    nowhere.mkdir()

    for command in ("graph", "branches", "tree"):
        result = runner.invoke(app, [command, "--repo", str(nowhere)])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "skraken init" in result.output
