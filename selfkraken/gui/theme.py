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

#: gap between sibling widgets inside a group (header -> content)
LAYOUT_SPACING: Final[int] = SPACE_4
#: gap between two sections of a pane
PANEL_SPACING: Final[int] = SPACE_8

# --------------------------------------------------------------------------- #
# control + row heights, one border style
# --------------------------------------------------------------------------- #
BORDER_WIDTH: Final[int] = 1
RADIUS: Final[int] = 4  #: inputs, buttons, badges, chips — everything rounded
RADIUS_PILL: Final[int] = 9  #: half the pill height, so it reads as a capsule

ROW_HEIGHT: Final[int] = 22  #: tables, trees, dock titles
#: a section header is one notch shorter than a row and sits 4px above its own
#: content, so panes read as one continuous column instead of stacked bands
SECTION_HEADER_HEIGHT: Final[int] = 20
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

#: default dock widths as a share of the window: the graph keeps the rest.
#: The diff lives in the centre now, so the review column needs less and the
#: code editor more (a side-by-side diff wants ~700px to be readable).
LEFT_DOCK_RATIO: Final[float] = 0.16
REVIEW_DOCK_RATIO: Final[float] = 0.28
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

UI_FONT: Final[str] = "Switzer"
MONO_FONT: Final[str] = "IBM Plex Mono"
#: families tried for the diff/console font when the bundled Plex Mono is absent
MONO_FALLBACKS: Final[tuple[str, ...]] = (
    "IBM Plex Mono", "DejaVu Sans Mono", "Ubuntu Mono", "Liberation Mono",
    "Noto Mono", "Menlo", "Consolas", "Courier New",
)
FONT_PT: Final[float] = 9.0      # 12px: the UI's body size
META_PT: Final[float] = 8.0     # 10.5px: section labels, counters, hints
MONO_PT: Final[float] = 8.5     # 11px: paths, log lines, diff

# --------------------------------------------------------------------------- #
# resolved font families
# --------------------------------------------------------------------------- #
# ``QFont("")`` is *not* "the UI font": on X11 it resolves to a fixed-pitch face
# (Andale Mono here), which is how the whole window went monospace on a real
# desktop.  The app therefore measures the platform font once at startup and
# hands the concrete family to everything that builds a QFont by hand.
_UI_FAMILY: str | None = None
_MONO_FAMILY: str | None = None


def set_ui_family(family: str) -> None:
    """Remember the concrete UI family (called once fonts are resolved)."""
    global _UI_FAMILY
    if (family or "").strip():
        _UI_FAMILY = family.strip()


def set_mono_family(family: str) -> None:
    global _MONO_FAMILY
    if (family or "").strip():
        _MONO_FAMILY = family.strip()


def ui_family() -> str:
    """Family for every hand-built UI font — never empty, never monospace."""
    return _UI_FAMILY or UI_FONT or "Sans Serif"


def mono_family() -> str:
    """Family for the diff / sha / test-output font."""
    return _MONO_FAMILY or MONO_FONT or "Monospace"

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
    #: the two interaction fills every hover/pressed state uses
    hover: str
    pressed: str
    #: the application's own red: the wordmark, the pen of a live mark
    pen: str
    lanes: tuple[str, ...]
    #: proposal status -> (foreground, background)
    status: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ref-badge kind -> (foreground, background, border)
    badge: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: diff line kind -> (foreground, background or "" for none)
    diff: dict[str, tuple[str, str]] = field(default_factory=dict)


DARK: Final[Palette] = Palette(
    name="dark",
    # flat greys, one blue: panels and page are the same tone and the 1px line
    # is what separates them, so nothing reads as a raised card.
    window="#444444",
    panel="#444444",
    base="#333333",
    surface="#333333",
    stripe="#3d3d3d",
    text="#dddddd",
    muted="#a0a0a0",
    border="#5a5a5a",
    accent="#53a0e3",
    accent_text="#12222e",
    selection_bg="#48627a",
    selection_text="#ffffff",
    ok="#5c8a5c",
    err="#cc3333",
    warning="#e8a735",
    error_bg="#3a2323",
    hover="#686868",
    pressed="#5e5e5e",
    pen="#ff2d3f",
    lanes=(
        "#4a90d9", "#e8a735", "#5bb85f", "#c964a8",
        "#3fa9a0", "#e8615a", "#7a6bd6", "#8a9a5b",
    ),
    status={
        "proposed": ("#79b8ea", "#26384a"),
        "applied": ("#e8a735", "#3a2c12"),
        "tests_passed": ("#8fbf8f", "#1e2d1e"),
        "tests_failed": ("#e88a8a", "#3a1c1c"),
        "rejected": ("#b4b4b4", "#3a3a3a"),
        "reverted": ("#a79cc9", "#2c2638"),
    },
    badge={
        "head": ("#12222e", "#53a0e3", "#53a0e3"),
        "local": ("#9ccf9c", "#26332a", "#3f5f3f"),
        "remote": ("#c4c4c4", "#3a3a3a", "#5a5a5a"),
        "tag": ("#e8a735", "#3a2c12", "#7a5a1f"),
    },
    diff={
        "add": ("#9ccf9c", "#252f25"),
        "del": ("#e88a8a", "#332020"),
        "hunk": ("#53a0e3", "#26323c"),  # the accent, not a third hue
        "header": ("#dddddd", ""),
    },
)

