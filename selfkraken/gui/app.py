"""The selfkraken desktop window: toolbar, graph, proposals, review loop.

``run()`` is what ``skraken gui`` calls.  Every slow operation (indexing, the
model call, apply+tests, import/export, sample data, graph load) is handed to
:class:`~selfkraken.gui.worker.JobRunner`, which opens its own ``Store`` in a
worker thread; the UI thread only does light SQLite reads for the tables.

``MainWindow(run_jobs_inline=True)`` runs those same jobs synchronously — the
tests drive the whole loop with it, without event-loop timing.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from selfkraken import diffs, indexer, portability, sample, workflow
from selfkraken.config import Settings, load_settings
from selfkraken.db import Proposal, Repo, Store, TestRun
from selfkraken.errors import SKError
from selfkraken.graph import GraphRow, layout, list_branches, read_commits
from selfkraken.workflow import ApplyOutcome, DEGRADED_MODE_HINT

from selfkraken.gui.diff_view import DiffHighlighter, monospace_font
from selfkraken.gui.graph_widget import GraphView, read_commit_detail
from selfkraken.gui.models import (
    BranchListModel,
    FileTreeModel,
    LogTableModel,
    ProposalTableModel,
    local_time,
)
from selfkraken.gui.theme import (
    ACTIVITY_LOG_HEIGHT,
    BADGE_HEIGHT,
    BADGE_H_PAD,
    BADGE_RADIUS,
    BORDER_WIDTH,
    BUTTON_HEIGHT,
    CELL_PADDING,
    CONTROL_PADDING_V,
    DOCK_TITLE_INSET,
    FONT_PT,
    INPUT_HEIGHT,
    LAYOUT_SPACING,
    MARGIN_NESTED,
    MARGIN_PANEL,
    META_PT,
    MONO_FONT,
    MONO_PT,
    PANEL_SPACING,
    PILL_HEIGHT,
    ROW_HEIGHT,
    SPACE_12,
    SPACE_4,
    SPACE_6,
    SPACE_8,
    STATUS_BAR_HEIGHT,
    UI_FONT,
    WINDOW_SIZE,
    Palette,
    resolve,
    stylesheet,
)
from selfkraken.gui.worker import JobRunner

MAX_REQUEST_CHARS = workflow.MAX_REQUEST_CHARS
MIN_REQUEST_CHARS = workflow.MIN_REQUEST_CHARS

GRAPH_LIMIT = 300
LOG_LIMIT = 200

#: remembered window geometry, kept inside the app's own data directory
WINDOW_STATE_FILENAME = "gui-window.json"
MIN_REMEMBERED_SIZE = (800, 600)

#: how many lines each read-only / input text box shows before it scrolls
RATIONALE_MAX_LINES = 6
COMMIT_MESSAGE_LINES = 3
REQUEST_LINES = 4
PASTED_DIFF_LINES = 6
TEST_OUTPUT_LINES = 7
DIFF_MIN_LINES = 6
#: the document margin inside a plain-text box: the stylesheet padding already
#: spaces the text, so the document itself adds nothing
TEXT_BOX_MARGIN = 0
#: a pixel of rounding slack, so a box that fits exactly never grows a scrollbar
TEXT_BOX_SLACK = 2
#: the big empty state: two escape hatches, centred in a generous frame
GUIDANCE_BUTTON_WIDTH = 160
GUIDANCE_PADDING = SPACE_12 * 2
#: the toolbar is text-only — a small icon slot so nothing ever grows the row
TOOLBAR_ICON_SIZE = SPACE_12
#: room for a repository name plus its [SAMPLE] tag
REPO_COMBO_WIDTH = 200
#: docks: how far they shrink and where they open
DOCK_MIN_WIDTH = 200
REVIEW_MIN_WIDTH = 360
LEFT_DOCK_WIDTH = 220
REVIEW_DOCK_WIDTH = 440
#: graph columns that are not the stretchy subject
AUTHOR_COLUMN_WIDTH = 130
#: activity log columns (Detail takes whatever is left)
LOG_WHEN_WIDTH = 120
LOG_KIND_WIDTH = 80
LOG_PROPOSAL_WIDTH = 70


# --------------------------------------------------------------------------- #
# small reusable widgets
# --------------------------------------------------------------------------- #
class BusyBar(QProgressBar):
    """Indeterminate busy indicator (always paired with a text status)."""

    def __init__(self, parent: Optional[QWidget] = None, width: int = 96) -> None:
        super().__init__(parent)
        self.setRange(0, 0)
        self.setTextVisible(False)
        self.setFixedWidth(width)
        self.setFixedHeight(SPACE_12)
        self.setVisible(False)
        self.setAccessibleName("Busy indicator")


class ElidedLabel(QLabel):
    """Single-line label that elides instead of clipping or stealing rows.

    A word-wrapped ``QLabel`` reports a two-line ``sizeHint`` for a one-line
    text, which is how the meta rows in the review pane read as clipped to any
    geometry check.  These labels stay exactly one line tall: they ask for the
    room the full text needs and, when the pane is narrower, show an ellipsis
    with the whole value in the tooltip.
    """

    def __init__(
        self,
        text: str = "",
        palette: Optional[Palette] = None,
        muted: bool = False,
        mono: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(text, parent)
        self._full = str(text)
        self._palette = palette
        self._muted = muted
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        style = []
        if mono:
            style.append(f"font-family: {MONO_FONT}; font-size: {MONO_PT:g}pt;")
        if muted and palette is not None:
            style.append(f"color: {palette.muted}; font-size: {META_PT:g}pt;")
        if style:
            self.setStyleSheet(" ".join(style))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt name
        self._full = str(text)
        self.setToolTip(self._full)
        self._repaint_elided()

    def fullText(self) -> str:  # noqa: N802 - Qt name
        return self._full

    def _repaint_elided(self) -> None:
        width = max(self.width() - 2, 0)
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, width
        ) if width else self._full
        if elided != QLabel.text(self):
            super().setText(elided)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        self._repaint_elided()

    def text(self) -> str:  # noqa: N802 - Qt name
        """The *full* text, never the visible ellipsis."""
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt name
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self._full) + SPACE_4, metrics.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt name
        hint = self.sizeHint()
        hint.setWidth(SPACE_12)
        return hint


class WrappedLabel(QLabel):
    """Word-wrapped label whose ``sizeHint`` knows the width it actually got.

    ``QLabel`` sizes a wrapped label from its *minimum* width, so a one-line
    message reported a two-line hint and looked clipped to any layout check.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt name
        hint = super().sizeHint()
        if self.wordWrap() and self.width() > 0:
            hint.setHeight(
                max(self.heightForWidth(self.width()), self.fontMetrics().height())
            )
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt name
        return self.sizeHint()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        self.updateGeometry()


class SectionHeader(QFrame):
    """Compact section header: uppercase semibold muted label + 1px rule.

    Replaces the titled ``QGroupBox`` frames, so the review pane is a stack of
    sections rather than boxes inside boxes.  A *collapsible* header is the
    same widget with a chevron and a ``toggled`` signal.
    """

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        palette: Palette,
        collapsible: bool = False,
        expanded: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sectionHeader")
        self._title = title
        self._note = ""
        self._collapsible = collapsible
        self._expanded = expanded
        # a button (not a tool button) so the label sits on the panel margin:
        # QToolButton always adds the style's own 6px text margin
        self._button = QPushButton(self)
        self._button.setObjectName("sectionTitle")
        self._button.setCheckable(collapsible)
        self._button.setChecked(expanded)
        self._button.setFlat(True)
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setFocusPolicy(
            Qt.FocusPolicy.TabFocus if collapsible else Qt.FocusPolicy.NoFocus
        )
        self._button.clicked.connect(self._on_clicked)
        self._rule = QFrame(self)
        self._rule.setObjectName("sectionRule")
        self._rule.setFixedHeight(BORDER_WIDTH)
        row = QHBoxLayout(self)
        row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        row.setSpacing(SPACE_4)
        row.addWidget(self._button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._rule, 1, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(ROW_HEIGHT)
        self._refresh()

    # -- text ---------------------------------------------------------------
    def _refresh(self) -> None:
        chevron = ("▾ " if self._expanded else "▸ ") if self._collapsible else ""
        note = f" · {self._note}" if self._note else ""
        self._button.setText(f"{chevron}{self._title.upper()}{note}")

    def setText(self, title: str) -> None:  # noqa: N802 - Qt name
        self._title = str(title)
        self._refresh()

    def text(self) -> str:  # noqa: N802 - Qt name
        return self._title

    def set_note(self, note: str) -> None:
        """Small trailing state, e.g. ``(PASSED, exit 0)``."""
        self._note = note
        self._refresh()

    # -- collapse -----------------------------------------------------------
    def isExpanded(self) -> bool:  # noqa: N802 - Qt name
        return self._expanded

    def setExpanded(self, expanded: bool) -> None:  # noqa: N802 - Qt name
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._button.setChecked(expanded)
        self._refresh()
        self.toggled.emit(expanded)

    def _on_clicked(self) -> None:
        if self._collapsible:
            self.setExpanded(self._button.isChecked())

    def isChecked(self) -> bool:  # noqa: N802 - Qt name
        return self._expanded

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt name
        if self._collapsible:
            self.setExpanded(checked)

    def setAccessible(self, name: str) -> None:
        self._button.setAccessibleName(name)
        self.setAccessibleName(name)


def field_label(text: str) -> QLabel:
    """The caption above/next to a field: the muted meta tier of the type scale."""
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def notice_page(message: str, palette: Palette, parent=None) -> QWidget:
    """Centred guidance text used for the smaller empty states."""
    page = QWidget(parent)
    layout_box = QVBoxLayout(page)
    layout_box.setContentsMargins(GUIDANCE_PADDING, MARGIN_PANEL, GUIDANCE_PADDING, MARGIN_PANEL)
    layout_box.setSpacing(0)
    label = WrappedLabel(message)
    label.setObjectName("emptyState")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setAccessibleName("Empty state message")
    layout_box.addStretch(1)
    layout_box.addWidget(label)
    layout_box.addStretch(1)
    return page


class ProposalStatusDelegate(QStyledItemDelegate):
    """Status cell: a coloured chip *and* the status text (never colour alone)."""

    def __init__(self, palette: Palette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._chip_font = QFont()
        self._chip_font.setPointSizeF(META_PT)

    def paint(self, painter, option, index) -> None:  # noqa: D102
        status = index.data(Qt.ItemDataRole.DisplayRole) or ""
        colours = self._palette.status.get(str(status))
        if colours is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        background = (
            QColor(self._palette.selection_bg)
            if option.state & QStyle.StateFlag.State_Selected
            else QColor(self._palette.base)
        )
        painter.fillRect(option.rect, background)
        font = painter.font()
        font.setPointSizeF(self._chip_font.pointSizeF())
        painter.setFont(font)
        metrics = QFontMetrics(font)
        chip = QRectF(
            option.rect.left() + CELL_PADDING,
            option.rect.center().y() - BADGE_HEIGHT / 2.0,
            metrics.horizontalAdvance(str(status)) + 2 * BADGE_H_PAD,
            BADGE_HEIGHT,
        )
        path = QPainterPath()
        path.addRoundedRect(chip, BADGE_RADIUS, BADGE_RADIUS)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colours[1]))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(colours[0])))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, str(status))
        painter.restore()


