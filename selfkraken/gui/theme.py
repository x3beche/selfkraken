"""Design tokens and the Qt stylesheet for the selfkraken desktop GUI.

Everything the UI measures itself with lives here: the spacing scale, the row
and control heights, the single border style, the font tiers and both colour
palettes.  Layout code (:mod:`selfkraken.gui.app`, :mod:`~selfkraken.gui.graph_widget`)
imports these constants instead of writing numbers, and the stylesheet below is
built from them, so a change here is a change everywhere.

Pure Python on purpose — no Qt import, so the tokens can be unit-tested
anywhere.  Colours are picked for WCAG-AA contrast (≥4.5:1 for body text,
≥3:1 for lane graphics) on both palettes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

# --------------------------------------------------------------------------- #
# spacing scale — the only horizontal/vertical gaps the UI uses
# --------------------------------------------------------------------------- #
SPACE_2: Final[int] = 2
SPACE_4: Final[int] = 4
SPACE_6: Final[int] = 6
SPACE_8: Final[int] = 8
SPACE_12: Final[int] = 12
SPACING_SCALE: Final[tuple[int, ...]] = (SPACE_2, SPACE_4, SPACE_6, SPACE_8, SPACE_12)

#: contentsMargins: the window adds none, docks/panels and group content add 6,
#: and *every* nested layout adds 0 (no double margins anywhere).
MARGIN_WINDOW: Final[int] = 0
MARGIN_PANEL: Final[int] = SPACE_6
MARGIN_GROUP: Final[int] = SPACE_6
MARGIN_NESTED: Final[int] = 0

#: gap between sibling widgets inside a panel / group
LAYOUT_SPACING: Final[int] = SPACE_4
PANEL_SPACING: Final[int] = SPACE_6

# --------------------------------------------------------------------------- #
# control + row heights, one border style
# --------------------------------------------------------------------------- #
BORDER_WIDTH: Final[int] = 1
RADIUS: Final[int] = 4  #: inputs, buttons, badges, chips — everything rounded
RADIUS_PILL: Final[int] = 9  #: half the pill height, so it reads as a capsule

ROW_HEIGHT: Final[int] = 22  #: tables, trees, section headers, dock titles
GRAPH_ROW_HEIGHT: Final[int] = 24
HEADER_HEIGHT: Final[int] = 22
BUTTON_HEIGHT: Final[int] = 24
INPUT_HEIGHT: Final[int] = 24
DOCK_TITLE_HEIGHT: Final[int] = 22
#: Qt insets a dock title's own text by this much before the stylesheet padding
#: (measured through the Fusion title bar; ``apply_theme`` re-derives it from the
#: live style), so the title lines up with the 6px content underneath it.
DOCK_TITLE_INSET: Final[int] = 4
STATUS_BAR_HEIGHT: Final[int] = 20
ACTIVITY_LOG_HEIGHT: Final[int] = 140
CELL_PADDING: Final[int] = SPACE_6  #: tables: 6px each side, header text too
SPLITTER_HIT: Final[int] = 4  #: 1px line inside a 4px grab area
SCROLLBAR_WIDTH: Final[int] = 10  #: thin bars: the content, not the chrome, is UI
PILL_HEIGHT: Final[int] = 20

#: badges and chips share one shape: ref badges in the graph, status chips in
#: the proposals table, the model pill in the toolbar.
BADGE_HEIGHT: Final[int] = 16
BADGE_RADIUS: Final[int] = 3
BADGE_H_PAD: Final[int] = SPACE_4
BADGE_GAP: Final[int] = SPACE_4

#: content-box heights: the control height minus its own padding and border, so
#: focus can change the border *colour* only and never move a pixel.
#: vertical padding of a bordered control; the stylesheet and the text boxes
#: both use it, so a box's height can be computed from its line count exactly.
CONTROL_PADDING_V: Final[int] = SPACE_2
BUTTON_INNER: Final[int] = BUTTON_HEIGHT - 2 * CONTROL_PADDING_V - 2 * BORDER_WIDTH
INPUT_INNER: Final[int] = INPUT_HEIGHT - 2 * CONTROL_PADDING_V - 2 * BORDER_WIDTH
HEADER_INNER: Final[int] = HEADER_HEIGHT - 2 * SPACE_2 - BORDER_WIDTH
PILL_INNER: Final[int] = PILL_HEIGHT - 2 * SPACE_2 - 2 * BORDER_WIDTH

# --------------------------------------------------------------------------- #
# fonts — one body size, one smaller size for meta text
# --------------------------------------------------------------------------- #
#: The offscreen platform has no system UI font; DejaVu Sans is the one family
#: that is always installed there.  An empty family means "ask Qt".
IS_OFFSCREEN: Final[bool] = os.environ.get("QT_QPA_PLATFORM", "").strip().lower().startswith(
    "offscreen"
)

UI_FONT: Final[str] = "DejaVu Sans" if IS_OFFSCREEN else ""
MONO_FONT: Final[str] = "DejaVu Sans Mono"
FONT_PT: Final[float] = 9.0 if IS_OFFSCREEN else 9.5
META_PT: Final[float] = FONT_PT - 1.0
MONO_PT: Final[float] = 9.0

#: default window size (the geometry the screenshots are taken at is bigger)
WINDOW_SIZE: Final[tuple[int, int]] = (1280, 800)

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
    """Every colour token the GUI paints with, as hex strings.

    ``window`` is the page background (bg), ``panel`` the chrome and headers
    (bg-alt), ``base`` the deep background tables and the graph are painted on,
    ``surface`` the inputs.
    """

    name: str
    window: str
    panel: str
    base: str
    surface: str
    stripe: str
    text: str
    muted: str
    border: str
    accent: str
    accent_text: str
    selection_bg: str
    selection_text: str
    ok: str
    err: str
    warning: str
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
    surface="#141a22",
    stripe="#1a212a",
    text="#e6edf7",
    muted="#9aa6b5",
    border="#303a46",
    accent="#54a6ff",
    accent_text="#0c1118",
    selection_bg="#2a4d7a",
    selection_text="#f0f6ff",
    ok="#4ac26b",
    err="#ff7b72",
    warning="#e3b341",
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
        "hunk": ("#54a6ff", "#0c2030"),  # accent, not a second hue
        "header": ("#e6edf7", ""),
    },
)

LIGHT: Final[Palette] = Palette(
    name="light",
    window="#f2f4f8",
    panel="#ffffff",
    base="#ffffff",
    surface="#ffffff",
    stripe="#f6f8fa",
    text="#1f2328",
    muted="#57606a",
    border="#d0d7de",
    accent="#0550ae",
    accent_text="#ffffff",
    selection_bg="#cce5ff",
    selection_text="#0a3069",
    ok="#1a7f37",
    err="#cf222e",
    warning="#9a6700",
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
        "hunk": ("#0550ae", "#ddf4ff"),  # accent
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


def ui_font_css() -> str:
    """The ``font-family`` declaration, or '' to keep the platform UI font."""
    return f"font-family: {UI_FONT};" if UI_FONT else ""


def stylesheet(p: Palette, dock_title_inset: int = DOCK_TITLE_INSET) -> str:
    """One app-wide stylesheet, built entirely from the tokens above.

    The rules of the design system: a single 1px solid border in the palette's
    border colour, radius 4 on controls and 0 where a widget is flush with a
    panel edge, focus that only recolours that border (never resizes it), and
    the same 6px left padding for dock titles, section headers and table cells
    so every text column starts on one line.

    ``dock_title_inset`` is the style's own inset for dock title text; the
    title's padding is reduced by it so the title sits on the same left edge as
    the content underneath.
    """
    edge = f"{BORDER_WIDTH}px solid {p.border}"
    title_pad = max(MARGIN_PANEL - dock_title_inset, 0)
    return f"""
    /* ---------------------------------------------------------- surfaces */
    QMainWindow, QDialog {{ background: {p.window}; }}
    QWidget {{
        color: {p.text};
        {ui_font_css()}
        font-size: {FONT_PT:g}pt;
        background: {p.window};
    }}
    QToolTip {{
        background: {p.panel}; color: {p.text}; border: {edge};
        border-radius: {RADIUS}px; padding: {SPACE_2}px {SPACE_4}px;
        font-size: {META_PT:g}pt;
    }}

    /* ------------------------------------------------------------ menubar */
    QMenuBar {{ background: {p.panel}; border: none; spacing: 0px; }}
    QMenuBar::item {{
        background: transparent; color: {p.text};
        padding: {SPACE_4}px {SPACE_8}px;
    }}
    QMenuBar::item:selected {{ background: {p.selection_bg}; color: {p.selection_text}; }}
    QMenu {{
        background: {p.panel}; border: {edge};
        border-radius: {RADIUS}px; padding: {SPACE_2}px 0;
    }}
    QMenu::item {{ background: transparent; padding: {SPACE_2}px {SPACE_12}px; }}
    QMenu::item:selected {{ background: {p.selection_bg}; color: {p.selection_text}; }}
    QMenu::separator {{ height: {BORDER_WIDTH}px; background: {p.border}; margin: {SPACE_2}px {SPACE_6}px; }}

    /* -------------------------------------- toolbar: one compact flat row */
    QToolBar#mainToolBar {{
        background: {p.panel}; border: none;
        border-bottom: {edge};
        spacing: {SPACE_4}px; padding: {SPACE_4}px {MARGIN_PANEL}px;
    }}
    QToolBar#mainToolBar::separator {{
        width: {BORDER_WIDTH}px; background: {p.border};
        margin: {SPACE_4}px {SPACE_4}px;
    }}
    QToolBar#mainToolBar QToolButton {{
        background: transparent; color: {p.text};
        border: {BORDER_WIDTH}px solid transparent;
        border-radius: {RADIUS}px;
        padding: {SPACE_2}px {SPACE_8}px; min-height: {BUTTON_INNER}px;
    }}
    QToolBar#mainToolBar QToolButton:hover {{
        background: {p.selection_bg}; color: {p.selection_text};
    }}
    QToolBar#mainToolBar QToolButton:pressed,
    QToolBar#mainToolBar QToolButton:checked {{ border-color: {p.accent}; }}
    QToolBar#mainToolBar QToolButton:disabled {{ color: {p.muted}; background: transparent; }}
    QToolBar#mainToolBar QToolButton::menu-indicator {{ image: none; width: 0px; }}
    QLabel#modelPill {{
        background: {p.surface}; color: {p.muted};
        border: {edge}; border-radius: {RADIUS_PILL}px;
        padding: {SPACE_2}px {SPACE_8}px; min-height: {PILL_INNER}px;
        font-size: {META_PT:g}pt;
    }}

    /* ------------------------------------------------------------ buttons */
    QPushButton {{
        background: {p.panel}; color: {p.text};
        border: {edge}; border-radius: {RADIUS}px;
        padding: {SPACE_2}px {SPACE_8}px; min-height: {BUTTON_INNER}px;
    }}
    QPushButton:hover {{ border-color: {p.accent}; }}
    QPushButton:pressed {{ background: {p.selection_bg}; color: {p.selection_text}; }}
    QPushButton:default {{ background: {p.accent}; color: {p.accent_text}; border-color: {p.accent}; }}
    QPushButton:disabled {{ color: {p.muted}; background: {p.window}; border-color: {p.border}; }}
    QPushButton:focus {{ border: {BORDER_WIDTH}px solid {p.accent}; }}

    /* ------------------------------------------------------------ inputs */
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
        background: {p.surface}; color: {p.text};
        border: {edge}; border-radius: {RADIUS}px;
        padding: {CONTROL_PADDING_V}px {CELL_PADDING}px; min-height: {INPUT_INNER}px;
        selection-background-color: {p.selection_bg};
        selection-color: {p.selection_text};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: {BORDER_WIDTH}px solid {p.accent};
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{
        color: {p.muted}; background: {p.window};
    }}
    QComboBox QAbstractItemView {{
        background: {p.panel}; color: {p.text}; border: {edge};
        border-radius: {RADIUS}px;
        selection-background-color: {p.selection_bg};
        selection-color: {p.selection_text};
        padding: {SPACE_2}px;
    }}
    QCheckBox {{ spacing: {SPACE_4}px; background: transparent; }}
    QCheckBox::indicator {{ width: 13px; height: 13px; }}

    /* ------------------------------------------------------- dock widgets */
    QDockWidget {{
        background: {p.window}; border: none;
        titlebar-close-icon: none; titlebar-normal-icon: none;
    }}
    QDockWidget::title {{
        background: {p.panel}; color: {p.muted};
        font-size: {META_PT:g}pt; font-weight: bold;
        text-align: left;
        padding: {SPACE_4}px {title_pad}px;
        border-bottom: {edge};
    }}
    /* the 1px line *is* the splitter; the extra width is the grab area */
    QMainWindow::separator {{ background: {p.window}; width: {SPLITTER_HIT}px; height: {SPLITTER_HIT}px; }}
    QMainWindow::separator:hover {{ background: {p.border}; }}
    QScrollArea {{ background: {p.window}; border: none; }}

    /* --------------------------------------------------------------- tabs */
    QTabWidget::pane {{ border: {edge}; background: {p.base}; }}
    /* the docks' own tab widgets sit flush against the window edge and against
       the central pane: only the central pane draws the dividing line, so two
       framed widgets never meet as a double border */
    QTabWidget#leftTabs::pane {{ border: none; }}
    QTabBar::tab {{
        background: {p.panel}; color: {p.muted};
        border: {edge}; border-bottom: none;
        border-top-left-radius: {RADIUS}px; border-top-right-radius: {RADIUS}px;
        padding: {SPACE_2}px {CELL_PADDING}px; min-height: {ROW_HEIGHT - 2 * SPACE_2 - BORDER_WIDTH}px;
    }}
    QTabBar::tab:selected {{ background: {p.base}; color: {p.text}; }}
    QTabBar::tab:hover:!selected {{ color: {p.text}; }}

    /* ------------------------------------------------------ item views */
    QTreeView, QListView, QTableView, QColumnView {{
        background: {p.base}; color: {p.text};
        alternate-background-color: {p.stripe};
        border: none; outline: 0;
        selection-background-color: {p.selection_bg};
        selection-color: {p.selection_text};
        show-decoration-selected: 1;
    }}
    QHeaderView {{ background: {p.panel}; border: none; }}
    QHeaderView::section {{
        background: {p.panel}; color: {p.muted};
        border: none; border-bottom: {edge};
        padding: {SPACE_2}px {CELL_PADDING}px;
        min-height: {HEADER_INNER}px;
        font-size: {META_PT:g}pt; font-weight: bold;
    }}
    QHeaderView::section:horizontal {{ border-right: none; }}
    QTreeView::item, QListView::item, QTableView::item {{
        padding: {SPACE_4}px {CELL_PADDING}px; border: none;
    }}
    QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
        background: {p.selection_bg}; color: {p.selection_text};
    }}
    QTreeView::item:focus, QListView::item:focus, QTableView::item:focus {{
        border: {BORDER_WIDTH}px solid {p.accent}; border-radius: {RADIUS}px;
    }}

    /* ------------------------------------- section headers (no more boxes) */
    QFrame#sectionHeader {{ background: transparent; border: none; }}
    QPushButton#sectionTitle {{
        background: transparent; border: none; padding: 0px; min-height: 0px;
        text-align: left; color: {p.muted};
        font-size: {META_PT:g}pt; font-weight: bold;
    }}
    QPushButton#sectionTitle:disabled {{ color: {p.border}; }}
    QPushButton#sectionTitle:hover, QPushButton#sectionTitle:focus {{ color: {p.text}; }}
    QPushButton#sectionTitle:pressed {{ background: transparent; color: {p.text}; }}
    QFrame#sectionRule {{ background: {p.border}; border: none; }}

    /* ------------------------------------------------------- status, busy */
    QStatusBar {{
        background: {p.panel}; color: {p.text};
        border-top: {edge};
        font-size: {META_PT:g}pt;
    }}
    QStatusBar::item {{ border: none; }}
    QProgressBar {{
        border: {edge}; border-radius: {RADIUS}px;
        background: {p.base}; text-align: center;
        max-height: {SPACE_12 + SPACE_2}px;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: {RADIUS - 1}px; }}

    /* ------------------------------------- scrollbars: thin, no arrow pads */
    QScrollBar {{
        background: transparent;
        border: none;
    }}
    QScrollBar:vertical {{ width: {SCROLLBAR_WIDTH}px; margin: {SPACE_2}px; }}
    QScrollBar:horizontal {{ height: {SCROLLBAR_WIDTH}px; margin: {SPACE_2}px; }}
    QScrollBar::handle {{
        background: {p.border};
        border-radius: {(SCROLLBAR_WIDTH - 2 * SPACE_2) // 2}px;
        min-width: {SPACE_4}px; min-height: {SPACE_4}px;
    }}
    QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {p.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ------------------------------------------------------ text helpers */
    QLabel[hintRole="muted"] {{ color: {p.muted}; font-size: {META_PT:g}pt; }}
    QLabel[hintRole="error"] {{ color: {p.err}; font-weight: bold; }}
    QLabel[hintRole="ok"] {{ color: {p.ok}; font-weight: bold; }}
    QLabel#fieldLabel {{ color: {p.muted}; font-size: {META_PT:g}pt; }}
    QLabel#emptyState {{ color: {p.muted}; }}
    """
