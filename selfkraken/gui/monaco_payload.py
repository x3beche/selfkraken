"""The data side of the Monaco editor — pure Python, no Qt, no network.

Everything the vendored editor renders is built here as a plain ``dict`` and
serialised with :func:`payload_json`, which is the **only** way untrusted text
(file contents, model output, diff bodies) reaches JavaScript.  ``json.dumps``
plus the ``<`` / ``>`` / ``&`` escapes mean a payload containing ``</script>``,
backticks or quotes cannot end its own string and become code.

The module also owns the two things the editor cannot work out by itself:

* :func:`original_and_modified` — the real before/after text for one file of a
  proposal, taken from ``git show HEAD:<path>`` and by applying that file's chunk
  to a copy in a temp directory with ``git apply`` (verified to work outside a
  repository, which is exactly why it is safe to run against a scratch dir);
* :func:`language_for_path` — Monaco's language id for a file name, restricted to
  the languages the vendored bundle actually ships (see ``scripts/vendor_monaco.py``).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from selfkraken.gui import theme as _theme
from selfkraken.gui.theme import Palette

__all__ = [
    "FilePatch",
    "MAX_TEXT_BYTES",
    "build_clear_payload",
    "build_diff_payload",
    "build_file_payload",
    "build_text_payload",
    "build_theme_payload",
    "count_changes",
    "head_text",
    "language_for_path",
    "original_and_modified",
    "payload_json",
    "split_diff",
    "theme_tokens",
    "touched_lines",
    "working_tree_text",
]

#: past this size a file is not shown in the editor (the viewer is for reading
#: code, and a 40 MB blob would freeze the renderer process)
MAX_TEXT_BYTES = 2 * 1024 * 1024

#: ``git apply`` is a subprocess: bound it like every other external call here
_APPLY_TIMEOUT = 30

# --------------------------------------------------------------------------- #
# per-file diff splitting
# --------------------------------------------------------------------------- #
_GIT_HEADER = re.compile(r"^diff --git[ \t]+a/(?P<old>.*?)\s+b/(?P<new>.*?)\s*$")
_MINUS = re.compile(r"^--- (?P<path>\S.*)$")
_PLUS = re.compile(r"^\+\+\+ (?P<path>\S.*)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(value: str, prefix: str) -> str:
    text = value.strip().split("\t", 1)[0].strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


@dataclass(frozen=True)
class FilePatch:
    """One file's section of a unified diff, with the counts the UI shows."""

    path: str  # repo-relative path the patch *lands on* ("" when pure deletion)
    old_path: str  # repo-relative path it starts from ("" when an addition)
    chunk: str  # the patch text for just this file (valid for ``git apply``)
    added: int
    removed: int
    is_new: bool = False
    is_delete: bool = False

    @property
    def label(self) -> str:
        """What the file list and tab title show."""
        if self.is_delete and self.old_path:
            return f"{self.old_path} (deleted)"
        return self.path or self.old_path


def split_diff(diff: str) -> list[FilePatch]:
    """Cut a multi-file unified diff into one :class:`FilePatch` per file.

    Accepts both a real ``git diff`` (``diff --git`` sections) and a bare patch
    that only carries ``--- a/x`` / ``+++ b/y`` headers, which is what models
    tend to emit.  Hunk bodies are never re-indented or re-wrapped: the chunk is
    exactly the lines it had in the original patch, because ``git apply`` is
    byte-precise.
    """
    patches: list[FilePatch] = []
    current: list[str] = []
    old_path = new_path = ""
    saw_git_header = False

    def flush() -> None:
        nonlocal old_path, new_path, saw_git_header
        if not current:
            return
        chunk = "\n".join(current)
        if not chunk.endswith("\n"):
            chunk += "\n"
        added = removed = 0
        is_new = is_delete = False
        for line in current:
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        text_old = _minus_path(current)
        text_new = _plus_path(current)
        if text_old == "/dev/null" or (saw_git_header and not text_old and text_new):
            is_new = True
        if text_new == "/dev/null":
            is_delete = True
        patches.append(
            FilePatch(
                path=new_path,
                old_path=old_path,
                chunk=chunk,
                added=added,
                removed=removed,
                is_new=is_new,
                is_delete=is_delete,
            )
        )
        current.clear()
        old_path = new_path = ""
        saw_git_header = False

    for line in (diff or "").splitlines():
        match = _GIT_HEADER.match(line)
        if match:
            flush()
            old_path = _strip_prefix(match.group("old"), "a/")
            new_path = _strip_prefix(match.group("new"), "b/")
            saw_git_header = True
            current.append(line)
            continue
        if line.startswith("--- "):
            if not saw_git_header and current:
                flush()
            target = _strip_prefix(line[4:], "a/")
            if target != "/dev/null":
                old_path = old_path or target
                new_path = new_path or target
            current.append(line)
            continue
        if line.startswith("+++ "):
            target = _strip_prefix(line[4:], "b/")
            if target != "/dev/null":
                new_path = target
            current.append(line)
            continue
        current.append(line)
    flush()
    return patches


