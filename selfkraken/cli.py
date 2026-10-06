"""skraken — the selfkraken command line.

One loop, one command each: ``init`` a repo, ``index`` it, ask for a ``propose``d
change, ``show`` the diff, ``apply`` it, run ``test``s, keep or roll it back, and
read the audit trail with ``log``.  Visualisation: ``graph``, ``branches``, ``tree``.

Exit codes: ``0`` success, ``1`` expected failure, ``2`` invalid input.
Output is plain text + `rich`; states are always labelled with words (``OK``,
``FAILED``, ``Error:``), never with colour alone, and ``NO_COLOR`` is honoured.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence
from urllib.parse import urlparse

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from selfkraken import diffs, gitops, indexer, portability, sample, views, workflow
from selfkraken.config import Settings, load_settings
from selfkraken.db import PROPOSAL_STATUSES, Change, Proposal, Repo, Store, TestRun
from selfkraken.errors import SKError, ValidationError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2

#: ``Console`` resolves ``sys.stdout`` lazily, so one shared instance is fine and
#: keeps ``NO_COLOR`` / piping / test capture working.
console = Console(highlight=False)

GRAPH_LIMIT_MAX = 2000
LOG_LIMIT_MAX = 1000
TREE_DEPTH_MAX = 64


# --------------------------------------------------------------------------- #
# small output helpers
# --------------------------------------------------------------------------- #
def _ok(message: str) -> None:
    """Success state: always carries the text label ``OK``."""
    console.print(f"[green bold]OK[/green bold] {message}")


def _error(message: str) -> None:
    """Failure/validation state: always carries the text label ``Error:``."""
    console.print(f"[red bold]Error:[/red bold] {message}")


def _failed(message: str) -> None:
    """Operation completed but did not succeed: text label ``FAILED``."""
    console.print(f"[red bold]FAILED[/red bold] {message}")


def _empty(message: str) -> None:
    """Empty state: nothing recorded yet, plus what to type next."""
    console.print(f"[bold]Nothing to show.[/bold] {message}")


def _hint(message: str) -> None:
    console.print(f"[dim]→ {message}[/dim]")


def _note(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def _short(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


def _clip(text: str, limit: int) -> str:
    """Truncate but keep line breaks (rationale, test output)."""
    body = str(text).strip()
    return body if len(body) <= limit else body[: max(1, limit - 1)] + "…"


def _status(text: str) -> Any:
    """A spinner for loading states — only on a TTY; a no-op when piped."""
    if console.is_terminal:
        return console.status(text)
    return contextlib.nullcontext()


def _confirm(message: str) -> None:
    """Blocking confirmation before a change is written to the working tree."""
    try:
        answer = typer.confirm(message, default=False)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        _error("cancelled — nothing was applied (interrupted at the prompt)")
        raise typer.Exit(EXIT_FAILED) from None
    except Exception as exc:  # stdin is not interactive (click/typer Abort)
        _error("cancelled — nothing was applied (re-run with --yes to skip the prompt)")
        _note(f"the prompt ended with {type(exc).__name__}")
        raise typer.Exit(EXIT_FAILED) from None
    if not answer:
        _error("cancelled — nothing was applied (answer 'y', or re-run with --yes)")
        raise typer.Exit(EXIT_FAILED)


def _reason(exc: SKError) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


def _guarded(command: Callable[..., None]) -> Callable[..., None]:
    """Print expected errors instead of tracebacks and map them to exit codes."""

    @wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return command(*args, **kwargs)
        except SKError as exc:
            _error(_reason(exc))
            code = EXIT_INVALID if isinstance(exc, ValidationError) else EXIT_FAILED
            raise typer.Exit(code) from None
        except typer.Exit:
            raise
        except KeyboardInterrupt:
            _error("interrupted")
            raise typer.Exit(EXIT_FAILED) from None

    return wrapper


@contextlib.contextmanager
def _store(settings: Settings) -> Iterator[Store]:
    """A Store opened from ``settings.db_path`` and always closed afterwards."""
    store = Store(settings.db_path)
    try:
        yield store
    finally:
        store.close()


def _repo_option() -> Any:
    return typer.Option(
        None,
        "--repo",
        help="Repository to work on (path inside a registered repo; default: the current directory).",
    )


def _resolve(store: Store, repo: Optional[Path]) -> Repo:
    return workflow.resolve_repo(store, repo)


def _validate_limit(value: int, name: str, maximum: int) -> int:
    if value < 1 or value > maximum:
        raise ValidationError(f"{name} must be between 1 and {maximum} (got {value})")
    return value


def _print_diff(diff: str) -> None:
    console.print(Syntax(diff, "diff", background_color="default"))


def _proposal_meta(proposal: Proposal, repo: Repo | None) -> Table:
    table = Table(box=box.SIMPLE, show_header=False, expand=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("id", str(proposal.id))
    table.add_row("status", proposal.status)
    table.add_row("model", proposal.model)
    table.add_row("repo", f"{proposal.repo_id}" + (f" ({repo.name})" if repo else ""))
    table.add_row("files", str(len(diffs.touched_paths(proposal.diff))))
    table.add_row("request", _short(proposal.request, 200))
    table.add_row("created", proposal.created_at)
    table.add_row("updated", proposal.updated_at)
    if proposal.is_sample:
        table.add_row("sample", "yes")
    return table


def _print_proposal(proposal: Proposal, repo: Repo | None, *, with_diff: bool = True) -> None:
    console.print(_proposal_meta(proposal, repo))
    if proposal.rationale:
        console.print(Panel(_clip(proposal.rationale, 1200), title="rationale", expand=False))
    if with_diff:
        console.print(Panel("unified diff", style="dim", expand=False))
        _print_diff(proposal.diff)


def _test_run_table(runs: Sequence[TestRun]) -> Table:
    table = Table(box=box.SIMPLE)
    table.add_column("id", justify="right")
    table.add_column("result", justify="left")
    table.add_column("exit", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("command")
    for run in runs:
        table.add_row(
            str(run.id),
            "PASSED" if run.passed else "FAILED",
            str(run.exit_code),
            str(run.duration_ms),
            _short(run.command, 60),
        )
    return table


def _repo_table(repos: Sequence[Repo], store: Store) -> Table:
    table = Table(box=box.SIMPLE, title="registered repositories")
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("files", justify="right")
    table.add_column("test command")
    table.add_column("path")
    table.add_column("flags")
    for repo in repos:
        table.add_row(
            str(repo.id),
            repo.name,
            str(store.count_files(repo.id)),
            repo.test_command or "(not set)",
            _short(repo.path, 60),
            "sample" if repo.is_sample else "",
        )
    return table


def _change_table(changes: Sequence[Change]) -> Table:
    table = Table(box=box.SIMPLE, title="activity log (newest first)")
    table.add_column("when")
    table.add_column("kind")
    table.add_column("repo", justify="right")
    table.add_column("prop", justify="right")
    table.add_column("detail")
    for change in changes:
        table.add_row(
            change.created_at.replace("T", " ")[:19],
            change.kind,
            str(change.repo_id if change.repo_id is not None else "-"),
            str(change.proposal_id if change.proposal_id is not None else "-"),
            _short(change.detail, 120),
        )
    return table


DEGRADED_HELP = (
    "selfkraken is in degraded mode: no model is configured, so it cannot ask one.\n"
    "Two ways forward:\n"
    "  1. Configure a model — put these in .env next to where you run skraken "
    "(or export them):\n"
    "       SK_LLM_BASE_URL=http://localhost:11434/v1\n"
    "       SK_LLM_MODEL=llama3.1\n"
    "     SK_LLM_API_KEY is optional (bearer token, never printed).\n"
    "  2. Stay in manual mode: skraken propose \"what you want\" --diff-file my.patch\n"
    "     Manual proposals go through the same validation, apply, test and rollback path."
)


def _print_degraded() -> None:
    console.print("[red bold]FAILED[/red bold] degraded mode: no model configured")
    console.print(DEGRADED_HELP)


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
app = typer.Typer(
    name="skraken",
    help="Private, repository-local Git assistant: propose → review → apply → test → keep or roll back.",
    no_args_is_help=True,
    add_completion=False,
)

sample_app = typer.Typer(help="Work with the bundled demo repository (never touches your own repos).")
app.add_typer(sample_app, name="sample")


@app.command("init")
@_guarded
def init(
    path: Optional[Path] = typer.Argument(
        None, help="Repository to register (default: the current directory)."
    ),
    test_cmd: Optional[str] = typer.Option(
        None, "--test-cmd", help='Test command to run after applying a change, e.g. "python -m pytest -q".'
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Display name for the repository (default: its directory name)."
    ),
) -> None:
    """Register a git repository so selfkraken can index it and propose changes."""
    settings = load_settings()
    with _store(settings) as store:
        repo = workflow.register_repo(store, path or Path.cwd(), test_cmd, name)
        _ok(f"registered repo {repo.id} ({repo.name})")
        console.print(_repo_table([repo], store))
        _hint(f"next: skraken index --repo {repo.path}")
        if not repo.test_command:
            _hint('set a test command: skraken set-test "python -m pytest -q"')


@app.command("repos")
@_guarded
def repos() -> None:
    """List the repositories selfkraken knows about."""
    settings = load_settings()
    with _store(settings) as store:
        found = store.list_repos()
        if not found:
            _empty("No repositories registered yet — try: skraken init .  (or: skraken sample load)")
            return
        _ok(f"{len(found)} repository record(s)")
        console.print(_repo_table(found, store))


@app.command("set-test")
@_guarded
def set_test(
    command: str = typer.Argument(..., help='Test command, e.g. "python -m pytest -q".'),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Set the test command that is run after a change is applied."""
    settings = load_settings()
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        updated = store.set_test_command(repo.id, command)
        _ok(f"test command for repo {updated.id} ({updated.name}) set to: {updated.test_command}")