def guidance_page(
    message: str, palette: Palette, on_add: Callable[[], None], on_sample: Callable[[], None]
) -> QWidget:
    """Big centred empty state with the two actions that escape it."""
    page = QWidget()
    layout_box = QVBoxLayout(page)
    layout_box.setContentsMargins(GUIDANCE_PADDING, GUIDANCE_PADDING, GUIDANCE_PADDING, GUIDANCE_PADDING)
    layout_box.setSpacing(PANEL_SPACING)
    label = WrappedLabel(message)
    label.setObjectName("emptyState")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setAccessibleName("Empty state guidance")
    row = QHBoxLayout()
    row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
    row.setSpacing(LAYOUT_SPACING)
    row.setAlignment(Qt.AlignmentFlag.AlignCenter)
    add_button = QPushButton("Add Repo…")
    add_button.setObjectName("guidanceAddRepo")
    add_button.clicked.connect(on_add)
    sample_button = QPushButton("Load sample data")
    sample_button.setObjectName("guidanceLoadSample")
    sample_button.clicked.connect(on_sample)
    for button in (add_button, sample_button):
        button.setMinimumWidth(GUIDANCE_BUTTON_WIDTH)
        button.setDefault(False)
        row.addWidget(button)
    layout_box.addStretch(2)
    layout_box.addWidget(label)
    layout_box.addLayout(row)
    layout_box.addStretch(3)
    return page


_LINE_HEIGHTS: dict[float, int] = {}


def line_height(point_size: float) -> int:
    """Pixel height of one text line at *point_size* (measured once per size)."""
    key = round(float(point_size), 2)
    if key not in _LINE_HEIGHTS:
        font = QFont(UI_FONT)
        font.setPointSizeF(key)
        _LINE_HEIGHTS[key] = QFontMetrics(font).height()
    return _LINE_HEIGHTS[key]


def box_height(lines: int, point_size: float = FONT_PT) -> int:
    """Height of a plain-text box that shows exactly *lines* lines of text.

    Mirrors what the stylesheet gives the widget: a border and
    ``CONTROL_PADDING_V`` on each side, plus the slack that keeps a box whose
    text just fits from showing a scrollbar.  One line is exactly a row.
    """
    return (
        2 * BORDER_WIDTH
        + 2 * CONTROL_PADDING_V
        + 2 * TEXT_BOX_MARGIN
        + max(lines, 1) * line_height(point_size)
        + TEXT_BOX_SLACK
    )


