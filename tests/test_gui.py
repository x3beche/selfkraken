"""Tests for the Qt desktop GUI (PySide6, offscreen platform).

No pytest-qt: one plain QApplication fixture, ``QT_QPA_PLATFORM=offscreen``
and a temp ``SK_DATA_DIR`` per test.  ``MainWindow(run_jobs_inline=True)``
makes every background job synchronous, so the full loop is testable without
event-loop timing.  The GUI *preflight* and *launch probe* tests (the checks
``skraken gui`` runs before Qt is imported — see ``selfkraken.gui.preflight``)
monkeypatch the environment instead and never touch Qt at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QTextDocument  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from selfkraken import workflow  # noqa: E402

from selfkraken.db import Store  # noqa: E402
from selfkraken.graph import layout, list_branches, read_commits  # noqa: E402

from selfkraken.gui import preflight as gui_preflight  # noqa: E402
from selfkraken.gui.diff_view import DiffHighlighter  # noqa: E402
from selfkraken.gui.models import GraphTableModel  # noqa: E402
from selfkraken.gui.theme import DARK, LIGHT, resolve  # noqa: E402

from fixtures_git import make_multibranch_repo  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
# qapp, gui_settings, no_dialogs and window live in tests/conftest.py so the
# Monaco tests can drive the same window.


# --------------------------------------------------------------------------- #
# construction + empty state
# --------------------------------------------------------------------------- #
def test_main_window_constructs_with_empty_db_and_shows_empty_state(window) -> None:
    assert window.repo_combo.count() == 0
    assert window.graph_stack.currentIndex() == 0  # guidance page
    assert window.proposals_stack.currentIndex() == 0
    texts = [
        label.text()
        for label in window.graph_guidance.findChildren(QLabel)
    ]
    assert any("No repositories yet" in text for text in texts)
    assert "Degraded mode" in window.model_status.text()  # no model configured
    assert window.apply_button.isEnabled() is False
    assert window.submit_button.isEnabled() is False  # empty request is invalid


def test_registering_a_repo_populates_the_combo(window, git_repo) -> None:
    store = Store(window.settings.db_path)
    try:
        repo = workflow.register_repo(store, git_repo, "python -m pytest -q")
    finally:
        store.close()
    window.refresh_repos()
    assert window.repo_combo.count() == 1
    assert window.repo_combo.itemData(0) == repo.id
    assert window.current_repo() is not None
    assert window.current_repo().id == repo.id


def test_request_validation_is_inline_and_textual(window) -> None:
    valid, message = window.validate_request("")
    assert not valid and "required" in message.lower()
    valid, message = window.validate_request("ab")
    assert not valid and "too short" in message
    valid, message = window.validate_request("x" * 4001)
    assert not valid and "too long" in message
    valid, message = window.validate_request("guard the division")
    assert valid and message == ""


# --------------------------------------------------------------------------- #
# layout: nothing may clip at the smallest supported window size
# --------------------------------------------------------------------------- #
def _visible_labels(widget):
    from PySide6.QtWidgets import QLabel

    for label in widget.findChildren(QLabel):
        if not label.isVisible() or not label.text().strip():
            continue
        yield label


def _assert_no_clipped_labels(window, note=""):
    """No QLabel may be shorter than the height it asks for."""
    clipped = []
    for label in _visible_labels(window):
        needed = label.sizeHint().height()
        if label.height() < needed:
            clipped.append(
                f"{label.text()[:38]!r} height={label.height()} needs={needed}"
            )
    assert not clipped, f"clipped labels ({note}):\n  " + "\n  ".join(clipped)


def test_main_window_at_1024x700_has_no_clipped_labels(
    qapp, gui_settings, no_dialogs, git_repo, make_patch, sources, test_command
) -> None:
    """Every state the review pane can hold, at the smallest window we support.

    The offscreen platform is exactly what the screenshots are taken with, so
    this is the same layout the pictures show: dock titles, section headers,
    the two-column commit form and the toolbar pill all have to fit their text.
    """
    from selfkraken.gui.app import MainWindow, apply_theme
    from selfkraken.gui.theme import DARK

    previous = qapp.styleSheet()
    apply_theme(qapp, DARK)
    window = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        window.resize(1024, 700)
        window.show()
        qapp.processEvents()
        _assert_no_clipped_labels(window, "empty state")

        window.add_repo(git_repo, test_command)
        window.index_repo()
        qapp.processEvents()
        _assert_no_clipped_labels(window, "repository loaded")

        window.graph_view.select_row(0)
        qapp.processEvents()
        _assert_no_clipped_labels(window, "commit details open")

        patch = make_patch(
            git_repo,
            {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]},
        )
        window.submit_proposal("Add a sub() function with a test", diff_text=patch)
        window.tabs.setCurrentIndex(1)
        qapp.processEvents()
        _assert_no_clipped_labels(window, "proposal detail open")

        window.diff_box.setChecked(True)
        window.tests_toggle.setExpanded(True)
        qapp.processEvents()
        _assert_no_clipped_labels(window, "manual diff + test output open")

        assert window.width() >= 1024 and window.height() >= 700
        assert window.minimumWidth() <= 1024, "the window must be able to reach 1024 wide"
    finally:
        window.close()
        qapp.setStyleSheet(previous)


def test_window_geometry_is_remembered_in_the_data_dir(
    qapp, gui_settings, no_dialogs
) -> None:
    """The window comes back the size you left it, stored beside the database."""
    import json

    from selfkraken.gui.app import MainWindow, WINDOW_STATE_FILENAME

    first = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    first.resize(1100, 720)
    first.show()
    qapp.processEvents()
    first.close()

    state_file = Path(gui_settings.data_dir) / WINDOW_STATE_FILENAME
    assert state_file.exists(), "geometry belongs in the app's own data dir"
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert (stored["w"], stored["h"]) == (1100, 720)

    second = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        assert (second.width(), second.height()) == (1100, 720)
    finally:
        second.close()


def test_review_pane_children_never_overflow_their_dock(
    qapp, gui_settings, no_dialogs, git_repo, make_patch, sources, test_command
) -> None:
    """Nothing in the review column may be wider than the dock that holds it.

    The apply options once sat in one row needing more width than the dock had,
    so "Keep on failure" painted as "Keep on failur" and the commit message box
    ran under the border.  A dock that scrolls sideways is not a fix — the
    content has to fit the dock it is given.
    """
    from PySide6.QtWidgets import QApplication, QWidget

    from selfkraken.gui.app import MainWindow, apply_theme
    from selfkraken.gui.theme import DARK

    previous = qapp.styleSheet()
    apply_theme(qapp, DARK)
    window = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        window.add_repo(git_repo, test_command)
        window.index_repo()
        window.submit_proposal(
            "Add a sub() function with a test",
            diff_text=make_patch(
                git_repo,
                {
                    "calc.py": sources["calc_with_sub"],
                    "test_calc.py": sources["test_calc_with_sub"],
                },
            ),
        )
        window.graph_view.select_row(0)
        window.tests_toggle.setExpanded(True)
        for width in (1024, 1280):
            window.resize(width, 700)
            window.show()
            QApplication.processEvents()
            scroll = window.right_scroll
            viewport_width = scroll.viewport().width()
            inner = scroll.widget()
            assert inner.width() <= viewport_width + 1, (
                f"{width}px: review content is {inner.width()}px wide in a "
                f"{viewport_width}px dock"
            )
            offenders = []
            for child in inner.findChildren(QWidget):
                if not child.isVisible() or child.height() <= 0:
                    continue
                right = child.mapTo(scroll, child.rect().topRight()).x()
                if right > viewport_width + 1:
                    offenders.append(
                        f"{child.metaObject().className}#{child.objectName()} "
                        f"right={right} > {viewport_width}"
                    )
            assert not offenders, (
                f"{width}px: review children past the dock edge:\n  "
                + "\n  ".join(offenders)
            )
    finally:
        window.close()
        qapp.setStyleSheet(previous)


def test_activity_log_dock_snaps_to_whole_rows(qapp, gui_settings, no_dialogs) -> None:
    """A long log never shows a half row: the viewport is a whole multiple of it.

    The dock's non-row pixels (title, frame, margins, the table header) are not a
    round number, so the height has to be measured and snapped, not estimated.
    """
    from PySide6.QtWidgets import QApplication

    from selfkraken.gui.app import MainWindow

    window = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        for index in range(12):  # more rows than the dock will ever show
            window.store.record_change("index", f"synthetic log line {index}")
        window.refresh_log()
        window.resize(1280, 800)
        window.show()
        QApplication.processEvents()
        window._fit_log_dock(force=True)
        QApplication.processEvents()

        table = window.log_table
        row_height = table.verticalHeader().defaultSectionSize()
        viewport_height = table.viewport().height()
        assert window.log_model.rowCount() >= 8
        assert viewport_height % row_height == 0, (
            f"viewport {viewport_height}px is not whole rows of {row_height}px "
            f"({viewport_height / row_height:.2f} rows — the last one is cut)"
        )
        last = table.rowAt(viewport_height - 1)
        assert last >= 0
        assert table.visualRect(table.model().index(last, 0)).bottom() <= viewport_height
    finally:
        window.close()


def test_graph_column_is_never_narrower_than_its_header(qapp, git_repo) -> None:
    """One lane used to elide the "Graph" header to "ra|"."""
    from selfkraken.gui.graph_widget import GraphView, graph_column_minimum
    from selfkraken.graph import layout, read_commits

    view = GraphView(DARK)
    try:
        view.set_rows(layout(read_commits(git_repo)), [])
        qapp.processEvents()
        assert view.table_model.max_lanes <= 1, "a linear repo keeps the lane count down"
        width = view.columnWidth(0)
        needed = QFontMetrics(view.horizontalHeader().font()).horizontalAdvance("Graph")
        assert width >= graph_column_minimum(), f"column {width} < the header's own budget"
        assert width > needed, f"{width}px cannot fit the {needed}px header"
    finally:
        view.close()


def test_stylesheet_is_built_from_the_design_tokens() -> None:
    """One border width, one radius, one font tier pair — no ad-hoc numbers."""
    from selfkraken.gui import theme
    from selfkraken.gui.theme import stylesheet

    css = stylesheet(DARK)
    assert f"font-size: {theme.META_PT:g}pt" in css
    # the body tier is carried by the application font, NOT by a global
    # `QWidget { font-size }`: that rule made Qt discard the smaller "meta"
    # font a table model sets per cell, so dates and authors silently grew back
    # to body size and truncated their columns
    assert "QWidget {" in css
    widget_block = css.split("QWidget {", 1)[1].split("}", 1)[0]
    assert "font-size" not in widget_block, widget_block
    assert "2px solid" not in css, "focus must not change the border width"
    assert css.count(f"{theme.BORDER_WIDTH}px solid") > 10
    assert f"border-radius: {theme.RADIUS}px" in css
    # the spacing scale is the only source of gaps
    for token in (theme.SPACE_2, theme.SPACE_4, theme.SPACE_6, theme.SPACE_8, theme.SPACE_12):
        assert f"{token}px" in css
    assert theme.SPACING_SCALE == (2, 4, 6, 8, 12)
    assert theme.ROW_HEIGHT == theme.HEADER_HEIGHT == theme.DOCK_TITLE_HEIGHT == 22
    assert theme.BUTTON_HEIGHT == theme.INPUT_HEIGHT == 24
    assert theme.STATUS_BAR_HEIGHT == 20


# --------------------------------------------------------------------------- #
# graph model vs graph.layout
# --------------------------------------------------------------------------- #
def test_graph_model_matches_graph_layout(tmp_path: Path) -> None:
    repo = make_multibranch_repo(tmp_path / "demo")
    commits = read_commits(repo, limit=300)
    rows = layout(commits)
    assert commits, "fixture repo must have commits"

    model = GraphTableModel(DARK)
    model.set_rows(rows, list_branches(repo))

    assert model.rowCount() == len(rows) == len(commits)
    merges = [index for index, row in enumerate(rows) if len(row.commit.parents) >= 2]
    assert merges, "the multibranch fixture must contain merge commits"
    for index, row in enumerate(rows):
        assert model.row_at(index) is row
        assert model.row_at(index).column == row.column
        assert model.is_merge(index) == (len(row.commit.parents) >= 2)
        # the layout invariant the painter relies on
        if index + 1 < len(rows):
            assert row.lanes_out == rows[index + 1].lanes_in
    # badges: HEAD, the tag and a remote ref all appear somewhere
    all_badges = [text for row in range(model.rowCount()) for text, _kind in model.badges_for(row)]
    assert "HEAD" in all_badges
    assert any(text.startswith("tag: v0.1.0") for text in all_badges)
    assert any(text.startswith("origin/") for text in all_badges)
    # the full subject lives in the tooltip (the cell itself may elide)
    subject_index = model.index(0, 2)
    tooltip = model.data(subject_index, Qt.ToolTipRole) or ""
    assert commits[0].subject in tooltip


def test_graph_delegate_paints_without_errors(qapp, tmp_path: Path) -> None:
    from selfkraken.gui.graph_widget import GraphView

    repo = make_multibranch_repo(tmp_path / "demo2")
    view = GraphView(DARK)
    rows = layout(read_commits(repo))
    view.set_rows(rows, list_branches(repo))
    view.resize(900, 600)
    view.show()
    qapp.processEvents()
    image = view.grab()
    assert image.width() > 0 and image.height() > 0
    view.close()


# --------------------------------------------------------------------------- #
# diff highlighter
# --------------------------------------------------------------------------- #
def test_diff_highlighter_formats_added_and_removed_lines_differently(qapp) -> None:
    document = QTextDocument()
    highlighter = DiffHighlighter(document, DARK)
    added = highlighter.format_for_line("+    return a - b")
    removed = highlighter.format_for_line("-    return a + b")
    hunk = highlighter.format_for_line("@@ -1,5 +1,6 @@")
    header = highlighter.format_for_line("+++ b/calc.py")
    context = highlighter.format_for_line(" def add(a, b):")

    assert added is not None and removed is not None
    assert added.foreground().color() != removed.foreground().color()
    assert added.background().color() != removed.background().color()
    assert hunk is not None and hunk.foreground().color() != added.foreground().color()
    assert header is not None and header.fontWeight() > added.fontWeight()
    assert context is None
    # only the file headers are bold: body, +/- and @@ stay regular weight mono
    from PySide6.QtGui import QFont

    regular = int(QFont.Weight.Normal)
    bold = int(QFont.Weight.Bold)
    assert header.fontWeight() == bold
    for fmt in (added, removed, hunk):
        assert fmt.fontWeight() == regular, "diff body text must not be bold"
    # the hunk header uses the theme's accent, not a third hue
    assert hunk.foreground().color().name() == DARK.accent


def test_file_tree_expands_and_marks_changed_files(qapp) -> None:
    from PySide6.QtWidgets import QTreeView

    from selfkraken.gui.models import FileTreeModel

    model = FileTreeModel(DARK)
    model.set_files(
        ["src/app/core.py", "src/app/auth.py", "tests/test_core.py", "README.md"],
        ["src/app/core.py"],
    )
    view = QTreeView()
    view.setModel(model)
    view.show()
    view.expandAll()
    qapp.processEvents()

    root_children = [
        model.index(row, 0).data() for row in range(model.rowCount())
    ]
    assert "src/" in root_children
    src = model.index(0, 0)
    assert view.isExpanded(src)
    app_node = model.index(0, 0, src)
    assert app_node.data() == "app/"
    assert model.parent(app_node).data() == "src/"
    files = [model.index(i, 0, app_node).data() for i in range(model.rowCount(app_node))]
    assert any(f.startswith("core.py") and FileTreeModel.CHANGED_MARK in f for f in files)
    assert "auth.py" in files
    view.close()


def test_read_commit_detail_roundtrip(tmp_path: Path) -> None:
    from selfkraken.gui.graph_widget import read_commit_detail

    repo = make_multibranch_repo(tmp_path / "detail")
    commits = read_commits(repo, limit=3)
    assert commits
    detail = read_commit_detail(repo, commits[0].sha)
    assert detail["sha"] == commits[0].sha
    assert detail["subject"] == commits[0].subject
    assert isinstance(detail["parents"], list)
    assert detail["author"]


def test_local_time_is_one_consistent_format() -> None:
    import re

    from selfkraken.gui.models import local_time

    assert local_time("") == ""
    for stamp in (
        "2023-11-14T13:13:20+00:00",  # UTC (selfkraken timestamps)
        "2023-11-14T16:13:20+03:00",  # other offset (git commits)
    ):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", local_time(stamp))
    # both describe the same instant -> identical local rendering
    assert local_time("2023-11-14T13:13:20+00:00") == local_time(
        "2023-11-14T16:13:20+03:00"
    )


def test_theme_resolution() -> None:
    assert resolve("dark").name == "dark"
    assert resolve("light").name == "light"
    assert resolve("auto", prefers_dark=False).name == "light"
    assert resolve("auto", prefers_dark=True).name == "dark"
    assert LIGHT.text != DARK.text


# --------------------------------------------------------------------------- #
# the full loop, driven through the window's controller methods (inline jobs)
# --------------------------------------------------------------------------- #
def test_full_loop_propose_apply_tests_commit(window, git_repo, make_patch, sources, test_command) -> None:
    # register (background job runs inline)
    window.add_repo(git_repo, test_command)
    assert window.repo_combo.count() == 1
    repo = window.current_repo()
    assert repo is not None and repo.test_command == test_command

    # index
    window.index_repo()
    assert window.store.count_files(repo.id) >= 2

    # propose from a pasted (manual) diff
    patch = make_patch(
        git_repo,
        {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]},
    )
    window.submit_proposal("Add a sub() function with a test", diff_text=patch)
    proposal_id = window._selected_proposal_id
    assert proposal_id is not None
    proposal = window.store.get_proposal(proposal_id)
    assert proposal.status == "proposed"
    assert proposal.model == "manual"
    assert window.proposals_model.rowCount() == 1
    assert window.diff_view.toPlainText().startswith("diff --git")
    # touched files are marked in the tree
    assert window.file_model.rowCount() > 0

    # apply with tests + commit (confirm dialog accepted by no_dialogs)
    window.confirm_handler = lambda title, text: True
    window.apply_proposal(proposal_id, run_tests=True, commit=True, keep_on_failure=False)

    final = window.store.get_proposal(proposal_id)
    assert final.status == "tests_passed"
    # selecting a proposal collapses the form so the diff owns the pane
    assert not window.new_proposal_toggle.isChecked()
    assert not window.proposal_form.isVisibleTo(window)
    kinds = window.log_model.kinds()
    for kind in ("register", "index", "propose", "apply", "test", "commit"):
        assert kind in kinds, f"activity log must contain {kind!r}: {kinds}"
    assert "OK" in window.status_label.text()

    # the commit really exists in git
    from selfkraken.gitops import head_sha

    assert head_sha(git_repo) is not None
    # graph reloaded through the controller
    assert window.graph_view.table_model.rowCount() >= 1

    # reject path on a fresh proposal
    patch2 = make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    window.submit_proposal("Break add on purpose", diff_text=patch2)
    second_id = window._selected_proposal_id
    window.reject_proposal(second_id)
    assert window.store.get_proposal(second_id).status == "rejected"
    assert "reject" in window.log_model.kinds()


def test_failed_tests_rollback_is_reported(window, git_repo, make_patch, sources, test_command) -> None:
    window.add_repo(git_repo, test_command)
    window.confirm_handler = lambda title, text: True
    patch = make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    window.submit_proposal("Break add() so the tests fail", diff_text=patch)
    proposal_id = window._selected_proposal_id
    window.apply_proposal(proposal_id, run_tests=True, commit=False, keep_on_failure=False)
    assert window.store.get_proposal(proposal_id).status == "tests_failed"
    assert "rollback" in window.log_model.kinds()
    assert "Error" in window.status_label.text()
    # the working tree is clean again
    from selfkraken.gitops import changed_paths

    assert changed_paths(git_repo) == []


# --------------------------------------------------------------------------- #
# GUI preflight: the checks that run before Qt is imported (pure stdlib)
# --------------------------------------------------------------------------- #
LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="xcb-cursor/wayland checks are Linux-only")


def _qt_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: Optional[str] = None,
    display: Optional[str] = ":1",
    wayland: Optional[str] = None,
) -> None:
    """Pin the three display variables; restores the host's values exactly.

    The first ``setenv`` registers the original for monkeypatch even when the
    variable does not exist, which also captures ``QT_QPA_PLATFORM`` when
    ``preflight()`` itself sets it (the Wayland branch).
    """
    for name, value in (
        ("QT_QPA_PLATFORM", platform),
        ("DISPLAY", display),
        ("WAYLAND_DISPLAY", wayland),
    ):
        monkeypatch.setenv(name, "")
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _cursor_library(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    """Pin the libxcb-cursor probe so the host's libraries never matter."""
    if present:
        monkeypatch.setattr(
            gui_preflight.ctypes.util, "find_library", lambda name: gui_preflight.XCB_CURSOR_LIBRARY
        )
        monkeypatch.setattr(gui_preflight.ctypes, "CDLL", lambda *a, **k: object())
    else:
        monkeypatch.setattr(gui_preflight.ctypes.util, "find_library", lambda name: None)

        def _missing(*_a: object, **_k: object) -> None:
            raise OSError(f"{gui_preflight.XCB_CURSOR_LIBRARY}: cannot open shared object file")

        monkeypatch.setattr(gui_preflight.ctypes, "CDLL", _missing)


