"""Tests for selfkraken.views: render graph/branches/tree to text."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from fixtures_git import make_criss_cross_repo, make_multibranch_repo, make_octopus_repo
from selfkraken.views import render_branches, render_file_tree, render_graph

BOX_GLYPHS = "●│╲╱─┼╳╮╯╰╭"


def capture(renderable, width: int = 120) -> str:
    console = Console(record=True, width=width, color_system=None)
    console.print(renderable)
    return console.export_text()


def _make_empty(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-b", "main"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return path


# ------------------------------------------------------------------ render_graph


def test_graph_unicode_contains_key_text(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    text = capture(render_graph(repo))
    assert "●" in text
    assert "[HEAD]" in text
    assert "[main]" in text
    assert "[merge]" in text
    assert "v0.1.0" in text
    assert "Merge feature/login into main" in text
    assert "Merge main into feature/search" in text
    assert "experiment/cache" in text
    assert "Kraken Fixture" in text


def test_graph_shows_lane_glyphs_for_fixtures(tmp_path: Path) -> None:
    for builder in (make_multibranch_repo, make_octopus_repo, make_criss_cross_repo):
        repo = builder(tmp_path / builder.__name__)
        text = capture(render_graph(repo))
        assert "●" in text and "[HEAD]" in text, builder.__name__
        if builder is not make_octopus_repo:
            assert "[merge]" in text


def test_graph_octopus_row_draws_multiple_out_edges(tmp_path: Path) -> None:
    repo = make_octopus_repo(tmp_path / "repo")
    text = capture(render_graph(repo), width=160)
    octo_row = next(line for line in text.splitlines() if "octopus" in line)
    assert octo_row.count("╮") == 3 or octo_row.count("+") == 3  # three side lanes


def test_graph_ascii_only_has_no_box_glyphs(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    text = capture(render_graph(repo, ascii_only=True), width=160)
    for glyph in BOX_GLYPHS:
        assert glyph not in text, glyph
    assert "*" in text
    assert "|" in text
    assert "[merge]" in text


def test_graph_limit_and_rows(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    text = capture(render_graph(repo, limit=5), width=160)
    rows = [line for line in text.splitlines() if line.strip()]
    assert len(rows) == 5
    assert "Initial project scaffold" not in text
    assert "Bump release metadata" in text


def test_graph_empty_repo_friendly(tmp_path: Path) -> None:
    empty = _make_empty(tmp_path / "empty")
    text = capture(render_graph(empty))
    assert "No commits yet" in text


def test_graph_ref_labels_are_text_not_color_only(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    # color_system=None strips all styling; labels must survive as plain text
    text = capture(render_graph(repo))
    assert "[tag: v0.1.0]" in text
    assert "[origin/feature/search]" in text


# ---------------------------------------------------------------- render_branches


def test_branches_table(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "repo")
    text = capture(render_branches(repo))
    assert "Branches" in text
    assert "* (current)" in text
    assert "main" in text
    assert "feature/login" in text
    assert "hotfix/div-zero" in text
    assert "experiment/cache" in text
    assert "origin/main" in text
    assert "+1" in text  # main is one commit ahead of its pushed state


def test_branches_empty_repo(tmp_path: Path) -> None:
    empty = _make_empty(tmp_path / "empty")
    text = capture(render_branches(empty))
    assert "No branches" in text


# --------------------------------------------------------------- render_file_tree

FILES = [
    "README.md",
    "src/app/__init__.py",
    "src/app/core.py",
    "src/app/auth.py",
    "src/app/search.py",
    "tests/test_core.py",
    "docs/deep/nested/inner/guide.md",
]


def test_tree_orders_dirs_before_files_alphabetically() -> None:
    tree = render_file_tree("demo", FILES)
    text = capture(tree)
    assert text.index("docs/") < text.index("src/") < text.index("tests/")
    assert text.index("src/") < text.index("README.md")  # dirs first at root level
    assert "app/" in text
    assert "core.py" in text and "auth.py" in text
    assert text.index("auth.py") < text.index("core.py")  # alphabetical files


def test_tree_highlight() -> None:
    tree = render_file_tree("demo", FILES, highlight=("src/app/core.py",))
    text = capture(tree)
    assert "✎ changed" in text
    line = next(l for l in text.splitlines() if "✎ changed" in l)
    assert "core.py" in line
    # untouched files have no marker
    assert text.count("✎ changed") == 1


def test_tree_collapses_deep_dirs() -> None:
    tree = render_file_tree("demo", FILES, max_depth=2)
    text = capture(tree)
    assert "… (" in text and "files)" in text
    assert "guide.md" not in text  # below the collapsed wall
    assert "docs/" in text and "deep/" in text  # dirs shown, children collapsed


def test_tree_sizes_and_empty() -> None:
    tree = render_file_tree("demo", ["src/app/core.py"], sizes={"src/app/core.py": 1500})
    text = capture(tree)
    assert "1.5 KB" in text

    empty_tree = render_file_tree("demo", [])
    assert "(no files)" in capture(empty_tree)


def test_tree_root_name_shown() -> None:
    text = capture(render_file_tree("my-repo", ["a.py"]))
    assert "my-repo/" in text