def compact_item_view(view: QAbstractItemView, select_rows: bool = True) -> None:
    """One row rhythm for every table, tree and list in the window.

    No grid lines (the row height and the alternating tint carry the rows),
    22px rows, elided text with the full value in the tooltip, and left-aligned
    header text sitting on the same 6px line as the cells below it.
    """
    if hasattr(view, "setShowGrid"):
        view.setShowGrid(False)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    if select_rows:
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    vertical = view.verticalHeader() if hasattr(view, "verticalHeader") else None
    if vertical is not None:
        vertical.setVisible(False)
        vertical.setDefaultSectionSize(ROW_HEIGHT)
    if isinstance(view, QTableView):
        header = view.horizontalHeader()
    elif isinstance(view, QTreeView):
        header = view.header()
    else:
        header = None
    if header is not None:
        header.setHighlightSections(False)
        header.setSectionsMovable(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    if isinstance(view, QListView):
        view.setUniformItemSizes(True)


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    """The selfkraken desktop window and its controller methods."""

    def __init__(
        self,
        *,
        run_jobs_inline: bool = False,
        theme: str = "auto",
        settings: Optional[Settings] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or load_settings()
        self.palette: Palette = resolve(theme, prefers_dark=_system_prefers_dark())
        self.store = Store(self.settings.db_path)  # UI-thread light reads only
        self.runner = JobRunner(inline=run_jobs_inline, parent=self)
        self.runner.started.connect(self._job_started)
        self.runner.finished.connect(self._job_finished)

        #: tests replace this to accept (or refuse) dialogs programmatically
        self.confirm_handler: Optional[Callable[[str, str], bool]] = None

        self._current_repo_id: Optional[int] = None
        self._selected_proposal_id: Optional[int] = None
        self._suppress_signals = False
        self._files: list[str] = []
        self._request_valid = False

        self.setWindowTitle("selfkraken — repository-local Git assistant")

        self._build_actions()
        self._build_toolbar()
        self._build_central()
        self._build_left_dock()
        self._build_right_dock()
        self._build_bottom_dock()
        self._build_status_bar()
        self._build_menu()
        self._set_tab_order()
        if not self._restore_geometry():
            self.resize(*WINDOW_SIZE)

        self.refresh()

    # ---------------------------------------------------------------- toolbar
    def _build_actions(self) -> None:
        self.act_add_repo = QAction("&Add Repo", self)
        self.act_add_repo.setToolTip(
            "Register a git repository and give it a test command"
        )
        self.act_add_repo.triggered.connect(self.add_repo_dialog)

        self.act_index = QAction("&Index", self)
        self.act_index.setToolTip("Index the selected repository's files (Ctrl+I)")
        self.act_index.triggered.connect(self.index_repo)

        self.act_tests = QAction("Run &Tests", self)
        self.act_tests.setToolTip("Run the repository's test command (Ctrl+T)")
        self.act_tests.triggered.connect(self.run_tests)

        self.act_export = QAction("E&xport…", self)
        self.act_export.setToolTip(
            "Export repos, proposals, test runs and the audit log as JSON"
        )
        self.act_export.triggered.connect(self.export_dialog)

        self.act_import = QAction("&Import…", self)
        self.act_import.setToolTip("Import a JSON export file")
        self.act_import.triggered.connect(self.import_dialog)

        self.act_sample_load = QAction("Load [SAMPLE] &data", self)
        self.act_sample_load.setToolTip(
            "Create the [SAMPLE] demo repo and proposal (never touches your repos)"
        )
        self.act_sample_load.triggered.connect(self.load_sample)

        self.act_sample_remove = QAction("Remove [SAMPLE] &data", self)
        self.act_sample_remove.setToolTip("Delete the [SAMPLE] repo and its rows")
        self.act_sample_remove.triggered.connect(self.remove_sample)

        self.act_refresh = QAction("Re&fresh", self)
        self.act_refresh.setShortcut(QKeySequence("F5"))
        self.act_refresh.triggered.connect(self.refresh)

        self.act_new_proposal = QAction("&New proposal", self)
        self.act_new_proposal.setShortcut(QKeySequence("Ctrl+N"))
        self.act_new_proposal.setToolTip("Focus and clear the new-proposal form (Ctrl+N)")
        self.act_new_proposal.triggered.connect(self.focus_new_proposal)

        self.act_tab_graph = QAction("Show &Graph tab", self)
        self.act_tab_graph.setShortcut(QKeySequence("Ctrl+1"))
        self.act_tab_graph.triggered.connect(lambda: self.tabs.setCurrentIndex(0))

        self.act_tab_proposals = QAction("Show &Proposals tab", self)
        self.act_tab_proposals.setShortcut(QKeySequence("Ctrl+2"))
        self.act_tab_proposals.triggered.connect(lambda: self.tabs.setCurrentIndex(1))

        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_help = QAction("&Keyboard shortcuts", self)
        self.act_help.setShortcut(QKeySequence(Qt.Key.Key_F1))
        self.act_help.triggered.connect(self.show_shortcuts)

        for action in (
            self.act_refresh, self.act_new_proposal, self.act_tab_graph,
            self.act_tab_proposals, self.act_quit, self.act_help,
        ):
            self.addAction(action)

    def _build_toolbar(self) -> None:
        """One compact row: repo selector | actions | sample menu … model pill."""
        bar = QToolBar("Main toolbar")
        bar.setObjectName("mainToolBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.setIconSize(QSize(TOOLBAR_ICON_SIZE, TOOLBAR_ICON_SIZE))
        self.addToolBar(bar)
        self.toolbar = bar

        repo_label = QLabel("Repo:")
        repo_label.setObjectName("fieldLabel")
        repo_label.setAccessibleName("Repository selector label")
        self.repo_combo = QComboBox()
        self.repo_combo.setMinimumWidth(REPO_COMBO_WIDTH)
        self.repo_combo.setFixedHeight(INPUT_HEIGHT)
        self.repo_combo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.repo_combo.setAccessibleName("Repository selector")
        self.repo_combo.setToolTip("The repository every toolbar action works on")
        self.repo_combo.currentIndexChanged.connect(self._on_repo_combo_changed)
        bar.addWidget(repo_label)
        bar.addWidget(self.repo_combo)
        bar.addSeparator()
        for action in (self.act_add_repo, self.act_index, self.act_tests):
            bar.addAction(action)
        bar.addSeparator()
        for action in (self.act_export, self.act_import):
            bar.addAction(action)
        bar.addSeparator()

        # the buttons keep their height from the style's own margins; pin it so
        # the row is exactly one BUTTON_HEIGHT plus the panel padding
        for widget in bar.findChildren(QToolButton):
            widget.setFixedHeight(BUTTON_HEIGHT)

        # the two sample actions share one dropdown so the row stays short
        self.sample_menu = QMenu(self)
        self.sample_menu.addAction(self.act_sample_load)
        self.sample_menu.addAction(self.act_sample_remove)
        self.sample_button = QToolButton()
        self.sample_button.setText("Sample ▾")
        self.sample_button.setMenu(self.sample_menu)
        self.sample_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sample_button.setAccessibleName("Sample data")
        self.sample_button.setToolTip("Load or remove the [SAMPLE] demo data")
        self.sample_button.setFixedHeight(BUTTON_HEIGHT)
        bar.addWidget(self.sample_button)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self.model_status = QLabel("")
        self.model_status.setObjectName("modelPill")
        self.model_status.setAccessibleName("Model status")
        self.model_status.setTextFormat(Qt.TextFormat.PlainText)
        self.model_status.setFixedHeight(PILL_HEIGHT)
        if self.settings.llm_enabled:
            host = _host_of(self.settings.llm_base_url or "")
            self.model_status.setText(f"Model: {self.settings.llm_model} @ {host}")
            self.model_status.setToolTip(
                "Proposals are generated by this OpenAI-compatible endpoint.\n"
                "Configure it with SK_LLM_BASE_URL / SK_LLM_MODEL / SK_LLM_API_KEY\n"
                "in the .env next to where you launch skraken."
            )
        else:
            self.model_status.setText("Degraded mode — manual diff only")
            self.model_status.setToolTip(
                "No model is configured (that is fine — manual proposals still work).\n"
                "To enable model proposals, set in .env:\n"
                "  SK_LLM_BASE_URL=http://localhost:11434/v1\n"
                "  SK_LLM_MODEL=qwen2.5-coder:7b\n"
                "SK_LLM_API_KEY is optional (never printed, never stored).\n"
                "Or paste a unified diff in the 'New proposal' section instead."
            )
        bar.addWidget(self.model_status)

    # ---------------------------------------------------------------- central
    def _build_central(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setObjectName("centerTabs")
        self.tabs.setAccessibleName("Graph and proposals tabs")

        # -- Graph tab: guidance / no-commits / graph ----------------------
        self.graph_stack = QStackedWidget()
        self.graph_guidance = guidance_page(
            "No repositories yet — Add Repo or Load sample data",
            self.palette,
            self.add_repo_dialog,
            self.load_sample,
        )
        self.graph_empty = notice_page(
            "No commits to show — this repository has no history yet.", self.palette
        )
        self.graph_view = GraphView(self.palette)
        self.graph_view.commit_selected.connect(self._on_commit_selected)
        self.graph_stack.addWidget(self.graph_guidance)      # index 0
        self.graph_stack.addWidget(self.graph_empty)         # index 1
        self.graph_stack.addWidget(self.graph_view)          # index 2

        # -- Proposals tab ---------------------------------------------------
        self.proposals_stack = QStackedWidget()
        self.proposals_guidance = guidance_page(
            "No repositories yet — Add Repo or Load sample data",
            self.palette,
            self.add_repo_dialog,
            self.load_sample,
        )
        self.proposals_empty = notice_page(
            "No proposals for this repository yet — write a request in the\n"
            "'New proposal' section on the right, or Load sample data.",
            self.palette,
        )
        self.proposals_table = QTableView()
        self.proposals_table.setObjectName("proposalsTable")
        self.proposals_model = ProposalTableModel(self.palette, self)
        self.proposals_table.setModel(self.proposals_model)
        compact_item_view(self.proposals_table)
        self.proposals_table.setAlternatingRowColors(True)
        self.proposals_table.setItemDelegateForColumn(
            1, ProposalStatusDelegate(self.palette, self.proposals_table)
        )
        header = self.proposals_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.proposals_table.setMinimumWidth(0)
        self.proposals_table.setAccessibleName("Proposals table")
        self.proposals_table.setAccessibleDescription(
            "Stored proposals, newest first. Columns: id, status, request, model, created."
        )
        self.proposals_table.selectionModel().selectionChanged.connect(
            self._on_proposal_selected
        )
        self.proposals_stack.addWidget(self.proposals_guidance)  # index 0
        self.proposals_stack.addWidget(self.proposals_empty)     # index 1
        self.proposals_stack.addWidget(self.proposals_table)     # index 2

        self.tabs.addTab(self.graph_stack, "&Graph")
        self.tabs.addTab(self.proposals_stack, "&Proposals")
        self.setCentralWidget(self.tabs)

    # -------------------------------------------------------------- left dock
    def _build_left_dock(self) -> None:
        dock = self._make_dock("Repository", "leftDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.left_tabs = QTabWidget()
        self.left_tabs.setObjectName("leftTabs")
        self.left_tabs.setAccessibleName("Repository views")

        self.file_model = FileTreeModel(self.palette, self)
        self.files_view = QTreeView()
        self.files_view.setModel(self.file_model)
        self.files_view.setHeaderHidden(True)
        compact_item_view(self.files_view)
        self.files_view.setAccessibleName("File tree")
        self.files_view.setToolTip(
            "Repository files. Files touched by the selected proposal are shown "
            "bold with '✎ changed'."
        )
        self.files_empty = notice_page(
            "No files to show — pick a repository and press Index.", self.palette
        )
        self.files_stack = QStackedWidget()
        self.files_stack.addWidget(self.files_empty)
        self.files_stack.addWidget(self.files_view)

        self.branch_model = BranchListModel(self.palette, self)
        self.branches_view = QListView()
        self.branches_view.setModel(self.branch_model)
        compact_item_view(self.branches_view)
        self.branches_view.setAccessibleName("Branch list")
        self.branches_empty = notice_page("No branches to show.", self.palette)
        self.branches_stack = QStackedWidget()
        self.branches_stack.addWidget(self.branches_empty)
        self.branches_stack.addWidget(self.branches_view)

        self.left_tabs.addTab(self.files_stack, "&Files")
        self.left_tabs.addTab(self.branches_stack, "&Branches")
        dock.setWidget(self.left_tabs)
        dock.setMinimumWidth(DOCK_MIN_WIDTH)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.left_dock = dock

    # ------------------------------------------------------------- right dock
    def _build_right_dock(self) -> None:
        """The review column: stacked sections, no boxes inside boxes."""
        dock = self._make_dock("Review", "rightDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setMinimumWidth(REVIEW_MIN_WIDTH)

        inner = QWidget()
        inner.setObjectName("reviewPane")
        column = QVBoxLayout(inner)
        column.setContentsMargins(MARGIN_PANEL, MARGIN_PANEL, MARGIN_PANEL, MARGIN_PANEL)
        column.setSpacing(PANEL_SPACING)

        # -- commit details (revealed by a graph selection) -----------------
        self.commit_group = QWidget()
        self.commit_group.setObjectName("commitDetails")
        self.commit_group.setAccessibleName("Commit details")
        commit_box = QVBoxLayout(self.commit_group)
        commit_box.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        commit_box.setSpacing(LAYOUT_SPACING)
        self.commit_header = SectionHeader("Commit details", self.palette)
        self.commit_header.setAccessible("Commit details")
        commit_box.addWidget(self.commit_header)

        grid = QGridLayout()
        grid.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        grid.setHorizontalSpacing(SPACE_6)
        grid.setVerticalSpacing(LAYOUT_SPACING)
        grid.setColumnStretch(1, 1)
        # sha and parents are hex: monospace, like the diff and the test output
        self.commit_sha_label = ElidedLabel("", self.palette, mono=True)
        self.commit_author_label = ElidedLabel("", self.palette)
        self.commit_parents_label = ElidedLabel("", self.palette, mono=True)
        self.commit_message_edit = QPlainTextEdit()
        self.commit_message_edit.setReadOnly(True)
        self.commit_message_edit.setFont(monospace_font(MONO_PT))
        self.commit_message_edit.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.commit_message_edit.setFixedHeight(box_height(COMMIT_MESSAGE_LINES, MONO_PT))
        self.commit_message_edit.setAccessibleName("Commit message")
        for row, (caption_text, widget) in enumerate(
            (
                ("SHA", self.commit_sha_label),
                ("Author / date", self.commit_author_label),
                ("Parents", self.commit_parents_label),
            )
        ):
            grid.addWidget(self._form_caption(caption_text, widget), row, 0)
            grid.addWidget(widget, row, 1)
        grid.addWidget(
            self._form_caption("Message", self.commit_message_edit), 3, 0
        )
        grid.addWidget(self.commit_message_edit, 3, 1)
        commit_box.addLayout(grid)
        self.commit_group.setVisible(False)
        column.addWidget(self.commit_group)

        # -- new proposal form (collapsible so the diff gets the space) ------
        self.new_proposal_toggle = SectionHeader(
            "New proposal", self.palette, collapsible=True
        )
        self.new_proposal_toggle.setAccessible("Toggle new proposal form")
        self.new_proposal_toggle.setToolTip("Ctrl+N opens it again")
        self.new_proposal_toggle.toggled.connect(self._on_new_proposal_toggle)
        column.addWidget(self.new_proposal_toggle)

        form = QWidget()
        form.setObjectName("proposalForm")
        form.setAccessibleName("New proposal form")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        form_layout.setSpacing(LAYOUT_SPACING)

        self.request_label = field_label("Request (what should change)")
        self.request_edit = QPlainTextEdit()
        self.request_edit.setTabChangesFocus(True)
        self.request_edit.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.request_edit.setFixedHeight(box_height(REQUEST_LINES, FONT_PT))
        self.request_edit.setAccessibleName("Proposal request")
        self.request_edit.setPlaceholderText(
            "e.g. Guard divide() against a zero divisor and add a test for it"
        )
        self.request_label.setBuddy(self.request_edit)
        self.request_counter = ElidedLabel(
            f"0 / {MAX_REQUEST_CHARS} characters", self.palette, muted=True
        )
        self.request_counter.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.request_counter.setAccessibleName("Request character count")
        self.request_validation = WrappedLabel("")
        self.request_validation.setWordWrap(True)
        self.request_validation.setTextFormat(Qt.TextFormat.PlainText)
        self.request_validation.setStyleSheet(f"color: {self.palette.err};")
        self.request_validation.setVisible(False)
        self.request_validation.setAccessibleName("Request validation message")
        self.request_edit.textChanged.connect(self._on_request_changed)

        self.paths_label = field_label("Context files (optional, comma-separated)")
        self.paths_edit = QLineEdit()
        self.paths_edit.setPlaceholderText("repo-relative paths for the model context")
        self.paths_edit.setAccessibleName("Context files")
        self.paths_label.setBuddy(self.paths_edit)

        self.diff_box = QCheckBox("Paste unified diff (manual mode)")
        self.diff_box.setChecked(False)
        self.diff_box.setAccessibleName("Paste unified diff (manual mode)")
        self.diff_box.setToolTip(
            "No model configured? Write the patch yourself — it goes through the "
            "same validation, apply, test and rollback pipeline."
        )
        self.diff_input = QPlainTextEdit()
        self.diff_input.setPlaceholderText(
            "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ …"
        )
        self.diff_input.setFont(monospace_font(MONO_PT))
        self.diff_input.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.diff_input.setFixedHeight(box_height(PASTED_DIFF_LINES, MONO_PT))
        self.diff_input.setAccessibleName("Pasted unified diff")
        self.diff_input.setVisible(False)
        self.diff_box.toggled.connect(self.diff_input.setVisible)

        submit_row = QHBoxLayout()
        submit_row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        submit_row.setSpacing(LAYOUT_SPACING)
        self.submit_button = QPushButton("Submit proposal")
        self.submit_button.setDefault(True)
        self.submit_button.setShortcut(QKeySequence("Ctrl+Return"))
        self.submit_button.setToolTip("Ctrl+Enter — ask the model, or use the pasted diff")
        self.submit_button.setAccessibleName("Submit proposal")
        self.submit_button.clicked.connect(self._on_submit_clicked)
        self.submit_busy = BusyBar(width=80)
        submit_row.addWidget(self.submit_button)
        submit_row.addWidget(self.submit_busy)
        submit_row.addStretch(1)

        form_layout.addWidget(self.request_label)
        form_layout.addWidget(self.request_edit)
        form_layout.addWidget(self.request_counter)
        form_layout.addWidget(self.request_validation)
        form_layout.addWidget(self.paths_label)
        form_layout.addWidget(self.paths_edit)
        form_layout.addWidget(self.diff_box)
        form_layout.addWidget(self.diff_input)
        form_layout.addLayout(submit_row)
        self.proposal_form = form
        column.addWidget(form)

        # -- proposal detail --------------------------------------------------
        self.detail_group = QWidget()
        self.detail_group.setObjectName("proposalDetail")
        self.detail_group.setAccessibleName("Proposal detail")
        detail_layout = QVBoxLayout(self.detail_group)
        detail_layout.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        detail_layout.setSpacing(LAYOUT_SPACING)

        self.detail_header = SectionHeader("Proposal detail", self.palette)
        self.detail_header.setAccessible("Proposal detail")
        detail_layout.addWidget(self.detail_header)

        self.detail_meta = ElidedLabel("No proposal selected.", self.palette, muted=True)
        self.detail_meta.setAccessibleName("Proposal summary line")
        detail_layout.addWidget(self.detail_meta)

        rationale_caption = field_label("Rationale")
        self.rationale_edit = QPlainTextEdit()
        self.rationale_edit.setReadOnly(True)
        self.rationale_edit.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.rationale_edit.setAccessibleName("Proposal rationale")
        rationale_caption.setBuddy(self.rationale_edit)
        self.rationale_edit.textChanged.connect(self._fit_rationale)
        self._fit_rationale()

        diff_caption = field_label("Diff")
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setFont(monospace_font(MONO_PT))
        self.diff_view.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.diff_view.setMinimumHeight(box_height(DIFF_MIN_LINES, MONO_PT))
        self.diff_view.setAccessibleName("Proposal diff viewer")
        diff_caption.setBuddy(self.diff_view)
        self.diff_highlighter = DiffHighlighter(self.diff_view.document(), self.palette)

        option_row = QHBoxLayout()
        option_row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        option_row.setSpacing(SPACE_8)
        self.tests_check = QCheckBox("&Run tests")
        self.tests_check.setChecked(True)
        self.tests_check.setToolTip(
            "Run the repository's test command after applying the diff"
        )
        self.commit_check = QCheckBox("Commit on &success")
        self.commit_check.setToolTip(
            "git add + commit the touched files if — and only if — the tests pass"
        )
        self.keep_check = QCheckBox("&Keep on failure")
        self.keep_check.setToolTip(
            "Leave the change in the working tree even when tests fail "
            "(no automatic rollback)"
        )
        for box in (self.tests_check, self.commit_check, self.keep_check):
            box.setAccessibleName(box.text().replace("&", ""))
            option_row.addWidget(box)
        option_row.addStretch(1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        button_row.setSpacing(SPACE_6)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setToolTip(
            "Apply the diff with git apply, run tests, roll back automatically on failure"
        )
        self.apply_button.setAccessibleName("Apply proposal")
        self.apply_button.clicked.connect(self._on_apply_clicked)
        self.reject_button = QPushButton("Reject")
        self.reject_button.setToolTip(
            "Mark this proposal as rejected (the working tree is untouched)"
        )
        self.reject_button.setAccessibleName("Reject proposal")
        self.reject_button.clicked.connect(self._on_reject_clicked)
        self.revert_button = QPushButton("Revert")
        self.revert_button.setToolTip(
            "Reverse this applied diff in the working tree (git history stays)"
        )
        self.revert_button.setAccessibleName("Revert proposal")
        self.revert_button.clicked.connect(self._on_revert_clicked)
        self.detail_busy = BusyBar(width=64)
        for button in (self.apply_button, self.reject_button, self.revert_button):
            button.setEnabled(False)
            button_row.addWidget(button)
        button_row.addWidget(self.detail_busy)
        button_row.addStretch(1)

        detail_layout.addWidget(rationale_caption)
        detail_layout.addWidget(self.rationale_edit)
        detail_layout.addWidget(diff_caption)
        detail_layout.addWidget(self.diff_view, 1)
        detail_layout.addLayout(option_row)
        detail_layout.addLayout(button_row)
        column.addWidget(self.detail_group, 1)

        # -- test output (same header style, collapsed unless the tests ran) --
        self.tests_toggle = SectionHeader(
            "Test output", self.palette, collapsible=True, expanded=False
        )
        self.tests_toggle.setAccessible("Toggle test output section")
        self.tests_toggle.setEnabled(False)
        self.tests_output = QPlainTextEdit()
        self.tests_output.setReadOnly(True)
        self.tests_output.setFont(monospace_font(MONO_PT))
        self.tests_output.document().setDocumentMargin(TEXT_BOX_MARGIN)
        self.tests_output.setFixedHeight(box_height(TEST_OUTPUT_LINES, MONO_PT))
        self.tests_output.setVisible(False)
        self.tests_output.setAccessibleName("Test output tail")
        self.tests_toggle.toggled.connect(self.tests_output.setVisible)
        column.addWidget(self.tests_toggle)
        column.addWidget(self.tests_output)

        scroll = QScrollArea()
        scroll.setObjectName("reviewScroll")
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        dock.setWidget(scroll)
        self.right_scroll = scroll
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.right_dock = dock

    def _form_caption(self, text: str, buddy: QWidget) -> QLabel:
        """Right-aligned muted caption of the two-column detail form."""
        caption = field_label(text)
        caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        caption.setBuddy(buddy)
        return caption

    def _fit_rationale(self) -> None:
        """Rationale shows all its text up to a few lines, then it scrolls.

        ``lineCount`` already counts wrapped lines, so the box is exactly as
        tall as its text and never clips the last line.
        """
        lines = max(self.rationale_edit.document().lineCount(), 1)
        self.rationale_edit.setFixedHeight(
            box_height(min(lines, RATIONALE_MAX_LINES), FONT_PT)
        )

    # ------------------------------------------------------------ bottom dock
    def _build_bottom_dock(self) -> None:
        dock = self._make_dock("Activity log (newest first)", "bottomDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.log_table = QTableView()
        self.log_table.setObjectName("logTable")
        self.log_model = LogTableModel(self.palette, self)
        self.log_table.setModel(self.log_model)
        compact_item_view(self.log_table)
        self.log_table.setAlternatingRowColors(True)
        log_header = self.log_table.horizontalHeader()
        log_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        log_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        log_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        log_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.log_table.setColumnWidth(0, LOG_WHEN_WIDTH)
        self.log_table.setColumnWidth(1, LOG_KIND_WIDTH)
        self.log_table.setColumnWidth(2, LOG_PROPOSAL_WIDTH)
        self.log_table.setAccessibleName("Activity log")
        self.log_table.setAccessibleDescription(
            "Audit trail: every register, index, propose, apply, test, commit, "
            "rollback, reject, revert — newest first."
        )
        dock.setWidget(self.log_table)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.bottom_dock = dock
        self.resizeDocks(
            [self.left_dock, self.right_dock],
            [LEFT_DOCK_WIDTH, REVIEW_DOCK_WIDTH],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks([dock], [ACTIVITY_LOG_HEIGHT], Qt.Orientation.Vertical)

    def _make_dock(self, title: str, object_name: str) -> QDockWidget:
        """A dock with the one compact title bar the whole window uses.

        The title text starts at the panel margin, so it sits on the same left
        edge as the content underneath it.
        """
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        return dock

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.setFixedHeight(STATUS_BAR_HEIGHT)
        margins = bar.contentsMargins()
        margins.setLeft(MARGIN_PANEL)
        margins.setRight(MARGIN_PANEL)
        margins.setTop(0)
        margins.setBottom(0)
        bar.setContentsMargins(margins)
        bar.layout().setSpacing(SPACE_8)
        self.status_label = ElidedLabel("Ready.", self.palette)
        self.status_label.setAccessibleName("Status message")
        bar.addWidget(self.status_label, 1)
        self.status_busy = BusyBar(width=SPACE_12 * 8)
        bar.addPermanentWidget(self.status_busy)

    def _build_menu(self) -> None:
        app_menu = self.menuBar().addMenu("&selfkraken")
        app_menu.addAction(self.act_add_repo)
        app_menu.addAction(self.act_index)
        app_menu.addAction(self.act_tests)
        app_menu.addSeparator()
        app_menu.addAction(self.act_export)
        app_menu.addAction(self.act_import)
        app_menu.addSeparator()
        app_menu.addAction(self.act_sample_load)
        app_menu.addAction(self.act_sample_remove)
        app_menu.addSeparator()
        app_menu.addAction(self.act_refresh)
        app_menu.addAction(self.act_quit)
        views_menu = self.menuBar().addMenu("&Views")
        views_menu.addAction(self.act_tab_graph)
        views_menu.addAction(self.act_tab_proposals)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.act_help)

    def _set_tab_order(self) -> None:
        chain = [
            self.repo_combo,
            self.act_add_repo, self.act_index, self.act_tests,
            self.act_export, self.act_import, self.sample_button,
            self.left_tabs, self.graph_view, self.proposals_table,
            self.new_proposal_toggle, self.request_edit, self.paths_edit,
            self.diff_input, self.submit_button,
            self.apply_button, self.reject_button, self.revert_button,
            self.tests_toggle, self.log_table,
        ]
        previous: Optional[QWidget] = None
        for item in chain:
            widget = self.toolbar.widgetForAction(item) if isinstance(item, QAction) else item
            if not isinstance(widget, QWidget):
                continue
            if previous is not None:
                QWidget.setTabOrder(previous, widget)
            previous = widget

    # ------------------------------------------------------------ states/UIX
    def say_ok(self, message: str) -> None:
        """Success state: the 'OK' text label is always present."""
        self._set_status(f"OK: {message}", ok=True)

    def say_error(self, message: str) -> None:
        """Failure state: 'Error:' text label plus a message box."""
        self._set_status(f"Error: {message}", ok=False)
        QMessageBox.critical(self, "selfkraken — error", f"Error: {message}")

    def _set_status(self, text: str, ok: bool) -> None:
        self.status_label.setText(text)
        colour = self.palette.ok if ok else self.palette.err
        self.status_label.setStyleSheet(f"color: {colour}; font-weight: bold;")

    def _job_started(self, _label: str) -> None:
        if not self.runner.inline:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._sync_busy()

    def _job_finished(self, _label: str) -> None:
        if not self.runner.inline:
            QApplication.restoreOverrideCursor()
        self._sync_busy()

    def _sync_busy(self) -> None:
        busy = self.runner.active_count() > 0
        self.status_busy.setVisible(busy)
        self.submit_busy.setVisible(busy)
        self.detail_busy.setVisible(busy)
        for action in (
            self.act_add_repo, self.act_index, self.act_tests, self.act_export,
            self.act_import, self.act_sample_load, self.act_sample_remove,
        ):
            action.setEnabled(not busy)
        if not busy:
            self._update_action_enabling()
        self._update_detail_buttons()

    def _update_action_enabling(self) -> None:
        busy = self.runner.active_count() > 0
        repo = self.current_repo()
        self.act_index.setEnabled(repo is not None and not busy)
        self.act_tests.setEnabled(
            repo is not None and bool(repo and repo.test_command) and not busy
        )
        self.submit_button.setEnabled(not busy and self._request_valid)
        self._update_detail_buttons()

    def _update_detail_buttons(self) -> None:
        busy = self.runner.active_count() > 0
        proposal = self._selected_proposal()
        if proposal is None:
            apply_enabled = reject_enabled = revert_enabled = False
        else:
            apply_enabled = reject_enabled = proposal.status == "proposed"
            revert_enabled = proposal.status in workflow.REVERTIBLE_STATUSES
        self.apply_button.setEnabled(apply_enabled and not busy)
        self.reject_button.setEnabled(reject_enabled and not busy)
        self.revert_button.setEnabled(revert_enabled and not busy)

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Reload everything from the database for the current selection."""
        self.refresh_repos()
        self.refresh_proposals()
        self.refresh_log()

    def refresh_repos(self, select_id: Optional[int] = None) -> None:
        """Repopulate the repo combo; cascades a full repo load when it changed."""
        repos = self.store.list_repos()
        previous = self._current_repo_id
        target = select_id if select_id is not None else previous
        self._suppress_signals = True
        try:
            self.repo_combo.clear()
            for repo in repos:
                label = repo.name
                if repo.is_sample and not label.startswith(sample.SAMPLE_LABEL):
                    label = f"{sample.SAMPLE_LABEL} {label}"
                self.repo_combo.addItem(label, repo.id)
                self.repo_combo.setItemData(
                    self.repo_combo.count() - 1, repo.path, Qt.ToolTipRole
                )
            index = self._combo_index_for(target)
            if index < 0 and self.repo_combo.count():
                index = 0
            self.repo_combo.setCurrentIndex(index)
        finally:
            self._suppress_signals = False
        new_id = self._combo_repo_id()
        repo_changed = new_id != previous
        self._current_repo_id = new_id
        if repo_changed:
            self._on_repo_changed()
        else:
            self._update_empty_states()
            self._update_action_enabling()

    def _combo_index_for(self, repo_id: Optional[int]) -> int:
        if repo_id is None:
            return -1
        for i in range(self.repo_combo.count()):
            if self.repo_combo.itemData(i) == repo_id:
                return i
        return -1

    def _combo_repo_id(self) -> Optional[int]:
        index = self.repo_combo.currentIndex()
        if index < 0:
            return None
        value = self.repo_combo.itemData(index)
        return None if value is None else int(value)

    def current_repo(self) -> Optional[Repo]:
        if self._current_repo_id is None:
            return None
        try:
            return self.store.get_repo(self._current_repo_id)
        except SKError:
            return None

    def refresh_proposals(self) -> None:
        repo_id = self._current_repo_id
        proposals = self.store.list_proposals(repo_id=repo_id) if repo_id is not None else []
        self.proposals_model.set_proposals(proposals)
        row = self.proposals_model.row_for_id(self._selected_proposal_id or -1)
        if row >= 0:
            self.proposals_table.selectRow(row)
        elif proposals:
            self.proposals_table.selectRow(0)
        else:
            self._show_proposal_detail(None)
        self._update_empty_states()

    def refresh_log(self) -> None:
        self.log_model.set_changes(self.store.list_changes(limit=LOG_LIMIT))

    def reload_all(self, select_id: Optional[int] = None) -> None:
        """Full reload after a mutation that can change the whole world."""
        same_repo = select_id is not None and select_id == self._current_repo_id
        self.refresh_repos(select_id=select_id)
        if same_repo:
            # refresh_repos only cascades when the selection actually changed
            self.refresh_proposals()
            self.refresh_log()
            self.load_graph()
            self.load_files()

    # ---------------------------------------------------------- background IO
    def load_graph(self) -> None:
        """Background: read commits, compute the lane layout, list branches."""
        repo = self.current_repo()
        if repo is None:
            self.graph_view.clear_rows()
            self.branch_model.clear()
            self._update_empty_states()
            return
        repo_path = Path(repo.path)

        def job(_store: Store, _settings: Settings):
            rows = layout(read_commits(repo_path, limit=GRAPH_LIMIT))
            return rows, list_branches(repo_path)

        self.runner.submit(
            "graph",
            job,
            on_ok=self._after_graph,
            on_error=lambda message: self.say_error(f"graph: {message}"),
        )

    def _after_graph(self, result) -> None:
        rows, branches = result
        self.graph_view.set_rows(rows, branches)
        self.branch_model.set_branches(branches)
        if rows:
            self.graph_view.select_row(0)
        self._update_empty_states()

    def load_files(self) -> None:
        """Background: the repo's indexable file list for the Files tree."""
        repo = self.current_repo()
        if repo is None:
            self._files = []
            self.file_model.clear()
            self._update_empty_states()
            return
        repo_path = Path(repo.path)

        def job(_store: Store, _settings: Settings):
            return [
                found
                for found in indexer.list_repo_files(repo_path)
                if not indexer.should_skip(found)
            ]

        self.runner.submit(
            "files",
            job,
            on_ok=self._after_files,
            on_error=lambda message: self.say_error(f"file list: {message}"),
        )

    def _after_files(self, files: list[str]) -> None:
        self._files = files
        proposal = self._selected_proposal()
        changed = diffs.touched_paths(proposal.diff) if proposal is not None else []
        self.file_model.set_files(files, changed)
        self._update_empty_states()

    def _update_empty_states(self) -> None:
        has_repos = self.repo_combo.count() > 0
        if not has_repos:
            self.graph_stack.setCurrentIndex(0)
            self.proposals_stack.setCurrentIndex(0)
            self.files_stack.setCurrentIndex(0)
            self.branches_stack.setCurrentIndex(0)
            self.commit_group.setVisible(False)
            return
        has_commits = self.graph_view.table_model.rowCount() > 0
        self.graph_stack.setCurrentIndex(2 if has_commits else 1)
        has_proposals = self.proposals_model.rowCount() > 0
        self.proposals_stack.setCurrentIndex(2 if has_proposals else 1)
        self.files_stack.setCurrentIndex(1 if self._files else 0)
        self.branches_stack.setCurrentIndex(1 if self.branch_model.rowCount() > 0 else 0)

    # ------------------------------------------------------------ controller
    def _on_repo_combo_changed(self) -> None:
        if self._suppress_signals:
            return
        self._on_repo_changed()

    def _on_repo_changed(self) -> None:
        self._current_repo_id = self._combo_repo_id()
        self._selected_proposal_id = None
        self.commit_group.setVisible(False)
        self.refresh_proposals()
        self.refresh_log()
        self.load_graph()
        self.load_files()

    # -- add repo -------------------------------------------------------------
    def add_repo_dialog(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a git repository folder", str(Path.home())
        )
        if not directory:
            return
        command, accepted = QInputDialog.getText(
            self,
            "Test command",
            "Command that runs this repository's tests\n"
            "(split with shlex, no shell — leave empty to skip):\n",
            text="python -m pytest -q",
        )
        if not accepted:
            return
        self.add_repo(Path(directory), command.strip() or None)

    def add_repo(
        self,
        path: Path,
        test_command: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        """Register *path* (background job) and switch to it when done."""

        def job(store: Store, _settings: Settings):
            return workflow.register_repo(store, path, test_command, name)

        self.runner.submit(
            f"register {path}",
            job,
            on_ok=self._after_add_repo,
            on_error=self.say_error,
        )

    def _after_add_repo(self, repo: Repo) -> None:
        self.reload_all(select_id=repo.id)
        self.say_ok(f"registered repo {repo.id} ({repo.name}) — index it with Ctrl+I")

    # -- index -------------------------------------------------------------------
    def index_repo(self) -> None:
        repo = self.current_repo()
        if repo is None:
            self.say_error("no repository selected — add one first")
            return

        def job(store: Store, _settings: Settings):
            stats = indexer.index_repo(store, repo)
            store.record_change(
                "index",
                f"indexed repo {repo.id}: {stats.indexed} indexed, "
                f"{stats.unchanged} unchanged, {stats.skipped} skipped, "
                f"{stats.removed} removed",
                repo_id=repo.id,
            )
            return stats

        self.runner.submit(
            f"index {repo.name}",
            job,
            on_ok=lambda stats: (
                self.say_ok(
                    f"indexed {repo.name}: {stats.indexed} indexed, "
                    f"{stats.unchanged} unchanged, {stats.skipped} skipped, "
                    f"{stats.removed} removed"
                ),
                self.refresh_log(),
                self.load_files(),
            ),
            on_error=self.say_error,
        )

    # -- tests -----------------------------------------------------------------------
    def run_tests(self) -> None:
        repo = self.current_repo()
        if repo is None:
            self.say_error("no repository selected — add one first")
            return

        def job(store: Store, settings: Settings):
            return workflow.run_repo_tests(store, settings, repo)

        def done(run: TestRun) -> None:
            self.refresh_log()
            if run.passed:
                self.say_ok(f"tests passed for {repo.name} (exit 0, {run.duration_ms} ms)")
            else:
                self.say_error(
                    f"tests FAILED for {repo.name} (exit {run.exit_code}, "
                    f"{run.duration_ms} ms)"
                )

        self.runner.submit(
            f"tests {repo.name}", job, on_ok=done, on_error=self.say_error
        )

    # --------------------------------------------------------------- proposals
    def _on_new_proposal_toggle(self, open_form: bool) -> None:
        self.proposal_form.setVisible(open_form)

    def _set_form_open(self, open_form: bool) -> None:
        """Collapse the form (from the header) so the diff owns the pane."""
        self.new_proposal_toggle.setExpanded(open_form)
        self.proposal_form.setVisible(open_form)

    def _on_request_changed(self) -> None:
        text = self.request_edit.toPlainText()
        count = len(text.strip())
        self.request_counter.setText(f"{count} / {MAX_REQUEST_CHARS} characters")
        valid, message = self.validate_request(text)
        self._request_valid = valid
        self.request_validation.setText(message)
        self.request_validation.setVisible(not valid)
        self.submit_button.setEnabled(valid and self.runner.active_count() == 0)

    @staticmethod
    def validate_request(text: str) -> tuple[bool, str]:
        """Mirror workflow.propose's rules so validation is inline, not a surprise."""
        count = len((text or "").strip())
        if count == 0:
            return False, "Request is required — describe the change you want (3–4000 characters)."
        if count < MIN_REQUEST_CHARS:
            return False, (
                f"Request is too short ({count} characters) — minimum {MIN_REQUEST_CHARS}."
            )
        if count > MAX_REQUEST_CHARS:
            return False, (
                f"Request is too long ({count} characters) — maximum {MAX_REQUEST_CHARS}."
            )
        return True, ""

    def _on_submit_clicked(self) -> None:
        if not self.submit_button.isEnabled():
            return
        self.submit_proposal(
            self.request_edit.toPlainText(),
            diff_text=self._pasted_diff_or_none(),
            extra_paths=_split_paths(self.paths_edit.text()),
        )

    def _pasted_diff_or_none(self) -> Optional[str]:
        if self.diff_box.isChecked() and self.diff_input.toPlainText().strip():
            return self.diff_input.toPlainText()
        return None

    def submit_proposal(
        self,
        request: str,
        diff_text: Optional[str] = None,
        extra_paths: Sequence[str] = (),
    ) -> None:
        """Create a proposal (background); *diff_text* selects manual mode."""
        repo = self.current_repo()
        if repo is None:
            self.say_error("no repository selected — add one first")
            return
        valid, message = self.validate_request(request)
        if not valid:
            self.request_validation.setText(message)
            self.request_validation.setVisible(True)
            return
        if diff_text is None and not self.settings.llm_enabled:
            self.say_error(
                "no model is configured and no diff was pasted — " + DEGRADED_MODE_HINT
            )
            return

        def job(store: Store, settings: Settings):
            return workflow.propose(
                store, settings, repo, request,
                diff_text=diff_text, extra_paths=extra_paths,
            )

        self.runner.submit("propose", job, on_ok=self._after_propose, on_error=self.say_error)

    def _after_propose(self, proposal: Proposal) -> None:
        self.say_ok(f"proposal {proposal.id} created (model {proposal.model})")
        self.diff_input.setPlainText("")
        self.diff_box.setChecked(False)
        self.refresh_proposals()
        self.refresh_log()
        self.select_proposal(proposal.id)

    def select_proposal(self, proposal_id: int) -> None:
        row = self.proposals_model.row_for_id(proposal_id)
        if row >= 0:
            self.proposals_table.selectRow(row)

    def _on_proposal_selected(self, _selected, _deselected) -> None:
        rows = self.proposals_table.selectionModel().selectedRows()
        if not rows:
            self._show_proposal_detail(None)
            return
        self._show_proposal_detail(self.proposals_model.proposal_at(rows[0].row()))

    def _selected_proposal(self) -> Optional[Proposal]:
        if self._selected_proposal_id is None:
            return None
        try:
            return self.store.get_proposal(self._selected_proposal_id)
        except SKError:
            return None

    def _show_proposal_detail(self, proposal: Optional[Proposal]) -> None:
        if proposal is None:
            self._selected_proposal_id = None
            self.detail_meta.setText("No proposal selected.")
            self.rationale_edit.setPlainText("")
            self.diff_view.setPlainText("")
            self.tests_output.setPlainText("")
            self.tests_output.setVisible(False)
            self.tests_toggle.setExpanded(False)
            self.tests_toggle.set_note("")
            self.tests_toggle.setEnabled(False)
            self.file_model.set_changed([])
            self._update_detail_buttons()
            self._set_form_open(True)
            return
        self._selected_proposal_id = proposal.id
        sample_note = " · [SAMPLE]" if proposal.is_sample else ""
        self.detail_meta.setText(
            f"Proposal {proposal.id} · status: {proposal.status} · model: "
            f"{proposal.model} · created {local_time(proposal.created_at)}{sample_note}"
        )
        self.rationale_edit.setPlainText(proposal.rationale or "(no rationale)")
        self.diff_view.setPlainText(proposal.diff.rstrip("\n"))
        self.file_model.set_changed(diffs.touched_paths(proposal.diff))
        self._load_test_tail(proposal.id)
        self._update_detail_buttons()
        self._update_empty_states()
        self._set_form_open(False)  # the diff gets the whole pane

    def _load_test_tail(self, proposal_id: int) -> None:
        runs = self.store.list_test_runs(proposal_id=proposal_id)
        if not runs:
            self.tests_output.setPlainText("(no test run recorded for this proposal yet)")
            self.tests_toggle.setEnabled(True)
            self.tests_toggle.setExpanded(False)
            self.tests_output.setVisible(False)
            self.tests_toggle.set_note("none yet")
            return
        run = runs[0]
        verdict = "PASSED" if run.passed else "FAILED"
        self.tests_output.setPlainText(
            f"run {run.id} · {_middle(run.command, 56)} · exit {run.exit_code} · "
            f"{verdict} · {run.duration_ms} ms\n\n{run.output_tail}"
        )
        self.tests_toggle.setEnabled(True)
        self.tests_toggle.set_note(f"{verdict}, exit {run.exit_code}")
        failed_open = not run.passed
        self.tests_toggle.setExpanded(failed_open)
        self.tests_output.setVisible(failed_open)

    # ------------------------------------------------------------- decisions
    def confirm(self, title: str, text: str) -> bool:
        """Yes/No gate before touching the working tree (tests override it)."""
        if self.confirm_handler is not None:
            return bool(self.confirm_handler(title, text))
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_apply_clicked(self) -> None:
        proposal = self._selected_proposal()
        if proposal is None:
            return
        repo = self.store.get_repo(proposal.repo_id)
        tests_note = (
            " and runs the test command" if self.tests_check.isChecked() else ""
        )
        commit_note = (
            "\nThe touched files are committed if the tests pass."
            if self.commit_check.isChecked()
            else ""
        )
        if not self.confirm(
            "Apply proposal",
            f"Apply proposal {proposal.id} to {repo.path}?\n\n"
            f"This edits the working tree with git apply{tests_note}.{commit_note}",
        ):
            return
        self.apply_proposal(
            proposal.id,
            run_tests=self.tests_check.isChecked(),
            commit=self.commit_check.isChecked(),
            keep_on_failure=self.keep_check.isChecked(),
        )

    def apply_proposal(
        self,
        proposal_id: int,
        run_tests: bool = True,
        commit: bool = False,
        keep_on_failure: bool = False,
    ) -> None:
        """Background: validate → git apply → tests → keep/roll back → commit."""

        def job(store: Store, settings: Settings):
            return workflow.apply_proposal(
                store,
                settings,
                proposal_id,
                run_tests=run_tests,
                commit=commit,
                keep_on_failure=keep_on_failure,
            )

        self.runner.submit(
            f"apply {proposal_id}", job, on_ok=self._after_apply, on_error=self.say_error
        )

    def _after_apply(self, outcome: ApplyOutcome) -> None:
        proposal = outcome.proposal
        self.refresh_proposals()
        self.refresh_log()
        self.load_graph()
        self.load_files()
        self.select_proposal(proposal.id)
        if proposal.status == "tests_failed":
            where = (
                " — changes rolled back, working tree restored"
                if outcome.rolled_back
                else " — kept in the working tree (keep-on-failure)"
            )
            self.say_error(f"proposal {proposal.id}: tests FAILED{where}")
            return
        committed = (
            f", committed {outcome.commit_sha[:12]}" if outcome.commit_sha else ""
        )
        self.say_ok(f"proposal {proposal.id} applied — status {proposal.status}{committed}")

    def _on_reject_clicked(self) -> None:
        proposal = self._selected_proposal()
        if proposal is not None:
            self.reject_proposal(proposal.id)

    def reject_proposal(self, proposal_id: int) -> None:
        def job(store: Store, _settings: Settings):
            return workflow.reject_proposal(store, proposal_id)

        def done(rejected: Proposal) -> None:
            self.say_ok(f"proposal {rejected.id} rejected — status {rejected.status}")
            self.refresh_proposals()
            self.refresh_log()
            self.select_proposal(rejected.id)

        self.runner.submit(
            f"reject {proposal_id}", job, on_ok=done, on_error=self.say_error
        )

    def _on_revert_clicked(self) -> None:
        proposal = self._selected_proposal()
        if proposal is not None:
            self.revert_proposal(proposal.id)

    def revert_proposal(self, proposal_id: int) -> None:
        def job(store: Store, _settings: Settings):
            return workflow.revert_proposal(store, proposal_id)

        def done(reverted: Proposal) -> None:
            self.say_ok(
                f"proposal {reverted.id} reverted — working tree restored, "
                "git history untouched"
            )
            self.refresh_proposals()
            self.refresh_log()
            self.load_graph()
            self.load_files()
            self.select_proposal(reverted.id)

        self.runner.submit(
            f"revert {proposal_id}", job, on_ok=done, on_error=self.say_error
        )

    # -------------------------------------------------------------- portability
    def export_dialog(self) -> None:
        suggested = str(Path.home() / f"selfkraken-{date.today().isoformat()}.json")
        target, _ = QFileDialog.getSaveFileName(
            self, "Export data", suggested, "JSON files (*.json);;All files (*)"
        )
        if target:
            self.export_data(Path(target))

    def export_data(self, path: Path) -> None:
        def job(store: Store, _settings: Settings):
            return portability.export_data(store, path)

        def done(counts: dict) -> None:
            self.say_ok(f"exported to {path} ({_counts_text(counts)})")
            self.refresh_log()

        self.runner.submit("export", job, on_ok=done, on_error=self.say_error)

    def import_dialog(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "Import data", str(Path.home()), "JSON files (*.json);;All files (*)"
        )
        if source:
            self.import_data(Path(source))

    def import_data(self, path: Path) -> None:
        def job(store: Store, _settings: Settings):
            return portability.import_data(store, path)

        def done(counts: dict) -> None:
            self.say_ok(f"imported from {path} ({_counts_text(counts)})")
            self.reload_all()

        self.runner.submit("import", job, on_ok=done, on_error=self.say_error)

    # ------------------------------------------------------------------ sample
    def load_sample(self) -> None:
        def job(store: Store, settings: Settings):
            return sample.load_sample(store, settings)

        def done(repo: Repo) -> None:
            self.say_ok(
                f"sample repo {repo.id} loaded at {repo.path} "
                f"({sample.SAMPLE_LABEL} labelled)"
            )
            self.reload_all(select_id=repo.id)

        self.runner.submit("sample load", job, on_ok=done, on_error=self.say_error)

    def remove_sample(self) -> None:
        def job(store: Store, settings: Settings):
            return sample.remove_sample(store, settings)

        def done(removed: int) -> None:
            self.say_ok(f"sample data removed ({removed} row(s))")
            self.reload_all()

        self.runner.submit("sample remove", job, on_ok=done, on_error=self.say_error)

    # ------------------------------------------------------------------- graph
    def _on_commit_selected(self, graph_row: Optional[GraphRow]) -> None:
        if graph_row is None:
            self.commit_group.setVisible(False)
            return
        repo = self.current_repo()
        if repo is None:
            return
        commit = graph_row.commit
        self.commit_group.setVisible(True)
        self.commit_sha_label.setText(f"{commit.sha[:16]}…")
        self.commit_sha_label.setToolTip(commit.sha)
        self.commit_author_label.setText(f"{commit.author} · {local_time(commit.date)}")
        self.commit_parents_label.setText(
            ", ".join(parent[:12] for parent in commit.parents)
            if commit.parents
            else "(none — root commit)"
        )
        self.commit_message_edit.setPlainText(commit.subject or "(no subject)")
        repo_path = Path(repo.path)

        def job(_store: Store, _settings: Settings):
            return read_commit_detail(repo_path, commit.sha)

        def done(detail: dict) -> None:
            subject = str(detail.get("subject") or "")
            body = str(detail.get("body") or "")
            message = f"{subject}\n\n{body}".strip() if body else subject
            self.commit_message_edit.setPlainText(message or "(empty message)")
            parents = detail.get("parents") or []
            if parents:
                self.commit_parents_label.setText(
                    ", ".join(str(parent)[:12] for parent in parents)
                )

        self.runner.submit(
            "commit detail",
            job,
            on_ok=done,
            on_error=lambda message: self._set_status(
                f"Error: commit detail unavailable — {message}", ok=False
            ),
        )

    # -------------------------------------------------------------------- misc
    def focus_new_proposal(self) -> None:
        self.tabs.setCurrentIndex(1)
        self._set_form_open(True)
        self.request_edit.clear()
        self.request_edit.setFocus()
        self.right_scroll.ensureWidgetVisible(self.request_edit, 0, 40)

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self, self.palette).exec()

    # ------------------------------------------------------ window geometry
    @property
    def _geometry_file(self) -> Path:
        return Path(self.settings.data_dir) / WINDOW_STATE_FILENAME

    def _restore_geometry(self) -> bool:
        """Re-apply the last size/position; False when nothing usable is stored."""
        try:
            stored = json.loads(self._geometry_file.read_text(encoding="utf-8"))
            x, y, width, height = (int(stored[key]) for key in ("x", "y", "w", "h"))
        except (OSError, ValueError, KeyError):
            return False
        if (width, height) < MIN_REMEMBERED_SIZE:
            return False
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if not available.intersects(QRect(x, y, width, height)):
                return False
        self.setGeometry(x, y, width, height)
        maximised = bool(stored.get("maximised", False))
        if maximised:
            self.showMaximized()
        return True

    def _save_geometry(self) -> None:
        if self.isMaximized() or self.isMinimized():
            rect = self.normalGeometry()
            maximised = self.isMaximized()
        else:
            rect = self.geometry()
            maximised = False
        state = {
            "x": rect.x(),
            "y": rect.y(),
            "w": rect.width(),
            "h": rect.height(),
            "maximised": maximised,
        }
        try:
            self._geometry_file.parent.mkdir(parents=True, exist_ok=True)
            self._geometry_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:  # pragma: no cover - a missing write is never fatal
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt name
        self._save_geometry()
        self.runner.wait(5000)
        self.store.close()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# shortcuts dialog
# --------------------------------------------------------------------------- #
SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+1", "Show the Graph tab"),
    ("Ctrl+2", "Show the Proposals tab"),
    ("Ctrl+N", "New proposal (focus and clear the request form)"),
    ("Ctrl+Enter", "Submit the proposal request"),
    ("Ctrl+I", "Index the selected repository"),
    ("Ctrl+T", "Run the repository's tests"),
    ("F5", "Refresh everything from the database"),
    ("F1", "Show this keyboard-shortcuts help"),
    ("Arrow keys", "Navigate the commit graph and tables"),
    ("Ctrl+Q", "Quit"),
)


class ShortcutsDialog(QDialog):
    """F1: every shortcut, in text."""

    def __init__(self, parent: Optional[QWidget] = None, palette: Optional[Palette] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        self.setMinimumWidth(440)
        pal = palette or resolve("dark")
        grid = QGridLayout()
        grid.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        grid.setHorizontalSpacing(SPACE_12)
        grid.setVerticalSpacing(LAYOUT_SPACING)
        for row, (key, description) in enumerate(SHORTCUTS):
            key_label = QLabel(key)
            key_label.setStyleSheet(f"font-weight: bold; color: {pal.accent};")
            key_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(key_label, row, 0)
            text = ElidedLabel(description, pal)
            grid.addWidget(text, row, 1)
        box_layout = QVBoxLayout(self)
        box_layout.setContentsMargins(MARGIN_PANEL, MARGIN_PANEL, MARGIN_PANEL, MARGIN_PANEL)
        box_layout.setSpacing(PANEL_SPACING)
        box_layout.addLayout(grid)
        close_row = QHBoxLayout()
        close_row.setContentsMargins(MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED, MARGIN_NESTED)
        close_row.addStretch(1)
        close_button = QPushButton("OK")
        close_button.setDefault(True)
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        box_layout.addLayout(close_row)
        self.setAccessibleName("Keyboard shortcuts dialog")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _host_of(url: str) -> str:
    return urlparse(url).netloc or url


def _counts_text(counts: dict) -> str:
    if not counts:
        return "no rows"
    return ", ".join(f"{name}: {count}" for name, count in sorted(counts.items()))


def _split_paths(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    for chunk in str(text or "").replace(",", "\n").splitlines():
        piece = chunk.strip()
        if piece:
            parts.append(piece)
    return tuple(parts)


def _middle(text: str, limit: int) -> str:
    """Keep head and tail of a long single-line value (paths, commands)."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    keep = max(limit - 1, 8)
    head = keep // 2
    tail = keep - head
    return flat[:head] + "…" + flat[-tail:]


def _system_prefers_dark() -> Optional[bool]:
    app = QApplication.instance()
    if app is None:
        return None
    try:
        scheme = app.styleHints().colorScheme()
    except Exception:  # pragma: no cover - older Qt without colorScheme
        return None
    return scheme == Qt.ColorScheme.Dark


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def apply_theme(app: QApplication, palette: Palette) -> None:
    """The design system's fonts and colours for the whole application."""
    base_font = QFont(UI_FONT)
    base_font.setPointSizeF(FONT_PT)
    app.setFont(base_font)
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(palette.window))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Base, QColor(palette.base))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.stripe))
    qp.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Button, QColor(palette.panel))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette.selection_bg))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.selection_text))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.panel))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.muted))
    qp.setColor(QPalette.ColorRole.Link, QColor(palette.accent))
    qp.setColor(QPalette.ColorRole.BrightText, QColor(palette.err))
    app.setPalette(qp)
    # the style insets dock title text before our padding: take it off the padding
    # so the title's left edge is the panel margin the content below uses
    title_margin = app.style().pixelMetric(QStyle.PixelMetric.PM_DockWidgetTitleMargin)
    app.setStyleSheet(stylesheet(palette, DOCK_TITLE_INSET + title_margin - 1))


def run(theme: str = "auto", argv: Optional[Sequence[str]] = None) -> int:
    """Open the desktop window and start the Qt event loop. Exit code."""
    app = QApplication.instance() or QApplication(
        list(argv) if argv is not None else sys.argv[:1]
    )
    app.setApplicationName("selfkraken")
    app.setOrganizationName("selfkraken")
    palette = resolve(theme, prefers_dark=_system_prefers_dark())
    apply_theme(app, palette)
    window = MainWindow(theme=theme)
    window.show()
    return app.exec()
