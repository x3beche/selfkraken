"""SQLite persistence layer for selfkraken (stdlib ``sqlite3`` only).

One :class:`Store` owns one database file at ``settings.db_path``.  The schema is
created on first open and versioned in the ``meta`` table.  Code search uses FTS5
when the SQLite build provides it; otherwise a plain ``chunks`` table with LIKE
search is used (same public API either way).

Every statement is parameterised, and free-text queries are tokenised and quoted
before they reach ``MATCH``, so raw user input is never part of SQL syntax.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .errors import NotFoundError, ValidationError

SCHEMA_VERSION: Final[str] = "1"

PROPOSAL_STATUSES: Final[tuple[str, ...]] = (
    "proposed",
    "applied",
    "tests_passed",
    "tests_failed",
    "rejected",
    "reverted",
)

CHANGE_KINDS: Final[tuple[str, ...]] = (
    "register",
    "index",
    "propose",
    "propose_failed",
    "apply",
    "test",
    "rollback",
    "commit",
    "reject",
    "revert",
    "import",
    "export",
    "sample_load",
    "sample_remove",
)

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")


def utc_now() -> str:
    """UTC ISO-8601 timestamp with second resolution (project convention)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class Repo:
    id: int
    path: str
    name: str
    test_command: str | None
    is_sample: bool
    created_at: str


@dataclass
class Proposal:
    id: int
    repo_id: int
    request: str
    model: str
    diff: str
    rationale: str
    status: str
    is_sample: bool
    created_at: str
    updated_at: str


@dataclass
class TestRun:
    id: int
    repo_id: int
    proposal_id: int | None
    command: str
    exit_code: int
    passed: bool
    duration_ms: int
    output_tail: str
    created_at: str


@dataclass
class Change:
    id: int
    repo_id: int | None
    proposal_id: int | None
    kind: str
    detail: str
    created_at: str


@dataclass
class Chunk:
    path: str
    start_line: int
    content: str
    score: float


def _data(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _repo_from(row: sqlite3.Row) -> Repo:
    data = _data(row)
    return Repo(
        id=int(data["id"]),
        path=str(data["path"]),
        name=str(data["name"]),
        test_command=None if data["test_command"] is None else str(data["test_command"]),
        is_sample=bool(int(data["is_sample"] or 0)),
        created_at=str(data["created_at"]),
    )


def _proposal_from(row: sqlite3.Row) -> Proposal:
    data = _data(row)
    return Proposal(
        id=int(data["id"]),
        repo_id=int(data["repo_id"]),
        request=str(data["request"]),
        model=str(data["model"]),
        diff=str(data["diff"]),
        rationale=str(data["rationale"] or ""),
        status=str(data["status"]),
        is_sample=bool(int(data["is_sample"] or 0)),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )


def _test_run_from(row: sqlite3.Row) -> TestRun:
    data = _data(row)
    proposal_id = data["proposal_id"]
    return TestRun(
        id=int(data["id"]),
        repo_id=int(data["repo_id"]),
        proposal_id=None if proposal_id is None else int(proposal_id),
        command=str(data["command"]),
        exit_code=int(data["exit_code"]),
        passed=bool(int(data["passed"] or 0)),
        duration_ms=int(data["duration_ms"]),
        output_tail=str(data["output_tail"] or ""),
        created_at=str(data["created_at"]),
    )


def _change_from(row: sqlite3.Row) -> Change:
    data = _data(row)
    repo_id = data["repo_id"]
    proposal_id = data["proposal_id"]
    return Change(
        id=int(data["id"]),
        repo_id=None if repo_id is None else int(repo_id),
        proposal_id=None if proposal_id is None else int(proposal_id),
        kind=str(data["kind"]),
        detail=str(data["detail"]),
        created_at=str(data["created_at"]),
    )


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

_META_SQL: Final[str] = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)

