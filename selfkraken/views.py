"""Rich renderables for GitKraken-style views.

Every function returns a renderable; nothing is printed here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from selfkraken.graph import Commit, GraphRow, _git, layout, list_branches, read_commits

# High-contrast palette; lane index picks the colour so lanes stay stable.
LANE_COLORS: tuple[str, ...] = (
    "bright_cyan",
    "magenta",
    "yellow",
    "green",
    "bright_blue",
    "red",
    "bright_magenta",
    "cyan",
)

_UNICODE_GLYPHS: dict[str, str] = {
    "commit": "●",
    "vert": "│",
    "diag_r": "╲",
    "diag_l": "╱",
    "hline": "─",
    "cross": "┼",
    "x": "╳",
    "up_right": "╰",     # lane above bends right (incoming merge, left of commit)
    "up_left": "╯",      # lane above bends left (incoming merge, right of commit)
    "down_left": "╮",    # bends down toward lane right of commit (outgoing edge)
    "down_right": "╭",   # bends down toward lane left of commit (outgoing edge)
}

_ASCII_GLYPHS: dict[str, str] = {
    "commit": "*",
    "vert": "|",
    "diag_r": "\\",
    "diag_l": "/",
    "hline": "-",
    "cross": "+",
    "x": "x",
    "up_right": "+",
    "up_left": "+",
    "down_left": "+",
    "down_right": "+",
}


# --------------------------------------------------------------------------- graph


def _relative_date(iso: str, now: datetime | None = None) -> str:
    """'3h ago' style date from an ISO-8601 string, or 'YYYY-MM-DD' when older."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso.split("T")[0] if "T" in iso else iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now or datetime.now(timezone.utc)) - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return dt.strftime("%Y-%m-%d")


def _ref_labels(commit: Commit) -> list[tuple[str, str]]:
    """(label, style) pairs for ref decorations, always readable as text."""
    labels: list[tuple[str, str]] = []
    for ref in commit.refs:
        if ref == "HEAD":
            labels.append(("[HEAD]", "bold green"))
        elif ref.startswith("tag: "):
            labels.append((f"[{ref}]", "bold magenta"))
        elif ref.endswith("]") and " -> " in ref:  # e.g. remote ref form
            labels.append((f"[{ref}]", "bold cyan"))
        elif " -> " in ref:
            head, _, target = ref.partition(" -> ")
            labels.append((f"[{head.strip()}]", "bold green"))
            labels.append((f"[{target.strip()}]", "bold cyan"))
        else:
            labels.append((f"[{ref}]", "bold cyan"))
    return labels


def _render_row(row: GraphRow, width_lanes: int, glyphs: dict[str, str],
                colored: bool) -> Text:
    """Draw one commit row: lane glyphs, sha, labels, subject, author, date."""
    lanes_in = row.lanes_in
    lanes_out = row.lanes_out
    sha = row.commit.sha
    column = row.column

    chars: list[str | None] = [None] * (2 * width_lanes)
    styles: list[str | None] = [None] * (2 * width_lanes)

    def lane_color(i: int) -> str:
        color = LANE_COLORS[i % len(LANE_COLORS)]
        return color if colored else ""

    def get(i: int, lanes: list[str | None]) -> str | None:
        return lanes[i] if i < len(lanes) else None

    def put(pos: int, ch: str, style: str) -> None:
        chars[pos] = ch
        styles[pos] = style

    def cross(pos: int, style: str) -> None:
        """Lay a horizontal segment through a position without clobbering nodes."""
        cur = chars[pos]
        if pos % 2 == 0:  # lane cell
            if cur in (None, " ", glyphs["hline"]):
                put(pos, glyphs["hline"], style)
            elif cur != glyphs["commit"]:
                put(pos, glyphs["cross"], style)
        else:  # gap between lanes
            if cur in (None, " ", glyphs["hline"]):
                put(pos, glyphs["hline"], style)
            elif cur in (glyphs["diag_r"], glyphs["diag_l"]):
                put(pos, glyphs["x"], style)

    # 1. Plain lane continuations (before any curves overwrite them).
    for i in range(width_lanes):
        up, down = get(i, lanes_in), get(i, lanes_out)
        if up is not None or down is not None:
            put(2 * i, glyphs["vert"], lane_color(i))

    # 2. Incoming merges: lanes above whose edge terminates at this commit.
    # Farthest first so nearer corners/paths overwrite crossing positions.
    incoming = [i for i in range(len(lanes_in)) if lanes_in[i] == sha and i != column]
    use_diag_in = len(incoming) == 1
    for j in sorted(incoming, key=lambda i: -abs(i - column)):
        if use_diag_in and abs(j - column) == 1:
            gap = 2 * min(j, column) + 1
            put(gap, glyphs["diag_r"] if j < column else glyphs["diag_l"], lane_color(j))
            if get(j, lanes_out) is None:  # no continuing line below: blank the cell
                put(2 * j, " ", "")
        else:
            put(2 * j, glyphs["up_right" if j < column else "up_left"], lane_color(j))
            lo, hi = sorted((j, column))
            for pos in range(2 * lo + 1, 2 * hi):
                cross(pos, lane_color(j))

    # 3. Outgoing edges: parents carried by a lane other than the commit's own.
    outgoing: list[int] = []
    for parent in row.commit.parents:
        k = next((i for i, s in enumerate(lanes_out) if s == parent), None)
        if k is not None and k != column:
            outgoing.append(k)
    use_diag_out = len(outgoing) == 1
    for k in sorted(set(outgoing), key=lambda i: -abs(i - column)):
        if use_diag_out and abs(k - column) == 1:
            gap = 2 * min(k, column) + 1
            put(gap, glyphs["diag_r"] if k > column else glyphs["diag_l"], lane_color(k))
        else:
            put(2 * k, glyphs["down_left" if k > column else "down_right"], lane_color(k))
            lo, hi = sorted((k, column))
            for pos in range(2 * lo + 1, 2 * hi):
                cross(pos, lane_color(column))

    # 4. The commit node itself (always overwrites any '│' at this lane).
    put(2 * column, glyphs["commit"], lane_color(column))
    for pos in range(width_lanes * 2):
        if chars[pos] is None:
            chars[pos] = " "
            styles[pos] = None

    text = Text()
    for ch, style in zip(chars, styles):
        text.append(ch, style or "")
    text.append("  ")
    text.append(row.commit.short, style="dim")
    text.append(" ")
    for label, style in _ref_labels(row.commit):
        text.append(label, style=style)
        text.append(" ")
    if len(row.commit.parents) >= 2:
        text.append("[merge]", style="bold yellow")
        text.append(" ")
    text.append(row.commit.subject)
    text.append("  (", style="dim")
    text.append(row.commit.author, style="dim")
    text.append(f", {_relative_date(row.commit.date)})", style="dim")
    return text


