"""Qt item models for the selfkraken GUI (graph, proposals, log, files, branches).

Data classes from :mod:`selfkraken.db` and :mod:`selfkraken.graph` are handed
over by value; nothing here talks to SQLite or git.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QAbstractTableModel,
    QModelIndex,
    Qt,
)
from PySide6.QtGui import QColor, QFont

from selfkraken.db import Change, Proposal
from selfkraken.graph import Branch, GraphRow
from selfkraken.gui.theme import META_PT, Palette, UI_FONT


def local_time(iso: str) -> str:
    """Any ISO-8601 stamp -> local wall-clock 'YYYY-MM-DD HH:MM'.

    selfkraken stores UTC; git reports the commit's own offset — both are
    converted so the whole GUI shows one consistent (local) time.
    """
    text = (iso or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:16].replace("T", " ")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _display_date(iso: str) -> str:
    return local_time(iso)


def _one_line(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# commit graph
# --------------------------------------------------------------------------- #
class GraphTableModel(QAbstractTableModel):
    """Rows of :class:`~selfkraken.graph.GraphRow` as table rows.

    Column 0 (Graph) and column 1 (Refs) are painted by the delegate; the
    model still hands the badge text to accessibility clients.
    """

    COLUMNS = ("Graph", "Refs", "Subject", "Author", "Date")

    def __init__(self, palette: Palette, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._rows: list[GraphRow] = []
        self._branch_info: dict[str, Branch] = {}

    # -- data -----------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: D102
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        commit = row.commit
        if role == Qt.DisplayRole:
            if index.column() == 1:
                badges = self.badges_for(index.row())
                return ", ".join(text for text, _kind in badges) or None
            if index.column() == 2:
                return commit.subject or "(no subject)"
            if index.column() == 3:
                return commit.author
            if index.column() == 4:
                return _display_date(commit.date)
            return None
        if role == Qt.ToolTipRole:
            if index.column() == 2:
                return f"{commit.short}  {commit.subject}"
            if index.column() == 1:
                badges = self.badges_for(index.row())
                return ", ".join(text for text, _kind in badges) or None
            return None
        if role == Qt.TextAlignmentRole and index.column() == 4:
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and index.column() in (3, 4):
            return QColor(self._palette.muted)
        if role == Qt.FontRole and index.column() in (3, 4):
            font = QFont(UI_FONT)
            font.setPointSizeF(META_PT)
            return font
        return None

    # -- graph accessors for the delegate / view -------------------------
    def set_rows(self, rows: Sequence[GraphRow], branches: Sequence[Branch]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._branch_info = {b.name: b for b in branches}
        self.endResetModel()

    def clear(self) -> None:
        self.set_rows([], [])

    def row_at(self, row: int) -> Optional[GraphRow]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def is_merge(self, row: int) -> bool:
        """True when the commit in *row* has two or more parents (ringed dot)."""
        graph_row = self.row_at(row)
        return graph_row is not None and len(graph_row.commit.parents) >= 2

    @property
    def max_lanes(self) -> int:
        """Widest lane state seen in any row (in or out) — column-0 budget."""
        widest = 0
        for row in self._rows:
            widest = max(widest, len(row.lanes_in), len(row.lanes_out))
        return widest

    def badges_for(self, row: int) -> list[tuple[str, str]]:
        """(text, kind) ref badges for a row: head / local / remote / tag.

        A remote ref that mirrors a local branch on the same commit is folded
        into the local badge (``feature/login ⇄ origin``) so the run stays
        narrow; ``origin/HEAD`` is noise and never shown.
        """
        graph_row = self.row_at(row)
        if graph_row is None:
            return []
        refs = list(graph_row.commit.refs)
        local_names = {
            ref
            for ref in refs
            if ref != "HEAD"
            and not ref.startswith("tag: ")
            and self._ref_kind(ref) == "local"
        }
        badges: list[tuple[str, str]] = []
        for ref in refs:
            if ref == "HEAD":
                badges.append(("HEAD", "head"))
            elif ref.startswith("tag: "):
                badges.append((ref, "tag"))
            elif ref.endswith("/HEAD"):
                continue
            else:
                kind = self._ref_kind(ref)
                if kind == "remote":
                    mirrored = ref.partition("/")[2]
                    if mirrored in local_names:
                        continue  # folded into the local badge below
                    badges.append((ref, "remote"))
                else:
                    badges.append((ref + self._suffix_for(ref, refs, local_names), "local"))
        return badges

    def _ref_kind(self, ref: str) -> str:
        info = self._branch_info.get(ref)
        if info is not None:
            return "remote" if info.is_remote else "local"
        # not in the branch map (detached tip hash, gone branch): a slash and
        # no dot reads as remote/ns, a hex-ish or dotted token does not.
        return "remote" if "/" in ref and "." not in ref.split("/", 1)[0] else "local"

    def _suffix_for(self, name: str, refs: list[str], local_names: set[str]) -> str:
        info = self._branch_info.get(name)
        if info is not None and info.ahead is not None and info.behind is not None:
            if info.ahead == 0 and info.behind == 0:
                return " ⇄ origin"
            parts = []
            if info.ahead:
                parts.append(f"↑{info.ahead}")
            if info.behind:
                parts.append(f"↓{info.behind}")
            return " " + "".join(parts)
        has_remote_twin = any(
            ref.endswith("/" + name) and self._ref_kind(ref) == "remote" for ref in refs
        )
        return " ⇄ origin" if has_remote_twin else ""

    def lane_column_width(self, lane_width: int, left_pad: int, minimum: int = 0) -> int:
        """Pixels needed by the lane block of column 0 (never below *minimum*)."""
        return max(minimum, left_pad * 2 + lane_width * max(self.max_lanes, 1))

    def refs_column_width(
        self, metrics, badge_gap: int, badge_h_pad: int
    ) -> int:
        """Pixels needed by the widest badge run in the Refs column."""
        widest = 0
        for row in range(len(self._rows)):
            badges = self.badges_for(row)
            if not badges:
                continue
            total = sum(
                metrics.horizontalAdvance(text) + 2 * badge_h_pad
                for text, _kind in badges
            )
            widest = max(widest, total + badge_gap * (len(badges) - 1))
        return widest


# --------------------------------------------------------------------------- #
# proposals
# --------------------------------------------------------------------------- #
class ProposalTableModel(QAbstractTableModel):
    """Proposals, newest first: id, status, request, model, created."""

    COLUMNS = ("Id", "Status", "Request", "Model", "Created")

    def __init__(self, palette: Palette, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._proposals: list[Proposal] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._proposals)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: D102
        if not index.isValid() or not (0 <= index.row() < len(self._proposals)):
            return None
        proposal = self._proposals[index.row()]
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return str(proposal.id)
            if index.column() == 1:
                return proposal.status
            if index.column() == 2:
                prefix = "[SAMPLE] " if proposal.is_sample else ""
                return _one_line(prefix + proposal.request, 90)
            if index.column() == 3:
                return proposal.model
            if index.column() == 4:
                return _display_date(proposal.created_at)
            return None
        if role == Qt.ToolTipRole and index.column() == 2:
            return proposal.request
        if role == Qt.ForegroundRole and index.column() in (3, 4):
            return QColor(self._palette.muted)
        if role == Qt.FontRole and index.column() in (3, 4):
            font = QFont(UI_FONT)
            font.setPointSizeF(META_PT)
            return font
        return None

    def set_proposals(self, proposals: Sequence[Proposal]) -> None:
        self.beginResetModel()
        self._proposals = list(proposals)
        self.endResetModel()

    def proposal_at(self, row: int) -> Optional[Proposal]:
        if 0 <= row < len(self._proposals):
            return self._proposals[row]
        return None

    def row_for_id(self, proposal_id: int) -> int:
        for row, proposal in enumerate(self._proposals):
            if proposal.id == proposal_id:
                return row
        return -1


# --------------------------------------------------------------------------- #
# activity log
# --------------------------------------------------------------------------- #
class LogTableModel(QAbstractTableModel):
    """Audit changes, newest first: when, kind, proposal, detail."""

    COLUMNS = ("When", "Kind", "Proposal", "Detail")

    def __init__(self, palette: Palette, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._changes: list[Change] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._changes)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: D102
        if not index.isValid() or not (0 <= index.row() < len(self._changes)):
            return None
        change = self._changes[index.row()]
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return _display_date(change.created_at)
            if index.column() == 1:
                return change.kind
            if index.column() == 2:
                return "-" if change.proposal_id is None else str(change.proposal_id)
            if index.column() == 3:
                return _one_line(change.detail, 160)
            return None
        if role == Qt.ToolTipRole and index.column() == 3:
            return change.detail
        if role == Qt.ForegroundRole and index.column() == 0:
            return QColor(self._palette.muted)
        if role == Qt.FontRole and index.column() == 0:
            font = QFont(UI_FONT)
            font.setPointSizeF(META_PT)
            return font
        return None

    def set_changes(self, changes: Sequence[Change]) -> None:
        self.beginResetModel()
        self._changes = list(changes)
        self.endResetModel()

    def kinds(self) -> list[str]:
        return [change.kind for change in self._changes]


# --------------------------------------------------------------------------- #
# file tree
# --------------------------------------------------------------------------- #
class _TreeNode:
    __slots__ = ("name", "path", "is_dir", "children", "parent")

    def __init__(self, name: str, path: str, is_dir: bool, parent=None) -> None:
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.children: dict[str, _TreeNode] = {}
        self.parent = parent

    def child(self, name: str, is_dir: bool) -> _TreeNode:
        node = self.children.get(name)
        if node is None:
            prefix = f"{self.path}/" if self.path else ""
            node = _TreeNode(name, prefix + name, is_dir, self)
            self.children[name] = node
        return node

    def sorted_children(self) -> list[_TreeNode]:
        dirs = sorted((n for n in self.children.values() if n.is_dir), key=lambda n: n.name.casefold())
        files = sorted((n for n in self.children.values() if not n.is_dir), key=lambda n: n.name.casefold())
        return dirs + files


class FileTreeModel(QAbstractItemModel):
    """Repository file tree; files touched by the selected proposal are marked."""

    CHANGED_MARK = "✎ changed"

    def __init__(self, palette: Palette, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._root = _TreeNode("", "", True)
        self._changed: set[str] = set()

    # -- building --------------------------------------------------------
    def set_files(self, paths: Sequence[str], changed: Sequence[str] = ()) -> None:
        self.beginResetModel()
        root = _TreeNode("", "", True)
        for rel in paths:
            parts = str(rel).split("/")
            node = root
            for part in parts[:-1]:
                node = node.child(part, True)
            node.child(parts[-1], False)
        self._root = root
        self._changed = set(changed)
        self.endResetModel()

    def set_changed(self, changed: Sequence[str]) -> None:
        """Re-mark the files touched by the selected proposal (no reset)."""
        self._changed = set(changed)
        self._emit_changed(self._root, QModelIndex())

    def _emit_changed(self, node: _TreeNode, parent_index: QModelIndex) -> None:
        for row, child in enumerate(node.sorted_children()):
            index = self.createIndex(row, 0, child)
            self.dataChanged.emit(index, index)
            if child.is_dir:
                self._emit_changed(child, index)

    def clear(self) -> None:
        self.set_files([], [])

    # -- model plumbing ---------------------------------------------------
    @staticmethod
    def _node(index: QModelIndex) -> _TreeNode:
        return index.internalPointer() if index.isValid() else None  # type: ignore[return-value]

    def index(self, row, column, parent=QModelIndex()):  # noqa: D102
        parent_node = parent.internalPointer() if parent.isValid() else self._root
        children = parent_node.sorted_children()
        if not (0 <= row < len(children)):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # noqa: D102
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        node_parent = node.parent
        if node_parent is None or node_parent is self._root:
            return QModelIndex()
        grandparent = node_parent.parent
        siblings = grandparent.sorted_children() if grandparent else []
        try:
            row = siblings.index(node_parent)
        except ValueError:  # pragma: no cover - defensive
            return QModelIndex()
        return self.createIndex(row, 0, node_parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.column() > 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self._root
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 1

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: D102
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == Qt.DisplayRole:
            if node.is_dir:
                return node.name + "/"
            if node.path in self._changed:
                return f"{node.name}  {self.CHANGED_MARK}"
            return node.name
        if role == Qt.ToolTipRole:
            return node.path
        if role == Qt.FontRole and not node.is_dir and node.path in self._changed:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ForegroundRole and not node.is_dir and node.path in self._changed:
            return QColor(self._palette.ok)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:  # noqa: D102
        base = Qt.ItemIsEnabled
        if index.column() == 0:
            base = base | Qt.ItemIsSelectable
        return base


# --------------------------------------------------------------------------- #
# branches
# --------------------------------------------------------------------------- #
class BranchItem:
    __slots__ = ("branch", "text", "tooltip")

    def __init__(self, branch: Branch) -> None:
        self.branch = branch
        label = branch.name
        if branch.is_current:
            label = f"● current · {label}"
        if not branch.is_remote and branch.ahead is not None and branch.behind is not None:
            label += f" — ahead {branch.ahead}, behind {branch.behind}"
        elif branch.is_remote:
            label += "  (remote)"
        self.text = label
        self.tooltip = f"{branch.name} @ {branch.sha[:12]}"


class BranchListModel(QAbstractListModel):
    def __init__(self, palette: Palette, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._items: list[BranchItem] = []

    def set_branches(self, branches: Sequence[Branch]) -> None:
        self.beginResetModel()
        self._items = [BranchItem(b) for b in branches]
        self.endResetModel()

    def clear(self) -> None:
        self.set_branches([])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: D102
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == Qt.DisplayRole:
            return item.text
        if role == Qt.ToolTipRole:
            return item.tooltip
        if role == Qt.ForegroundRole:
            if item.branch.is_remote:
                return QColor(self._palette.muted)
            if item.branch.is_current:
                return QColor(self._palette.accent)
        return None
