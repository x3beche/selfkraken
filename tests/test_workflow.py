"""Tests for the UI-free core loop: propose -> apply -> test -> keep/roll back.

Everything here runs against the real diff/git/test machinery, so the patches are
generated with ``git`` itself (see the ``make_patch`` fixture).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from selfkraken import gitops
from selfkraken.db import Proposal, Repo, Store
from selfkraken.errors import LLMUnavailable, NotFoundError, ValidationError
from selfkraken.workflow import (
    ApplyOutcome,
    apply_proposal,
    propose,
    reject_proposal,
    register_repo,
    resolve_repo,
    revert_proposal,
    run_repo_tests,
)

# A syntactically well-formed hunk whose context does not exist in the repo.
UNAPPLICABLE_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def nope(x):\n"
    "-    return 0\n"
    "+def nope(x):\n"
    "+    return 1\n"
)


@pytest.fixture
def repo(store: Store, git_repo: Path, test_command: str) -> Repo:
    return register_repo(store, git_repo, test_command)


def _kinds(store: Store, repo_id: int | None = None) -> list[str]:
    return [change.kind for change in store.list_changes(repo_id=repo_id, limit=100)]


def _details(store: Store, kind: str, repo_id: int | None = None) -> list[str]:
    return [
        change.detail
        for change in store.list_changes(repo_id=repo_id, limit=100)
        if change.kind == kind
    ]


# --------------------------------------------------------------------------- #
# registration / resolution
# --------------------------------------------------------------------------- #
def test_register_repo_records_a_register_change(store: Store, git_repo: Path, test_command: str) -> None:
    registered = register_repo(store, git_repo, test_command)

    assert registered.path == str(Path(git_repo).resolve())
    assert registered.name == git_repo.name
    assert registered.test_command == test_command
    assert _kinds(store, registered.id)[:1] == ["register"]
    assert "def add" not in _details(store, "register")[0]


def test_register_repo_resolves_to_the_git_root(store: Store, git_repo: Path) -> None:
    nested = git_repo / "src" / "pkg"
    nested.mkdir(parents=True)

    registered = register_repo(store, nested, None)

    assert Path(registered.path) == git_repo.resolve()


def test_register_repo_requires_a_git_repository(store: Store, tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    with pytest.raises(ValidationError):
        register_repo(store, plain, None)


def test_register_repo_twice_is_a_validation_error(store: Store, git_repo: Path) -> None:
    register_repo(store, git_repo, None)

    with pytest.raises(ValidationError, match="already registered"):
        register_repo(store, git_repo, None)


def test_resolve_repo_defaults_to_cwd_and_matches_subdirectories(
    store: Store, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = register_repo(store, git_repo, None)
    (git_repo / "sub").mkdir()
    monkeypatch.chdir(git_repo / "sub")

    assert resolve_repo(store, None).id == registered.id
    assert resolve_repo(store, git_repo / "sub").id == registered.id


def test_resolve_repo_hint_mentions_init(store: Store, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError) as caught:
        resolve_repo(store, tmp_path)

    assert "skraken init" in str(caught.value)


# --------------------------------------------------------------------------- #
# propose
# --------------------------------------------------------------------------- #
def test_propose_manual_diff_creates_proposed_proposal(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})

    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)

    assert isinstance(proposal, Proposal)
    assert proposal.status == "proposed"
    assert proposal.model == "manual"
    assert proposal.repo_id == repo.id
    assert proposal.diff.strip() == diff.strip()
    assert "propose" in _kinds(store, repo.id)
    detail = _details(store, "propose", repo.id)[0]
    assert "1 file(s)" in detail
    assert "diff --git" not in detail and "def sub" not in detail


def test_propose_manual_diff_keeps_leading_prose_as_rationale(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    patch_file = "Notes for myself: add subtraction.\n\n" + diff

    proposal = propose(store, settings, repo, "Add a sub function", diff_text=patch_file)

    assert "add subtraction" in proposal.rationale
    assert "def sub" not in proposal.rationale


def test_propose_invalid_diff_raises_and_records_propose_failed(
    store: Store, settings, repo: Repo
) -> None:
    with pytest.raises(ValidationError):
        propose(store, settings, repo, "Fix calc", diff_text=UNAPPLICABLE_DIFF)

    assert "propose_failed" in _kinds(store, repo.id)
    assert "propose" not in _kinds(store, repo.id)
    assert store.list_proposals(repo_id=repo.id) == []
    detail = _details(store, "propose_failed", repo.id)[0]
    assert "return 1" not in detail and "def nope" not in detail


def test_propose_diff_without_any_diff_text_fails(
    store: Store, settings, repo: Repo
) -> None:
    with pytest.raises(ValidationError):
        propose(store, settings, repo, "Add a sub function", diff_text="just prose, no patch")

    assert "propose_failed" in _kinds(store, repo.id)


def test_propose_validates_request_length(store: Store, settings, repo: Repo) -> None:
    for bad in ("", "ab", "x" * 4001):
        with pytest.raises(ValidationError):
            propose(store, settings, repo, bad, diff_text=UNAPPLICABLE_DIFF)

    assert "propose_failed" not in _kinds(store, repo.id)


def test_propose_without_model_raises_llm_unavailable(store: Store, settings, repo: Repo) -> None:
    assert settings.llm_enabled is False

    with pytest.raises(LLMUnavailable) as caught:
        propose(store, settings, repo, "Add a sub function")

    message = str(caught.value)
    assert "SK_LLM_BASE_URL" in message and "SK_LLM_MODEL" in message and "--diff-file" in message
    assert store.list_proposals(repo_id=repo.id) == []
    assert "propose_failed" not in _kinds(store, repo.id)


def test_propose_uses_fake_llm_client(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    fake = FakeLLM(
        "calc.py has addition but no subtraction, so here is a sub function.\n\n"
        f"```diff\n{diff}\n```\n"
    )

    proposal = propose(store, settings, repo, "Add a sub function", llm=fake)

    assert proposal.model == "fake-model"
    assert proposal.status == "proposed"
    assert "subtraction" in proposal.rationale
    assert "def sub" in proposal.diff
    assert "```" not in proposal.diff
    assert fake.calls is not None and len(fake.calls) == 1


def test_propose_llm_path_sends_repo_context(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    fake = FakeLLM(f"Because.\n\n```diff\n{diff}\n```")

    propose(store, settings, repo, "Add a sub function", llm=fake, extra_paths=("calc.py",))

    system, user = fake.calls[0]
    assert system and "diff" in system.lower()
    assert "Add a sub function" in user
    assert "calc.py" in user
    assert git_repo.name in user


def test_propose_llm_output_without_diff_fails_cleanly(
    store: Store, settings, repo: Repo
) -> None:
    fake = FakeLLM("I could not find anything to change, sorry.")

    with pytest.raises(ValidationError):
        propose(store, settings, repo, "Add a sub function", llm=fake)

    assert "propose_failed" in _kinds(store, repo.id)
    assert store.list_proposals(repo_id=repo.id) == []


class FakeLLM:
    """Stand-in for a ``selfkraken.llm.LLMClient``."""

    name = "fake-model"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def test_apply_with_passing_tests_records_tests_passed(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(
        git_repo, {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]}
    )
    proposal = propose(store, settings, repo, "Add sub and a test", diff_text=diff)

    outcome = apply_proposal(store, settings, proposal.id)

    assert isinstance(outcome, ApplyOutcome)
    assert outcome.proposal.status == "tests_passed"
    assert outcome.rolled_back is False
    assert outcome.commit_sha is None
    assert "def sub" in (git_repo / "calc.py").read_text()
    assert "def test_sub" in (git_repo / "test_calc.py").read_text()
    assert outcome.test_run is not None and outcome.test_run.passed
    assert outcome.test_run.exit_code == 0
    runs = store.list_test_runs(repo_id=repo.id, proposal_id=proposal.id)
    assert [run.id for run in runs] == [outcome.test_run.id]
    assert "apply" in _kinds(store, repo.id) and "test" in _kinds(store, repo.id)
    assert store.get_proposal(proposal.id).status == "tests_passed"
    detail = _details(store, "test", repo.id)[0]
    assert "exit 0" in detail and "passed" in detail


def test_apply_rolls_back_when_tests_fail(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    proposal = propose(store, settings, repo, "Make add subtract instead", diff_text=diff)

    outcome = apply_proposal(store, settings, proposal.id)

    assert outcome.proposal.status == "tests_failed"
    assert outcome.rolled_back is True
    assert outcome.test_run is not None and not outcome.test_run.passed
    assert (git_repo / "calc.py").read_text() == sources["calc"]
    assert gitops.changed_paths(git_repo) == []
    assert "rollback" in _kinds(store, repo.id)
    assert "commit" not in _kinds(store, repo.id)


def test_apply_keep_on_failure_leaves_broken_change(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    proposal = propose(store, settings, repo, "Make add subtract instead", diff_text=diff)

    outcome = apply_proposal(store, settings, proposal.id, keep_on_failure=True)

    assert outcome.rolled_back is False
    assert outcome.proposal.status == "tests_failed"
    assert (git_repo / "calc.py").read_text() == sources["calc_wrong"]
    assert "rollback" not in _kinds(store, repo.id)


def test_apply_without_tests_leaves_status_applied(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)

    outcome = apply_proposal(store, settings, proposal.id, run_tests=False)

    assert outcome.proposal.status == "applied"
    assert outcome.test_run is None
    assert store.list_test_runs(repo_id=repo.id) == []
    assert "def sub" in (git_repo / "calc.py").read_text()


def test_apply_commit_creates_git_commit(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    before = gitops.head_sha(git_repo)
    diff = make_patch(
        git_repo, {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]}
    )
    proposal = propose(store, settings, repo, "Add sub and a test", diff_text=diff)

    outcome = apply_proposal(store, settings, proposal.id, commit=True)

    assert outcome.commit_sha
    assert before and outcome.commit_sha != before
    head = gitops.head_sha(git_repo)
    assert head and head.startswith(outcome.commit_sha[:7])
    assert len(outcome.commit_sha) in (7, 40)
    assert gitops.changed_paths(git_repo) == []
    assert "commit" in _kinds(store, repo.id)
    subject = gitops.run_git(git_repo, "log", "-1", "--pretty=%s").stdout
    assert subject.startswith("selfkraken: apply proposal")
    assert str(proposal.id) in subject


def test_apply_is_rejected_the_second_time(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)
    apply_proposal(store, settings, proposal.id, run_tests=False)

    with pytest.raises(ValidationError) as caught:
        apply_proposal(store, settings, proposal.id)

    assert "applied" in str(caught.value)
    with pytest.raises(ValidationError):
        apply_proposal(store, settings, proposal.id)


def test_apply_unknown_proposal_raises_not_found(store: Store, settings) -> None:
    with pytest.raises(NotFoundError):
        apply_proposal(store, settings, 4242)


# --------------------------------------------------------------------------- #
# reject / revert
# --------------------------------------------------------------------------- #
def test_reject_transitions_and_blocks_apply(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)

    rejected = reject_proposal(store, proposal.id)

    assert rejected.status == "rejected"
    assert "reject" in _kinds(store, repo.id)
    assert (git_repo / "calc.py").read_text() == sources["calc"]
    with pytest.raises(ValidationError):
        reject_proposal(store, proposal.id)
    with pytest.raises(ValidationError):
        apply_proposal(store, settings, proposal.id)


def test_revert_after_applied_restores_the_file(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)
    apply_proposal(store, settings, proposal.id, run_tests=False)

    reverted = revert_proposal(store, proposal.id)

    assert reverted.status == "reverted"
    assert (git_repo / "calc.py").read_text() == sources["calc"]
    assert "revert" in _kinds(store, repo.id)
    with pytest.raises(ValidationError):
        revert_proposal(store, proposal.id)


def test_revert_after_tests_passed_restores_the_file(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(
        git_repo, {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]}
    )
    proposal = propose(store, settings, repo, "Add sub and a test", diff_text=diff)
    apply_proposal(store, settings, proposal.id)

    reverted = revert_proposal(store, proposal.id)

    assert reverted.status == "reverted"
    assert "def sub" not in (git_repo / "calc.py").read_text()


def test_revert_is_refused_for_an_unapplied_proposal(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    proposal = propose(store, settings, repo, "Add a sub function", diff_text=diff)

    with pytest.raises(ValidationError):
        revert_proposal(store, proposal.id)


def test_revert_is_refused_after_an_automatic_rollback(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    proposal = propose(store, settings, repo, "Make add subtract instead", diff_text=diff)
    outcome = apply_proposal(store, settings, proposal.id)
    assert outcome.rolled_back is True

    with pytest.raises(ValidationError) as caught:
        revert_proposal(store, proposal.id)

    assert "reversed" in str(caught.value).lower()


# --------------------------------------------------------------------------- #
# tests without a proposal
# --------------------------------------------------------------------------- #
def test_change_details_carry_metadata_only(
    store: Store, settings, repo: Repo, git_repo: Path, make_patch, sources
) -> None:
    """No file body or diff may ever reach the audit log."""
    diff = make_patch(
        git_repo, {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]}
    )
    proposal = propose(store, settings, repo, "Add sub and a test", diff_text=diff)
    apply_proposal(store, settings, proposal.id, commit=True)
    run_repo_tests(store, settings, repo)

    forbidden = ("diff --git", "@@", "+++", "--- a/", "def sub", "return a - b", "assert sub")
    for change in store.list_changes(limit=100):
        for needle in forbidden:
            assert needle not in change.detail, f"{change.kind}: {change.detail}"


def test_run_repo_tests_records_a_test_change(
    store: Store, settings, repo: Repo
) -> None:
    run = run_repo_tests(store, settings, repo)

    assert run.passed is True
    assert run.exit_code == 0
    assert run.proposal_id is None
    assert "test" in _kinds(store, repo.id)
    assert store.list_test_runs(repo_id=repo.id)[0].id == run.id


def test_run_repo_tests_without_a_test_command(
    store: Store, settings, git_repo: Path
) -> None:
    repo = register_repo(store, git_repo, None)

    with pytest.raises(ValidationError) as caught:
        run_repo_tests(store, settings, repo)

    assert "set-test" in str(caught.value)


def test_run_repo_tests_detects_a_broken_repo(
    store: Store, settings, repo: Repo, git_repo: Path, sources
) -> None:
    (git_repo / "calc.py").write_text(sources["calc_broken"], encoding="utf-8")

    run = run_repo_tests(store, settings, repo)

    assert run.passed is False
    assert run.exit_code != 0