@linux_only
@pytest.mark.parametrize("platform", [None, "xcb", "xcb:caca"])
def test_preflight_missing_xcb_cursor_reports_install_hint(monkeypatch, platform) -> None:
    _qt_env(monkeypatch, platform=platform)
    _cursor_library(monkeypatch, present=False)
    problems = gui_preflight.preflight()
    assert len(problems) == 1
    hint = problems[0]
    assert "Qt needs the system library libxcb-cursor0" in hint
    assert "sudo apt install libxcb-cursor0 (Debian/Ubuntu)" in hint
    assert "sudo dnf install xcb-util-cursor (Fedora)" in hint
    assert "sudo pacman -S xcb-util-cursor (Arch)" in hint


@linux_only
def test_preflight_without_display_reports_no_graphical_display(monkeypatch) -> None:
    _qt_env(monkeypatch, display=None)
    _cursor_library(monkeypatch, present=True)
    problems = gui_preflight.preflight()
    assert len(problems) == 1
    assert "no graphical display" in problems[0]
    assert "DISPLAY" in problems[0]
    assert "desktop session" in problems[0]


@linux_only
def test_preflight_reports_both_problems(monkeypatch) -> None:
    _qt_env(monkeypatch, display=None)
    _cursor_library(monkeypatch, present=False)
    problems = gui_preflight.preflight()
    assert len(problems) == 2
    assert any("no graphical display" in p for p in problems)
    assert any("libxcb-cursor0" in p for p in problems)


