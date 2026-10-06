"""Rich renderables for GitKraken-style views.

Every function returns a renderable; nothing is printed here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
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

# Cell compositor: each graph cell accumulates stroke directions, then the
# glyph is looked up once — so every edge is continuous across rows.
_UP, _DOWN, _LEFT, _RIGHT = 1, 2, 4, 8

_UNICODE_GLYPHS: dict[int, str] = {
    _UP: "│",
    _DOWN: "│",
    _UP | _DOWN: "│",
    _LEFT: "─",
    _RIGHT: "─",
    _LEFT | _RIGHT: "─",
    _UP | _LEFT: "╯",       # line from above bends down-left (incoming merge)
    _UP | _RIGHT: "╰",      # line from above bends down-right (incoming merge)
    _DOWN | _LEFT: "╮",     # line bends down-left (edge to parent below)
    _DOWN | _RIGHT: "╭",    # line bends down-right (edge to parent below)
    _UP | _DOWN | _LEFT: "┤",
    _UP | _DOWN | _RIGHT: "├",
    _UP | _LEFT | _RIGHT: "┴",
    _DOWN | _LEFT | _RIGHT: "┬",
    _UP | _DOWN | _LEFT | _RIGHT: "┼",
}

_ASCII_GLYPHS: dict[int, str] = {
    _UP: "|",
    _DOWN: "|",
    _UP | _DOWN: "|",
    _LEFT: "-",
    _RIGHT: "-",
    _LEFT | _RIGHT: "-",
    _UP | _LEFT: "/",
    _UP | _RIGHT: "\\",
    _DOWN | _LEFT: "\\",
    _DOWN | _RIGHT: "/",
    _UP | _DOWN | _LEFT: "+",
    _UP | _DOWN | _RIGHT: "+",
    _UP | _LEFT | _RIGHT: "+",
    _DOWN | _LEFT | _RIGHT: "+",
    _UP | _DOWN | _LEFT | _RIGHT: "+",
}


# --------------------------------------------------------------------------- graph


def _short_date(iso: str) -> str:
    """YYYY-MM-DD from an ISO-8601 timestamp (falls back to the raw string)."""
    date_part = iso.split("T")[0]
    try:
        year, month, day = date_part.split("-")[:3]
        int(year), int(month), int(day)
    except ValueError:
        return iso
    return "-".join((year, month, day))


def _ref_labels(commit: Commit, ascii_only: bool = False) -> list[tuple[str, str]]:
    """(label, style) pairs for ref decorations, always readable as text.

    A remote-tracking ref pointing at the same commit as its local branch
    collapses into one label: ``[main ⇄ origin]`` (``[main <-> origin]`` in
    ascii mode). Remote-only refs stay ``[origin/x]``.
    """
    refs = commit.refs
    swap = " <-> " if ascii_only else " ⇄ "
    remote_of: dict[str, str] = {}  # local branch name -> remote prefix
    for ref in refs:
        if ref.startswith("tag:") or " -> " in ref or "/" not in ref:
            continue
        prefix, _, tail = ref.partition("/")
        if tail in refs and not tail.startswith("tag:"):
            remote_of[tail] = prefix
    hidden = {f"{prefix}/{local}" for local, prefix in remote_of.items()}
    labels: list[tuple[str, str]] = []
    for ref in refs:
        if ref in hidden:  # represented by the collapsed local label
            continue
        if ref == "HEAD":
            labels.append(("[HEAD]", "bold green"))
        elif ref.startswith("tag: "):
            labels.append((f"[{ref}]", "bold magenta"))
        elif ref in remote_of:
            labels.append((f"[{ref}{swap}{remote_of[ref]}]", "bold cyan"))
        elif "/" in ref:
            labels.append((f"[{ref}]", "cyan"))  # remote-only
        else:
            labels.append((f"[{ref}]", "bold cyan"))
    return labels


def _render_row(row: GraphRow, width_lanes: int, glyphs: dict[int, str],
                ascii_only: bool) -> tuple[Text, Text, Text]:
    """Draw one commit row: (graph cell, labels cell, date) as styled Texts."""
    lanes_in = row.lanes_in
    lanes_out = row.lanes_out
    sha = row.commit.sha
    column = row.column

    masks = [0] * (2 * width_lanes)
    commit_cell = 2 * column

    def get(i: int, lanes: list[str | None]) -> str | None:
        return lanes[i] if i < len(lanes) else None

    def stroke(pos: int, bits: int) -> None:
        masks[pos] |= bits

    def horizontal(a: int, b: int) -> None:
        """Stroke the cells strictly between lane a and lane b (both directions)."""
        lo, hi = sorted((a, b))
        for pos in range(2 * lo + 1, 2 * hi):
            stroke(pos, _LEFT | _RIGHT)

    # 1. Plain lane continuations.
    for i in range(width_lanes):
        up, down = get(i, lanes_in), get(i, lanes_out)
        bits = (_UP if up is not None else 0) | (_DOWN if down is not None else 0)
        if bits:
            stroke(2 * i, bits)

    # 2. Incoming merges: lanes above whose edge terminates at this commit.
    # Farthest first so nearer corners compose correctly.
    incoming = [i for i in range(len(lanes_in)) if lanes_in[i] == sha and i != column]
    for j in sorted(incoming, key=lambda i: -abs(i - column)):
        stroke(2 * j, _UP | (_LEFT if j > column else _RIGHT))
        horizontal(j, column)

    # 3. Outgoing edges: parents carried by a lane other than the commit's own.
    outgoing: set[int] = set()
    for parent in row.commit.parents:
        k = next((i for i, s in enumerate(lanes_out) if s == parent), None)
        if k is not None and k != column:
            outgoing.add(k)
    for k in sorted(outgoing, key=lambda i: -abs(i - column)):
        stroke(2 * k, _DOWN | (_LEFT if k > column else _RIGHT))
        horizontal(k, column)

    # 4. Compose the cell string; the commit node overrides its cell.
    graph = Text()
    for pos in range(2 * width_lanes):
        if pos == commit_cell:
            ch = "*" if ascii_only else "●"
        else:
            ch = glyphs.get(masks[pos], " ")
        lane = pos // 2
        style = LANE_COLORS[lane % len(LANE_COLORS)] if ch != " " and not ascii_only else ""
        graph.append(ch, style=style)

    # 5. Labels cell: refs + the [merge] text tag.
    labels = Text()
    for label, style in _ref_labels(row.commit, ascii_only):
        labels.append(label, style=style)
        labels.append(" ")
    if len(row.commit.parents) >= 2:
        labels.append("[merge]", style="bold yellow")
    return graph, labels, Text(_short_date(row.commit.date))


def render_graph(
    repo_path: Path,
    *,
    limit: int = 50,
    all_refs: bool = True,
    ascii_only: bool = False,
) -> RenderableType:
    """GitKraken-style commit graph: one non-wrapping row per commit."""
    commits = read_commits(repo_path, limit=limit, all_refs=all_refs)
    if not commits:
        return Text("No commits yet", style="dim italic")
    rows = layout(commits)
    width = max(
        max(len(row.lanes_in), len(row.lanes_out), row.column + 1) for row in rows
    )
    glyphs = _ASCII_GLYPHS if ascii_only else _UNICODE_GLYPHS

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)  # graph
    grid.add_column(no_wrap=True, style="dim")  # short sha
    grid.add_column(no_wrap=True)  # ref labels + [merge]
    grid.add_column(no_wrap=True, overflow="ellipsis", ratio=1)  # subject
    grid.add_column(no_wrap=True, style="dim")  # author
    grid.add_column(no_wrap=True, style="dim")  # date
    for row in rows:
        graph, labels, date = _render_row(row, width, glyphs, ascii_only)
        grid.add_row(graph, Text(row.commit.short), labels, Text(row.commit.subject),
                     Text(row.commit.author), date)
    return grid


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
