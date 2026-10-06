"""Colour palettes and the Qt stylesheet for the selfkraken desktop GUI.

Pure Python on purpose — no Qt import here, so the tokens can be unit-tested
anywhere.  Colours are picked for WCAG-AA contrast (≥4.5:1 for body text,
≥3:1 for lane graphics) on both palettes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

UI_FONT: Final[str] = "DejaVu Sans"
MONO_FONT: Final[str] = "DejaVu Sans Mono"

LANE_COLORS_DARK: Final[tuple[str, ...]] = (
    "#54a6ff",  # blue
    "#d2a8ff",  # purple
    "#f0883e",  # orange
    "#4ac26b",  # green
    "#ef6bb4",  # pink
    "#2ed3c6",  # teal
    "#e3b341",  # yellow
    "#ff7b72",  # red
)
LANE_COLORS_LIGHT: Final[tuple[str, ...]] = (
    "#0550ae",  # blue
    "#8250df",  # purple
    "#bc4c00",  # orange
    "#1a7f37",  # green
    "#bf3989",  # pink
    "#0b7285",  # teal
    "#8a6200",  # yellow-brown
    "#cf222e",  # red
)


@dataclass(frozen=True)
class Palette:
    """Every colour token the GUI paints with, as hex strings."""

    name: str
    window: str
    panel: str
    base: str
    text: str
    muted: str
    border: str
    accent: str
    accent_text: str
    selection_bg: str
    selection_text: str
    ok: str
    err: str
    error_bg: str
    lanes: tuple[str, ...]
    #: proposal status -> (foreground, background)
    status: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ref-badge kind -> (foreground, background, border)
    badge: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: diff line kind -> (foreground, background or "" for none)
    diff: dict[str, tuple[str, str]] = field(default_factory=dict)


DARK: Final[Palette] = Palette(
    name="dark",
    window="#161b22",
    panel="#1c222c",
    base="#0f141a",
    text="#e6edf7",
    muted="#9aa6b5",
    border="#303a46",
    accent="#54a6ff",
    accent_text="#0c1118",
    selection_bg="#2a4d7a",
    selection_text="#f0f6ff",
    ok="#4ac26b",
    err="#ff7b72",
    error_bg="#3a1113",
    lanes=LANE_COLORS_DARK,
    status={
        "proposed": ("#79c0ff", "#0c2d52"),
        "applied": ("#f0883e", "#3d2307"),
        "tests_passed": ("#56d364", "#0f2d16"),
        "tests_failed": ("#ff9d95", "#490202"),
        "rejected": ("#aab5c3", "#2a313a"),
        "reverted": ("#d2a8ff", "#271a45"),
    },
    badge={
        "head": ("#0c1118", "#54a6ff", "#54a6ff"),
        "local": ("#56d364", "#12261e", "#238636"),
        "remote": ("#9aa6b5", "#1c222c", "#444c56"),
        "tag": ("#e3b341", "#2d2305", "#9e6a03"),
    },
    diff={
        "add": ("#4ac26b", "#0d2818"),
        "del": ("#f85149", "#2d0e0f"),
        "hunk": ("#3fc6f0", "#0c2030"),
        "header": ("#e6edf7", ""),
    },
)

LIGHT: Final[Palette] = Palette(
    name="light",
    window="#f2f4f8",
    panel="#ffffff",
    base="#ffffff",
    text="#1f2328",
    muted="#57606a",
    border="#d0d7de",
    accent="#0550ae",
    accent_text="#ffffff",
    selection_bg="#cce5ff",
    selection_text="#0a3069",
    ok="#1a7f37",
    err="#cf222e",
    error_bg="#ffebe9",
    lanes=LANE_COLORS_LIGHT,
    status={
        "proposed": ("#0550ae", "#ddeeff"),
        "applied": ("#9a4a09", "#ffe8d2"),
        "tests_passed": ("#116329", "#d9fbe0"),
        "tests_failed": ("#8b1507", "#ffdede"),
        "rejected": ("#57606a", "#e7ebf0"),
        "reverted": ("#6639ba", "#eee7fa"),
    },
    badge={
        "head": ("#ffffff", "#0550ae", "#0550ae"),
        "local": ("#116329", "#dafbe1", "#4ac26b"),
        "remote": ("#57606a", "#f2f4f8", "#afb8c1"),
        "tag": ("#7d5a00", "#fff8c5", "#d4a72e"),
    },
    diff={
        "add": ("#116329", "#dafbe1"),
        "del": ("#cf222e", "#ffebe9"),
        "hunk": ("#0550ae", "#ddf4ff"),
        "header": ("#1f2328", ""),
    },
)


def lane_color(palette: Palette, lane: int) -> str:
    """Stable lane colour for a lane index (palette order, wrapping)."""
    return palette.lanes[lane % len(palette.lanes)]


def resolve(name: str, prefers_dark: bool | None = None) -> Palette:
    """Pick a palette for ``--theme`` (auto|dark|light).

    ``prefers_dark`` is the caller's answer to "does the system want dark?"
    (Qt's ``styleHints().colorScheme()`` in the app); ``None`` falls back to
    the dark palette, matching selfkraken's GitKraken-ish look.
    """
    choice = (name or "auto").strip().lower()
    if choice == "dark":
        return DARK
    if choice == "light":
        return LIGHT
    if choice not in ("", "auto"):
        raise ValueError(f"unknown theme {name!r} — use auto, dark or light")
    return DARK if (prefers_dark is None or prefers_dark) else LIGHT


def stylesheet(p: Palette) -> str:
    """One app-wide stylesheet: rounded panels, clear focus, tidy tables."""
    return f"""
    QMainWindow, QDialog {{ background: {p.window}; }}
    QWidget {{ color: {p.text}; background: {p.window}; font-size: 10pt; }}
    QToolTip {{ background: {p.panel}; color: {p.text}; border: 1px solid {p.border}; }}

    QToolBar {{ background: {p.panel}; border-bottom: 1px solid {p.border};
                spacing: 6px; padding: 4px; }}
    QToolBar QToolButton {{ background: transparent; border: 1px transparent;
                padding: 5px 9px; border-radius: 6px; color: {p.text}; }}
    QToolBar QToolButton:hover {{ background: {p.selection_bg}; color: {p.selection_text}; }}
    QToolBar QToolButton:disabled {{ color: {p.muted}; }}

    QPushButton {{ background: {p.panel}; color: {p.text};
                border: 1px solid {p.border}; border-radius: 6px;
                padding: 5px 14px; }}
    QPushButton:hover {{ border-color: {p.accent}; }}
    QPushButton:default {{ background: {p.accent}; color: {p.accent_text};
                border-color: {p.accent}; }}
    QPushButton:disabled {{ color: {p.muted}; background: {p.window}; }}
    QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus,
    QComboBox:focus, QAbstractItemView:focus {{
                outline: none; border: 2px solid {p.accent}; }}

    QComboBox, QLineEdit, QPlainTextEdit {{ background: {p.base}; color: {p.text};
                border: 1px solid {p.border}; border-radius: 6px; padding: 3px 6px;
                selection-background-color: {p.selection_bg};
                selection-color: {p.selection_text}; }}
    QComboBox QAbstractItemView {{ background: {p.base}; border: 1px solid {p.border};
                selection-background-color: {p.selection_bg};
                selection-color: {p.selection_text}; }}

    QDockWidget {{ border: 1px solid {p.border}; titlebar-close-icon: none;
                titlebar-normal-icon: none; }}
    QDockWidget::title {{ background: {p.panel}; color: {p.text};
                padding: 5px 8px; border-bottom: 1px solid {p.border}; }}

    QTabWidget::pane {{ border: 1px solid {p.border}; background: {p.window}; }}
    QTabBar::tab {{ background: {p.panel}; color: {p.muted}; padding: 6px 14px;
                border: 1px solid {p.border}; border-bottom: none;
                border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background: {p.window}; color: {p.text};
                border-bottom: 2px solid {p.accent}; }}

    QTreeView, QListView, QTableView {{ background: {p.base}; color: {p.text};
                border: 1px solid {p.border};
                outline: 0; }}
    QHeaderView::section {{ background: {p.panel}; color: {p.muted};
                border: none; border-right: 1px solid {p.border};
                border-bottom: 1px solid {p.border}; padding: 5px 8px;
                font-weight: bold; }}
    QTableView::item, QTreeView::item, QListView::item {{ padding: 2px; }}
    QTableView::item:selected, QTreeView::item:selected,
    QListView::item:selected {{ background: {p.selection_bg}; color: {p.selection_text}; }}
    QTableView::item:focus, QTreeView::item:focus, QListView::item:focus {{
                border: 1px solid {p.accent}; }}

    QGroupBox {{ border: 1px solid {p.border}; border-radius: 8px;
                margin-top: 10px; padding-top: 6px; background: {p.panel}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                color: {p.muted}; font-weight: bold; }}

    QStatusBar {{ background: {p.panel}; border-top: 1px solid {p.border};
                color: {p.text}; }}
    QProgressBar {{ border: 1px solid {p.border}; border-radius: 4px;
                background: {p.base}; text-align: center; max-height: 16px; }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

    QLabel[hintRole="muted"] {{ color: {p.muted}; }}
    QLabel[hintRole="error"] {{ color: {p.err}; font-weight: bold; }}
    QLabel[hintRole="ok"] {{ color: {p.ok}; font-weight: bold; }}
    """
