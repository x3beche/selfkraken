"""Generate SVG screenshots of the selfkraken visualisations.

Usage:  PYTHONPATH=. python scripts/screenshots.py [--out docs/screenshots]

Always writes graph.svg / branches.svg / tree.svg for the multibranch demo
fixture. If the full `selfkraken.cli` module is importable (built by another
agent), also runs a complete CLI demo session in a throwaway SK_DATA_DIR and
saves each command's captured output as cli-<command>.svg. Skips that part
gracefully when the CLI does not exist yet.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from fixtures_git import make_multibranch_repo  # noqa: E402
from selfkraken.graph import _git  # noqa: E402
from selfkraken.views import render_branches, render_file_tree, render_graph  # noqa: E402

HIGHLIGHT = {"src/app/core.py", "README.md"}


def _save_svg(path: Path, renderable, title: str) -> None:
    console = Console(record=True, width=110)
    console.print(renderable)
    path.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(path), title=title)


def render_views(repo: Path, out: Path) -> list[Path]:
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
                "demo-app", files, highlight=HIGHLIGHT, sizes=sizes, max_depth=3
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


def _make_demo_diff(repo: Path, patch_file: Path) -> None:
    """Create a valid unified diff (a README note) then restore the worktree."""
    readme = repo / "README.md"
    original = readme.read_text(encoding="utf-8")
    readme.write_text(original + "\n## Proposal demo\n\nAdded via skraken propose --diff-file.\n", encoding="utf-8")
    diff = _git(repo, "diff", "--", "README.md")
    readme.write_text(original, encoding="utf-8")  # restore clean worktree
    patch_file.write_text(diff, encoding="utf-8")


def run_cli_demo(repo: Path, out: Path, work: Path) -> list[Path]:
    written: list[Path] = []
    data_dir = work / "sk-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    patch_file = work / "proposal.diff"
    _make_demo_diff(repo, patch_file)

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
        ("doctor", ["-m", "selfkraken", "doctor"]),
        ("init", ["-m", "selfkraken", "init", str(repo), "--test-cmd", f"{py} -m pytest -q"]),
        ("index", ["-m", "selfkraken", "index"]),
        ("tree", ["-m", "selfkraken", "tree"]),
        ("graph", ["-m", "selfkraken", "graph"]),
        (
            "propose",
            [
                "-m",
                "selfkraken",
                "propose",
                "Document the proposal workflow in README",
                "--diff-file",
                str(patch_file),
                "--yes",
            ],
        ),
        ("show", ["-m", "selfkraken", "show", "1"]),
        ("apply", ["-m", "selfkraken", "apply", "1", "--yes"]),
        ("log", ["-m", "selfkraken", "log"]),
    ]
    for name, argv in commands:
        cmd = [py, *argv]
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
        _save_svg(target, Text.from_ansi(output), f"skraken {' '.join(argv[1:])}")
        written.append(target)
        print(f"wrote {target}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate selfkraken SVG screenshots")
    parser.add_argument("--out", default="docs/screenshots", help="output directory")
    args = parser.parse_args()
    out = Path(args.out)

    with tempfile.TemporaryDirectory(prefix="selfkraken-shots-") as tmp:
        work = Path(tmp)
        repo = make_multibranch_repo(work / "demo-app")
        print(f"built demo repo at {repo}")
        written = render_views(repo, out)
        if cli_available():
            print("selfkraken.cli found — running full CLI demo session")
            written += run_cli_demo(repo, out, work)
        else:
            print("selfkraken.cli not importable yet — skipping CLI session screenshots")

    print("\nfiles written:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
