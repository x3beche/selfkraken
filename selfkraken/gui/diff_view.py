"""Read-only unified-diff viewer with syntax highlighting.

Body text is *regular weight monospace everywhere*: added lines green, removed
lines red, ``@@`` hunk headers in the accent colour, and only the file headers
(``diff --git`` / ``index`` / ``---`` / ``+++``) are bold.  Subtle backgrounds
carry the meaning too, so colour is never the only signal (WCAG-friendly).
:meth:`DiffHighlighter.format_for_line` is a pure lookup that tests can call
without a document.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from selfkraken.gui.theme import MONO_FONT, MONO_PT, Palette

#: the two weights the diff ever uses — nothing inherits a bold face
NORMAL = QFont.Weight.Normal
BOLD = QFont.Weight.Bold


def _fmt(fg: str, bg: str = "", weight: QFont.Weight = NORMAL) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(fg))
    if bg:
        fmt.setBackground(QColor(bg))
    # always explicit: a bold widget font or a bold inherited block cannot leak
    fmt.setFontWeight(int(weight))
    return fmt


class DiffHighlighter(QSyntaxHighlighter):
    """QSyntaxHighlighter for a ``git diff`` text."""

    def __init__(self, document, palette: Palette) -> None:
        super().__init__(document)
        self._palette = palette
        d = palette.diff
        self._formats: dict[str, QTextCharFormat] = {
            "add": _fmt(d["add"][0], d["add"][1], NORMAL),
            "del": _fmt(d["del"][0], d["del"][1], NORMAL),
            "hunk": _fmt(d["hunk"][0], d["hunk"][1], NORMAL),
            "header": _fmt(d["header"][0], "", BOLD),
        }

    def format_for_line(self, text: str) -> Optional[QTextCharFormat]:
        """The format for one diff line (``None`` = context, no change)."""
        if text.startswith("@@"):
            return self._formats["hunk"]
        if text.startswith("diff --git") or text.startswith("index "):
            return self._formats["header"]
        if text.startswith("+++ ") or text.startswith("--- "):
            return self._formats["header"]
        if text.startswith("+"):
            return self._formats["add"]
        if text.startswith("-"):
            return self._formats["del"]
        return None

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt name
        fmt = self.format_for_line(text)
        if fmt is not None:
            self.setFormat(0, len(text), fmt)


def monospace_font(point_size: float = MONO_PT) -> QFont:
    """The GUI's diff/console font with a sensible family."""
    font = QFont(MONO_FONT)
    font.setPointSizeF(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font
