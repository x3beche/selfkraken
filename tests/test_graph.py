"""Tests for selfkraken.graph: commit reading, branch listing, lane layout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fixtures_git import make_criss_cross_repo, make_multibranch_repo, make_octopus_repo
from selfkraken.errors import GitError
from selfkraken.graph import layout, list_branches, read_commits

BUILDERS = [make_multibranch_repo, make_octopus_repo, make_criss_cross_repo]
IDS = ["multibranch", "octopus", "criss-cross"]


@pytest.fixture(params=BUILDERS, ids=IDS)
def demo_repo(request, tmp_path: Path) -> Path:
    return request.param(tmp_path / "repo")


def _make_empty(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-b", "main"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return path


# ------------------------------------------------------------------ read_commits


def test_commit_fields(demo_repo: Path) -> None:
    commits = read_commits(demo_repo)
    assert commits
    for commit in commits:
        assert len(commit.sha) == 40
        assert len(commit.short) >= 7
        assert commit.sha.startswith(commit.short)
        assert commit.author
        assert commit.subject
        assert commit.date  # iso-strict, kept verbatim


def test_every_commit_appears_exactly_once(demo_repo: Path) -> None:
    commits = read_commits(demo_repo)
    rows = layout(commits)
    assert len(rows) == len(commits)
    assert len({row.commit.sha for row in rows}) == len(rows)
    assert [row.commit.sha for row in rows] == [commit.sha for commit in commits]


def test_rows_topologically_ordered(demo_repo: Path) -> None:
    commits = read_commits(demo_repo)
    index = {commit.sha: i for i, commit in enumerate(commits)}
    for i, commit in enumerate(commits):
        for parent in commit.parents:
            assert parent in index, f"{commit.subject}: parent {parent} missing (--all/limit?)"
            assert index[parent] > i, f"parent {parent} listed before child {commit.subject}"


def test_limit_truncates(demo_repo: Path) -> None:
    limited = read_commits(demo_repo, limit=3)
    assert len(limited) == 3
    assert limited == read_commits(demo_repo)[:3]


def test_all_refs_false_head_only(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    head_only = read_commits(repo, all_refs=False)
    everything = read_commits(repo, all_refs=True)
    assert len(head_only) < len(everything)
    # the abandoned experiment is not reachable from HEAD
    cache_tip = {c.sha for c in everything if c.subject == "Draft cache layer"}
    assert cache_tip and not (cache_tip & {c.sha for c in head_only})


# ----------------------------------------------------------------------- layout


def test_lane_state_carries_across_rows(demo_repo: Path) -> None:
    rows = layout(read_commits(demo_repo))
    for before, after in zip(rows, rows[1:]):
        assert before.lanes_out == after.lanes_in, before.commit.subject
    for row in rows:
        waiting = [s for s in row.lanes_out if s]
        assert len(waiting) == len(set(waiting)), "one sha tracked by two lanes"


def test_column_valid(demo_repo: Path) -> None:
    for row in layout(read_commits(demo_repo)):
        assert row.column >= 0
        assert row.column <= max(len(row.lanes_in), len(row.lanes_out))
        if row.column < len(row.lanes_in):
            assert row.lanes_in[row.column] in (None, row.commit.sha)


def test_merges_connect_every_parent_lane(demo_repo: Path) -> None:
    rows = layout(read_commits(demo_repo))
    merges = [row for row in rows if len(row.commit.parents) >= 2]
    assert merges, "fixture should contain merge commits"
    for row in merges:
        for parent in row.commit.parents:
            assert parent in row.lanes_out, f"{row.commit.subject}: parent lane missing"


def test_merge_labelled_commits_have_two_parents(demo_repo: Path) -> None:
    for commit in read_commits(demo_repo):
        if commit.subject.startswith("Merge"):
            assert len(commit.parents) >= 2, commit.subject


def test_lane_count_stays_bounded(demo_repo: Path) -> None:
    rows = layout(read_commits(demo_repo))
    lanes = max(max(len(r.lanes_in), len(r.lanes_out)) for r in rows)
    # multibranch peaks at 3, octopus at 4: well under a generous bound
    assert lanes <= 6


def test_children_converge_into_leftmost_lane(demo_repo: Path) -> None:
    for row in layout(read_commits(demo_repo)):
        waiting = [i for i, s in enumerate(row.lanes_in) if s == row.commit.sha]
        if waiting:
            assert row.column == waiting[0]


# -------------------------------------------------------------------- octopus/criss


def test_octopus_merge_shape(tmp_path: Path) -> None:
    repo = make_octopus_repo(tmp_path / "repo")
    commits = read_commits(repo)
    rows = layout(commits)
    octo = [c for c in commits if len(c.parents) >= 4]
    assert len(octo) == 1, "exactly one octopus merge expected"
    row = next(r for r in rows if r.commit is octo[0])
    assert all(p in row.lanes_out for p in octo[0].parents)
    assert max(len(r.lanes_out) for r in rows) >= 4


def test_criss_cross_has_merge_of_merges(tmp_path: Path) -> None:
    repo = make_criss_cross_repo(tmp_path / "repo")
    commits = read_commits(repo)
    by_sha = {c.sha: c for c in commits}
    merge_shas = {c.sha for c in commits if len(c.parents) >= 2}
    assert any(
        any(p in merge_shas for p in c.parents)
        for c in commits
        if len(c.parents) >= 2
    ), "criss-cross should have a merge whose parent is itself a merge"
    assert len(merge_shas) >= 4


# ------------------------------------------------------------------ list_branches


def test_branch_listing_multibranch(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    branches = list_branches(repo)
    locals_ = {b.name for b in branches if not b.is_remote}
    remotes = {b.name for b in branches if b.is_remote}
    assert {
        "main",
        "feature/login",
        "feature/search",
        "hotfix/div-zero",
        "experiment/cache",
    } <= locals_
    assert "origin/main" in remotes

    current = [b for b in branches if b.is_current]
    assert len(current) == 1
    assert current[0].name == "main"
    assert all(not b.is_current for b in branches if b.is_remote)
    assert all(b.ahead is None and b.behind is None for b in branches if b.is_remote)


def test_ahead_behind_vs_upstream(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    main = next(b for b in list_branches(repo) if b.name == "main")
    assert main.ahead == 1
    assert main.behind == 0
    # branches without upstream tracking report None
    login = next(b for b in list_branches(repo) if b.name == "feature/login")
    assert login.ahead is None and login.behind is None


def test_unmerged_branch_not_in_head_history(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    branches = {b.name: b for b in list_branches(repo)}
    experiment = branches["experiment/cache"]
    head_shas = {c.sha for c in read_commits(repo, all_refs=False)}
    assert experiment.sha not in head_shas
    assert not experiment.is_current


def test_tag_present_in_refs(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    tagged = [c for c in read_commits(repo) if any(r.startswith("tag:") for r in c.refs)]
    assert any("v0.1.0" in r for c in tagged for r in c.refs)
    # HEAD -> main must appear on exactly one commit
    head_rows = [c for c in read_commits(repo) if "HEAD" in c.refs]
    assert len(head_rows) == 1
    assert "main" in head_rows[0].refs


# --------------------------------------------------------------------------- misc


def test_empty_repo(tmp_path: Path) -> None:
    empty = _make_empty(tmp_path / "empty")
    assert read_commits(empty) == []
    assert list_branches(empty) == []
    assert layout([]) == []


def test_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError):
        read_commits(plain)
    with pytest.raises(GitError):
        list_branches(plain)