@linux_only
def test_preflight_wayland_session_prefers_wayland(monkeypatch) -> None:
    _qt_env(monkeypatch, display=None, wayland="wayland-0")
    _cursor_library(monkeypatch, present=False)  # must not even be consulted
    assert gui_preflight.preflight() == []
    # set before QApplication would exist — Qt honours it at startup
    assert os.environ["QT_QPA_PLATFORM"] == "wayland;xcb"


@linux_only
def test_preflight_explicit_platform_is_never_overridden(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="wayland", display=None, wayland="wayland-0")
    _cursor_library(monkeypatch, present=False)
    assert gui_preflight.preflight() == []
    assert os.environ["QT_QPA_PLATFORM"] == "wayland"


@linux_only
def test_preflight_offscreen_platform_needs_nothing(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="offscreen", display=None)
    _cursor_library(monkeypatch, present=False)  # missing lib + no display: irrelevant
    assert gui_preflight.preflight() == []


@linux_only
def test_preflight_ok_with_xcb_and_loadable_library(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="xcb")
    _cursor_library(monkeypatch, present=True)
    assert gui_preflight.preflight() == []


def test_preflight_reports_nothing_off_linux(monkeypatch) -> None:
    _cursor_library(monkeypatch, present=False)
    monkeypatch.setattr(gui_preflight.sys, "platform", "win32")
    assert gui_preflight.preflight() == []
    monkeypatch.setattr(gui_preflight.sys, "platform", "darwin")
    assert gui_preflight.preflight() == []


