"""Tests for selfkraken.db (owner A)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from selfkraken import db as db_module
from selfkraken.db import (
    CHANGE_KINDS,
    PROPOSAL_STATUSES,
    SCHEMA_VERSION,
    Change,
    Chunk,
    Proposal,
    Repo,
    Store,
)
from selfkraken.errors import NotFoundError, ValidationError

# TestRun is reached through the module: a module-level name starting with "Test"
# makes pytest try to collect the dataclass as a test class.


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    return tmp_path / "work"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "db" / "selfkraken.db") as instance:
        yield instance


@pytest.fixture()
def repo(store: Store, base: Path) -> Repo:
    (base / "proj").mkdir(parents=True)
    return store.add_repo(base / "proj", "proj", test_command="python -m pytest -q")


# --------------------------------------------------------------------- schema


def test_schema_version_and_pragmas(store: Store) -> None:
    assert store.meta_get("schema_version") == SCHEMA_VERSION == "1"
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {
        str(row["name"])
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"meta", "repos", "files", "chunks", "proposals", "test_runs", "changes"} <= tables


def test_reopening_keeps_data_and_version(tmp_path: Path, base: Path) -> None:
    db_path = tmp_path / "nested" / "selfkraken.db"
    with Store(db_path) as first:
        assert first.db_path.parent == db_path.parent and db_path.is_file()
        (base / "proj").mkdir(parents=True)
        added = first.add_repo(base / "proj", "proj")
    with Store(db_path) as second:
        assert second.meta_get("schema_version") == SCHEMA_VERSION
        assert second.get_repo(added.id).name == "proj"


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    with Store(db_path) as store:
        store.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        store.conn.commit()
    with pytest.raises(ValidationError):
        Store(db_path)


def test_fts5_flag_matches_backend(store: Store) -> None:
    sql = str(
        store.conn.execute("SELECT sql FROM sqlite_master WHERE name='chunks'").fetchone()["sql"]
    )
    assert store.fts5 is ("fts5" in sql.lower())
    assert store.fts5 is True  # this SQLite build has FTS5, so the real backend is used


def test_database_built_without_fts5_uses_like_search(tmp_path: Path) -> None:
    """A pre-existing plain chunks table (no FTS5 build) keeps working."""
    import sqlite3

    db_path = tmp_path / "plain.db"
    connection = sqlite3.connect(db_path)
    connection.execute(db_module._CHUNKS_PLAIN_SQL)
    connection.commit()
    connection.close()

    with Store(db_path) as store:
        assert store.fts5 is False
        repo = store.add_repo(tmp_path / "proj", "proj")
        store.replace_file_index(repo.id, "a.py", "sha", 4, [(1, "alpha beta"), (9, "gamma")])
        assert [hit.path for hit in store.search_chunks(repo.id, "beta")] == ["a.py"]
        assert store.search_chunks(repo.id, "gamma")[0].start_line == 9
        assert store.remove_files_not_in(repo.id, {"a.py"}) == 0
        assert store.delete_sample_data() == 0


# ---------------------------------------------------------------------- repos


def test_add_repo_resolves_path(store: Store, base: Path) -> None:
    target = base / "proj"
    target.mkdir(parents=True)
    added = store.add_repo(target, "  my project  ")
    assert added.path == str(target.resolve())
    assert added.name == "my project"
    assert added.is_sample is False
    assert isinstance(added.is_sample, bool)
    assert added.test_command is None
    assert datetime.fromisoformat(added.created_at)


def test_add_repo_duplicate_path_rejected(store: Store, base: Path) -> None:
    target = base / "proj"
    target.mkdir(parents=True)
    store.add_repo(target, "first")
    with pytest.raises(ValidationError) as excinfo:
        store.add_repo(target, "second")
    assert "already registered" in str(excinfo.value)


def test_add_repo_empty_name_rejected(store: Store, base: Path) -> None:
    with pytest.raises(ValidationError):
        store.add_repo(base / "proj", "   ")


def test_get_repo_missing_raises_not_found(store: Store) -> None:
    with pytest.raises(NotFoundError):
        store.get_repo(4242)


def test_find_repo_by_path_matches_self_and_ancestors(store: Store, base: Path) -> None:
    project = base / "proj"
    (project / "src" / "pkg").mkdir(parents=True)
    added = store.add_repo(project, "proj")
    for probe in (project, project / "src", project / "src" / "pkg" / "main.py"):
        found = store.find_repo_by_path(probe)
        assert found is not None and found.id == added.id
    assert store.find_repo_by_path(base) is None
    assert store.find_repo_by_path(base / "unrelated") is None


def test_find_repo_by_path_prefers_most_specific(store: Store, base: Path) -> None:
    outer = store.add_repo(base / "proj", "outer")
    inner = store.add_repo(base / "proj" / "nested", "inner")
    deep = store.find_repo_by_path(base / "proj" / "nested" / "deep" / "file.py")
    assert deep is not None and deep.id == inner.id
    shallow = store.find_repo_by_path(base / "proj" / "file.py")
    assert shallow is not None and shallow.id == outer.id


def test_find_repo_by_path_is_not_a_string_prefix_match(store: Store, base: Path) -> None:
    store.add_repo(base / "proj", "proj")
    assert store.find_repo_by_path(base / "proj-extra" / "file.py") is None


def test_list_repos_sorted_by_id(store: Store, base: Path) -> None:
    first = store.add_repo(base / "a", "a")
    second = store.add_repo(base / "b", "b")
    assert [item.id for item in store.list_repos()] == [first.id, second.id]


def test_set_test_command(store: Store, repo: Repo) -> None:
    assert store.set_test_command(repo.id, "make test").test_command == "make test"
    assert store.set_test_command(repo.id, None).test_command is None
    assert store.set_test_command(repo.id, "   ").test_command is None
    with pytest.raises(NotFoundError):
        store.set_test_command(9999, "x")


def test_delete_repo_cascades(store: Store, repo: Repo) -> None:
    proposal = store.create_proposal(repo.id, "request", "model", "--- a\n+++ b\n")
    store.replace_file_index(repo.id, "a.py", "sha", 3, [(1, "alpha"), (31, "beta")])
    store.add_test_run(repo.id, proposal.id, "pytest", 0, 5, "ok")
    store.record_change("register", "registered", repo_id=repo.id)
    store.delete_repo(repo.id)
    with pytest.raises(NotFoundError):
        store.get_repo(repo.id)
    assert store.list_repos() == []
    assert store.list_proposals() == []
    assert store.list_test_runs() == []
    assert store.list_changes() == []
    assert store.count_files(repo.id) == 0
    assert store.search_chunks(repo.id, "alpha") == []


# ---------------------------------------------------------------------- index


def test_replace_file_index_updates_and_replaces_chunks(store: Store, repo: Repo) -> None:
    store.replace_file_index(repo.id, "a.py", "sha-1", 10, [(1, "alpha beta"), (21, "gamma")])
    assert store.count_files(repo.id) == 1
    assert store.get_file_shas(repo.id) == {"a.py": "sha-1"}
    assert len(store.search_chunks(repo.id, "gamma")) == 1

    store.replace_file_index(repo.id, "a.py", "sha-2", 12, [(1, "delta only")])
    assert store.count_files(repo.id) == 1
    assert store.get_file_shas(repo.id) == {"a.py": "sha-2"}
    assert store.search_chunks(repo.id, "gamma") == []
    hits = store.search_chunks(repo.id, "delta")
    assert [hit.path for hit in hits] == ["a.py"]
    assert hits[0].start_line == 1
    assert isinstance(hits[0], Chunk)


def test_replace_file_index_requires_repo_and_path(store: Store, repo: Repo) -> None:
    with pytest.raises(ValidationError):
        store.replace_file_index(repo.id, "  ", "sha", 1, [])
    with pytest.raises(NotFoundError):
        store.replace_file_index(9999, "a.py", "sha", 1, [])


def test_remove_files_not_in(store: Store, repo: Repo) -> None:
    store.replace_file_index(repo.id, "keep.py", "sha-a", 5, [(1, "keepme")])
    store.replace_file_index(repo.id, "drop.py", "sha-b", 5, [(1, "dropme")])
    assert store.count_files(repo.id) == 2
    assert store.remove_files_not_in(repo.id, {"keep.py"}) == 1
    assert store.get_file_shas(repo.id) == {"keep.py": "sha-a"}
    assert store.search_chunks(repo.id, "dropme") == []
    assert store.remove_files_not_in(repo.id, {"keep.py"}) == 0
    assert store.remove_files_not_in(repo.id, set()) == 1
    assert store.count_files(repo.id) == 0


def test_search_chunks_is_scoped_to_repo(store: Store, base: Path) -> None:
    one = store.add_repo(base / "one", "one")
    two = store.add_repo(base / "two", "two")
    store.replace_file_index(one.id, "a.py", "sha", 4, [(1, "shared token")])
    store.replace_file_index(two.id, "a.py", "sha", 4, [(1, "shared token")])
    hits = store.search_chunks(one.id, "shared")
    assert len(hits) == 1
    assert store.count_files(one.id) == 1


def test_search_chunks_tokenises_and_limits(store: Store, repo: Repo) -> None:
    for line in range(1, 12):
        store.replace_file_index(repo.id, f"f{line}.py", "sha", 4, [(1, f"needle file{line}")])
    hits = store.search_chunks(repo.id, "needle", limit=3)
    assert len(hits) == 3
    assert store.search_chunks(repo.id, "needle", limit=0) == []
    # every token is quoted and OR-joined: a multi-word query matches any of them
    assert len(store.search_chunks(repo.id, "needle file5")) >= 1


@pytest.mark.parametrize(
    "query",
    [
        'foo" OR 1=1 --',
        'alpha" OR "beta',
        '"',
        "*",
        "a AND",
        "NOT alpha",
        "NEAR(alpha beta)",
        "^alpha",
        "-alpha",
        "alpha:c",
        "()",
        ";;; drop table repos; --",
        "   ",
        "",
        "1=1",
    ],
)
def test_search_chunks_never_crashes_on_hostile_input(store: Store, repo: Repo, query: str) -> None:
    store.replace_file_index(repo.id, "a.py", "sha", 6, [(1, "alpha beta gamma")])
    result = store.search_chunks(repo.id, query)
    assert isinstance(result, list)
    if not query.strip():
        assert result == []
    if query == 'foo" OR 1=1 --':
        # tokens foo / OR / 1 are quoted terms: none is in the indexed text
        assert result == []
    if query == "alpha:c":
        assert [hit.path for hit in result] == ["a.py"]


def test_search_chunks_like_fallback(store: Store, repo: Repo) -> None:
    store.replace_file_index(
        repo.id, "a.py", "sha", 6, [(1, "alpha BETA"), (11, "gamma only"), (21, "alpha and gamma")]
    )
    store.fts5 = False
    assert [hit.path for hit in store.search_chunks(repo.id, "beta")] == ["a.py"]
    best = store.search_chunks(repo.id, "alpha gamma")
    assert best[0].score == 2.0
    assert best[0].start_line == 21
    assert store.search_chunks(repo.id, "zzz") == []
    # '_' and '%' are LIKE wildcards; they must be escaped, not matched everywhere
    assert store.search_chunks(repo.id, "50% _off") == []


def test_search_chunks_fts_backend_and_score(store: Store, repo: Repo) -> None:
    store.replace_file_index(repo.id, "a.py", "sha", 6, [(1, "alpha beta")])
    assert store.fts5 is True
    hits = store.search_chunks(repo.id, "alpha")
    assert isinstance(hits[0].score, float)


# ------------------------------------------------------------------ proposals


def test_create_proposal_defaults(store: Store, repo: Repo) -> None:
    proposal = store.create_proposal(repo.id, "make it faster", "test-model", "+hello\n")
    assert isinstance(proposal, Proposal)
    assert proposal.repo_id == repo.id
    assert proposal.status == "proposed"
    assert proposal.rationale == ""
    assert proposal.is_sample is False
    assert proposal.created_at == proposal.updated_at
    assert datetime.fromisoformat(proposal.updated_at)


def test_create_proposal_validates(store: Store, repo: Repo) -> None:
    with pytest.raises(ValidationError):
        store.create_proposal(repo.id, "request", "model", "diff", status="merged")
    with pytest.raises(ValidationError):
        store.create_proposal(repo.id, "  ", "model", "diff")
    with pytest.raises(ValidationError):
        store.create_proposal(repo.id, "request", "model", "   ")
    with pytest.raises(NotFoundError):
        store.create_proposal(9999, "request", "model", "diff")


def test_get_proposal_missing(store: Store) -> None:
    with pytest.raises(NotFoundError):
        store.get_proposal(77)


def test_list_proposals_newest_first_and_filtered(store: Store, base: Path) -> None:
    one = store.add_repo(base / "one", "one")
    two = store.add_repo(base / "two", "two")
    first = store.create_proposal(one.id, "r1", "m", "d1")
    second = store.create_proposal(two.id, "r2", "m", "d2", status="applied")
    third = store.create_proposal(one.id, "r3", "m", "d3", status="rejected")

    assert [item.id for item in store.list_proposals()] == [third.id, second.id, first.id]
    assert [item.id for item in store.list_proposals(repo_id=one.id)] == [third.id, first.id]
    assert [item.id for item in store.list_proposals(status="applied")] == [second.id]
    assert store.list_proposals(repo_id=two.id, status="rejected") == []
    with pytest.raises(ValidationError):
        store.list_proposals(status="bogus")


def test_set_proposal_status(store: Store, repo: Repo) -> None:
    proposal = store.create_proposal(repo.id, "request", "model", "diff")
    for status in PROPOSAL_STATUSES:
        updated = store.set_proposal_status(proposal.id, status)
        assert updated.status == status
    with pytest.raises(ValidationError):
        store.set_proposal_status(proposal.id, "cancelled")
    with pytest.raises(NotFoundError):
        store.set_proposal_status(9999, "applied")


def test_status_and_kind_lists_are_exact() -> None:
    assert PROPOSAL_STATUSES == (
        "proposed",
        "applied",
        "tests_passed",
        "tests_failed",
        "rejected",
        "reverted",
    )
    assert CHANGE_KINDS[:4] == ("register", "index", "propose", "propose_failed")
    assert {"import", "export", "sample_load", "sample_remove"} <= set(CHANGE_KINDS)


# ------------------------------------------------------------------ test runs


def test_add_test_run_passed_flag(store: Store, repo: Repo) -> None:
    proposal = store.create_proposal(repo.id, "request", "model", "diff")
    green = store.add_test_run(repo.id, proposal.id, "python -m pytest -q", 0, 120, "1 passed")
    red = store.add_test_run(repo.id, proposal.id, "python -m pytest -q", 1, 90, "1 failed")
    assert isinstance(green, db_module.TestRun)
    assert green.passed is True
    assert red.passed is False
    assert red.exit_code == 1
    assert green.proposal_id == proposal.id
    lone = store.add_test_run(repo.id, None, "make test", 127, 3, "missing")
    assert lone.proposal_id is None


def test_add_test_run_validates_references(store: Store, repo: Repo) -> None:
    with pytest.raises(NotFoundError):
        store.add_test_run(repo.id, 9999, "cmd", 0, 1, "")
    with pytest.raises(NotFoundError):
        store.add_test_run(9999, None, "cmd", 0, 1, "")


def test_list_test_runs_filters(store: Store, base: Path) -> None:
    one = store.add_repo(base / "one", "one")
    two = store.add_repo(base / "two", "two")
    proposal = store.create_proposal(one.id, "r", "m", "d")
    first = store.add_test_run(one.id, proposal.id, "cmd", 0, 1, "ok")
    second = store.add_test_run(two.id, None, "cmd", 1, 1, "no")
    assert [item.id for item in store.list_test_runs()] == [second.id, first.id]
    assert [item.id for item in store.list_test_runs(repo_id=one.id)] == [first.id]
    assert [item.id for item in store.list_test_runs(proposal_id=proposal.id)] == [first.id]


# ---------------------------------------------------------------------- audit


def test_record_change(store: Store, repo: Repo) -> None:
    change = store.record_change("index", "indexed 12 files", repo_id=repo.id)
    assert isinstance(change, Change)
    assert change.kind == "index"
    assert change.repo_id == repo.id
    assert change.proposal_id is None
    global_change = store.record_change("import", "imported a file")
    assert global_change.repo_id is None


def test_record_change_validates(store: Store, repo: Repo) -> None:
    with pytest.raises(ValidationError):
        store.record_change("teleport", "nope")
    with pytest.raises(NotFoundError):
        store.record_change("index", "nope", repo_id=9999)
    with pytest.raises(NotFoundError):
        store.record_change("index", "nope", proposal_id=9999)


def test_record_change_accepts_every_kind(store: Store) -> None:
    for kind in CHANGE_KINDS:
        assert store.record_change(kind, f"{kind} detail").kind == kind


def test_list_changes_newest_first_and_limited(store: Store, base: Path) -> None:
    one = store.add_repo(base / "one", "one")
    two = store.add_repo(base / "two", "two")
    for index in range(4):
        store.record_change("index", f"one {index}", repo_id=one.id)
    store.record_change("index", "two zero", repo_id=two.id)
    everything = store.list_changes()
    assert len(everything) == 5
    assert everything[0].detail == "two zero"
    assert [item.detail for item in store.list_changes(repo_id=one.id)] == [
        "one 3",
        "one 2",
        "one 1",
        "one 0",
    ]
    assert len(store.list_changes(limit=2)) == 2
    assert store.list_changes(limit=0) == []


# --------------------------------------------------------------------- sample


def test_delete_sample_data_only_touches_sample(store: Store, repo: Repo, base: Path) -> None:
    real_proposal = store.create_proposal(repo.id, "keep me", "model", "diff")
    store.add_test_run(repo.id, real_proposal.id, "cmd", 0, 5, "ok")
    store.record_change("register", "real repo", repo_id=repo.id)

    sample = store.add_repo(base / "sample-repo", "[SAMPLE] calculator", is_sample=True)
    store.replace_file_index(sample.id, "calc.py", "sha", 8, [(1, "def add"), (7, "def div")])
    sample_proposal = store.create_proposal(
        sample.id, "sample request", "sample", "diff", is_sample=True
    )
    store.add_test_run(sample.id, sample_proposal.id, "cmd", 1, 5, "bad")
    store.record_change("sample_load", "loaded sample", repo_id=sample.id)
    store.record_change("propose", "proposed sample", repo_id=sample.id)

    removed = store.delete_sample_data()
    assert removed == 1 + 1 + 2 + 1 + 1 + 2  # repo, file, chunks, proposal, run, changes
    assert store.list_repos() == [repo]
    assert [item.id for item in store.list_proposals()] == [real_proposal.id]
    assert store.count_files(repo.id) == 0
    assert len(store.list_test_runs()) == 1
    assert [item.detail for item in store.list_changes()] == ["real repo"]


def test_delete_sample_data_removes_sample_proposal_on_real_repo(store: Store, repo: Repo) -> None:
    proposal = store.create_proposal(
        repo.id, "sample request", "sample", "diff", is_sample=True
    )
    store.record_change("propose", "sample proposal", proposal_id=proposal.id)
    assert store.delete_sample_data() == 2  # the change + the proposal
    assert store.list_proposals() == []
    assert store.list_changes() == []
    assert store.list_repos() == [repo]


def test_delete_sample_data_on_empty_db(store: Store) -> None:
    assert store.delete_sample_data() == 0


# ---------------------------------------------------------------- transaction


def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.record_change("index", "should not survive")
            raise RuntimeError("boom")
    assert store.list_changes() == []


def test_nested_transaction_rolls_back_with_the_outer_one(store: Store) -> None:
    with pytest.raises(ValidationError, match="aborted"):
        with store.transaction():
            store.record_change("index", "outer")
            with store.transaction():
                store.record_change("index", "inner")
            raise ValidationError("aborted")
    assert store.list_changes() == []


def test_transaction_commits_records_written_inside_it(store: Store, repo: Repo) -> None:
    with store.transaction():
        store.record_change("index", "one", repo_id=repo.id)
        store.record_change("index", "two", repo_id=repo.id)
    assert [item.detail for item in store.list_changes()] == ["two", "one"]
    with Store(store.db_path) as reopened:
        assert len(reopened.list_changes()) == 2
