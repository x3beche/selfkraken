"""Read-only unified-diff viewer with syntax highlighting.

``+`` lines green, ``-`` lines red, ``@@`` hunk headers cyan, file headers
bold — plus subtle backgrounds so the colours never have to carry meaning
alone (WCAG-friendly).  :meth:`DiffHighlighter.format_for_line` is a pure
lookup that tests can call without a document.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from selfkraken.gui.theme import MONO_FONT, Palette


def _fmt(fg: str, bg: str = "", bold: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(fg))
    if bg:
        fmt.setBackground(QColor(bg))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    return fmt


class DiffHighlighter(QSyntaxHighlighter):
    """QSyntaxHighlighter for a ``git diff`` text."""

    def __init__(self, document, palette: Palette) -> None:
        super().__init__(document)
        self._palette = palette
        d = palette.diff
        self._formats: dict[str, QTextCharFormat] = {
            "add": _fmt(d["add"][0], d["add"][1]),
            "del": _fmt(d["del"][0], d["del"][1]),
            "hunk": _fmt(d["hunk"][0], d["hunk"][1], bold=True),
            "header": _fmt(d["header"][0], "", bold=True),
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


def monospace_font(point_size: float = 9.5) -> QFont:
    """The GUI's diff/console font with a sensible family."""
    font = QFont(MONO_FONT)
    font.setPointSizeF(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font