# --------------------------------------------------------------------------- #
# launch probe: a subprocess takes the core dump, the CLI prints the fix
# --------------------------------------------------------------------------- #
def test_launch_probe_skipped_when_offscreen(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="offscreen")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the probe must not spawn Qt when offscreen")

    monkeypatch.setattr(gui_preflight.subprocess, "run", _boom)
    assert gui_preflight.launch_probe() is None


def test_launch_probe_success_returns_none(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="xcb")

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(gui_preflight.subprocess, "run", lambda *a, **k: _Ok())
    assert gui_preflight.launch_probe() is None


def test_launch_probe_abort_headlines_and_collects_qt_qpa_lines(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="xcb")

    class _Aborted:
        returncode = -6  # SIGABRT: Qt's "Aborted (core dumped)"
        stdout = ""
        stderr = "\n".join(
            [f"qt.qpa.plugin: synthetic complaint {i}" for i in range(7)]
            + ["some other error noise"]
        )

    monkeypatch.setattr(gui_preflight.subprocess, "run", lambda *a, **k: _Aborted())
    lines = gui_preflight.launch_probe()
    assert lines is not None
    assert lines[0].startswith("Qt could not start a window on this display")
    assert "SIGABRT" in lines[0]
    qpa = [line for line in lines if line.startswith("qt.qpa")]
    assert len(qpa) == gui_preflight.PROBE_QT_LINES_MAX == 5  # capped, noise dropped
    assert "some other error noise" not in "\n".join(lines)
    assert gui_preflight.XCB_CURSOR_HINT in lines


