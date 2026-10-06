"""The selfkraken core loop, free of any UI.

register repo -> index -> ask one model for a change -> unified diff proposal ->
apply -> run tests -> keep or roll back, with every step written to the audit log.

Audit ``detail`` strings carry **metadata only** (ids, paths, counts, statuses,
exit codes, reasons).  Never a file body, never a diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from selfkraken import runner
from selfkraken.config import Settings
from selfkraken.db import Proposal, Repo, Store, TestRun
from selfkraken.diffs import (
    apply_diff,
    can_reverse,
    extract_diff,
    reverse_diff,
    validate_diff,
)
from selfkraken.errors import (
    GitError,
    LLMError,
    LLMUnavailable,
    NotFoundError,
    SKError,
    ValidationError,
)
from selfkraken.gitops import changed_paths, commit_paths, repo_root
from selfkraken.indexer import build_context
from selfkraken.llm import (
    SYSTEM_PROMPT,
    LLMClient,
    build_user_prompt,
    make_client,
    split_rationale,
)

MIN_REQUEST_CHARS = 3
MAX_REQUEST_CHARS = 4000

#: ``proposals.model`` value for hand-written patches (``--diff-file``).
MANUAL_MODEL = "manual"

#: Statuses a proposal can be reverted from.  A ``tests_failed`` proposal that
#: was already rolled back is refused later by the ``can_reverse`` check, since
#: its diff is no longer present in the working tree.
REVERTIBLE_STATUSES: tuple[str, ...] = ("applied", "tests_passed", "tests_failed")

#: Shared with the CLI so degraded mode is explained the same way everywhere.
DEGRADED_MODE_HINT = (
    "degraded mode: no model is configured, so nothing can be asked of it "
    "(set SK_LLM_BASE_URL and SK_LLM_MODEL, or pass a patch with --diff-file)"
)


def register_repo(
    store: Store,
    path: Path,
    test_command: str | None,
    name: str | None = None,
) -> Repo:
    """Register the git repository that contains *path* and audit it."""
    target = Path(path)
    try:
        root = Path(repo_root(target))
    except GitError as exc:
        raise ValidationError(f"{target} is not a git repository ({exc})") from exc

    repo = store.add_repo(root, name or root.name, test_command)
    store.record_change(
        "register",
        f"registered repo {repo.id} ({repo.name}) at {repo.path}",
        repo_id=repo.id,
    )
    return repo


def resolve_repo(store: Store, path: Path | None) -> Repo:
    """Return the repository registered for *path* (default: the cwd)."""
    target = Path(path).resolve() if path is not None else Path.cwd().resolve()
    repo = store.find_repo_by_path(target)
    if repo is None:
        raise NotFoundError(
            f"no repository is registered for {target} — "
            f"run: skraken init {target}"
        )
    return repo


def propose(
    store: Store,
    settings: Settings,
    repo: Repo,
    request: str,
    *,
    llm: LLMClient | None = None,
    diff_text: str | None = None,
    extra_paths: Sequence[str] = (),
) -> Proposal:
    """Produce a validated unified-diff proposal and audit it.

    With *diff_text* the caller supplies the patch itself ("manual mode");
    otherwise the configured model is asked for one.  Every rejected attempt is
    recorded as ``propose_failed`` with the reason only.
    """
    clean_request = (request or "").strip()
    if len(clean_request) < MIN_REQUEST_CHARS:
        raise ValidationError(
            f"request is too short ({len(clean_request)} characters) — "
            f"minimum {MIN_REQUEST_CHARS}"
        )
    if len(clean_request) > MAX_REQUEST_CHARS:
        raise ValidationError(
            f"request is too long ({len(clean_request)} characters) — "
            f"maximum {MAX_REQUEST_CHARS}"
        )

    repo_path = Path(repo.path)
    try:
        if diff_text is not None:
            model = MANUAL_MODEL
            diff = _extract_manual_diff(diff_text)
            rationale = _leading_prose(diff_text)
        else:
            client = llm or make_client(settings)
            if client is None:
                raise LLMUnavailable(DEGRADED_MODE_HINT)
            context = build_context(
                store,
                repo,
                clean_request,
                settings.max_context_chars,
                extra_paths=tuple(extra_paths),
            )
            raw = client.complete(SYSTEM_PROMPT, build_user_prompt(clean_request, context, repo.name))
            diff = extract_diff(raw)
            rationale = split_rationale(raw)
            model = client.name or "model"
        paths = validate_diff(diff, repo_path)
    except (ValidationError, GitError, LLMError) as exc:
        _record_propose_failure(store, repo, exc)
        raise

    proposal = store.create_proposal(
        repo.id,
        request=clean_request,
        model=model,
        diff=diff,
        rationale=rationale,
        status="proposed",
        is_sample=repo.is_sample,
    )
    store.record_change(
        "propose",
        f"proposal {proposal.id} for repo {repo.id}: {len(paths)} file(s), "
        f"model {model}, status proposed",
        repo_id=repo.id,
        proposal_id=proposal.id,
    )
    return proposal


@dataclass
class ApplyOutcome:
    """Result of :func:`apply_proposal`."""

    proposal: Proposal
    test_run: TestRun | None
    rolled_back: bool
    commit_sha: str | None


def apply_proposal(
    store: Store,
    settings: Settings,
    proposal_id: int,
    *,
    run_tests: bool = True,
    commit: bool = False,
    keep_on_failure: bool = False,
    commit_message: str | None = None,
) -> ApplyOutcome:
    """Apply a proposed diff, test it, keep or roll it back, and audit all of it."""
    proposal = store.get_proposal(proposal_id)
    if proposal.status != "proposed":
        raise ValidationError(
            f"proposal {proposal.id} cannot be applied from status "
            f"'{proposal.status}' — only 'proposed' proposals can be applied"
        )

    repo = store.get_repo(proposal.repo_id)
    repo_path = Path(repo.path)

    paths = validate_diff(proposal.diff, repo_path)

    if commit:
        # The commit would carry every uncommitted change in the touched paths,
        # not just the proposal's — refuse rather than swallow the user's WIP.
        dirty = sorted(set(paths) & set(changed_paths(repo_path)))
        if dirty:
            raise ValidationError(
                f"cannot --commit: uncommitted changes in {len(dirty)} touched "
                f"file(s): {', '.join(dirty)}; commit or stash them first, or "
                "apply without --commit"
            )

    apply_diff(repo_path, proposal.diff)
    proposal = store.set_proposal_status(proposal.id, "applied")
    store.record_change(
        "apply",
        f"applied proposal {proposal.id} to repo {repo.id}: {len(paths)} file(s)",
        repo_id=repo.id,
        proposal_id=proposal.id,
    )

    test_run: TestRun | None = None
    rolled_back = False
    if run_tests and repo.test_command:
        result = runner.run_tests(repo_path, repo.test_command, settings.test_timeout)
        test_run = store.add_test_run(
            repo.id,
            proposal.id,
            result.command,
            result.exit_code,
            result.duration_ms,
            result.output_tail,
        )
        store.record_change(
            "test",
            f"test run {test_run.id} for proposal {proposal.id} in repo {repo.id}: "
            f"exit {result.exit_code} ({'passed' if result.passed else 'failed'})",
            repo_id=repo.id,
            proposal_id=proposal.id,
        )
        if result.passed:
            proposal = store.set_proposal_status(proposal.id, "tests_passed")
        else:
            proposal = store.set_proposal_status(proposal.id, "tests_failed")
            if not keep_on_failure:
                reverse_diff(repo_path, proposal.diff)
                rolled_back = True
                store.record_change(
                    "rollback",
                    f"rolled back proposal {proposal.id} in repo {repo.id} after "
                    "failing tests",
                    repo_id=repo.id,
                    proposal_id=proposal.id,
                )

    commit_sha: str | None = None
    tests_ok = test_run is None or test_run.passed
    if commit and not rolled_back and tests_ok:
        message = commit_message or _default_commit_message(proposal)
        commit_sha = commit_paths(repo_path, paths, message)
        store.record_change(
            "commit",
            f"committed proposal {proposal.id} in repo {repo.id}: {len(paths)} file(s), "
            f"sha {commit_sha[:12]}",
            repo_id=repo.id,
            proposal_id=proposal.id,
        )

    return ApplyOutcome(
        proposal=store.get_proposal(proposal.id),
        test_run=test_run,
        rolled_back=rolled_back,
        commit_sha=commit_sha,
    )


def reject_proposal(store: Store, proposal_id: int) -> Proposal:
    """Mark an as-yet-unapplied proposal as rejected."""
    proposal = store.get_proposal(proposal_id)
    if proposal.status != "proposed":
        raise ValidationError(
            f"proposal {proposal.id} cannot be rejected from status "
            f"'{proposal.status}' — only 'proposed' proposals can be rejected"
        )
    proposal = store.set_proposal_status(proposal.id, "rejected")
    store.record_change(
        "reject",
        f"rejected proposal {proposal.id} in repo {proposal.repo_id}: status rejected",
        repo_id=proposal.repo_id,
        proposal_id=proposal.id,
    )
    return proposal


def revert_proposal(store: Store, proposal_id: int) -> Proposal:
    """Reverse an applied diff in the working tree (git history is untouched)."""
    proposal = store.get_proposal(proposal_id)
    if proposal.status not in REVERTIBLE_STATUSES:
        raise ValidationError(
            f"proposal {proposal.id} cannot be reverted from status "
            f"'{proposal.status}' — revert works from "
            f"{', '.join(REVERTIBLE_STATUSES)}"
        )
    repo = store.get_repo(proposal.repo_id)
    repo_path = Path(repo.path)
    if not can_reverse(repo_path, proposal.diff):
        raise ValidationError(
            f"proposal {proposal.id} can no longer be reversed cleanly — its changes "
            f"are not present in {repo.path} (already rolled back or edited)"
        )
    reverse_diff(repo_path, proposal.diff)
    proposal = store.set_proposal_status(proposal.id, "reverted")
    store.record_change(
        "revert",
        f"reverted proposal {proposal.id} in repo {repo.id}: status reverted, "
        "git history unchanged",
        repo_id=repo.id,
        proposal_id=proposal.id,
    )
    return proposal


def run_repo_tests(store: Store, settings: Settings, repo: Repo) -> TestRun:
    """Run the repository's configured test command and record the result."""
    if not repo.test_command:
        raise ValidationError(
            f"repo {repo.id} ({repo.name}) has no test command — "
            "set one with: skraken set-test \"python -m pytest -q\""
        )
    result = runner.run_tests(Path(repo.path), repo.test_command, settings.test_timeout)
    test_run = store.add_test_run(
        repo.id,
        None,
        result.command,
        result.exit_code,
        result.duration_ms,
        result.output_tail,
    )
    store.record_change(
        "test",
        f"test run {test_run.id} for repo {repo.id}: exit {result.exit_code} "
        f"({'passed' if result.passed else 'failed'})",
        repo_id=repo.id,
    )
    return test_run


