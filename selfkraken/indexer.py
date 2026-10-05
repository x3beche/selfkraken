"""Turn a registered repository into a searchable, incremental file index.

Nothing here writes to the repository: files are read, hashed (sha256) and
chunked into line blocks that :class:`~selfkraken.db.Store` keeps in FTS5.

Two hard rules (CONTRACT.md):

* secret-looking files (``SECRET_PATTERNS``) and binaries are **never** read
  into the index, and any previously indexed copy is dropped again;
* :func:`build_context` only ever accepts paths that are relative, inside the
  repository and not secret -- the same boundary :mod:`selfkraken.diffs` uses
  for patch targets -- and it never exceeds ``max_chars``.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Sequence

from .errors import ValidationError
from .gitops import run_git

if TYPE_CHECKING:  # pragma: no cover - typing only, module owned by agent A
    from .db import Repo, Store

__all__ = [
    "MAX_FILE_BYTES",
    "CHUNK_LINES",
    "SECRET_PATTERNS",
    "list_repo_files",
    "should_skip",
    "is_probably_binary",
    "chunk_text",
    "IndexStats",
    "index_repo",
    "build_context",
]

#: files bigger than this are skipped, they are never useful as context
MAX_FILE_BYTES = 200_000
#: how many source lines make one indexed chunk
CHUNK_LINES = 60

#: basenames that must never be indexed, matched with :func:`fnmatch.fnmatch`
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.p12",
    "*.pfx",
    "secrets.*",
    "*.sqlite",
    "*.db",
)

_BINARY_SNIFF_BYTES = 8192
_GIT_TIMEOUT = 60


def list_repo_files(repo_path: Path) -> list[str]:
    """Tracked and untracked (non-ignored) files, relative to the repo root."""
    proc = run_git(
        repo_path,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        timeout=_GIT_TIMEOUT,
    )
    files = [entry for entry in proc.stdout.split("\0") if entry.strip()]
    return sorted(dict.fromkeys(files))


def should_skip(rel_path: str) -> bool:
    """True for secret-looking paths -- matched on the basename only."""
    name = PurePosixPath(str(rel_path).replace("\\", "/")).name
    if not name:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


def is_probably_binary(data: bytes) -> bool:
    """A NUL byte in the first 8 KiB, or text that is not valid UTF-8."""
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def chunk_text(text: str, chunk_lines: int = CHUNK_LINES) -> list[tuple[int, str]]:
    """Split *text* into ``chunk_lines``-sized blocks of ``(start_line, body)``.

    Line numbers are 1-based, so a chunk starting at line 61 really begins on
    line 61 of the file.  Empty input yields no chunks.
    """
    if chunk_lines < 1:
        chunk_lines = CHUNK_LINES
    lines = text.splitlines()
    chunks: list[tuple[int, str]] = []
    for start in range(0, len(lines), chunk_lines):
        body = "\n".join(lines[start : start + chunk_lines])
        chunks.append((start + 1, body))
    return chunks


@dataclass
class IndexStats:
    """Outcome of one :func:`index_repo` pass."""

    scanned: int
    indexed: int
    unchanged: int
    skipped: int
    removed: int


def index_repo(
    store: Store, repo: Repo, progress: Callable[[str], None] | None = None
) -> IndexStats:
    """(Re)index *repo*, only rewriting chunks whose content sha256 changed."""
    root = Path(repo.path)
    files = list_repo_files(root)
    known = store.get_file_shas(repo.id)

    scanned = indexed = unchanged = skipped = 0
    keep: set[str] = set()

    for rel in files:
        scanned += 1
        if should_skip(rel):
            skipped += 1
            continue
        target = root / rel
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                skipped += 1  # check the size before reading the file into memory
                continue
            data = target.read_bytes()
        except OSError:  # dangling symlink, race with a checkout, a directory
            skipped += 1
            continue
        if len(data) > MAX_FILE_BYTES or is_probably_binary(data):
            skipped += 1
            continue

        sha = hashlib.sha256(data).hexdigest()
        keep.add(rel)
        if known.get(rel) == sha:
            unchanged += 1
            continue

        store.replace_file_index(
            repo.id, rel, sha, len(data), chunk_text(data.decode("utf-8"))
        )
        indexed += 1
        if progress is not None:
            progress(rel)  # metadata only, never contents

    removed = store.remove_files_not_in(repo.id, keep)
    return IndexStats(
        scanned=scanned,
        indexed=indexed,
        unchanged=unchanged,
        skipped=skipped,
        removed=removed,
    )


def _checked_context_path(rel_path: str, root: Path) -> Path:
    """Validate a caller-supplied context path; return the file to read."""
    text = str(rel_path).strip()
    if not text:
        raise ValidationError("context path is empty")
    if "\x00" in text:
        raise ValidationError("context path contains NUL bytes")
    if text.startswith(("/", "\\")):
        raise ValidationError(f"context path must be relative to the repository: {text}")
    parts = PurePosixPath(text.replace("\\", "/")).parts
    if any(part == ".." for part in parts):
        raise ValidationError(f"context path traverses outside the repository: {text}")
    if any(part == ".git" for part in parts):
        raise ValidationError(f"context path is inside .git/: {text}")
    if should_skip(text):
        raise ValidationError(f"refusing to read secret-looking file: {text}")

    candidate = root / text
    try:
        resolved = candidate.resolve()
        resolved_root = root.resolve()
    except OSError as exc:  # symlink loop and friends
        raise ValidationError(f"context path could not be resolved: {text}") from exc
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValidationError(f"context path escapes the repository: {text}")
    if not resolved.is_file():
        raise ValidationError(f"context file not found: {text}")
    return resolved


def build_context(
    store: Store,
    repo: Repo,
    query: str,
    max_chars: int,
    extra_paths: Sequence[str] = (),
) -> str:
    """Assemble at most *max_chars* of repo context for a model prompt.

    Explicitly requested files (*extra_paths*) come first and are read in full,
    then FTS hits for *query*.  Each block is formatted
    ``### {path} (from line {n})\\n```\\n{content}\\n```\\n``.
    """
    if max_chars <= 0:
        return ""
    root = Path(repo.path)
    blocks: list[tuple[str, int, str]] = []
    included: set[str] = set()

    for raw in extra_paths or ():
        path = _checked_context_path(raw, root)
        rel = path.relative_to(root.resolve()).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ValidationError(f"context file could not be read: {rel}") from exc
        included.add(rel)
        blocks.append((rel, 1, content.rstrip("\n")))

    if query and query.strip():
        for hit in store.search_chunks(repo.id, query, limit=8):
            if hit.path in included:
                continue
            blocks.append((hit.path, hit.start_line, hit.content))

    out: list[str] = []
    used = 0
    for rel, start_line, content in blocks:
        header = f"### {rel} (from line {start_line})\n"
        fence = "```\n"
        tail = "\n```\n"
        overhead = len(header) + len(fence) + len(tail)
        available = max_chars - used - overhead
        if available <= 0 or (available < 20 and out):
            break
        body = content[:available]
        rendered = f"{header}{fence}{body}{tail}"
        out.append(rendered)
        used += len(rendered)
        if len(body) < len(content):
            break  # budget exhausted mid-file, stop cleanly
    return "".join(out)
