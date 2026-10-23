"""Pure data for GitKraken-style terminal visualisations.

Reads commit history, branches and lane layout from a real git repository.
No rendering happens here — see ``selfkraken.views`` for rich renderables.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from selfkraken.errors import GitError

# Field / record separators for --format parsing. \x1f cannot appear in sane
# metadata; %s (subject) is the LAST field so it may contain anything except
# NUL without breaking the parse (we split with maxsplit).
_FS = "\x1f"
_RS = "\x00"


@dataclass
class Commit:
    """One commit as reported by ``git log``."""

    sha: str
    short: str
    parents: list[str]
    author: str
    date: str
    subject: str
    refs: list[str]


@dataclass
class Branch:
    """One local or remote branch with its divergence vs upstream (if any)."""

    name: str
    sha: str
    is_current: bool
    is_remote: bool
    ahead: int | None
    behind: int | None


@dataclass
class GraphRow:
    """A commit plus its assigned lane column and the lane state before/after.

    ``lanes_in``/``lanes_out`` list, per lane, the sha that lane is waiting
    for before and after this row (``None`` = free lane). Invariant:
    ``rows[i].lanes_out == rows[i + 1].lanes_in``.
    """

    commit: Commit
    column: int
    lanes_in: list[str | None]
    lanes_out: list[str | None]


def _git(repo_path: Path, *args: str, timeout: int = 30) -> str:
    """Run git in ``repo_path`` and return stdout; raise GitError on failure."""
    cmd = ["git", "-C", str(repo_path), "--no-pager", *args]
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}  # stable English git messages
    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git missing on PATH
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        first_line = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(first_line[0] if first_line else f"git {' '.join(args)} failed")
    return proc.stdout


def _is_empty_repo_error(exc: GitError) -> bool:
    text = str(exc).lower()
    return "does not have any commits yet" in text or "bad default revision" in text


def _parse_ref_entries(raw: str) -> list[str]:
    """Parse a ``%D`` decoration string like ``HEAD -> main, tag: v0.1.0``."""
    refs: list[str] = []
    for entry in raw.split(", "):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("HEAD -> "):
            refs.append("HEAD")
            refs.append(entry[len("HEAD -> "):].strip())
        elif entry.startswith("HEAD"):
            refs.append("HEAD")
        else:
            refs.append(entry)
    return refs


def read_commits(
    repo_path: Path,
    *,
    limit: int = 200,
    all_refs: bool = True,
    ref: str | None = None,
) -> list[Commit]:
    """Return commits newest-first in topological order (parents after children).

    Empty repository -> ``[]``. Not a git repository -> GitError.
    """
    fmt = "%x1f".join(["%H", "%h", "%P", "%ad", "%D", "%an", "%s"]) + "%x00"
    args = ["log"]
    if ref:
        # one branch's history, newest first
        args.append(str(ref))
    elif all_refs:
        args.append("--all")
    args += [
        "--topo-order",
        "--date=iso-strict",
        "--decorate=short",
        "--no-color",
        f"-n{max(1, int(limit))}",
        f"--format={fmt}",
    ]
    try:
        out = _git(repo_path, *args)
    except GitError as exc:
        if _is_empty_repo_error(exc):
            return []
        raise
    commits: list[Commit] = []
    for record in out.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FS, 6)
        if len(fields) != 7:  # pragma: no cover - defensive against odd formats
            continue
        sha, short, parents, date, decorations, author, subject = fields
        commits.append(
            Commit(
                sha=sha,
                short=short,
                parents=parents.split(),
                author=author,
                date=date,
                subject=subject,
                refs=_parse_ref_entries(decorations),
            )
        )
    return commits


def list_branches(repo_path: Path) -> list[Branch]:
    """List local and remote branches (``git for-each-ref refs/heads refs/remotes``)."""
    # for-each-ref does not expand %xNN escapes, but ref names, object names
    # and upstream names can never contain newlines or \x1f — plain lines work.
    fmt = _FS.join(["%(refname)", "%(refname:short)", "%(objectname)", "%(upstream:short)"])
    try:
        out = _git(repo_path, "for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes")
    except GitError as exc:
        if _is_empty_repo_error(exc):
            return []
        raise
    try:
        current = _git(repo_path, "symbolic-ref", "-q", "--short", "HEAD").strip()
    except GitError:
        current = None  # detached HEAD or empty repo: no current branch

    branches: list[Branch] = []
    for line in out.splitlines():
        if not line:
            continue
        fields = line.split(_FS)
        if len(fields) != 4:  # pragma: no cover - defensive
            continue
        full_name, short_name, sha, upstream = fields
        is_remote = full_name.startswith("refs/remotes/")
        ahead: int | None = None
        behind: int | None = None
        if upstream and not is_remote:
            try:
                counts = _git(
                    repo_path,
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{upstream}...{short_name}",
                ).strip()
                left, _, right = counts.partition("\t")
                behind, ahead = int(left), int(right)
            except GitError:  # upstream gone / unreachable
                pass
        branches.append(
            Branch(
                name=short_name,
                sha=sha,
                is_current=(not is_remote and short_name == current),
                is_remote=is_remote,
                ahead=ahead,
                behind=behind,
            )
        )
    branches.sort(key=lambda b: (b.is_remote, b.name.casefold()))
    return branches


def _find_lane(lanes: list[str | None], sha: str) -> int | None:
    for i, waiting in enumerate(lanes):
        if waiting == sha:
            return i
    return None


def layout(commits: list[Commit]) -> list[GraphRow]:
    """Assign lanes to commits (classic GitKraken/gitgraph lane algorithm).

    Input must be in topological order (children before parents), as produced
    by ``git log --topo-order``. Each commit takes the lane that is waiting
    for it (leftmost when several — the others merge into it and are freed);
    otherwise the leftmost free lane, or a new lane. The first parent always
    inherits the commit's own column (possibly tracked twice, converging when
    it renders), keeping a branch's first-parent chain in one column. Extra
    parents (merges) reuse the lane that already waits for that sha or take a
    free/new lane. Trailing empty lanes are trimmed.
    """
    rows: list[GraphRow] = []
    lanes: list[str | None] = []
    for commit in commits:
        lanes_in = list(lanes)
        waiting = [i for i, sha in enumerate(lanes_in) if sha == commit.sha]
        if waiting:
            column = waiting[0]
        else:
            free = next((i for i, sha in enumerate(lanes_in) if sha is None), None)
            column = len(lanes_in) if free is None else free

        state = list(lanes_in)
        while len(state) <= column:  # brand-new lane appended at the right
            state.append(None)
        # Consume this commit: free every lane that was waiting for it.
        for j in waiting:
            if j != column:
                state[j] = None
        state[column] = None
        freed_incoming = [j for j in waiting if j != column]

        parents = list(commit.parents)
        if parents:
            first = parents[0]
            # The first parent ALWAYS inherits the commit's own column, so a
            # branch's first-parent chain never migrates lanes. When another
            # lane already tracks `first` (two branches share this ancestor),
            # the sha is intentionally tracked twice: when `first` renders it
            # takes the leftmost waiting lane and the other lane is drawn
            # merging into it.
            state[column] = first
            for extra in parents[1:]:
                if _find_lane(state, extra) is not None:
                    continue  # a lane already carries this parent
                # Prefer plain free lanes over lanes just freed by an incoming
                # merge, so a lane never needs an in-edge and an out-edge in
                # the same row.
                candidate = next(
                    (i for i, sha in enumerate(state) if sha is None and i not in freed_incoming),
                    None,
                )
                if candidate is None:
                    candidate = next((i for i, sha in enumerate(state) if sha is None), None)
                if candidate is None:
                    state.append(extra)
                else:
                    state[candidate] = extra

        while state and state[-1] is None:
            state.pop()
        rows.append(GraphRow(commit=commit, column=column, lanes_in=lanes_in, lanes_out=state))
        lanes = state
    return rows