def _extract_manual_diff(diff_text: str) -> str:
    """Pull a unified diff out of a patch file the user supplied."""
    if not diff_text.strip():
        raise ValidationError("the patch file is empty")
    try:
        return extract_diff(diff_text)
    except ValidationError as exc:
        raise ValidationError(str(exc).replace("model output", "the patch file")) from exc


def _leading_prose(diff_text: str) -> str:
    """Notes written *before* the patch in a ``--diff-file`` (never patch content)."""
    kept: list[str] = []
    for line in diff_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("diff --git", "--- ", "+++ ", "@@", "index ", "```")):
            break
        kept.append(line)
    return "\n".join(kept).strip()[:2000]


def _record_propose_failure(store: Store, repo: Repo, error: SKError) -> None:
    """Audit a failed proposal attempt: reason only, never contents."""
    reason = " ".join(str(error).split()) or type(error).__name__
    store.record_change(
        "propose_failed",
        f"no proposal created for repo {repo.id}: {reason[:300]}",
        repo_id=repo.id,
    )


def _default_commit_message(proposal: Proposal) -> str:
    summary = " ".join(proposal.request.split()).strip()
    if len(summary) > 60:
        summary = summary[:57] + "..."
    body = f"selfkraken: apply proposal {proposal.id}"
    return f"{body}: {summary}" if summary else body