def test_launch_probe_timeout_is_reported(monkeypatch) -> None:
    _qt_env(monkeypatch, platform="xcb")

    def _hang(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["python", "-c", "..."], timeout=1)

    monkeypatch.setattr(gui_preflight.subprocess, "run", _hang)
    lines = gui_preflight.launch_probe(timeout_seconds=1)
    assert lines is not None
    assert lines[0].startswith("Qt could not start a window on this display")
    assert "launch probe" in lines[0]


# --------------------------------------------------------------------------- #
# `skraken gui` — checks first, Qt only when the environment is startable
# --------------------------------------------------------------------------- #
def test_cli_gui_exits_1_with_hint_when_preflight_reports_problems(
    runner, cli_env, monkeypatch
) -> None:
    from selfkraken.cli import app

    monkeypatch.setattr(gui_preflight, "preflight", lambda: [gui_preflight.XCB_CURSOR_HINT])
    # Simulate the bug machine: importing Qt widgets aborts the process here,
    # so reaching that import at all would be a core dump, not a test failure.
    monkeypatch.setitem(sys.modules, "selfkraken.gui.app", None)
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "libxcb-cursor0" in result.output
    assert "sudo apt install libxcb-cursor0" in result.output
    assert "PySide6" not in result.output  # never reached the Qt import path