@app.command("index")
@_guarded
def index(
    repo_path: Optional[Path] = _repo_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each file as it is indexed."),
) -> None:
    """Index the repository's files and searchable chunks (incremental)."""
    settings = load_settings()
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        seen = {change.id for change in store.list_changes(repo_id=repo.id, limit=200)}

        def progress(message: str) -> None:
            if verbose and console.is_terminal:
                console.print(f"[dim]{_short(message, 100)}[/dim]")

        with _status(f"Indexing {repo.name}…"):
            stats = indexer.index_repo(store, repo, progress=progress)
        _ok(
            f"indexed repo {repo.id} ({repo.name}): {stats.indexed} indexed, {stats.unchanged} unchanged, "
            f"{stats.scanned} scanned, {stats.skipped} skipped, {stats.removed} removed"
        )
        # The audit trail must show an index step even if the indexer itself did
        # not record one; only add it when it is missing.
        fresh = [c for c in store.list_changes(repo_id=repo.id, limit=200) if c.id not in seen]
        if not any(c.kind == "index" for c in fresh):
            store.record_change(
                "index",
                f"indexed repo {repo.id}: {stats.indexed} indexed, {stats.unchanged} unchanged, "
                f"{stats.skipped} skipped, {stats.removed} removed",
                repo_id=repo.id,
            )


