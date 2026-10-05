"""Tests for selfkraken.portability (owner A)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from selfkraken.db import Repo, Store
from selfkraken.errors import ValidationError
from selfkraken.portability import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    MAX_IMPORT_BYTES,
    export_data,
    import_data,
)


@pytest.fixture()
def source(tmp_path: Path) -> Store:
    with Store(tmp_path / "source" / "selfkraken.db") as store:
        yield store


@pytest.fixture()
def target(tmp_path: Path) -> Store:
    with Store(tmp_path / "target" / "selfkraken.db") as store:
        yield store


def seed(store: Store, base: Path) -> tuple[Repo, int]:
    """One repo, an indexed file (never exported), a proposal, a run, audit rows."""
    project = base / "proj"
    project.mkdir(parents=True, exist_ok=True)
    repo = store.add_repo(project, "proj", test_command="python -m pytest -q", is_sample=False)
    store.replace_file_index(repo.id, "a.py", "deadbeef", 24, [(1, "chunkbody alpha secret")])
    proposal = store.create_proposal(
        repo.id,
        "make alpha louder",
        "model-x",
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-def alpha(): return 1\n+def alpha(): return 2\n",
        rationale="because",
        status="applied",
    )
    store.add_test_run(repo.id, proposal.id, "python -m pytest -q", 0, 1500, "1 passed")
    store.record_change("register", f"registered {repo.name}", repo_id=repo.id)
    store.record_change("propose", "proposed 1 change", repo_id=repo.id, proposal_id=proposal.id)
    store.record_change("commit", "committed globally")
    return repo, proposal.id


@pytest.fixture()
def exported(tmp_path: Path, source: Store) -> Path:
    seed(source, tmp_path / "repos")
    path = tmp_path / "out" / "sk-export.json"
    export_data(source, path)
    return path


# --------------------------------------------------------------------- export


def test_export_writes_document_and_counts(exported: Path) -> None:
    assert exported.is_file()
    document = json.loads(exported.read_text(encoding="utf-8"))
    assert document["format"] == EXPORT_FORMAT
    assert document["version"] == EXPORT_VERSION
    assert isinstance(document["exported_at"], str)
    assert set(document) == {
        "format",
        "version",
        "exported_at",
        "repos",
        "proposals",
        "test_runs",
        "changes",
    }
    assert len(document["repos"]) == 1
    assert len(document["proposals"]) == 1
    assert len(document["test_runs"]) == 1
    assert len(document["changes"]) == 3
    assert set(document["repos"][0]) == {
        "id",
        "path",
        "name",
        "test_command",
        "is_sample",
        "created_at",
    }
    assert document["repos"][0]["path"].startswith("/")
    assert document["proposals"][0]["status"] == "applied"


def test_export_returns_counts_and_records_change(source: Store, tmp_path: Path) -> None:
    seed(source, tmp_path / "repos")
    before = len(source.list_changes())
    counts = export_data(source, tmp_path / "export.json")
    assert counts == {"repos": 1, "proposals": 1, "test_runs": 1, "changes": 3}
    changes = source.list_changes()
    assert len(changes) == before + 1
    assert changes[0].kind == "export"
    assert source.list_changes()[0].detail  # metadata only, no private contents


def test_export_excludes_file_index_and_chunk_contents(exported: Path) -> None:
    text = exported.read_text(encoding="utf-8")
    document = json.loads(text)
    assert "files" not in document
    assert "chunks" not in document
    assert "chunkbody" not in text
    assert "deadbeef" not in text


def test_export_of_empty_store(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "empty.json"
    assert export_data(target, path) == {
        "repos": 0,
        "proposals": 0,
        "test_runs": 0,
        "changes": 0,
    }
    assert json.loads(path.read_text(encoding="utf-8"))["repos"] == []


# --------------------------------------------------------------------- import


def test_round_trip_into_fresh_store(exported: Path, target: Store) -> None:
    counts = import_data(target, exported)
    assert counts == {"repos": 1, "proposals": 1, "test_runs": 1, "changes": 3}

    repos = target.list_repos()
    assert len(repos) == 1
    repo = repos[0]
    source_repo = json.loads(exported.read_text(encoding="utf-8"))["repos"][0]
    assert repo.path == source_repo["path"]
    assert repo.name == source_repo["name"]
    assert repo.test_command == source_repo["test_command"]
    assert repo.is_sample is False
    assert repo.created_at == source_repo["created_at"]

    proposals = target.list_proposals()
    assert len(proposals) == 1
    assert proposals[0].repo_id == repo.id
    assert proposals[0].request == "make alpha louder"
    assert proposals[0].model == "model-x"
    assert proposals[0].rationale == "because"
    assert proposals[0].status == "applied"

    runs = target.list_test_runs()
    assert len(runs) == 1
    assert runs[0].repo_id == repo.id
    assert runs[0].proposal_id == proposals[0].id
    assert runs[0].passed is True
    assert runs[0].duration_ms == 1500

    changes = target.list_changes()
    assert [item.kind for item in changes][0] == "import"
    assert {item.kind for item in changes} == {"import", "register", "propose", "commit"}
    global_change = next(item for item in changes if item.kind == "commit")
    assert global_change.repo_id is None
    assert target.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_matches_repos_by_path_and_keeps_existing(
    exported: Path, target: Store, tmp_path: Path
) -> None:
    existing = seed(target, tmp_path / "elsewhere")
    document = json.loads(exported.read_text(encoding="utf-8"))
    document["repos"][0]["path"] = existing[0].path
    document["repos"][0]["name"] = "will be ignored"
    path = tmp_path / "adjusted.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    counts = import_data(target, path)
    assert counts["repos"] == 1
    assert len(target.list_repos()) == 1
    assert target.get_repo(existing[0].id).name == "proj"  # existing row untouched
    assert len(target.list_proposals()) == 2


def test_import_remaps_ids_when_repos_already_exist(
    exported: Path, target: Store, tmp_path: Path
) -> None:
    blocker = target.add_repo(tmp_path / "blocker", "blocker")
    target.create_proposal(blocker.id, "older request", "m", "older diff")
    counts = import_data(target, exported)
    assert counts["proposals"] == 1

    repo = next(item for item in target.list_repos() if item.name == "proj")
    assert repo.id != 1  # remapped because id 1 belongs to the blocker
    imported_proposal = next(item for item in target.list_proposals() if item.model == "model-x")
    assert imported_proposal.repo_id == repo.id
    run = target.list_test_runs()[0]
    assert run.repo_id == repo.id
    assert run.proposal_id == imported_proposal.id
    assert target.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_of_empty_document(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "nothing.json"
    path.write_text(
        json.dumps(
            {
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                "exported_at": "2026-01-01T00:00:00+00:00",
                "repos": [],
                "proposals": [],
                "test_runs": [],
                "changes": [],
            }
        ),
        encoding="utf-8",
    )
    assert import_data(target, path) == {
        "repos": 0,
        "proposals": 0,
        "test_runs": 0,
        "changes": 0,
    }
    assert target.list_repos() == []
    assert [item.kind for item in target.list_changes()] == ["import"]


def test_import_accepts_json_booleans_for_flags(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "bools.json"
    path.write_text(
        json.dumps(
            {
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                "exported_at": "2026-01-01T00:00:00+00:00",
                "repos": [
                    {
                        "id": 7,
                        "path": "/srv/project",
                        "name": "bool repo",
                        "test_command": None,
                        "is_sample": True,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "proposals": [],
                "test_runs": [],
                "changes": [],
            }
        ),
        encoding="utf-8",
    )
    import_data(target, path)
    assert target.list_repos()[0].is_sample is True


# ----------------------------------------------------------------- rejections


CASES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("wrong format", lambda d: d.update(format="selfkraken-other"), "format"),
    ("newer version", lambda d: d.update(version=2), "version"),
    ("string version", lambda d: d.update(version="1"), "version"),
    ("boolean version", lambda d: d.update(version=True), "version"),
    ("missing format", lambda d: d.pop("format"), "format"),
    ("missing exported_at", lambda d: d.pop("exported_at"), "exported_at"),
    ("bad exported_at", lambda d: d.update(exported_at="yesterday"), "exported_at"),
    ("missing section", lambda d: d.pop("repos"), "repos"),
    ("section not a list", lambda d: d.update(repos={}), "repos"),
    ("row not an object", lambda d: d.update(repos=["oops"]), "repos[0]"),
    ("missing required field", lambda d: d["proposals"][0].pop("diff"), "diff"),
    ("unknown field", lambda d: d["repos"][0].update(hacker="x"), "hacker"),
    ("id wrong type", lambda d: d["repos"][0].update(id="1"), "id"),
    ("id zero", lambda d: d["repos"][0].update(id=0), "id"),
    ("flag out of range", lambda d: d["repos"][0].update(is_sample=2), "is_sample"),
    ("name not a string", lambda d: d["repos"][0].update(name=5), "name"),
    ("empty name", lambda d: d["repos"][0].update(name="   "), "name"),
    ("bad status", lambda d: d["proposals"][0].update(status="merged"), "status"),
    ("bad kind", lambda d: d["changes"][0].update(kind="teleport"), "kind"),
    ("relative repo path", lambda d: d["repos"][0].update(path="srv/project"), "path"),
    ("escaping repo path", lambda d: d["repos"][0].update(path="/srv/../etc"), "path"),
    ("nul in text", lambda d: d["proposals"][0].update(request="a\x00b"), "request"),
    ("bad timestamp", lambda d: d["repos"][0].update(created_at="soon"), "created_at"),
    ("negative duration", lambda d: d["test_runs"][0].update(duration_ms=-4), "duration_ms"),
    ("exit code wrong type", lambda d: d["test_runs"][0].update(exit_code="0"), "exit_code"),
    ("unknown repo reference", lambda d: d["proposals"][0].update(repo_id=999), "repo_id"),
    ("unknown proposal reference", lambda d: d["test_runs"][0].update(proposal_id=999), "proposal_id"),
    ("duplicate repo id", lambda d: d["repos"].append(dict(d["repos"][0])), "id"),
    ("duplicate repo path", lambda d: d["repos"].append(dict(d["repos"][0], id=99)), "path"),
]


@pytest.mark.parametrize(
    ("label", "mutate", "needle"), CASES, ids=[case[0] for case in CASES]
)
def test_import_rejects_bad_documents(
    exported: Path, target: Store, tmp_path: Path, label: str, mutate: Any, needle: str
) -> None:
    document = json.loads(exported.read_text(encoding="utf-8"))
    mutate(document)
    bad = tmp_path / f"bad-{abs(hash(label))}.json"
    bad.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError) as excinfo:
        import_data(target, bad)
    assert needle in str(excinfo.value)
    # a rejected file leaves no trace at all
    assert target.list_repos() == []
    assert target.list_proposals() == []
    assert target.list_test_runs() == []
    assert target.list_changes() == []


def test_import_rejects_non_object_root(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        import_data(target, path)
    assert "JSON object" in str(excinfo.value)


def test_import_rejects_invalid_json(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"format": "selfkraken-export", ', encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        import_data(target, path)
    assert "not valid JSON" in str(excinfo.value)


def test_import_rejects_non_utf8(tmp_path: Path, target: Store) -> None:
    path = tmp_path / "binary.json"
    path.write_bytes(b"\xff\xfe\x00\x01\x02")
    with pytest.raises(ValidationError):
        import_data(target, path)


def test_import_rejects_missing_file(tmp_path: Path, target: Store) -> None:
    with pytest.raises(ValidationError):
        import_data(target, tmp_path / "nope.json")


def test_import_rejects_oversized_file(tmp_path: Path, target: Store) -> None:
    assert MAX_IMPORT_BYTES == 50 * 1024 * 1024
    path = tmp_path / "huge.json"
    with path.open("wb") as handle:
        handle.truncate(MAX_IMPORT_BYTES + 1)
    with pytest.raises(ValidationError) as excinfo:
        import_data(target, path)
    assert "too large" in str(excinfo.value)
    assert target.list_changes() == []


def test_failed_import_inside_transaction_rolls_back(exported: Path, target: Store, tmp_path: Path) -> None:
    document = json.loads(exported.read_text(encoding="utf-8"))
    document["proposals"][0]["repo_id"] = 4242  # passes row checks, breaks the mapping
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValidationError):
        import_data(target, path)
    assert target.list_repos() == []
    assert target.list_changes() == []


def test_sqlite_failure_rolls_back_whole_import(
    exported: Path, target: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(target, "record_change", explode)
    with pytest.raises(ValidationError):
        import_data(target, exported)
    assert target.list_repos() == []
    assert target.list_proposals() == []
    assert target.list_test_runs() == []
    assert target.list_changes() == []


def test_export_to_a_path_blocked_by_a_file(tmp_path: Path, target: Store) -> None:
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        export_data(target, blocker / "export.json")