def render_graph(
    repo_path: Path,
    *,
    limit: int = 50,
    all_refs: bool = True,
    ascii_only: bool = False,
) -> RenderableType:
    """GitKraken-style commit graph, one text row per commit."""
    commits = read_commits(repo_path, limit=limit, all_refs=all_refs)
    if not commits:
        return Text("No commits yet", style="dim italic")
    rows = layout(commits)
    width = max(
        max(len(row.lanes_in), len(row.lanes_out), row.column + 1) for row in rows
    )
    glyphs = _ASCII_GLYPHS if ascii_only else _UNICODE_GLYPHS
    colored = not ascii_only
    return Text("\n").join(_render_row(row, width, glyphs, colored) for row in rows)


# ------------------------------------------------------------------------ branches


def _divergence_text(value: int | None, symbol: str) -> str:
    if value is None:
        return "—"
    return f"{symbol}{value}"


def render_branches(repo_path: Path) -> RenderableType:
    """Table of local and remote branches with divergence and last subject."""
    branches = list_branches(repo_path)
    if not branches:
        return Text("No branches yet — nothing committed?", style="dim italic")
    table = Table(
        title="Branches",
        box=box.SIMPLE_HEAVY,
        header_style="bold",
        show_header=True,
        expand=False,
    )
    table.add_column("", no_wrap=True)
    table.add_column("Branch", style="cyan", no_wrap=True)
    table.add_column("Commit", style="dim", no_wrap=True)
    table.add_column("Ahead", justify="right")
    table.add_column("Behind", justify="right")
    table.add_column("Last commit")
    for branch in branches:
        marker = Text("* (current)", style="bold green") if branch.is_current else Text("")
        name = Text(branch.name, style="cyan" if not branch.is_remote else "dim cyan")
        subject = ""
        try:
            subject = _git(repo_path, "log", "-1", "--format=%s", branch.name).strip()
        except Exception:  # unreachable ref or similar: show without subject
            subject = ""
        table.add_row(
            marker,
            name,
            branch.sha[:7],
            _divergence_text(branch.ahead, "+"),
            _divergence_text(branch.behind, "-"),
            subject,
        )
    return table


# --------------------------------------------------------------------- file tree


def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # pragma: no cover - unreachable


class _DirNode:
    __slots__ = ("name", "dirs", "files", "path")

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        self.dirs: dict[str, _DirNode] = {}
        self.files: list[tuple[str, str]] = []  # (display name, full relative path)

    def file_count(self) -> int:
        return len(self.files) + sum(d.file_count() for d in self.dirs.values())


def render_file_tree(
    root_name: str,
    files: Sequence[str],
    *,
    max_depth: int | None = None,
    highlight: Collection[str] = (),
    sizes: Mapping[str, int] | None = None,
) -> Tree:
    """Render a repository file tree: directories first, then files, alphabetical.

    Dirs deeper than ``max_depth`` collapse to a ``name/ … (N files)`` label.
    Paths in ``highlight`` get a ``✎ changed`` text label and bold style.
    ``sizes`` maps a relative path to a byte count shown after the name.
    """
    tree = Tree(Text(f"{root_name}/", style="bold"))
    if not files:
        tree.add(Text("(no files)", style="dim italic"))
        return tree

    highlighted = {p.strip("/") for p in highlight}
    root = _DirNode(root_name, "")
    for rel in files:
        parts = [p for p in rel.strip("/").split("/") if p]
        if not parts:
            continue
        node = root
        for i, part in enumerate(parts[:-1]):
            node = node.dirs.setdefault(part, _DirNode(part, "/".join(parts[: i + 1])))
        node.files.append((parts[-1], "/".join(parts)))

    def render_into(parent: Tree, node: _DirNode, depth: int) -> None:
        # Directories first (alphabetical), then files (alphabetical).
        for name in sorted(node.dirs, key=str.casefold):
            child = node.dirs[name]
            label = Text(f"{name}/")
            collapsed = max_depth is not None and depth >= max_depth and child.file_count() > 0
            if collapsed:
                label.append(f" … ({child.file_count()} files)", style="dim")
                parent.add(label)  # no children below a collapsed dir
                continue
            child_branch = parent.add(label, style="bold")
            render_into(child_branch, child, depth + 1)
        for display, full in sorted(node.files, key=lambda f: (f[0].casefold(), f[0])):
            label = Text(display)
            if sizes is not None and full in sizes:
                label.append(f"  ({_human_size(sizes[full])})", style="dim")
            if full in highlighted:
                label.append("  ✎ changed", style="bold yellow")
                parent.add(label, style="bold yellow")
            else:
                parent.add(label)

    render_into(tree, root, 1)
    return tree
