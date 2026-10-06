"""Generate SVG screenshots of the selfkraken visualisations.

Usage:  PYTHONPATH=. python scripts/screenshots.py [--out docs/screenshots]

Always writes graph.svg / branches.svg / tree.svg for the multibranch demo
fixture (the tree highlights the files the demo proposal touches, which live
under src/). If the full ``selfkraken.cli`` module is importable, also runs a
complete CLI demo session in a throwaway SK_DATA_DIR and saves each command's
captured output as cli-<command>.svg. Skips that part gracefully when the CLI
does not exist yet.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from _datadir_guard import refuse_real_data_dir  # noqa: E402
from fixtures_git import make_multibranch_repo  # noqa: E402
from selfkraken.graph import _git  # noqa: E402
from selfkraken.views import render_branches, render_file_tree, render_graph  # noqa: E402


def _save_svg(path: Path, renderable, title: str) -> None:
    console = Console(record=True, width=110)
    console.print(renderable)
    path.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(path), title=title)


def render_views(repo: Path, out: Path, highlight: set[str]) -> list[Path]:
    written: list[Path] = []
    jobs = [
        ("graph.svg", render_graph(repo, limit=50), "selfkraken graph"),
        ("branches.svg", render_branches(repo), "selfkraken branches"),
    ]
    files = sorted(_git(repo, "ls-files").split())
    sizes = {}
    for rel in files:
        stat_path = repo / rel
        if stat_path.is_file():
            sizes[rel] = stat_path.stat().st_size
    jobs.append(
        (
            "tree.svg",
            render_file_tree(
                "demo-app", files, highlight=highlight, sizes=sizes, max_depth=3
            ),
            "selfkraken file tree",
        )
    )
    for name, renderable, title in jobs:
        target = out / name
        _save_svg(target, renderable, title)
        written.append(target)
        print(f"wrote {target}")
    return written


def cli_available() -> bool:
    try:
        return importlib.util.find_spec("selfkraken.cli") is not None
    except (ImportError, ValueError):
        return False


def _make_demo_diff(repo: Path, patch_file: Path) -> set[str]:
    """Build a valid unified diff touching src/app/core.py; restore the worktree.

    Returns the set of touched paths so the tree screenshot can highlight them.
    """
    target = repo / "src" / "app" / "core.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(
        original
        + '\n\ndef proposal_note() -> str:\n    """Added by the skraken propose demo."""\n    return "applied via skraken apply"\n',
        encoding="utf-8",
    )
    diff = _git(repo, "diff", "--", "src/app/core.py")
    target.write_text(original, encoding="utf-8")  # restore clean worktree
    patch_file.write_text(diff, encoding="utf-8")
    touched = set(re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.MULTILINE))
    return touched or {"src/app/core.py"}


def run_cli_demo(repo: Path, out: Path, work: Path, patch_file: Path) -> list[Path]:
    written: list[Path] = []
    data_dir = work / "sk-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # belt and braces: a TMPDIR inside the user data dir must never be used
    refuse_real_data_dir(data_dir, "the demo data dir (check TMPDIR)")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SK_LLM")  # degraded mode: no model vars
    }
    env.update(
        {
            "SK_DATA_DIR": str(data_dir),
            "FORCE_COLOR": "1",
            "COLUMNS": "110",
            "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
    )

    py = sys.executable
    commands: list[tuple[str, list[str]]] = [
        ("doctor", ["doctor"]),
        ("init", ["init", str(repo), "--test-cmd", f"{py} -m pytest -q"]),
        ("index", ["index"]),
        ("tree", ["tree"]),
        ("graph", ["graph"]),
        ("branches", ["branches"]),
        (
            "propose",
            [
                "propose",
                "Add a proposal_note() helper to src/app/core.py",
                "--diff-file",
                str(patch_file),
                "--yes",
            ],
        ),
        ("show", ["show", "1"]),
        ("apply", ["apply", "1", "--yes"]),
        ("log", ["log"]),
    ]
    for name, args in commands:
        cmd = [py, "-m", "selfkraken", *args]
        returncode = 0
        try:
            proc = subprocess.run(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(repo),
                env=env,
            )
            output = proc.stdout + proc.stderr
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            output = "(command timed out after 180s)\n"
        if not output.strip():
            output = "(no output)\n"
        if returncode != 0:
            print(f"  note: skraken {name} exited {returncode} — captured anyway")
        target = out / f"cli-{name}.svg"
        _save_svg(target, Text.from_ansi(output), f"$ skraken {' '.join(args)}")
        written.append(target)
        print(f"wrote {target}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate selfkraken SVG screenshots")
    parser.add_argument("--out", default="docs/screenshots", help="output directory")
    args = parser.parse_args()
    out = Path(args.out)

    # never read from (or hand anything that could write to) the real user data
    if os.environ.get("SK_DATA_DIR"):
        refuse_real_data_dir(os.environ["SK_DATA_DIR"], "the SK_DATA_DIR you exported")

    with tempfile.TemporaryDirectory(prefix="selfkraken-shots-") as tmp:
        work = Path(tmp)
        repo = make_multibranch_repo(work / "demo-app")
        print(f"built demo repo at {repo}")
        patch_file = work / "proposal.diff"
        touched = _make_demo_diff(repo, patch_file)
        written = render_views(repo, out, highlight=touched)
        if cli_available():
            print("selfkraken.cli found — running full CLI demo session")
            written += run_cli_demo(repo, out, work, patch_file)
        else:
            print("selfkraken.cli not importable yet — skipping CLI session screenshots")

    print("\nfiles written:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