def _minus_path(lines: Sequence[str]) -> str:
    for line in lines:
        if line.startswith("--- "):
            return _strip_prefix(line[4:], "a/")
    return ""


def _plus_path(lines: Sequence[str]) -> str:
    for line in reversed(lines):
        if line.startswith("+++ "):
            return _strip_prefix(line[4:], "b/")
    return ""


def count_changes(patch: FilePatch) -> tuple[int, int]:
    """``(+added, -removed)`` line counts for the compact file list."""
    return patch.added, patch.removed


# --------------------------------------------------------------------------- #
# before/after text
# --------------------------------------------------------------------------- #
def head_text(repo_path: Path, path: str) -> Optional[str]:
    """The file as committed at HEAD, ``None`` when HEAD has no such file."""
    rel = _safe_rel(path)
    if rel is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            ["git", "-C", str(repo_path), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            timeout=_APPLY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None  # not in HEAD (new file), or the repo is unborn
    return proc.stdout


def working_tree_text(repo_path: Path, path: str) -> Optional[str]:
    """The file as it sits on disk, ``None`` when missing, huge or unreadable."""
    rel = _safe_rel(path)
    if rel is None:
        return None
    target = Path(repo_path) / rel
    try:
        if not target.is_file():
            return None
        size = target.stat().st_size
        if size > MAX_TEXT_BYTES:
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8000]:
        return None  # binary: not something the code editor should try to show
    return raw.decode("utf-8", "replace")


def _safe_rel(path: str) -> Optional[str]:
    """A clean repo-relative path, or ``None`` for anything suspicious."""
    text = str(path or "").strip().replace("\\", "/")
    if not text or "\x00" in text or "\n" in text:
        return None
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or ".git" in parts:
        return None
    return "/".join(parts)


def original_and_modified(
    repo_path: Path, patch: FilePatch
) -> tuple[str, str] | None:
    """The two texts Monaco diffs for *patch*: (before, after).

    ``before`` is HEAD's version of the file (empty for a new file). ``after`` is
    the result of applying the file's own chunk to ``before`` **in a temporary
    directory**, so the user's working tree is never touched.  ``None`` means the
    chunk does not apply to HEAD's content — the caller then keeps showing the
    plain unified diff, which is always honest about what the patch says.
    """
    target = _safe_rel(patch.path or patch.old_path)
    if target is None:
        return None
    before = "" if patch.is_new else (head_text(repo_path, target) or "")
    after = _apply_chunk(
        target, before, patch.chunk, exists=not patch.is_new, reverse=False
    )
    if after is None:
        return None
    if patch.is_delete:
        after = ""
    return before, after


