"""Safe ``git`` wrappers used by every other module.

Rules enforced here (see CONTRACT.md):

* argv lists only -- ``shell=True`` is never used and every call has a timeout.
* failures surface as :class:`~selfkraken.errors.GitError` carrying at most the
  first line of git's stderr, never file contents.
* user supplied paths are always passed after a ``--`` separator so they can
  never be parsed as options.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .errors import GitError, ValidationError

__all__ = [
    "DEFAULT_GIT_TIMEOUT",
    "run_git",
    "is_git_repo",
    "repo_root",
    "head_sha",
    "changed_paths",
    "commit_paths",
]

DEFAULT_GIT_TIMEOUT = 60

#: how many pathspecs a single ``git add`` / ``git commit`` may receive
_ARG_BATCH = 100


def _brief(text: str | None, limit: int = 200) -> str:
    """First non-empty line of *text*, truncated. Never the whole payload."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def run_git(
    repo_path: Path,
    *args: str,
    input: str | None = None,  # noqa: A002 - contract name
    check: bool = True,
    timeout: int = DEFAULT_GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo_path *args`` and return the completed process.

    Raises :class:`GitError` when *check* is true and git exits non-zero
    (message is the first line of stderr), or when git is missing / times out.
    """
    argv = ["git", "-C", str(repo_path), *[str(a) for a in args]]
    label = args[0] if args else "git"
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git missing on PATH
        raise GitError("git executable not found - install git and retry") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {label} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(
            _brief(proc.stderr) or f"git {label} failed (exit {proc.returncode})"
        )
    return proc


def is_git_repo(path: Path) -> bool:
    """True when *path* is inside a git work tree."""
    if not Path(path).is_dir():
        return False
    proc = run_git(path, "rev-parse", "--is-inside-work-tree", check=False, timeout=20)
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def repo_root(path: Path) -> Path:
    """Absolute toplevel of the repository containing *path* (else GitError)."""
    proc = run_git(path, "rev-parse", "--show-toplevel", timeout=20)
    root = proc.stdout.strip()
    if not root:
        raise GitError(f"not a git repository: {path}")
    return Path(root)


def head_sha(repo_path: Path) -> str | None:
    """Commit SHA at HEAD, or ``None`` for an empty repository."""
    proc = run_git(repo_path, "rev-parse", "--verify", "HEAD", check=False, timeout=20)
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def changed_paths(repo_path: Path) -> list[str]:
    """Working tree changes (staged, unstaged and untracked), repo-relative."""
    proc = run_git(repo_path, "status", "--porcelain", "--untracked-files=all", "-z")
    paths: list[str] = []
    fields = [f for f in proc.stdout.split("\0") if f]
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:  # pragma: no cover - malformed porcelain line
            continue
        status, rel = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # rename/copy: the next field is the source path
            if rel:
                paths.append(rel)
            continue
        if rel:
            paths.append(rel)
    return sorted(dict.fromkeys(paths))


def _clean_paths(paths: list[str]) -> list[str]:
    """Reject anything that is not a plain repo-relative path."""
    cleaned: list[str] = []
    for raw in paths:
        text = str(raw).strip()
        if not text:
            continue
        if "\x00" in text or text.startswith(("/", "\\")) or ".." in Path(text).parts:
            raise ValidationError(f"refusing to use unsafe git path: {text[:120]!r}")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ValidationError("no paths given to commit")
    return cleaned


def commit_paths(repo_path: Path, paths: list[str], message: str) -> str:
    """Stage *paths*, commit them with *message*, return the new commit SHA."""
    root = repo_root(repo_path)
    targets = _clean_paths(paths)
    if not str(message or "").strip():
        raise ValidationError("commit message is empty")

    for start in range(0, len(targets), _ARG_BATCH):
        run_git(root, "add", "--", *targets[start : start + _ARG_BATCH])

    if len(targets) <= _ARG_BATCH:
        run_git(root, "commit", "--no-verify", "-m", message, "--", *targets)
    else:  # too many pathspecs for one argv: hand them over on a NUL file
        with tempfile.TemporaryDirectory(prefix="selfkraken-commit-") as scratch:
            spec = Path(scratch) / "pathspecs"
            spec.write_bytes(b"\x00".join(t.encode("utf-8") for t in targets))
            run_git(
                root,
                "commit",
                "--no-verify",
                "-m",
                message,
                f"--pathspec-from-file={spec}",
                "--pathspec-file-nul",
            )

    sha = head_sha(root)
    if sha is None:  # pragma: no cover - commit reported success but no HEAD
        raise GitError("commit did not produce a HEAD revision")
    return sha