_CORE_TABLES: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS repos(
      id INTEGER PRIMARY KEY,
      path TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      test_command TEXT,
      is_sample INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS files(
      id INTEGER PRIMARY KEY,
      repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
      path TEXT NOT NULL,
      sha TEXT NOT NULL,
      size INTEGER NOT NULL,
      indexed_at TEXT NOT NULL,
      UNIQUE(repo_id, path))""",
    """CREATE TABLE IF NOT EXISTS proposals(
      id INTEGER PRIMARY KEY,
      repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
      request TEXT NOT NULL,
      model TEXT NOT NULL,
      diff TEXT NOT NULL,
      rationale TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      is_sample INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS test_runs(
      id INTEGER PRIMARY KEY,
      repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
      proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL,
      command TEXT NOT NULL,
      exit_code INTEGER NOT NULL,
      passed INTEGER NOT NULL,
      duration_ms INTEGER NOT NULL,
      output_tail TEXT NOT NULL,
      created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS changes(
      id INTEGER PRIMARY KEY,
      repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
      proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL,
      kind TEXT NOT NULL,
      detail TEXT NOT NULL,
      created_at TEXT NOT NULL)""",
)

_CHUNKS_FTS_SQL: Final[str] = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
    "repo_id UNINDEXED, path UNINDEXED, start_line UNINDEXED, content)"
)

_CHUNKS_PLAIN_SQL: Final[str] = (
    "CREATE TABLE IF NOT EXISTS chunks("
    "repo_id INTEGER NOT NULL, path TEXT NOT NULL, start_line INTEGER NOT NULL,"
    " content TEXT NOT NULL)"
)

_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS files_repo_path ON files(repo_id, path)",
    "CREATE INDEX IF NOT EXISTS proposals_repo ON proposals(repo_id, id)",
    "CREATE INDEX IF NOT EXISTS test_runs_repo ON test_runs(repo_id, id)",
    "CREATE INDEX IF NOT EXISTS changes_repo ON changes(repo_id, id)",
)


def _clean_optional_text(value: str | None, label: str) -> str | None:
    """Trim to None when blank; reject NUL bytes."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "\x00" in text:
        raise ValidationError(f"{label} must not contain NUL characters")
    return text


def _tokenise(query: str) -> list[str]:
    """Split untrusted text into safe ``[A-Za-z0-9_]+`` tokens, de-duplicated."""
    seen: dict[str, None] = {}
    for match in _TOKEN_RE.finditer(query or ""):
        token = match.group(0)
        seen.setdefault(token, None)
    return list(seen)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_status(status: str, field: str = "status") -> str:
    if status not in PROPOSAL_STATUSES:
        raise ValidationError(
            f"{field} must be one of: {', '.join(PROPOSAL_STATUSES)} (got {status!r})"
        )
    return status


def _check_kind(kind: str, field: str = "kind") -> str:
    if kind not in CHANGE_KINDS:
        raise ValidationError(
            f"{field} must be one of: {', '.join(CHANGE_KINDS)} (got {kind!r})"
        )
    return kind


class Store:
    """Validated, parameterised data access over a single SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.fts5: bool = False
        self._depth = 0
        self._migrate()

    # ------------------------------------------------------------------ I/O
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One transaction: commit on success, roll back on any error.

        While inside it, the record-writing methods below skip their own
        ``commit()`` so a caller can group them atomically.  Nested calls reuse the
        outer transaction instead of committing early.
        """
        self._depth += 1
        try:
            if self._depth > 1:
                yield self.conn
            else:
                with self.conn:
                    yield self.conn
        finally:
            self._depth -= 1

    def _commit(self) -> None:
        if self._depth == 0:
            self.conn.commit()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, tuple(params))
        self._commit()
        return cursor

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        row = self._one(sql, params)
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def _count_table(self, table: str, repo_id: int) -> int:
        """Rows of one of our own tables for one repo (names are hard-coded)."""
        return self._count(f"SELECT COUNT(*) FROM {table} WHERE repo_id = ?", (repo_id,))

    # ------------------------------------------------------------ migration
    def _migrate(self) -> None:
        self.conn.execute(_META_SQL)
        row = self._one("SELECT value FROM meta WHERE key = 'schema_version'")
        fresh = row is None
        if not fresh:
            try:
                version = int(str(row["value"]))  # type: ignore[union-attr]
            except ValueError as exc:
                raise ValidationError("database schema version is not a number") from exc
            if version > int(SCHEMA_VERSION):
                raise ValidationError(
                    f"database uses a newer schema version ({version}); "
                    "selfkraken cannot open it"
                )
        for statement in _CORE_TABLES:
            self.conn.execute(statement)
        self.fts5 = self._ensure_chunks_table()
        for statement in _INDEXES:
            self.conn.execute(statement)
        if not self.fts5:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS chunks_repo ON chunks(repo_id, path)"
            )
        if fresh:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,)
            )
        self._commit()

    def _ensure_chunks_table(self) -> bool:
        """Create the chunks table (FTS5 if available); report which kind exists."""
        existing = self._one("SELECT sql FROM sqlite_master WHERE name = 'chunks'")
        if existing is not None:
            return "fts5" in str(existing["sql"] or "").lower()
        try:
            self.conn.execute(_CHUNKS_FTS_SQL)
        except sqlite3.OperationalError:
            self.conn.execute(_CHUNKS_PLAIN_SQL)
            return False
        return True

    def meta_get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    # ---------------------------------------------------------------- repos
    def add_repo(
        self,
        path: Path,
        name: str,
        test_command: str | None = None,
        is_sample: bool = False,
    ) -> Repo:
        resolved = str(Path(path).resolve())
        label = (name or "").strip()
        if not label:
            raise ValidationError("repo name must not be empty")
        command = _clean_optional_text(test_command, "test command")
        now = utc_now()
        try:
            cursor = self._execute(
                "INSERT INTO repos(path, name, test_command, is_sample, created_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (resolved, label, command, int(bool(is_sample)), now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"repo already registered: {resolved}") from exc
        return self.get_repo(int(cursor.lastrowid or 0))

    def get_repo(self, repo_id: int) -> Repo:
        row = self._one("SELECT * FROM repos WHERE id = ?", (int(repo_id),))
        if row is None:
            raise NotFoundError(f"no repo with id {repo_id}")
        return _repo_from(row)

    def find_repo_by_path(self, path: Path) -> Repo | None:
        """The registered repo whose path equals ``path`` or is an ancestor of it."""
        target = os.path.normcase(str(Path(path).resolve()))
        sep = os.sep
        best: tuple[int, Repo] | None = None
        for row in self._query("SELECT * FROM repos ORDER BY id"):
            repo = _repo_from(row)
            root = os.path.normcase(str(Path(repo.path).resolve()))
            if target == root or target.startswith(root.rstrip(sep) + sep):
                if best is None or len(root) > best[0]:
                    best = (len(root), repo)
        return None if best is None else best[1]

    def list_repos(self) -> list[Repo]:
        return [_repo_from(row) for row in self._query("SELECT * FROM repos ORDER BY id")]

    def set_test_command(self, repo_id: int, command: str | None) -> Repo:
        self.get_repo(repo_id)
        self._execute(
            "UPDATE repos SET test_command = ? WHERE id = ?",
            (_clean_optional_text(command, "test command"), int(repo_id)),
        )
        return self.get_repo(repo_id)

    def delete_repo(self, repo_id: int) -> None:
        self.get_repo(repo_id)
        with self.transaction():
            # chunks is a virtual/plain table without a foreign key
            self.conn.execute("DELETE FROM chunks WHERE repo_id = ?", (int(repo_id),))
            self.conn.execute("DELETE FROM repos WHERE id = ?", (int(repo_id),))

    # ---------------------------------------------------------------- index
    def get_file_shas(self, repo_id: int) -> dict[str, str]:
        rows = self._query("SELECT path, sha FROM files WHERE repo_id = ?", (int(repo_id),))
        return {str(row["path"]): str(row["sha"]) for row in rows}

    def replace_file_index(
        self,
        repo_id: int,
        path: str,
        sha: str,
        size: int,
        chunks: list[tuple[int, str]],
    ) -> None:
        rel = (path or "").strip()
        if not rel:
            raise ValidationError("index path must not be empty")
        self.get_repo(int(repo_id))
        rows: list[tuple[int, str, int, str]] = [
            (int(repo_id), rel, int(start_line), str(content))
            for start_line, content in chunks
        ]
        with self.transaction():
            self.conn.execute(
                "DELETE FROM chunks WHERE repo_id = ? AND path = ?", (int(repo_id), rel)
            )
            self.conn.execute(
                "INSERT INTO files(repo_id, path, sha, size, indexed_at) VALUES(?, ?, ?, ?, ?)"
                " ON CONFLICT(repo_id, path) DO UPDATE SET sha = excluded.sha,"
                " size = excluded.size, indexed_at = excluded.indexed_at",
                (int(repo_id), rel, str(sha), int(size), utc_now()),
            )
            if rows:
                self.conn.executemany(
                    "INSERT INTO chunks(repo_id, path, start_line, content) VALUES(?, ?, ?, ?)",
                    rows,
                )

    def remove_files_not_in(self, repo_id: int, keep: set[str]) -> int:
        wanted = {str(item) for item in keep}
        doomed = [
            str(row["path"])
            for row in self._query("SELECT path FROM files WHERE repo_id = ?", (int(repo_id),))
            if str(row["path"]) not in wanted
        ]
        if not doomed:
            return 0
        with self.transaction():
            for rel in doomed:
                self.conn.execute(
                    "DELETE FROM chunks WHERE repo_id = ? AND path = ?", (int(repo_id), rel)
                )
                self.conn.execute(
                    "DELETE FROM files WHERE repo_id = ? AND path = ?", (int(repo_id), rel)
                )
        return len(doomed)

    def count_files(self, repo_id: int) -> int:
        return self._count("SELECT COUNT(*) FROM files WHERE repo_id = ?", (int(repo_id),))

    def search_chunks(self, repo_id: int, query: str, limit: int = 8) -> list[Chunk]:
        """Free-text chunk search.  Input is tokenised first; nothing raw hits SQL."""
        tokens = _tokenise(query or "")
        cap = max(int(limit), 0)
        if not tokens or cap == 0:
            return []
        if self.fts5:
            found = self._search_fts(int(repo_id), tokens, cap)
            if found is not None:
                return found
        return self._search_like(int(repo_id), tokens, cap)

    def _search_fts(self, repo_id: int, tokens: list[str], limit: int) -> list[Chunk] | None:
        match = " OR ".join(f'"{token}"' for token in tokens)
        sql = (
            "SELECT path, start_line, content, bm25(chunks) AS score FROM chunks"
            " WHERE chunks MATCH ? AND repo_id = ?"
            " ORDER BY score ASC, path ASC, start_line ASC LIMIT ?"
        )
        try:
            rows = self._query(sql, (match, repo_id, limit))
        except sqlite3.Error:
            return None  # fall back to LIKE
        return [
            Chunk(
                path=str(row["path"]),
                start_line=int(row["start_line"]),
                content=str(row["content"]),
                score=float(row["score"]),
            )
            for row in rows
        ]

    def _search_like(self, repo_id: int, tokens: list[str], limit: int) -> list[Chunk]:
        needles = [token.lower() for token in tokens]
        predicates = " OR ".join(["lower(content) LIKE ? ESCAPE '\\'"] * len(needles))
        params: list[Any] = [repo_id]
        params.extend(f"%{_escape_like(needle)}%" for needle in needles)
        rows = self._query(
            f"SELECT path, start_line, content FROM chunks WHERE repo_id = ? AND ({predicates})",
            params,
        )
        scored: list[Chunk] = []
        for row in rows:
            haystack = str(row["content"]).lower()
            score = float(sum(1 for needle in needles if needle in haystack))
            scored.append(
                Chunk(
                    path=str(row["path"]),
                    start_line=int(row["start_line"]),
                    content=str(row["content"]),
                    score=score,
                )
            )
        scored.sort(key=lambda chunk: (-chunk.score, chunk.path, chunk.start_line))
        return scored[:limit]

    # ------------------------------------------------------------ proposals
    def create_proposal(
        self,
        repo_id: int,
        request: str,
        model: str,
        diff: str,
        rationale: str = "",
        status: str = "proposed",
        is_sample: bool = False,
    ) -> Proposal:
        self.get_repo(int(repo_id))
        if not (request or "").strip():
            raise ValidationError("proposal request must not be empty")
        if not (diff or "").strip():
            raise ValidationError("proposal diff must not be empty")
        _check_status(status)
        now = utc_now()
        cursor = self._execute(
            "INSERT INTO proposals(repo_id, request, model, diff, rationale, status,"
            " is_sample, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(repo_id),
                str(request),
                str(model or "manual"),
                str(diff),
                str(rationale or ""),
                status,
                int(bool(is_sample)),
                now,
                now,
            ),
        )
        return self.get_proposal(int(cursor.lastrowid or 0))

    def get_proposal(self, proposal_id: int) -> Proposal:
        row = self._one("SELECT * FROM proposals WHERE id = ?", (int(proposal_id),))
        if row is None:
            raise NotFoundError(f"no proposal with id {proposal_id}")
        return _proposal_from(row)

    def list_proposals(
        self, repo_id: int | None = None, status: str | None = None
    ) -> list[Proposal]:
        clauses: list[str] = []
        params: list[Any] = []
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(int(repo_id))
        if status is not None:
            _check_status(status)
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM proposals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [_proposal_from(row) for row in self._query(sql, params)]

    def set_proposal_status(self, proposal_id: int, status: str) -> Proposal:
        _check_status(status)
        self.get_proposal(int(proposal_id))
        self._execute(
            "UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), int(proposal_id)),
        )
        return self.get_proposal(int(proposal_id))

    # ---------------------------------------------------------------- tests
    def add_test_run(
        self,
        repo_id: int,
        proposal_id: int | None,
        command: str,
        exit_code: int,
        duration_ms: int,
        output_tail: str,
    ) -> TestRun:
        self.get_repo(int(repo_id))
        if proposal_id is not None:
            self.get_proposal(int(proposal_id))
        code = int(exit_code)
        cursor = self._execute(
            "INSERT INTO test_runs(repo_id, proposal_id, command, exit_code, passed,"
            " duration_ms, output_tail, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(repo_id),
                None if proposal_id is None else int(proposal_id),
                str(command),
                code,
                int(code == 0),
                max(int(duration_ms), 0),
                str(output_tail or ""),
                utc_now(),
            ),
        )
        return self._get_test_run(int(cursor.lastrowid or 0))

    def _get_test_run(self, run_id: int) -> TestRun:
        row = self._one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError(f"no test run with id {run_id}")
        return _test_run_from(row)

    def list_test_runs(
        self, repo_id: int | None = None, proposal_id: int | None = None
    ) -> list[TestRun]:
        clauses: list[str] = []
        params: list[Any] = []
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(int(repo_id))
        if proposal_id is not None:
            clauses.append("proposal_id = ?")
            params.append(int(proposal_id))
        sql = "SELECT * FROM test_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [_test_run_from(row) for row in self._query(sql, params)]

    # ---------------------------------------------------------------- audit
    def record_change(
        self,
        kind: str,
        detail: str,
        repo_id: int | None = None,
        proposal_id: int | None = None,
    ) -> Change:
        _check_kind(kind)
        if repo_id is not None:
            self.get_repo(int(repo_id))
        if proposal_id is not None:
            self.get_proposal(int(proposal_id))
        cursor = self._execute(
            "INSERT INTO changes(repo_id, proposal_id, kind, detail, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                None if repo_id is None else int(repo_id),
                None if proposal_id is None else int(proposal_id),
                kind,
                str(detail or ""),
                utc_now(),
            ),
        )
        row = self._one("SELECT * FROM changes WHERE id = ?", (int(cursor.lastrowid or 0),))
        if row is None:
            raise NotFoundError("change row disappeared")
        return _change_from(row)

    def list_changes(self, repo_id: int | None = None, limit: int = 50) -> list[Change]:
        sql = "SELECT * FROM changes"
        params: list[Any] = []
        if repo_id is not None:
            sql += " WHERE repo_id = ?"
            params.append(int(repo_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        return [_change_from(row) for row in self._query(sql, params)]

    # ---------------------------------------------------------------- sample
    def delete_sample_data(self) -> int:
        """Delete sample repos (cascading to their rows) and sample proposals.

        Returns the number of rows removed.
        """
        removed = 0
        with self.transaction():
            sample_repo_ids = [
                int(row["id"])
                for row in self._query("SELECT id FROM repos WHERE is_sample = 1")
            ]
            for repo_id in sample_repo_ids:
                for table in ("chunks", "files", "proposals", "test_runs", "changes"):
                    removed += self._count_table(table, repo_id)
                # chunks has no foreign key to repos (virtual/plain table), so empty it here
                self.conn.execute("DELETE FROM chunks WHERE repo_id = ?", (repo_id,))
                self.conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
                removed += 1  # the repo row itself
            leftover = [
                int(row["id"])
                for row in self._query(
                    "SELECT id FROM proposals WHERE is_sample = 1 AND repo_id IS NOT NULL"
                    " AND repo_id NOT IN (SELECT id FROM repos WHERE is_sample = 1)"
                )
            ]
            for proposal_id in leftover:
                removed += self._count(
                    "SELECT COUNT(*) FROM changes WHERE proposal_id = ?", (proposal_id,)
                )
                self.conn.execute("DELETE FROM changes WHERE proposal_id = ?", (proposal_id,))
                self.conn.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
                removed += 1
        return removed