LIGHT: Final[Palette] = Palette(
    name="light",
    window="#eceef1",
    panel="#eceef1",
    base="#f6f7f9",
    surface="#f6f7f9",
    stripe="#e4e7eb",
    text="#232a31",
    muted="#5e6873",
    border="#c3c9d1",
    accent="#1d6fb8",
    accent_text="#f2f8ff",
    selection_bg="#bcd6ee",
    selection_text="#12222e",
    ok="#3d7a3d",
    err="#b52a2a",
    warning="#a86a06",
    error_bg="#f7dede",
    hover="#dfe3e8",
    pressed="#d2d8df",
    pen="#e00d22",
    lanes=(
        "#2f6fa8", "#a86a06", "#3d7a3d", "#9a4b80",
        "#2f7f78", "#b52a2a", "#5b4fb0", "#6a7440",
    ),
    status={
        "proposed": ("#1d4f7a", "#d6e6f4"),
        "applied": ("#7a4c04", "#f6e6cc"),
        "tests_passed": ("#2c5c2c", "#dcecdc"),
        "tests_failed": ("#8c2020", "#f7dede"),
        "rejected": ("#4e5761", "#e2e5e9"),
        "reverted": ("#4b3f75", "#e6e2f2"),
    },
    badge={
        "head": ("#f2f8ff", "#1d6fb8", "#1d6fb8"),
        "local": ("#2c5c2c", "#dcecdc", "#9cc09c"),
        "remote": ("#4e5761", "#eef0f3", "#c3c9d1"),
        "tag": ("#7a4c04", "#f6e6cc", "#d3ae72"),
    },
    diff={
        "add": ("#2c5c2c", "#dcecdc"),
        "del": ("#8c2020", "#f7dede"),
        "hunk": ("#1d6fb8", "#dbe8f4"),  # the accent
        "header": ("#232a31", ""),
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
    """Empty on purpose — the UI family is set on the application font instead.

    A ``font-family`` in the global ``QWidget`` rule wins over every
    per-widget ``setFont()``, which silently made the monospace diff viewer
    proportional on a real desktop.  :func:`set_ui_family` plus
    ``QApplication.setFont`` carry the family instead.
    """
    return ""


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
        background: {p.window};
        font-family: "{UI_FONT}";
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
        background: {p.surface}; color: {p.text};
        border: {edge}; border-radius: {RADIUS}px;
        padding: {SPACE_2}px {SPACE_8}px; min-height: {BUTTON_INNER}px;
    }}
    QPushButton:hover {{ background: {p.hover}; border-color: {p.border}; }}
    QPushButton:pressed {{ background: {p.pressed}; }}
    QPushButton:default {{ background: {p.accent}; color: {p.accent_text}; border-color: {p.accent}; }}
    QPushButton:default:hover {{ background: {p.accent}; border-color: {p.text}; }}
    QPushButton:disabled {{ color: {p.muted}; background: {p.window}; border-color: {p.border}; }}
    QPushButton:focus {{ border: {BORDER_WIDTH}px solid {p.accent}; }}

    /* ------------------------------------------------------------ inputs */
    QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
        background: {p.surface}; color: {p.text};
        border: {edge}; border-radius: {RADIUS}px;
        padding: {CONTROL_PADDING_V}px {CELL_PADDING}px;
        selection-background-color: {p.selection_bg};
        selection-color: {p.selection_text};
    }}
    /* min-height belongs to one-line inputs only: on a multi-line editor it
       becomes the widget's minimum height and silently outvotes the box's own
       (measured) minimum, crushing the diff viewer to a single line */
    QLineEdit, QComboBox, QSpinBox {{ min-height: {INPUT_INNER}px; }}
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
    /* Tabs are pills in the top bar, not folders: no outline of their own,
       a soft fill on hover, and the open one told by brightness and weight. */
    QTabBar::tab {{
        background: transparent; color: {p.muted};
        border: {BORDER_WIDTH}px solid transparent;
        border-radius: {RADIUS}px;
        padding: {SPACE_4}px {SPACE_12}px;
        margin-right: {SPACE_2}px;
        min-height: {ROW_HEIGHT - 2 * SPACE_4 - BORDER_WIDTH}px;
        font-weight: 500;
    }}
    QTabBar::tab:hover:!selected {{ background: {p.hover}; color: {p.text}; }}
    QTabBar::tab:selected {{
        background: {p.hover}; color: {p.text};
        font-weight: 600;
    }}
    /* the close box on a file tab: a quiet glyph in the tab's own colour, never
       a filled red button — it sits next to text, not in a dialog title bar */
    QTabBar QToolButton#tabClose {{
        background: transparent; border: none;
        color: {p.muted}; font-size: {META_PT:g}pt;
        padding: 0px; margin-left: {SPACE_2}px; margin-right: {SPACE_2}px;
        border-radius: {BADGE_RADIUS}px;
    }}
    QTabBar QToolButton#tabClose:hover {{
        color: {p.text}; background: {p.selection_bg};
    }}
    QTabBar QToolButton#tabClose:pressed {{ background: {p.border}; color: {p.text}; }}

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