def test_cli_gui_exits_1_when_launch_probe_fails(runner, cli_env, monkeypatch) -> None:
    from selfkraken.cli import app

    monkeypatch.setattr(gui_preflight, "preflight", lambda: [])
    monkeypatch.setattr(
        gui_preflight,
        "launch_probe",
        lambda timeout_seconds=15: [
            "Qt could not start a window on this display: the Qt launch probe "
            "was killed by SIGABRT — Qt aborts (core dumped) when its platform "
            "plugin cannot load",
            'qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" '
            "even though it was found.",
            gui_preflight.XCB_CURSOR_HINT,
        ],
    )
    monkeypatch.setitem(sys.modules, "selfkraken.gui.app", None)
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Qt could not start a window on this display" in result.output
    assert "qt.qpa.plugin" in result.output
    assert "libxcb-cursor0" in result.output


def test_cli_gui_skip_checks_bypasses_the_checks(runner, cli_env, monkeypatch) -> None:
    from selfkraken.cli import app
    import selfkraken.gui.app as gui_app

    calls: list[str] = []

    def _preflight() -> list[str]:
        calls.append("preflight")
        return ["problem that --skip-checks must bypass"]

    def _probe(timeout_seconds: int = 15) -> Optional[list[str]]:
        calls.append("probe")
        return ["problem that --skip-checks must bypass"]

    monkeypatch.setattr(gui_preflight, "preflight", _preflight)
    monkeypatch.setattr(gui_preflight, "launch_probe", _probe)
    monkeypatch.setattr(gui_app, "run", lambda theme="auto", argv=None: 0)

    result = runner.invoke(app, ["gui", "--skip-checks"])
    assert result.exit_code == 0
    assert calls == []  # neither check ran
    assert "must be bypassed" not in result.output


