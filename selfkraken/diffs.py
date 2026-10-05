"""Unified-diff handling for **untrusted model output**.

:func:`validate_diff` is the security boundary between "a model said this" and
"this touches the working tree".  A patch is rejected *before* git ever sees it
when it is empty, oversized, contains NUL bytes, or names a path that is
absolute, escapes the repository through ``..`` or a symlink, or lives under
``.git/``.  Only then is ``git apply --check`` consulted.

Error messages carry reasons and paths only -- never file contents.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .errors import ValidationError
from .gitops import repo_root, run_git

__all__ = [
    "MAX_DIFF_BYTES",
    "extract_diff",
    "touched_paths",
    "validate_diff",
    "apply_diff",
    "reverse_diff",
    "can_reverse",
]

#: hard ceiling on the size of any patch we are willing to look at
MAX_DIFF_BYTES = 200_000

_APPLY_TIMEOUT = 60

_FENCED = re.compile(
    r"^[ \t]*```[ \t]*(?:diff|patch)\b[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_FENCED_UNCLOSED = re.compile(
    r"^[ \t]*```[ \t]*(?:diff|patch)\b[^\n]*\n(.*)$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_WIN_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_DIFF_GIT = re.compile(r"^diff --git[ \t]+(?P<paths>.*)$")
_GIT_HEADER_PATHS = re.compile(r"a/(?P<old>\S+) b/(?P<new>\S+)")


def _brief(text: str | None, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _looks_like_diff(text: str) -> bool:
    for line in text.splitlines():
        if line.startswith(("diff --git", "--- ", "+++ ", "@@ ", "Index: ")):
            return True
    return False


def extract_diff(text: str) -> str:
    """Pull a single unified diff out of raw model output.

    Prefers the first ```diff / ```patch fenced block, otherwise falls back to
    the output starting at the first ``diff --git`` / ``--- `` line.  The result
    always ends with a newline.  Raises :class:`ValidationError` when no
    plausible diff is present.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("model output contained no unified diff")

    candidates: list[str] = []
    candidates.extend(match.group(1) for match in _FENCED.finditer(text))
    unclosed = _FENCED_UNCLOSED.search(text)
    if unclosed:
        candidates.append(unclosed.group(1))

    for block in candidates:
        block = block.strip("\n").rstrip()
        if block and _looks_like_diff(block):
            return block + "\n"

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git") or line.startswith("--- "):
            candidate = "\n".join(lines[index:]).strip("\n").rstrip()
            if candidate and _looks_like_diff(candidate):
                return candidate + "\n"

    raise ValidationError("model output contained no unified diff")


def _header_value(raw: str) -> str | None:
    """Normalise the path part of a ``--- a/x`` / ``+++ b/x`` header."""
    value = raw.strip()
    if not value or value == "/dev/null" or value == "-":
        return None
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    for prefix in ("a/", "b/"):  # exactly what ``git diff`` writes
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    value = value.strip()
    return value or None


def touched_paths(diff: str) -> list[str]:
    """Files a patch touches, taken from ``---``/``+++`` headers.

    Only file-level headers are read: a ``---``/``+++`` that shows up *inside* a
    hunk is removed or added content, not a path (``diff --git`` resets the
    state for the next file section).
    """
    paths: set[str] = set()
    lines = diff.splitlines()
    in_hunk = False
    for index, line in enumerate(lines):
        if line.startswith("diff --git"):
            in_hunk = False
            match = _GIT_HEADER_PATHS.match(line[len("diff --git") :].strip())
            if match:
                for group in ("old", "new"):
                    path = _header_value(match.group(group))
                    if path:
                        paths.add(path)
        elif line.startswith("---"):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if not in_hunk or following.startswith("+++"):
                in_hunk = False
                path = _header_value(line[3:])
                if path:
                    paths.add(path)
        elif line.startswith("+++"):
            if not in_hunk:
                path = _header_value(line[3:])
                if path:
                    paths.add(path)
        elif line.startswith("@@"):
            in_hunk = True
    return sorted(paths)