def _apply_chunk(
    rel: str, base: str, chunk: str, *, exists: bool, reverse: bool
) -> Optional[str]:
    """Run ``git apply`` for one file inside a throwaway directory.

    The scratch dir holds only ``rel`` with the base content, so a patch cannot
    reach the repository; ``-p1`` strips the ``a/``/``b/`` prefixes exactly as
    ``git apply`` does inside a repo.  An addition must *not* find the file
    already there (``git apply`` refuses with "already exists"), so ``exists``
    decides whether the base content is written first.
    """
    with tempfile.TemporaryDirectory(prefix="selfkraken-monaco-") as scratch:
        root = Path(scratch)
        if exists:
            file_path = root / rel
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(base, encoding="utf-8", newline="")
            except OSError:
                return None
        # the zero-context form first (it accepts more model-made patches), then
        # the plain reading of the same chunk; both run inside the scratch dir.
        attempts = [["--unidiff-zero"], []]
        if reverse:
            for attempt in attempts:
                attempt.append("--reverse")
        applied = False
        for extra in attempts:
            argv = ["git", "apply", "--whitespace=nowarn", "-p1", *extra, "-"]
            try:
                proc = subprocess.run(  # noqa: S603 - argv list, never a shell
                    argv,
                    cwd=str(root),
                    input=chunk,
                    capture_output=True,
                    text=True,
                    timeout=_APPLY_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode == 0:
                applied = True
                break
        if not applied:
            return None
        try:
            return (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""  # a deletion removed the file: the after text is empty


# --------------------------------------------------------------------------- #
# touched lines (for highlighting a file the proposal changes)
# --------------------------------------------------------------------------- #
def touched_lines(diff: str, path: str) -> set[int]:
    """1-based line numbers *path* has **after** the patch, for touched lines.

    Added lines and the lines inside a hunk's modified window count; pure
    context outside a hunk does not.  Line numbers are in the *post-patch* file,
    because that is what the tree opens and what the user reads.
    """
    want = _safe_rel(path)
    if want is None:
        return set()
    lines: set[int] = set()
    for patch in split_diff(diff):
        if _safe_rel(patch.path or patch.old_path) != want:
            continue
        counter = 0
        in_hunk = False
        for line in patch.chunk.splitlines():
            match = _HUNK.match(line)
            if match:
                counter = int(match.group(3))
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("diff --git") or line.startswith("--- "):
                in_hunk = False
                continue
            if line.startswith("+++ "):
                continue
            if line.startswith("-"):
                continue  # removed: consumes no line in the post-patch file
            if line.startswith("+"):
                lines.add(counter)
                counter += 1
            elif line.startswith("\\"):  # "\ No newline at end of file"
                continue
            else:
                counter += 1
        break
    return lines


# --------------------------------------------------------------------------- #
# language + theme
# --------------------------------------------------------------------------- #
#: Monaco language ids for the languages the vendored bundle keeps.  Anything
#: else opens as ``plaintext`` — asking Monaco for a language it has no chunk for
#: is how a prune silently turns into a console error on every file open.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".json": "json", ".jsonc": "json", ".jsonl": "json", ".lock": "json",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css", ".scss": "css", ".less": "css",
    ".md": "markdown", ".markdown": "markdown",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".sh.hook": "shell",
    ".sql": "sql",
    ".ini": "ini", ".cfg": "ini", ".editorconfig": "ini",
    ".xml": "xml", ".svg": "xml", ".xsd": "xml", ".pom": "xml",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".dockerfile": "dockerfile",
}

#: names without a dot that still mean something
LANGUAGE_BY_NAME: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "shell",
    "gemfile": "ruby",
    "procfile": "shell",
    ".gitignore": "ini",
    ".gitattributes": "ini",
}


def language_for_path(path: str) -> str:
    """Monaco language id for *path* — always an id the vendored bundle ships."""
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not name:
        return "plaintext"
    if name in LANGUAGE_BY_NAME:
        return LANGUAGE_BY_NAME[name]
    for extension, language in LANGUAGE_BY_EXTENSION.items():
        if name.endswith(extension):
            return language
    if name.endswith(".toml"):  # no tokenizer vendored
        return "ini"
    return "plaintext"