def test_cli_gui_runs_checks_in_order_before_the_window(
    runner, cli_env, monkeypatch
) -> None:
    from selfkraken.cli import app
    import selfkraken.gui.app as gui_app

    calls: list[str] = []

    def _preflight() -> list[str]:
        calls.append("preflight")
        return []

    def _probe(timeout_seconds: int = 15) -> None:
        calls.append("probe")
        return None

    def _run(theme: str = "auto", argv=None) -> int:
        calls.append("run")
        return 0

    monkeypatch.setattr(gui_preflight, "preflight", _preflight)
    monkeypatch.setattr(gui_preflight, "launch_probe", _probe)
    monkeypatch.setattr(gui_app, "run", _run)

    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 0
    assert calls == ["preflight", "probe", "run"]  # Qt started only after both checks


def test_cli_gui_without_pyside6_prints_friendly_error(
    runner, cli_env, monkeypatch
) -> None:
    from selfkraken.cli import app

    _qt_env(monkeypatch, platform="offscreen")  # checks pass on any host
    # Simulate a core-only install: importing the GUI module raises ImportError.
    monkeypatch.setitem(sys.modules, "selfkraken.gui.app", None)
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "the desktop GUI needs PySide6" in result.output
    assert "pip install 'selfkraken[gui]'" in result.output


def test_core_imports_do_not_pull_pyside6() -> None:
    """A subprocess (fresh interpreter) proves laziness of the Qt imports."""
    code = (
        "import sys; import selfkraken.cli, selfkraken.workflow;"
        "import selfkraken.gui;"
        "assert 'PySide6' not in sys.modules, 'core import pulled PySide6'"
    )
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_settings_appearance_switches_theme_and_remembers_it(
    qapp, gui_settings, no_dialogs
) -> None:
    """Settings > Appearance repaints the window now and survives a restart."""
    import json

    from selfkraken.gui.app import MainWindow, WINDOW_STATE_FILENAME

    window = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        assert window.palette.name == "dark"
        window.set_theme("light")
        assert window.palette.name == "light"
        assert window.theme_cards.checkedButton().text() == "Light"

        stored = json.loads(
            (Path(gui_settings.data_dir) / WINDOW_STATE_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert stored["theme"] == "light"
    finally:
        window.close()

    reopened = MainWindow(run_jobs_inline=True, settings=gui_settings)
    try:
        assert reopened.palette.name == "light", "the choice did not survive a start"
    finally:
        reopened.close()
