"""Custom-painted commit graph (QTableView + delegate), GitKraken-style.

Column 0 of every row is painted from a :class:`~selfkraken.graph.GraphRow`:
straight anti-aliased lane verticals, bezier curves where a lane converges
into a merge dot or a new parent lane forks away below a dot, filled dots
(ringed for merges), and rounded ref badges (``HEAD`` / ``main ↑1`` /
``tag: v0.1.0`` / ``origin/main``).  Rows share one height and the grid is
off, so lanes connect seamlessly across rows.  The remaining columns show
subject, author and date.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTableView,
)

from selfkraken.gitops import run_git
from selfkraken.graph import Branch, GraphRow
from selfkraken.gui.models import TIMESTAMP_SAMPLE, GraphTableModel, sized_column_width
from selfkraken.gui.theme import (
    BADGE_GAP,
    BADGE_H_PAD,
    BADGE_HEIGHT,
    BADGE_RADIUS,
    BORDER_WIDTH,
    CELL_PADDING,
    GRAPH_ROW_HEIGHT,
    META_PT,
    SPACE_6,
    Palette,
    ui_family,
)

#: row metrics — taken from the shared design system (graph rows run 24px)
ROW_HEIGHT = GRAPH_ROW_HEIGHT
LANE_WIDTH = 14  # lane pitch: dot centre to dot centre
LANE_LEFT_PAD = SPACE_6  # lane block inset, mirrored on the right
DOT_RADIUS = 4.0
LINE_WIDTH = 2.0
BADGE_FONT_PT = META_PT
REFS_COLUMN_MAX = 260  # the widest badge run we will make room for
#: the author column may always elide further than this (tooltip has the rest)
AUTHOR_COLUMN_MAX = 110  # names elide; the tooltip has the whole one


def header_font() -> QFont:
    """The font the styled header sections actually use."""
    font = QFont(ui_family())
    font.setPointSizeF(META_PT)
    font.setBold(True)
    return font


def graph_column_minimum(title: str = "Graph") -> int:
    """Column 0 is never narrower than its own header text.

    With a single lane the lane block alone is ~20px, which used to elide
    "Graph" down to "ra|"; now the header always fits.
    """
    metrics = QFontMetrics(header_font())
    return metrics.horizontalAdvance(title) + 2 * CELL_PADDING + 2 * BORDER_WIDTH


def lane_x(rect: QRect, lane: int) -> float:
    """Centre x of one lane inside a cell rect."""
    return rect.left() + LANE_LEFT_PAD + lane * LANE_WIDTH + LANE_WIDTH / 2.0


# --------------------------------------------------------------------------- #
# painting
# --------------------------------------------------------------------------- #
def paint_lane_cell(
    painter: QPainter,
    rect: QRect,
    graph_row: GraphRow,
    palette: Palette,
    selected: bool,
) -> None:
    """Paint background, lanes and the commit dot for one graph row."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if selected:
        painter.fillRect(rect, QColor(palette.selection_bg))
    else:
        painter.fillRect(rect, QColor(palette.base))

    commit = graph_row.commit
    column = graph_row.column
    centre_y = rect.top() + ROW_HEIGHT / 2.0
    dot_x = lane_x(rect, column)
    top = float(rect.top())
    bottom = float(rect.bottom() + 1)

    lanes_total = max(len(graph_row.lanes_in), len(graph_row.lanes_out), column + 1)
    for lane in range(lanes_total):
        colour = QColor(palette.lanes[lane % len(palette.lanes)])
        pen = QPen(colour, LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        in_sha = graph_row.lanes_in[lane] if lane < len(graph_row.lanes_in) else None
        out_sha = graph_row.lanes_out[lane] if lane < len(graph_row.lanes_out) else None

        if lane == column:
            if in_sha is not None:  # line down into the dot
                painter.drawLine(dot_x, top, dot_x, centre_y - DOT_RADIUS)
            if out_sha is not None:  # first parent continues below the dot
                painter.drawLine(dot_x, centre_y + DOT_RADIUS, dot_x, bottom)
            continue

        if in_sha is None and out_sha is None:
            continue
        x = lane_x(rect, lane)
        if in_sha is not None and in_sha == out_sha:  # passing straight through
            painter.drawLine(x, top, x, bottom)
            continue
        if in_sha is not None:  # lane converges into this commit (merge-in)
            path = QPainterPath()
            path.moveTo(x, top)
            path.cubicTo(x, (top + centre_y) / 2.0, dot_x, (top + centre_y) / 2.0,
                         dot_x, centre_y - DOT_RADIUS)
            painter.drawPath(path)
        if out_sha is not None:  # a parent lane starts below this commit
            path = QPainterPath()
            path.moveTo(dot_x, centre_y + DOT_RADIUS)
            path.cubicTo(dot_x, (centre_y + bottom) / 2.0, x, (centre_y + bottom) / 2.0,
                         x, bottom)
            painter.drawPath(path)

    # commit dot: filled circle, ringed when merge (>= 2 parents)
    colour = QColor(palette.lanes[column % len(palette.lanes)])
    centre = QPointF(dot_x, centre_y)
    if len(commit.parents) >= 2:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(centre, DOT_RADIUS + 2.0, DOT_RADIUS + 2.0)
        painter.setBrush(QColor(palette.selection_bg if selected else palette.base))
        painter.drawEllipse(centre, DOT_RADIUS - 2.0, max(DOT_RADIUS - 2.0, 1.0))
    else:
        painter.setPen(QPen(colour.darker(130), 1.0))
        painter.setBrush(colour)
        painter.drawEllipse(centre, DOT_RADIUS, DOT_RADIUS)

    painter.restore()


def paint_refs_cell(
    painter: QPainter,
    rect: QRect,
    badges: Sequence[tuple[str, str]],
    palette: Palette,
    metrics: QFontMetrics,
    selected: bool,
) -> None:
    """Paint the rounded ref-badge run of one row (Refs column)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if selected:
        painter.fillRect(rect, QColor(palette.selection_bg))
    else:
        painter.fillRect(rect, QColor(palette.base))
    if not badges:
        painter.restore()
        return
    shown = 0
    previous_font = painter.font()
    font = QFont(ui_family())
    font.setPointSizeF(BADGE_FONT_PT)
    painter.setFont(font)
    x = float(rect.left() + CELL_PADDING)
    centre_y = rect.top() + ROW_HEIGHT / 2.0
    for text, kind in badges:
        fg, bg, border = palette.badge.get(kind, palette.badge["local"])
        width = metrics.horizontalAdvance(text) + 2 * BADGE_H_PAD
        box = QRectF(x, centre_y - BADGE_HEIGHT / 2.0, width, BADGE_HEIGHT)
        if box.right() > rect.right() - BORDER_WIDTH:
            break  # the run needs a "+N" chip instead of a cut-off badge
        shown += 1
        painter.setPen(QPen(QColor(border), 1.0))
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(box, BADGE_RADIUS, BADGE_RADIUS)
        painter.setPen(QPen(QColor(fg)))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        x += width + BADGE_GAP
    hidden = len(badges) - shown
    if hidden > 0:
        # the refs that did not fit are counted, never dropped silently
        label = f"+{hidden}"
        width = metrics.horizontalAdvance(label) + 2 * BADGE_H_PAD
        box = QRectF(x, centre_y - BADGE_HEIGHT / 2.0, width, BADGE_HEIGHT)
        if box.right() <= rect.right() - BORDER_WIDTH:
            painter.setPen(QPen(QColor(palette.border), 1.0))
            painter.setBrush(QColor(palette.panel))
            painter.drawRoundedRect(box, BADGE_RADIUS, BADGE_RADIUS)
            painter.setPen(QPen(QColor(palette.muted)))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
    painter.setFont(previous_font)
    painter.restore()


# --------------------------------------------------------------------------- #
# delegate
# --------------------------------------------------------------------------- #
class GraphDelegate(QStyledItemDelegate):
    """Paints the graph column by hand; the style draws the text columns."""

    def __init__(self, table_model: GraphTableModel, palette: Palette, parent=None) -> None:
        super().__init__(parent)
        self._table_model = table_model
        self._palette = palette
        self._badge_font = QFont(ui_family())
        self._badge_font.setPointSizeF(BADGE_FONT_PT)
        self._badge_metrics = QFontMetrics(self._badge_font)

    @property
    def badge_font(self) -> QFont:
        return self._badge_font

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        # every row gets the same height, so lanes connect without gaps
        if index.column() == 0:
            width = self._table_model.lane_column_width(
                LANE_WIDTH, LANE_LEFT_PAD, graph_column_minimum()
            )
            return QSize(width, ROW_HEIGHT)
        if index.column() == 1:
            return QSize(self.refs_width(), ROW_HEIGHT)
        return QSize(super().sizeHint(option, index).width(), ROW_HEIGHT)

    def refs_width(self) -> int:
        """Refs column: the widest badge run, capped, never below its header."""
        run = self._table_model.refs_column_width(
            self._badge_metrics, BADGE_GAP, BADGE_H_PAD
        )
        header = QFontMetrics(header_font()).horizontalAdvance(
            self._table_model.COLUMNS[1]
        )
        # exactly as wide as the widest badge run that is actually present
        return min(max(run + 2 * CELL_PADDING, header + 2 * CELL_PADDING), REFS_COLUMN_MAX)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: D102
        column = index.column()
        if column == 0:
            graph_row = self._table_model.row_at(index.row())
            if graph_row is None:
                return
            paint_lane_cell(
                painter,
                option.rect,
                graph_row,
                self._palette,
                bool(option.state & QStyle.StateFlag.State_Selected),
            )
            return
        if column == 1:
            graph_row = self._table_model.row_at(index.row())
            if graph_row is None:
                return
            paint_refs_cell(
                painter,
                option.rect,
                self._table_model.badges_for(index.row()),
                self._palette,
                self._badge_metrics,
                bool(option.state & QStyle.StateFlag.State_Selected),
            )
            return
        super().paint(painter, option, index)


# --------------------------------------------------------------------------- #
# view
# --------------------------------------------------------------------------- #
class GraphView(QTableView):
    """The graph table: single-row selection, uniform rows, keyboard navigable."""

    commit_selected = Signal(object)  # GraphRow | None

    def __init__(self, palette: Palette, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.table_model = GraphTableModel(palette, self)
        self.setModel(self.table_model)
        self.graph_delegate = GraphDelegate(self.table_model, palette, self)
        self.setItemDelegate(self.graph_delegate)

        self.setShowGrid(False)
        self.setAlternatingRowColors(False)  # lanes must read as one continuous grid
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsMovable(False)
        header.setMinimumSectionSize(2 * SPACE_6)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.selectionModel().selectionChanged.connect(self._emit_selection)
        self.setAccessibleName("Commit graph")
        self.setAccessibleDescription(
            "Repository commit history. Arrow keys move between commits."
        )
        self.setToolTip("Arrow keys navigate; Enter shows the commit details.")
        self._layout_columns()

    # -- data --------------------------------------------------------------
    def set_rows(self, rows: list[GraphRow], branches: Sequence[Branch]) -> None:
        self.table_model.set_rows(rows, branches)
        self._layout_columns()

    def clear_rows(self) -> None:
        self.table_model.clear()
        self._layout_columns()

    def row_graph(self, row: int) -> Optional[GraphRow]:
        return self.table_model.row_at(row)

    def select_row(self, row: int) -> None:
        if 0 <= row < self.table_model.rowCount():
            self.selectRow(row)

    # -- layout --------------------------------------------------------------
    def _layout_columns(self) -> None:
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(
            0,
            self.table_model.lane_column_width(
                LANE_WIDTH, LANE_LEFT_PAD, graph_column_minimum()
            ),
        )
        # Refs takes only what its badges need; Subject gets all the stretch
        self.setColumnWidth(1, self.graph_delegate.refs_width())
        # Author and Date are measured from the real font: the date column always
        # fits "YYYY-MM-DD HH:MM", the author column fits the longest author in
        # the history up to a cap (longer names elide, with the tooltip).
        authors = [row.commit.author for row in self.table_model.rows()] or ["Author"]
        widest_author = max(authors, key=len)
        self.setColumnWidth(
            3,
            min(
                sized_column_width(self, widest_author, self.table_model.COLUMNS[3]),
                AUTHOR_COLUMN_MAX,
            ),
        )
        self.setColumnWidth(
            4,
            sized_column_width(self, TIMESTAMP_SAMPLE, self.table_model.COLUMNS[4]),
        )

    def _emit_selection(self, _selected, _deselected) -> None:
        rows = self.selectionModel().selectedRows()
        if not rows:
            self.commit_selected.emit(None)
            return
        self.commit_selected.emit(self.table_model.row_at(rows[0].row()))


# --------------------------------------------------------------------------- #
# commit details (runs in a worker — spawns git)
# --------------------------------------------------------------------------- #
_DETAIL_FS = "\x1f"


def read_commit_detail(repo_path: Path, sha: str) -> dict[str, object]:
    """Metadata for one commit: sha, parents, author, date, full message.

    The separators are git ``%x`` escapes — literal NUL cannot go through
    ``execve`` (argv), so git must emit them itself.
    """
    fmt = "%x1f".join(["%H", "%P", "%an", "%ad", "%s", "%b"]) + "%x00"
    proc = run_git(
        Path(repo_path),
        "--no-pager", "show", "-s", f"--format={fmt}", "--date=iso-strict", sha,
    )
    out = proc.stdout.strip("\n")
    record, _, _ = out.partition("\x00")
    fields = record.split(_DETAIL_FS)
    if len(fields) != 6:  # pragma: no cover - defensive
        raise ValueError("unexpected git show output")
    full_sha, parents, author, date, subject, body = fields
    return {
        "sha": full_sha,
        "parents": parents.split(),
        "author": author,
        "date": date,
        "subject": subject,
        "body": body.strip(),
    }