def _shown(path: str, limit: int = 120) -> str:
    """Paths are metadata, but model output can be huge -- keep messages short."""
    return path if len(path) <= limit else path[:limit] + "..."


def _header_rejects(path: str, name: str) -> str | None:
    """Why *path* is unusable as a patch target, or ``None`` when it is fine."""
    shown = _shown(path)
    if "\x00" in path or "\n" in path or "\r" in path:
        return f"{name}: path contains control bytes"
    if path.startswith(("/", "\\")) or _WIN_ABSOLUTE.match(path):
        return f"{name}: path must be relative to the repository root ({shown})"
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or path in {".", ""}:
        return f"{name}: empty path"
    if any(part == ".." for part in parts):
        return f"{name}: path traverses outside the repository ({shown})"
    if any(part == ".git" for part in parts):
        return f"{name}: path is inside .git/ ({shown})"
    return None


def _resolves_inside(path: str, root: Path) -> bool:
    """Follow symlinks (also for not-yet-created tails) and stay inside *root*."""
    target = root / path
    try:
        resolved = target.resolve()
    except OSError:  # symlink loop, name too long, ...
        return False
    return resolved == root or root in resolved.parents


def validate_diff(diff: str, repo_path: Path) -> list[str]:
    """Validate untrusted *diff* against *repo_path*; return touched paths.

    Raises :class:`ValidationError` with the reason (never file contents) when
    the patch is empty, oversized, contains NUL bytes, names an unsafe path, or
    does not pass ``git apply --check``.
    """
    if not isinstance(diff, str) or not diff.strip():
        raise ValidationError("diff is empty")
    if "\x00" in diff:
        raise ValidationError("diff contains NUL bytes")
    size = len(diff.encode("utf-8"))
    if size > MAX_DIFF_BYTES:
        raise ValidationError(f"diff is too large ({size} bytes, max {MAX_DIFF_BYTES})")

    root = repo_root(Path(repo_path))
    try:
        root = root.resolve()
    except OSError as exc:  # pragma: no cover - unreadable cwd
        raise ValidationError("repository path could not be resolved") from exc

    paths = touched_paths(diff)
    if not paths:
        raise ValidationError("diff does not name any target file")
    for path in paths:
        reason = _header_rejects(path, "invalid patch")
        if reason:
            raise ValidationError(reason)
        if not _resolves_inside(path, root):
            raise ValidationError(
                f"invalid patch: path escapes the repository ({_shown(path)})"
            )

    proc = run_git(
        root, "apply", "--check", input=diff, check=False, timeout=_APPLY_TIMEOUT
    )
    if proc.returncode != 0:
        raise ValidationError(
            _brief(proc.stderr) or "diff does not apply cleanly to the repository"
        )
    return paths


def apply_diff(repo_path: Path, diff: str) -> None:
    """Apply *diff* to the working tree. Raises :class:`GitError` on failure."""
    if not str(diff or "").strip():
        raise ValidationError("diff is empty")
    run_git(repo_path, "apply", input=diff, timeout=_APPLY_TIMEOUT)


def reverse_diff(repo_path: Path, diff: str) -> None:
    """Undo a previously applied *diff* (``git apply -R``)."""
    if not str(diff or "").strip():
        raise ValidationError("diff is empty")
    run_git(repo_path, "apply", "--reverse", input=diff, timeout=_APPLY_TIMEOUT)


def can_reverse(repo_path: Path, diff: str) -> bool:
    """True when *diff* can currently be reversed cleanly."""
    if not str(diff or "").strip():
        return False
    proc = run_git(
        repo_path,
        "apply",
        "--reverse",
        "--check",
        input=diff,
        check=False,
        timeout=_APPLY_TIMEOUT,
    )
    return proc.returncode == 0
