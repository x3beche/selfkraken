"""JSON export / import of the selfkraken database (see CONTRACT.md §Import / export).

Export writes every row of ``repos``, ``proposals``, ``test_runs`` and ``changes``
as a JSON object per row.  The file index and chunk contents are deliberately not
exported: they are rebuildable from the working tree and hold private contents.

Import treats the file as untrusted input: strict format/version/type/required
field/status/kind checks, a 50 MB size ceiling, id remapping, and a single
transaction that rolls back completely on any error.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from .db import CHANGE_KINDS, PROPOSAL_STATUSES, Store, utc_now
from .errors import ValidationError

EXPORT_FORMAT: Final[str] = "selfkraken-export"
EXPORT_VERSION: Final[int] = 1
MAX_IMPORT_BYTES: Final[int] = 50 * 1024 * 1024

TABLES: Final[tuple[str, ...]] = ("repos", "proposals", "test_runs", "changes")

COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "repos": ("id", "path", "name", "test_command", "is_sample", "created_at"),
    "proposals": (
        "id",
        "repo_id",
        "request",
        "model",
        "diff",
        "rationale",
        "status",
        "is_sample",
        "created_at",
        "updated_at",
    ),
    "test_runs": (
        "id",
        "repo_id",
        "proposal_id",
        "command",
        "exit_code",
        "passed",
        "duration_ms",
        "output_tail",
        "created_at",
    ),
    "changes": ("id", "repo_id", "proposal_id", "kind", "detail", "created_at"),
}

# fields that must be non-empty text
_REQUIRED_TEXT: Final[dict[str, tuple[str, ...]]] = {
    "repos": ("path", "name", "created_at"),
    "proposals": ("request", "model", "diff", "status", "created_at", "updated_at"),
    "test_runs": ("command", "created_at"),
    "changes": ("kind", "created_at"),
}

_TIMESTAMP_FIELDS: Final[tuple[str, ...]] = ("created_at", "updated_at")


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def export_data(store: Store, path: Path) -> dict[str, int]:
    """Write all four audit tables to ``path`` as JSON; return per-table counts."""
    target = Path(path)
    payload: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": utc_now(),
    }
    counts: dict[str, int] = {}
    for table in TABLES:  # names come from a module constant, never from input
        rows = [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        payload[table] = rows
        counts[table] = len(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    store.record_change(
        "export",
        f"exported {sum(counts.values())} records to {target.name}",
    )
    return counts


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------


def _fail(where: str, reason: str) -> None:
    raise ValidationError(f"{where}: {reason}")


def _check_keys(table: str, index: int, row: dict[str, Any]) -> None:
    where = f"{table}[{index}]"
    known = set(COLUMNS[table])
    for key in row:
        if key not in known:
            _fail(where, f"unknown field {key!r}")


def _require_fields(table: str, index: int, row: dict[str, Any]) -> None:
    where = f"{table}[{index}]"
    for key in COLUMNS[table]:
        if key not in row:
            _fail(where, f"missing field {key!r}")


def _text(table: str, index: int, row: dict[str, Any], key: str) -> str:
    where = f"{table}[{index}].{key}"
    value = row[key]
    if not isinstance(value, str):
        _fail(where, "must be a string")
    if key in _REQUIRED_TEXT[table] and not value.strip():
        _fail(where, "must not be empty")
    if "\x00" in value:
        _fail(where, "must not contain NUL characters")
    if key in _TIMESTAMP_FIELDS:
        _timestamp(where, value)
    return value


def _optional_text(table: str, index: int, row: dict[str, Any], key: str) -> str | None:
    where = f"{table}[{index}].{key}"
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(where, "must be a string or null")
    if "\x00" in value:
        _fail(where, "must not contain NUL characters")
    text = value.strip()
    return text or None


def _int(table: str, index: int, row: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    where = f"{table}[{index}].{key}"
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(where, "must be an integer")
    if minimum is not None and value < minimum:
        _fail(where, f"must be >= {minimum}")
    return int(value)


def _optional_int(table: str, index: int, row: dict[str, Any], key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    return _int(table, index, row, key)


def _flag(table: str, index: int, row: dict[str, Any], key: str) -> int:
    where = f"{table}[{index}].{key}"
    value = row[key]
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    _fail(where, "must be 0 or 1")


def _timestamp(where: str, value: str) -> None:
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{where}: must be an ISO-8601 timestamp") from exc


def _repo_path(table: str, index: int, value: str) -> str:
    where = f"{table}[{index}].path"
    path = Path(value)
    if not path.is_absolute():
        _fail(where, "must be an absolute path")
    if ".." in path.parts:
        _fail(where, "must not contain '..'")
    return str(path)


# --------------------------------------------------------------------------
# parsed records
# --------------------------------------------------------------------------


def _parse_repos(rows: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_paths: set[str] = set()
    for index, row in enumerate(rows):
        _require_fields("repos", index, row)
        _check_keys("repos", index, row)
        repo_id = _int("repos", index, row, "id", minimum=1)
        if repo_id in seen_ids:
            _fail(f"repos[{index}].id", "duplicate id in import file")
        seen_ids.add(repo_id)
        path = _repo_path("repos", index, _text("repos", index, row, "path"))
        if path in seen_paths:
            _fail(f"repos[{index}].path", "duplicate path in import file")
        seen_paths.add(path)
        parsed.append(
            {
                "id": repo_id,
                "path": path,
                "name": _text("repos", index, row, "name"),
                "test_command": _optional_text("repos", index, row, "test_command"),
                "is_sample": _flag("repos", index, row, "is_sample"),
                "created_at": _text("repos", index, row, "created_at"),
            }
        )
    return parsed


def _parse_proposals(rows: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(rows):
        _require_fields("proposals", index, row)
        _check_keys("proposals", index, row)
        proposal_id = _int("proposals", index, row, "id", minimum=1)
        if proposal_id in seen_ids:
            _fail(f"proposals[{index}].id", "duplicate id in import file")
        seen_ids.add(proposal_id)
        status = _text("proposals", index, row, "status")
        if status not in PROPOSAL_STATUSES:
            _fail(
                f"proposals[{index}].status",
                f"must be one of: {', '.join(PROPOSAL_STATUSES)}",
            )
        parsed.append(
            {
                "id": proposal_id,
                "repo_id": _int("proposals", index, row, "repo_id", minimum=1),
                "request": _text("proposals", index, row, "request"),
                "model": _text("proposals", index, row, "model"),
                "diff": _text("proposals", index, row, "diff"),
                "rationale": _optional_text("proposals", index, row, "rationale") or "",
                "status": status,
                "is_sample": _flag("proposals", index, row, "is_sample"),
                "created_at": _text("proposals", index, row, "created_at"),
                "updated_at": _text("proposals", index, row, "updated_at"),
            }
        )
    return parsed


def _parse_test_runs(rows: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(rows):
        _require_fields("test_runs", index, row)
        _check_keys("test_runs", index, row)
        run_id = _int("test_runs", index, row, "id", minimum=1)
        if run_id in seen_ids:
            _fail(f"test_runs[{index}].id", "duplicate id in import file")
        seen_ids.add(run_id)
        parsed.append(
            {
                "id": run_id,
                "repo_id": _int("test_runs", index, row, "repo_id", minimum=1),
                "proposal_id": _optional_int("test_runs", index, row, "proposal_id"),
                "command": _text("test_runs", index, row, "command"),
                "exit_code": _int("test_runs", index, row, "exit_code"),
                "passed": _flag("test_runs", index, row, "passed"),
                "duration_ms": _int("test_runs", index, row, "duration_ms", minimum=0),
                "output_tail": _optional_text("test_runs", index, row, "output_tail") or "",
                "created_at": _text("test_runs", index, row, "created_at"),
            }
        )
    return parsed


def _parse_changes(rows: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(rows):
        _require_fields("changes", index, row)
        _check_keys("changes", index, row)
        change_id = _int("changes", index, row, "id", minimum=1)
        if change_id in seen_ids:
            _fail(f"changes[{index}].id", "duplicate id in import file")
        seen_ids.add(change_id)
        kind = _text("changes", index, row, "kind")
        if kind not in CHANGE_KINDS:
            _fail(f"changes[{index}].kind", f"must be one of: {', '.join(CHANGE_KINDS)}")
        parsed.append(
            {
                "id": change_id,
                "repo_id": _optional_int("changes", index, row, "repo_id"),
                "proposal_id": _optional_int("changes", index, row, "proposal_id"),
                "kind": kind,
                "detail": _optional_text("changes", index, row, "detail") or "",
                "created_at": _text("changes", index, row, "created_at"),
            }
        )
    return parsed


def _section(document: dict[str, Any], table: str) -> list[Any]:
    if table not in document:
        _fail(table, "missing section")
    value = document[table]
    if not isinstance(value, list):
        _fail(table, "must be a list of rows")
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            _fail(f"{table}[{index}]", "must be a JSON object")
    return value


def _read_document(path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise ValidationError(f"import file not found: {target.name}")
    size = target.stat().st_size
    if size > MAX_IMPORT_BYTES:
        raise ValidationError(
            f"import file is too large (max {MAX_IMPORT_BYTES // (1024 * 1024)} MB)"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"import file could not be read as UTF-8 text: {target.name}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("import file is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValidationError("import file must contain a JSON object")
    if "format" not in document:
        _fail("format", "missing")
    file_format = document["format"]
    if not isinstance(file_format, str):
        _fail("format", "must be a string")
    if file_format != EXPORT_FORMAT:
        _fail("format", f"expected {EXPORT_FORMAT!r}")

    if "version" not in document:
        _fail("version", "missing")
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("version", "must be an integer")
    if version != EXPORT_VERSION:
        _fail("version", f"unsupported version (expected {EXPORT_VERSION})")

    if "exported_at" not in document:
        _fail("exported_at", "missing")
    exported_at = document["exported_at"]
    if not isinstance(exported_at, str):
        _fail("exported_at", "must be a string")
    _timestamp("exported_at", exported_at)
    return document


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


def import_data(store: Store, path: Path) -> dict[str, int]:
    """Import a JSON export into ``store``; return per-table counts imported."""
    document = _read_document(path)
    repos = _parse_repos(_section(document, "repos"))
    proposals = _parse_proposals(_section(document, "proposals"))
    test_runs = _parse_test_runs(_section(document, "test_runs"))
    changes = _parse_changes(_section(document, "changes"))

    counts = {
        "repos": len(repos),
        "proposals": len(proposals),
        "test_runs": len(test_runs),
        "changes": len(changes),
    }

    repo_id_map: dict[int, int] = {}
    proposal_id_map: dict[int, int] = {}

    try:
        with store.transaction():
            conn = store.conn
            for row in repos:
                existing = conn.execute(
                    "SELECT id FROM repos WHERE path = ?", (row["path"],)
                ).fetchone()
                if existing is not None:
                    repo_id_map[row["id"]] = int(existing["id"])
                    continue
                cursor = conn.execute(
                    "INSERT INTO repos(path, name, test_command, is_sample, created_at)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (
                        row["path"],
                        row["name"],
                        row["test_command"],
                        row["is_sample"],
                        row["created_at"],
                    ),
                )
                repo_id_map[row["id"]] = int(cursor.lastrowid or 0)

            for row in proposals:
                target_repo = row["repo_id"]
                if target_repo not in repo_id_map:
                    _fail(
                        f"proposals[{row['id']}].repo_id",
                        "references a repo that is not in this file",
                    )
                cursor = conn.execute(
                    "INSERT INTO proposals(repo_id, request, model, diff, rationale, status,"
                    " is_sample, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repo_id_map[target_repo],
                        row["request"],
                        row["model"],
                        row["diff"],
                        row["rationale"],
                        row["status"],
                        row["is_sample"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
                proposal_id_map[row["id"]] = int(cursor.lastrowid or 0)

            for row in test_runs:
                target_repo = row["repo_id"]
                if target_repo not in repo_id_map:
                    _fail(
                        f"test_runs[{row['id']}].repo_id",
                        "references a repo that is not in this file",
                    )
                mapped_proposal = None
                if row["proposal_id"] is not None:
                    if row["proposal_id"] not in proposal_id_map:
                        _fail(
                            f"test_runs[{row['id']}].proposal_id",
                            "references a proposal that is not in this file",
                        )
                    mapped_proposal = proposal_id_map[row["proposal_id"]]
                conn.execute(
                    "INSERT INTO test_runs(repo_id, proposal_id, command, exit_code, passed,"
                    " duration_ms, output_tail, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repo_id_map[target_repo],
                        mapped_proposal,
                        row["command"],
                        row["exit_code"],
                        row["passed"],
                        row["duration_ms"],
                        row["output_tail"],
                        row["created_at"],
                    ),
                )

            for row in changes:
                mapped_repo = None
                if row["repo_id"] is not None:
                    if row["repo_id"] not in repo_id_map:
                        _fail(
                            f"changes[{row['id']}].repo_id",
                            "references a repo that is not in this file",
                        )
                    mapped_repo = repo_id_map[row["repo_id"]]
                mapped_proposal = None
                if row["proposal_id"] is not None:
                    if row["proposal_id"] not in proposal_id_map:
                        _fail(
                            f"changes[{row['id']}].proposal_id",
                            "references a proposal that is not in this file",
                        )
                    mapped_proposal = proposal_id_map[row["proposal_id"]]
                conn.execute(
                    "INSERT INTO changes(repo_id, proposal_id, kind, detail, created_at)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (mapped_repo, mapped_proposal, row["kind"], row["detail"], row["created_at"]),
                )

            store.record_change(
                "import",
                f"imported from {Path(path).name}: "
                f"repos={counts['repos']} proposals={counts['proposals']} "
                f"test_runs={counts['test_runs']} changes={counts['changes']}",
            )
    except sqlite3.Error as exc:
        raise ValidationError("import failed and was rolled back") from exc
    return counts