@app.command("search")
@_guarded
def search(
    query: str = typer.Argument(..., help="Words to look for in indexed files."),
    limit: int = typer.Option(8, "--limit", help="Maximum number of chunks to show."),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Search the indexed chunks of a repository."""
    settings = load_settings()
    if not query.strip():
        raise ValidationError("search query is empty")
    _validate_limit(limit, "--limit", 200)
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        with _status("Searching…"):
            hits = store.search_chunks(repo.id, query, limit=limit)
        if not hits:
            _empty(
                f"no chunks match {query!r} in repo {repo.id} — try: skraken index --repo {repo.path}"
            )
            return
        _ok(f"{len(hits)} chunk(s) match {query!r} in repo {repo.id}")
        for chunk in hits:
            console.print(
                f"[bold]{chunk.path}[/bold] (line {chunk.start_line}, score {chunk.score:.2f})"
            )
            preview = "\n".join(chunk.content.splitlines()[:4])
            console.print(Panel(_short(preview, 600) or "(empty chunk)", expand=False, style="dim"))


@app.command("propose")
@_guarded
def propose(
    request: str = typer.Argument(..., help="What you want changed, in plain words."),
    diff_file: Optional[Path] = typer.Option(
        None,
        "--diff-file",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Use this patch file instead of asking the model (manual mode; works in degraded mode).",
    ),
    files: list[Path] = typer.Option(
        [], "--file", help="Include this file in the model context (repeatable)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask whether to keep the proposal after showing it."
    ),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Ask the model (or a patch file) for one change and store the resulting diff."""
    settings = load_settings()
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        if diff_file is None and not settings.llm_enabled:
            _print_degraded()
            raise typer.Exit(EXIT_FAILED)

        diff_text: str | None = None
        if diff_file is not None:
            diff_text = diff_file.read_text(encoding="utf-8", errors="replace")

        extra: Sequence[str] = tuple(str(path) for path in files)
        with _status("Asking model…"):
            proposal = workflow.propose(
                store, settings, repo, request, diff_text=diff_text, extra_paths=extra
            )
        _ok(f"proposal {proposal.id} created (model {proposal.model})")
        _print_proposal(proposal, repo)
        _hint(f"apply it: skraken apply {proposal.id}   ·   drop it: skraken reject {proposal.id}")
        if not yes and console.is_terminal:
            keep = False
            try:
                keep = typer.confirm(f"Keep proposal {proposal.id} for now?", default=True)
            except Exception:
                keep = True
            if not keep:
                rejected = workflow.reject_proposal(store, proposal.id)
                _ok(f"proposal {rejected.id} rejected")
                return


@app.command("proposals")
@_guarded
def proposals(
    status: Optional[str] = typer.Option(
        None, "--status", help=f"One of: {', '.join(PROPOSAL_STATUSES)}."
    ),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """List stored proposals, newest first."""
    settings = load_settings()
    wanted = (status or "").strip() or None
    if wanted is not None and wanted not in PROPOSAL_STATUSES:
        raise ValidationError(
            f"unknown --status '{wanted}' — valid statuses: {', '.join(PROPOSAL_STATUSES)}"
        )
    with _store(settings) as store:
        repo = _resolve(store, repo_path) if repo_path is not None else None
        found = store.list_proposals(repo_id=repo.id if repo else None, status=wanted)
        if not found:
            _empty(
                "no proposals stored yet — try: skraken propose \"fix the division-by-zero bug\""
                "  (or: skraken sample load)"
            )
            return
        table = Table(box=box.SIMPLE, title="proposals (newest first)")
        table.add_column("id", justify="right")
        table.add_column("repo", justify="right")
        table.add_column("status")
        table.add_column("model")
        table.add_column("files", justify="right")
        table.add_column("request")
        table.add_column("created")
        for proposal in found:
            table.add_row(
                str(proposal.id),
                str(proposal.repo_id),
                proposal.status,
                _short(proposal.model, 20),
                str(len(diffs.touched_paths(proposal.diff))),
                _short(proposal.request, 60),
                proposal.created_at.replace("T", " ")[:19],
            )
        _ok(f"{len(found)} proposal(s)")
        console.print(table)
        _hint("see one in full: skraken show ID")


@app.command("show")
@_guarded
def show(
    proposal_id: int = typer.Argument(..., help="Proposal id (see: skraken proposals)."),
) -> None:
    """Show a proposal: metadata, rationale and the full unified diff."""
    settings = load_settings()
    with _store(settings) as store:
        proposal = store.get_proposal(proposal_id)
        repo = store.get_repo(proposal.repo_id)
        _print_proposal(proposal, repo)
        runs = store.list_test_runs(proposal_id=proposal.id)
        if runs:
            console.print(_test_run_table(runs[:5]))
        else:
            _note("no test run recorded for this proposal yet")


@app.command("apply")
@_guarded
def apply_change(
    proposal_id: int = typer.Argument(..., help="Proposal to apply (must still be 'proposed')."),
    no_test: bool = typer.Option(False, "--no-test", help="Apply without running the test command."),
    commit: bool = typer.Option(
        False, "--commit", help="Commit the touched files if tests pass (requires a clean tree in those files)."
    ),
    keep_on_failure: bool = typer.Option(
        False, "--keep-on-failure", help="Leave the change in the working tree even if tests fail."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
) -> None:
    """Apply a proposal, run the tests, and roll back automatically if they fail."""
    settings = load_settings()
    with _store(settings) as store:
        proposal = store.get_proposal(proposal_id)
        repo = store.get_repo(proposal.repo_id)
        _print_proposal(proposal, repo, with_diff=True)
        if not yes:
            _confirm(f"Apply proposal {proposal.id} to {repo.path}? This edits your working tree.")

        outcome = workflow.apply_proposal(
            store,
            settings,
            proposal.id,
            run_tests=not no_test,
            commit=commit,
            keep_on_failure=keep_on_failure,
        )
        result = outcome.proposal

        if result.status == "tests_failed":
            where = (
                " — changes rolled back, working tree restored"
                if outcome.rolled_back
                else " — kept in the working tree"
            )
            _failed(f"proposal {result.id} {result.status}{where}")
        else:
            committed = f", committed {outcome.commit_sha[:12]}" if outcome.commit_sha else ""
            _ok(f"proposal {result.id} applied — status {result.status}{committed}")
        if outcome.test_run is not None:
            console.print(_test_run_table([outcome.test_run]))
            if not outcome.test_run.passed and outcome.test_run.output_tail:
                console.print(
                    Panel(outcome.test_run.output_tail, title="test output (tail)", expand=False)
                )
        elif not no_test and not repo.test_command:
            _note("no test command configured for this repo — nothing was run (skraken set-test ...)")
        tests_ok = outcome.test_run is None or outcome.test_run.passed
        if commit and outcome.commit_sha is None and (outcome.rolled_back or not tests_ok):
            _note("--commit skipped: the change was not kept")
        raise typer.Exit(EXIT_OK if result.status != "tests_failed" else EXIT_FAILED)


@app.command("reject")
@_guarded
def reject(
    proposal_id: int = typer.Argument(..., help="Proposal to reject (must still be 'proposed')."),
) -> None:
    """Reject a proposal without touching the working tree."""
    settings = load_settings()
    with _store(settings) as store:
        proposal = workflow.reject_proposal(store, proposal_id)
        _ok(f"proposal {proposal.id} rejected — status {proposal.status}")


@app.command("revert")
@_guarded
def revert(
    proposal_id: int = typer.Argument(..., help="Applied proposal to reverse."),
) -> None:
    """Reverse an applied proposal's diff (git history is left alone)."""
    settings = load_settings()
    with _store(settings) as store:
        proposal = workflow.revert_proposal(store, proposal_id)
        _ok(f"proposal {proposal.id} reverted — status {proposal.status}")
        _hint("the files are back to their pre-apply state; use git to rewrite history if you committed")


@app.command("test")
@_guarded
def run_tests(
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Run the repository's test command and record the result."""
    settings = load_settings()
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        with _status("Running tests…"):
            run = workflow.run_repo_tests(store, settings, repo)
        console.print(_test_run_table([run]))
        if run.passed:
            _ok(f"tests passed (exit {run.exit_code}, {run.duration_ms} ms)")
        else:
            _failed(f"tests failed (exit {run.exit_code}, {run.duration_ms} ms)")
            if run.output_tail:
                console.print(Panel(run.output_tail, title="test output (tail)", expand=False))
        raise typer.Exit(EXIT_OK if run.passed else EXIT_FAILED)


@app.command("log")
@_guarded
def log(
    limit: int = typer.Option(50, "--limit", help="How many of the newest changes to show."),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Show the audit log (every registered, indexed, proposed, applied step)."""
    settings = load_settings()
    _validate_limit(limit, "--limit", LOG_LIMIT_MAX)
    with _store(settings) as store:
        repo = _resolve(store, repo_path) if repo_path is not None else None
        changes = store.list_changes(repo_id=repo.id if repo else None, limit=limit)
        if not changes:
            _empty("nothing recorded yet — try: skraken init . && skraken index")
            return
        _ok(f"{len(changes)} change(s)")
        console.print(_change_table(changes))


@app.command("export")
@_guarded
def export_archive(
    destination: Path = typer.Argument(..., help="JSON file to write."),
) -> None:
    """Export repos, proposals, test runs and the audit log as JSON (no file contents)."""
    settings = load_settings()
    with _store(settings) as store:
        with _status("Exporting…"):
            counts = portability.export_data(store, destination)
        _ok(f"exported to {destination}")
        _note(_counts_line(counts))


@app.command("import")
@_guarded
def import_archive(
    source: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="JSON file to import."),
) -> None:
    """Import an export file (repos are matched by path; ids are remapped)."""
    settings = load_settings()
    with _store(settings) as store:
        with _status("Importing…"):
            counts = portability.import_data(store, source)
        _ok(f"imported from {source}")
        _note(_counts_line(counts))


def _counts_line(counts: dict[str, int]) -> str:
    if not counts:
        return "no rows"
    return ", ".join(f"{name}: {count} row(s)" for name, count in sorted(counts.items()))


@sample_app.command("load")
@_guarded
def sample_load() -> None:
    """Load the bundled sample repository (a real git repo under the data dir)."""
    settings = load_settings()
    with _store(settings) as store:
        with _status("Loading sample repository…"):
            repo = sample.load_sample(store, settings)
        _ok(f"sample repo {repo.id} loaded at {repo.path} ({sample.SAMPLE_LABEL} labelled)")
        _hint(f'skraken propose "raise an error on division by zero" --repo {repo.path}')


@sample_app.command("remove")
@_guarded
def sample_remove() -> None:
    """Remove the bundled sample repository and its rows."""
    settings = load_settings()
    with _store(settings) as store:
        with _status("Removing sample repository…"):
            removed = sample.remove_sample(store, settings)
        _ok(f"sample data removed ({removed} row(s))")


# --------------------------------------------------------------------------- #
# visualisation
# --------------------------------------------------------------------------- #
@app.command("graph")
@_guarded
def graph(
    limit: int = typer.Option(50, "--limit", help=f"Maximum commits to draw (1..{GRAPH_LIMIT_MAX})."),
    current_only: bool = typer.Option(
        False, "--current-only", help="Only the checked-out branch, instead of all refs."
    ),
    ascii_only: bool = typer.Option(
        False, "--ascii", help="ASCII-only glyphs (no box-drawing characters)."
    ),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Draw the repository commit graph with refs."""
    settings = load_settings()
    _validate_limit(limit, "--limit", GRAPH_LIMIT_MAX)
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        with _status("Reading commit history…"):
            renderable = views.render_graph(
                Path(repo.path), limit=limit, all_refs=not current_only, ascii_only=ascii_only
            )
        _note(f"{repo.name} ({repo.path})")
        console.print(renderable)


@app.command("branches")
@_guarded
def branches(
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """List local and remote branches with their position."""
    settings = load_settings()
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        with _status("Reading branches…"):
            renderable = views.render_branches(Path(repo.path))
        _note(f"{repo.name} ({repo.path})")
        console.print(renderable)


@app.command("tree")
@_guarded
def tree(
    depth: Optional[int] = typer.Option(None, "--depth", help=f"Limit directory depth (1..{TREE_DEPTH_MAX})."),
    proposal_id: Optional[int] = typer.Option(
        None, "--proposal", help="Mark the files this proposal touches."
    ),
    repo_path: Optional[Path] = _repo_option(),
) -> None:
    """Draw the repository file tree, optionally highlighting a proposal's files."""
    settings = load_settings()
    if depth is not None and (depth < 1 or depth > TREE_DEPTH_MAX):
        raise ValidationError(f"--depth must be between 1 and {TREE_DEPTH_MAX} (got {depth})")
    with _store(settings) as store:
        repo = _resolve(store, repo_path)
        repo_root = Path(repo.path)

        highlight: tuple[str, ...] = ()
        if proposal_id is not None:
            proposal = store.get_proposal(proposal_id)
            if proposal.repo_id != repo.id:
                raise ValidationError(
                    f"proposal {proposal.id} belongs to repo {proposal.repo_id}, "
                    f"not repo {repo.id} ({repo.name})"
                )
            highlight = tuple(diffs.touched_paths(proposal.diff))

        with _status("Reading files…"):
            files = [
                found
                for found in indexer.list_repo_files(repo_root)
                if not indexer.should_skip(found)
            ]
        if not files:
            _empty(
                f"no indexable files in repo {repo.id} ({repo.name}) — is it empty? "
                f"try: skraken index --repo {repo.path}"
            )
            return
        console.print(
            views.render_file_tree(repo.name, files, max_depth=depth, highlight=highlight)
        )
        if proposal_id is not None:
            _note(f"highlighting {len(highlight)} file(s) touched by proposal {proposal_id}")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
@app.command("doctor")
@_guarded
def doctor() -> None:
    """Check configuration, data dir, database, git and model availability (secrets never printed)."""
    problems = 0
    settings = load_settings()
    rows: list[tuple[str, str, str]] = []

    def add(label: str, value: str, state: str = "ok") -> None:
        nonlocal problems
        rows.append((label, value, state))
        if state == "failed":
            problems += 1

    data_dir = settings.data_dir
    add("data dir", str(data_dir))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(str(data_dir), os.W_OK)
        add("data dir writable", "yes" if writable else "no", "ok" if writable else "failed")
    except OSError as exc:
        add("data dir writable", f"no ({type(exc).__name__})", "failed")

    if settings.db_path.exists():
        add("database", f"exists ({settings.db_path.stat().st_size} bytes)")
    else:
        add("database", f"not created yet ({settings.db_path}) — created on first use", "info")

    try:
        with _store(settings) as store:
            try:
                row = store.conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                add("schema version", str(row[0]) if row else "unknown", "info" if not row else "ok")
            except sqlite3.Error as exc:
                add("schema version", f"unreadable ({type(exc).__name__})", "failed")
            counts = []
            for label, count in (
                ("repos", len(store.list_repos())),
                ("proposals", len(store.list_proposals())),
                ("test runs", len(store.list_test_runs())),
                ("changes", len(store.list_changes(limit=1000))),
            ):
                counts.append(f"{label}={count}")
            add("records", " ".join(counts))
            current = store.find_repo_by_path(Path.cwd())
            if current is None:
                add("current repo", f"{Path.cwd()} is not inside a registered repo", "info")
            else:
                add("current repo", f"{current.id} ({current.name})", "ok")
                add(
                    "test command",
                    current.test_command or "not set — skraken set-test \"python -m pytest -q\"",
                    "ok" if current.test_command else "info",
                )
    except SKError as exc:
        add("database", f"could not be opened ({_reason(exc)})", "failed")
    except sqlite3.Error as exc:
        add("database", f"could not be opened ({type(exc).__name__})", "failed")

    try:
        result = gitops.run_git(Path.cwd(), "--version", check=True)
        add("git", (result.stdout or "").strip() or "git ran but printed nothing")
    except (SKError, OSError) as exc:
        reason = _reason(exc) if isinstance(exc, SKError) else type(exc).__name__
        add("git", f"not available ({reason})", "failed")

    if settings.llm_enabled:
        host = urlparse(str(settings.llm_base_url)).netloc or str(settings.llm_base_url)
        add("model", f"configured ({settings.llm_model} @ {host})")
    else:
        add(
            "model",
            "degraded mode: no model configured — manual --diff-file proposals still work",
            "info",
        )
    add("api key", "set" if settings.llm_api_key else "not set", "info")
    add("llm timeout", f"{settings.llm_timeout} s", "info")
    add("max context chars", str(settings.max_context_chars), "info")
    add("test timeout", f"{settings.test_timeout} s", "info")

    table = Table(box=box.SIMPLE, title="skraken doctor")
    table.add_column("state", justify="left")
    table.add_column("check")
    table.add_column("value")
    for label, value, state in rows:
        table.add_row(state.upper(), label, _short(value, 120))
    console.print(table)

    if not settings.llm_enabled:
        console.print(DEGRADED_HELP)

    failing = [label for label, _value, state in rows if state == "failed"]
    if failing:
        _failed(f"doctor found problems: {', '.join(failing)}")
        raise typer.Exit(EXIT_FAILED)
    _ok("doctor: configuration looks usable" + (" (model degraded — manual mode only)" if not settings.llm_enabled else ""))


def main() -> None:
    """Entry point used by ``python -m selfkraken``."""
    app()


if __name__ == "__main__":
    main()