def theme_tokens(palette: Palette) -> dict[str, Any]:
    """The Monaco theme, built from the app's own palette tokens.

    Monaco wants raw hex colours, not Qt roles, so the mapping is explicit: the
    editor background is the deep ``base`` the tables already sit on, the diff
    colours are the very same values :class:`~selfkraken.gui.diff_view.DiffHighlighter`
    paints, so the plain fallback and Monaco cannot disagree.
    """
    diff = palette.diff
    return {
        "name": f"selfkraken-{palette.name}",
        "base": f"vs-{palette.name}",
        "background": palette.base,
        "foreground": palette.text,
        "muted": palette.muted,
        "selectionBackground": palette.selection_bg,
        "selectionForeground": palette.selection_text,
        "lineHighlight": palette.stripe,
        "border": palette.border,
        "gutter": palette.window,
        "gutterForeground": palette.muted,
        "addedBackground": diff["add"][1],
        "addedForeground": diff["add"][0],
        "removedBackground": diff["del"][1],
        "removedForeground": diff["del"][0],
        "hunk": diff["hunk"][0],
        "markerBackground": palette.selection_bg,
        "fontFamily": _theme.mono_family(),
        "fontSize": 12,
        "lineHeight": 18,
    }


# --------------------------------------------------------------------------- #
# payloads
# --------------------------------------------------------------------------- #
def build_diff_payload(
    original: str,
    modified: str,
    path: str,
    *,
    side_by_side: bool,
    tokens: dict[str, Any],
) -> dict[str, Any]:
    """Monaco ``DiffEditor`` input for one file of a proposal."""
    return {
        "kind": "diff",
        "path": str(path or ""),
        "language": language_for_path(path),
        "original": _clip(original),
        "modified": _clip(modified),
        "sideBySide": bool(side_by_side),
        "tokens": tokens,
    }


def build_file_payload(
    text: str,
    path: str,
    *,
    highlight_lines: Iterable[int] = (),
    tokens: dict[str, Any],
) -> dict[str, Any]:
    """One repository file, read-only, with touched lines optionally marked."""
    return {
        "kind": "file",
        "path": str(path or ""),
        "language": language_for_path(path),
        "text": _clip(text),
        "truncated": len((text or "").encode("utf-8", "replace")) > MAX_TEXT_BYTES,
        "highlightLines": sorted({int(n) for n in highlight_lines if int(n) > 0}),
        "tokens": tokens,
    }


def build_text_payload(text: str, *, tokens: dict[str, Any]) -> dict[str, Any]:
    """Test output / console text: always plaintext, never a language guess."""
    return {
        "kind": "text",
        "path": "",
        "language": "plaintext",
        "text": _clip(text),
        "tokens": tokens,
    }


def build_theme_payload(tokens: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "theme", "tokens": tokens}


def build_clear_payload() -> dict[str, Any]:
    return {"kind": "clear"}


def _clip(text: str) -> str:
    """Keep the payload bounded; the caller says so in the UI when it happens."""
    data = (text or "").encode("utf-8", "replace")
    if len(data) <= MAX_TEXT_BYTES:
        return text or ""
    return data[:MAX_TEXT_BYTES].decode("utf-8", "ignore") + "\n…(truncated)"


# --------------------------------------------------------------------------- #
# serialisation — the one place untrusted text becomes JavaScript
# --------------------------------------------------------------------------- #
_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}


def payload_json(payload: dict[str, Any]) -> str:
    """A JSON literal that is safe to hand to ``runJavaScript``.

    ``json.dumps`` already escapes quotes, backslashes and control bytes, so the
    result is a single value rather than concatenated code.  ``<``, ``>`` and
    ``&`` are then turned into unicode escapes: JSON keeps them as their own
    characters (and ``json.loads`` gives them back), but no inline ``</script>``
    can end a script element and no template literal can be closed early.
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for character, replacement in _ESCAPES.items():
        text = text.replace(character, replacement)
    return text


def js_apply(payload: dict[str, Any]) -> str:
    """The JavaScript source that feeds *payload* to the page."""
    return f"window.skApply && window.skApply({payload_json(payload)})"
