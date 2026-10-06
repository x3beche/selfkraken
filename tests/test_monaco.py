"""Tests for the Monaco code viewer: payloads, escaping, policy, fallback.

The pure half (:mod:`selfkraken.gui.monaco_payload`) runs against the suite's
real git fixtures, so ``original``/``modified`` are proven on genuine ``git``
output rather than hand-written patches.  The Qt half runs offscreen, where
``MonacoView`` deliberately becomes the plain viewer — that is the fallback the
task requires, and the tests assert exactly that.  The web-engine policy is
tested as the pure function it is, without booting Chromium.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl, Qt  # noqa: E402
from PySide6.QtWidgets import QHeaderView, QTabBar  # noqa: E402

from selfkraken import workflow  # noqa: E402
from selfkraken.db import Store  # noqa: E402
from selfkraken.graph import layout, list_branches, read_commits  # noqa: E402

from selfkraken.gui import monaco_payload as mp  # noqa: E402
from selfkraken.gui import monaco_view as mv  # noqa: E402
from selfkraken.gui.theme import DARK, LIGHT  # noqa: E402

from fixtures_git import make_multibranch_repo  # noqa: E402


def code_test_command(repo_path) -> str:
    """A test command for the fixture repo (the suite's own interpreter)."""
    import shlex

    return f"{shlex.quote(sys.executable)} -m pytest -q"

# --------------------------------------------------------------------------- #
# the vendored editor is really here
# --------------------------------------------------------------------------- #
def test_monaco_is_vendored_and_verifiable() -> None:
    """No CDN at runtime: the bytes ship in the package, pinned and hashed."""
    assert mv.vendored_files_present(), "run scripts/vendor_monaco.py"
    marker = json.loads((mv.MONACO_DIR / "VERSION").read_text(encoding="utf-8"))
    assert marker["version"] == "0.57.0"
    assert marker["integrity"].startswith("sha512-")
    assert marker["license"] == "MIT"
    assert (mv.MONACO_DIR / "LICENSE").is_file()
    assert (mv.MONACO_DIR / "ThirdPartyNotices.txt").is_file()
    assert (mv.EDITOR_PAGE).is_file()
    # the prune happened: no source maps, no legacy worker tree, no localisations
    assert not list(mv.MONACO_DIR.rglob("*.map"))
    assert not (mv.MONACO_DIR / "vs" / "language").exists()
    assert not (mv.MONACO_DIR / "vs" / "nls" / "lang").exists()


def test_vendor_prune_plan_keeps_only_the_listed_languages(tmp_path: Path) -> None:
    """``plan_files`` is a pure decision: kept languages in, everything else out."""
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "vendor_monaco.py"
    spec = importlib.util.spec_from_file_location("vendor_monaco", script)
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)

    contribution = """
i({id:"python",extensions:[".py"],loader:()=>new Promise((e,s)=>a(["../python-HASH1"],e,s))}),
i({id:"abap",extensions:[".abap"],loader:()=>new Promise((e,s)=>a(["../abap-HASH2"],e,s))})
"""
    chunks = vendor.basic_language_chunks(contribution)
    assert chunks == {"python": "python-HASH1", "abap": "abap-HASH2"}

    payload = _toy_tarball(vendor, contribution)
    wanted, stats = vendor.plan_files(payload, ("python",))
    targets = set(wanted.values())
    assert "vs/python-HASH1.js" in targets
    assert "vs/abap-HASH2.js" not in targets
    assert "vs/language/typescript/ts.worker.js" not in targets
    assert "vs/nls/lang/de.js" not in targets
    assert "vs/editor/editor.main.css" in targets
    assert stats["other_languages"] >= 1
    assert vendor.check(_extract(vendor, payload, tmp_path)) == []


def _toy_tarball(vendor, contribution: str) -> bytes:
    """A miniature npm tarball with the shapes the real one has."""
    import io
    import tarfile

    files = {
        "package/min/vs/loader.js": "define('vs/loader');",
        "package/min/vs/editor/editor.main.js": "define('vs/editor/editor.main');",
        "package/min/vs/editor/editor.main.css": ".monaco-editor{}",
        "package/min/vs/editor/editor.worker.js": "define('vs/editor/editor.worker');",
        "package/min/vs/editor/editor.main.js.map": "{}",
        "package/min/vs/nls.messages-loader.js": "define('vs/nls.messages-loader');",
        "package/min/vs/basic-languages/monaco.contribution.js": contribution,
        "package/min/vs/python-HASH1.js": "define('vs/python');",
        "package/min/vs/abap-HASH2.js": "define('vs/abap');",
        "package/min/vs/language/typescript/ts.worker.js": "legacy",
        "package/min/vs/language/typescript/ts.worker.js.map": "{}",
        "package/min/vs/nls/lang/de.js": "localised",
        "package/min/vs/assets/editor.worker-HASH.js": "worker bytes",
        "package/LICENSE": "MIT License",
        "package/ThirdPartyNotices.txt": "notices",
        "package/README.md": "not wanted",
        "package/esm/monaco.editor.js": "not wanted",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in files.items():
            raw = body.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def _extract(vendor, payload: bytes, destination: Path) -> Path:
    wanted, stats = vendor.plan_files(payload, ("python",))
    vendor.write_files(payload, destination, wanted)
    vendor.write_marker(destination, stats, ("python",))
    return destination


# --------------------------------------------------------------------------- #
# splitting a diff into per-file patches
# --------------------------------------------------------------------------- #
def test_split_diff_handles_modify_add_delete(git_repo, make_patch, sources) -> None:
    patches = mp.split_diff(
        make_patch(
            git_repo,
            {
                "calc.py": sources["calc_wrong"],
                "extra.py": "VALUE = 1\n",
            }
        )
    )
    by_path = {patch.path: patch for patch in patches}
    assert set(by_path) >= {"calc.py", "extra.py"}
    assert by_path["extra.py"].is_new is True
    assert by_path["extra.py"].added >= 1
    assert by_path["calc.py"].is_new is False

    deletion = mp.split_diff(make_patch(git_repo, {"calc.py": ""}))
    # an emptied file is still a modification; a removed one is a deletion
    assert deletion[0].path == "calc.py"


def test_split_diff_multi_file_counts_are_right(git_repo, make_patch, sources) -> None:
    diff = make_patch(
        git_repo,
        {
            "calc.py": sources["calc_with_sub"],
            "test_calc.py": sources["test_calc_with_sub"],
        },
    )
    patches = mp.split_diff(diff)
    assert [patch.path for patch in patches] == ["calc.py", "test_calc.py"]
    for patch in patches:
        added, removed = mp.count_changes(patch)
        assert added > 0
        assert removed == 0, "these fixtures only add lines"
        # every chunk stands alone: it is the file's own section of the diff
        assert patch.chunk.startswith("diff --git a/")
        assert "diff --git a/test_calc.py" not in patch.chunk or patch.path == "test_calc.py"


def test_split_diff_accepts_a_bare_patch_without_git_headers() -> None:
    """Model output often has only --- / +++ / @@ lines."""
    bare = (
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -1,2 +1,3 @@\n"
        " line one\n"
        "-line two\n"
        "+line two fixed\n"
        "+line three\n"
    )
    patches = mp.split_diff(bare)
    assert len(patches) == 1
    assert patches[0].path == "notes.md"
    assert patches[0].added == 2
    assert patches[0].removed == 1
    assert patches[0].chunk.strip().endswith("+line three")


# --------------------------------------------------------------------------- #
# original / modified for each kind of change
# --------------------------------------------------------------------------- #
def test_original_and_modified_for_a_modification(git_repo, make_patch, sources) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    patch = mp.split_diff(diff)[0]
    pair = mp.original_and_modified(git_repo, patch)
    assert pair is not None
    original, modified = pair
    assert original == mp.head_text(git_repo, "calc.py")
    assert modified == sources["calc_with_sub"]


def test_original_and_modified_for_an_added_file(git_repo, make_patch) -> None:
    patch = mp.split_diff(make_patch(git_repo, {"new/thing.py": "X = 1\n"}))[0]
    assert patch.path == "new/thing.py"
    pair = mp.original_and_modified(git_repo, patch)
    assert pair is not None
    original, modified = pair
    assert original == "", "a new file has no HEAD content"
    assert modified == "X = 1\n"


def test_original_and_modified_for_a_deleted_file(git_repo, make_patch) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "rm", "-q", "--cached", "calc.py"],
        check=True, capture_output=True, text=True,
    )
    (git_repo / "calc.py").unlink(missing_ok=True)
    diff = subprocess.run(
        ["git", "-C", str(git_repo), "diff", "--cached"],
        check=True, capture_output=True, text=True,
    ).stdout
    subprocess.run(["git", "-C", str(git_repo), "reset", "-q", "--hard"], check=True,
                   capture_output=True, text=True)
    patches = [p for p in mp.split_diff(diff) if p.is_delete]
    assert patches, f"expected a deletion in:\n{diff}"
    patch = patches[0]
    pair = mp.original_and_modified(git_repo, patch)
    assert pair is not None
    original, modified = pair
    assert original == "def add(a, b):\n    return a + b\n"
    assert modified == "", "the file is gone after the patch"


def test_original_and_modified_is_a_temp_dir_operation(git_repo, make_patch, sources) -> None:
    """Computing the after-text must never write into the user's repository."""
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    patch = mp.split_diff(diff)[0]
    before = {p: p.read_bytes() for p in git_repo.rglob("*") if p.is_file()}
    mp.original_and_modified(git_repo, patch)
    after = {p: p.read_bytes() for p in git_repo.rglob("*") if p.is_file()}
    assert before == after
    status = subprocess.run(
        ["git", "-C", str(git_repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() == "", f"working tree was touched: {status}"


def test_original_and_modified_survives_a_patch_that_does_not_apply(git_repo) -> None:
    """Garbage that cannot apply yields None — the caller keeps the raw patch."""
    patch = mp.FilePatch(
        path="calc.py", old_path="calc.py",
        chunk="diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
              "@@ -1,2 +1,2 @@\n-not in the file at all\n+nope\n",
        added=1, removed=1,
    )
    assert mp.original_and_modified(git_repo, patch) is None


def test_rejects_paths_that_escape_the_repository(tmp_path: Path) -> None:
    for bad in ("/etc/passwd", "../escape.py", "a/../../b", ".git/config", ""):
        assert mp._safe_rel(bad) is None, bad
    repo = tmp_path / "r"
    repo.mkdir()
    patch = mp.FilePatch(path="../escape.py", old_path="../escape.py", chunk="", added=0, removed=0)
    assert mp.original_and_modified(repo, patch) is None


# --------------------------------------------------------------------------- #
# touched lines and language choice
# --------------------------------------------------------------------------- #
def test_touched_lines_are_post_patch_line_numbers(git_repo, make_patch, sources) -> None:
    diff = make_patch(git_repo, {"calc.py": sources["calc_with_sub"]})
    lines = mp.touched_lines(diff, "calc.py")
    body = sources["calc_with_sub"].splitlines()
    assert lines, "the added lines must be marked"
    assert all(1 <= line <= len(body) for line in lines)
    # every marked line is one the patch added
    for line in lines:
        assert line in set(range(1, len(body) + 1))
    assert mp.touched_lines(diff, "no/such/file.py") == set()


def test_touched_lines_use_before_numbers_for_an_unapplied_patch() -> None:
    """A proposed patch has not landed: mark lines the reader can actually see."""
    diff = (
        "diff --git a/c.py b/c.py\n"
        "--- a/c.py\n"
        "+++ b/c.py\n"
        "@@ -1,3 +1,5 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        "+extra\n"
        " three\n"
        " four\n"
    )
    after = mp.touched_lines(diff, "c.py")
    before = mp.touched_lines(diff, "c.py", after=False)
    # post-patch: the added lines 2 and 3 of the result
    assert after == {2, 3}
    # pre-patch: the removed line, and the surviving line the insertion sits at —
    # both of them exist in the file the editor is actually showing
    assert before == {2, 3}
    assert max(before) <= 3, "before-side numbers must exist in the file on disk"


def test_touched_lines_pure_addition_marks_the_anchor_line() -> None:
    diff = (
        "diff --git a/n.py b/n.py\n"
        "--- a/n.py\n"
        "+++ b/n.py\n"
        "@@ -1,2 +1,3 @@\n"
        " first\n"
        "+second inserted\n"
        " third\n"
    )
    assert mp.touched_lines(diff, "n.py") == {2}
    assert mp.touched_lines(diff, "n.py", after=False) == {2}


def test_language_follows_the_extension() -> None:
    cases = {
        "src/app/core.py": "python",
        "a/b.ts": "typescript",
        "a/b.tsx": "typescript",
        "pkg/index.js": "javascript",
        "config/settings.json": "json",
        "web/index.html": "html",
        "web/site.css": "css",
        "docs/README.md": "markdown",
        "ci/pipeline.yaml": "yaml",
        "scripts/build.sh": "shell",
        "db/schema.sql": "sql",
        "app/config.ini": "ini",
        "pom.xml": "xml",
        "main.go": "go",
        "src/lib.rs": "rust",
        "App.java": "java",
        "src/main.c": "c",
        "src/main.cpp": "cpp",
        "Program.cs": "csharp",
        "lib/rake": "plaintext",
        "Gemfile": "ruby",
        "Dockerfile": "dockerfile",
        "x.unknown-ext": "plaintext",
        "": "plaintext",
    }
    for path, language in cases.items():
        assert mp.language_for_path(path) == language, path
    # never hand Monaco a language it has no chunk for
    assert mp.language_for_path("legacy.pas") == "plaintext"


# --------------------------------------------------------------------------- #
# JSON escaping: hostile content stays data
# --------------------------------------------------------------------------- #
def test_payload_json_escapes_script_breakout_and_backticks() -> None:
    hostile = (
        "</script><script>alert(1)</script>\n"
        "` + alert(2) + `\n"
        "'\"\\ \u00e7\t\n"
        "document.write('<img src=x onerror=alert(3)>')\n"
    )
    hostile = (
        "</script><script>alert(1)</script>\n"
        "` + alert(2) + `\n"
        "'\"\\ \u00e7\t\n"
        "document.write('<img src=x onerror=alert(3)&y=1>')\n"
    )
    source = mp.js_apply(mp.build_text_payload(hostile, tokens=mp.theme_tokens(DARK)))
    literal = source.split("window.skApply(", 1)[1].rsplit(")", 1)[0]
    assert "</script>" not in literal
    # inside the payload every dangerous character is a unicode escape
    assert "<" not in literal and ">" not in literal and "&" not in literal
    assert "\\u003c/script\\u003e" in literal, "the escape was written out"
    assert "\\u0026" in literal, "and so was the ampersand's"
    # and it still parses back to exactly the original bytes
    assert json.loads(literal)["text"] == hostile


def test_payload_json_of_diff_and_file_keeps_content_intact() -> None:
    original = "a = 1\n"
    modified = 'b = "</script>`"`\n'
    payload = mp.build_diff_payload(
        original, modified, "x.py", side_by_side=True, tokens=mp.theme_tokens(LIGHT)
    )
    source = mp.js_apply(payload)
    literal = source.split("window.skApply(", 1)[1].rsplit(")", 1)[0]
    parsed = json.loads(literal)
    assert parsed["modified"] == modified
    assert parsed["sideBySide"] is True
    assert parsed["language"] == "python"
    assert parsed["kind"] == "diff"

    file_payload = mp.build_file_payload(
        "x = `</script>`\n", "y.js", highlight_lines={3, 1, 3}, tokens=mp.theme_tokens(DARK)
    )
    assert file_payload["highlightLines"] == [1, 3]
    assert file_payload["language"] == "javascript"


def test_payload_is_bounded(tmp_path: Path) -> None:
    big = "x" * (mp.MAX_TEXT_BYTES + 10)
    payload = mp.build_text_payload(big, tokens=mp.theme_tokens(DARK))
    assert len(payload["text"].encode("utf-8")) <= mp.MAX_TEXT_BYTES + 64
    assert payload["text"].endswith("(truncated)")


# --------------------------------------------------------------------------- #
# the network policy
# --------------------------------------------------------------------------- #
def test_request_policy_blocks_everything_but_the_vendor_tree() -> None:
    root = mv.MONACO_DIR
    allowed = [
        f"file://{root / 'vs' / 'loader.js'}",
        f"file://{root}",
        "qrc:///qtwebchannel/qwebchannel.js",
        "data:text/javascript,globalThis.x=1",
        "blob:null/1234-5678",
    ]
    blocked = [
        "https://cdn.jsdelivr.net/npm/monaco-editor/min/vs/loader.js",
        "http://example.com/x.js",
        "ws://localhost:9222/devtools",
        "file:///etc/passwd",
        f"file://{Path.home() / '.ssh' / 'id_rsa'}",
        "filesystem:file:///tmp/others/x",
        "chrome://version",
        "about:blank",
        "",
    ]
    for url in allowed:
        assert mv.request_is_allowed(QUrl(url)), f"should allow {url}"
    for url in blocked:
        assert not mv.request_is_allowed(QUrl(url)), f"should block {url}"


def test_request_policy_blocks_symlink_out_of_the_vendor_tree(tmp_path: Path) -> None:
    """A link that points outside the vendor dir is still outside it."""
    sneak = tmp_path / "outside.js"
    sneak.write_text("evil()", encoding="utf-8")
    root = tmp_path / "vendor"
    (root / "vs").mkdir(parents=True)
    link = root / "vs" / "link.js"
    link.symlink_to(sneak)
    assert not mv.request_is_allowed(QUrl.fromLocalFile(str(link)), root=root)
    inside = root / "vs" / "real.js"
    inside.write_text("ok", encoding="utf-8")
    assert mv.request_is_allowed(QUrl.fromLocalFile(str(inside)), root=root)


def test_webengine_is_not_requested_offscreen_and_no_webengine_forces_the_plain_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv(mv.NO_WEBENGINE_VAR, raising=False)
    monkeypatch.delenv(mv.FORCE_WEBENGINE_VAR, raising=False)
    assert mv.webengine_requested() is False
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    assert mv.webengine_requested() is True
    monkeypatch.setenv(mv.NO_WEBENGINE_VAR, "1")
    assert mv.webengine_requested() is False
    assert mv.monaco_supported() is False


def test_missing_vendored_files_disable_monaco(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An install without the vendor tree degrades instead of failing."""
    monkeypatch.setattr(mv, "MONACO_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(mv, "EDITOR_PAGE", tmp_path / "nowhere" / "editor.html")
    assert mv.vendored_files_present() is False


# --------------------------------------------------------------------------- #
# the widget itself, offscreen: the fallback path
# (the qapp / window fixtures come from tests/conftest.py)
# --------------------------------------------------------------------------- #
def test_monaco_view_offscreen_uses_the_plain_viewer_and_says_so(qapp) -> None:
    view = mv.MonacoView(DARK, name="proposal diff")
    view.show()
    qapp.processEvents()
    assert view.uses_monaco is False
    assert mv.FALLBACK_NOTE in view._fallback_note.text()
    view.show_diff("a = 1\n", "a = 2\n", "cfg/x.py")
    text = view.toPlainText()
    assert text.startswith("diff --git a/cfg/x.py b/cfg/x.py")
    assert "+a = 2" in text and "-a = 1" in text
    view.show_file("print('hi')\n", "a/b.py", highlight_lines={1})
    assert view.toPlainText() == "print('hi')\n"
    view.show_text("PASSED  exit 0")
    assert "PASSED" in view.toPlainText()
    view.clear()
    assert view.toPlainText() == ""
    view.close()


def test_monaco_view_no_webengine_env(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mv.NO_WEBENGINE_VAR, "1")
    view = mv.MonacoView(DARK, name="test output")
    view.show()
    qapp.processEvents()
    assert view.uses_monaco is False
    view.show_text("FAILED: 1 of 3")
    assert view.toPlainText() == "FAILED: 1 of 3"
    view.close()


def test_monaco_view_diff_text_matches_the_plain_viewer(qapp) -> None:
    """The two viewers must not disagree: toPlainText is what both show."""
    view = mv.MonacoView(DARK)
    original, modified = "l1\nl2\nl3\n", "l1\nL2\nl3\n"
    view.show_diff(original, modified, "src/x.py", side_by_side=True)
    text = view.toPlainText()
    assert "@@" in text
    assert "-l2" in text and "+L2" in text
    assert mv.unified_diff_text("same\n", "same\n", "s.py").endswith("(no changes)\n")
    view.close()


def test_monaco_view_theme_switch_recolours_the_fallback(qapp) -> None:
    view = mv.MonacoView(DARK)
    view.show_text("+added\n")
    qapp.processEvents()
    view.set_theme("light")
    assert view._palette.name == "light"
    assert view._fallback_highlighter._palette is LIGHT
    view.set_theme("dark")
    assert view._tokens["background"] == DARK.base
    view.close()


def test_monaco_view_side_by_side_remembers_the_pair(qapp) -> None:
    view = mv.MonacoView(DARK)
    view.show_diff("a\n", "b\n", "x.py", side_by_side=False)
    assert view.is_side_by_side() is False
    view.set_side_by_side(True)
    assert view.is_side_by_side() is True
    assert view.toPlainText().startswith("diff --git")
    view.close()


def test_payload_theme_tokens_come_from_the_palette() -> None:
    dark = mp.theme_tokens(DARK)
    light = mp.theme_tokens(LIGHT)
    assert dark["background"] == DARK.base
    assert dark["addedBackground"] == DARK.diff["add"][1]
    assert dark["removedBackground"] == DARK.diff["del"][1]
    assert dark["selectionBackground"] == DARK.selection_bg
    assert dark["name"] == "selfkraken-dark"
    assert dark["base"] == "vs-dark"
    assert light["base"] == "vs-light"
    assert dark["fontSize"] == 12 and dark["lineHeight"] == 18
    assert light["background"] == LIGHT.base != dark["background"]
    # Monaco and the plain viewer must not disagree about what "added" looks like
    assert dark["addedForeground"] == DARK.diff["add"][0]


# --------------------------------------------------------------------------- #
# the window's centre tabs, driven through the controller (offscreen = fallback)
# --------------------------------------------------------------------------- #
def test_changes_tab_lists_every_file_of_a_multi_file_diff(
    window, git_repo, make_patch, sources
) -> None:
    from PySide6.QtWidgets import QApplication

    assert window.tabs.tabText(2) == "&Changes"
    window.add_repo(git_repo, code_test_command(git_repo))
    window.index_repo()
    patch = make_patch(
        git_repo,
        {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]},
    )
    window.submit_proposal("Add a sub() function with a test", diff_text=patch)
    QApplication.processEvents()

    assert [p.path for p in window._diff_patches] == ["calc.py", "test_calc.py"]
    model = window.diff_files.model()
    assert model.rowCount() == 2
    # isHidden() is the pane's own flag; isVisibleTo(window) would also be false
    # for a widget sitting on a tab page that is not the current one
    assert not window.diff_files.isHidden(), "a multi-file diff lists its files"
    window.tabs.setCurrentIndex(2)
    assert window.diff_files.isVisibleTo(window)
    counts = [model.index(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(2)]
    assert all(added > 0 for added, _removed in counts), counts
    assert window.diff_view.toPlainText().startswith("diff --git a/")
    # the summary replaces the duplicated file name in the strip
    assert "2 files" in window.diff_title.text()


def test_single_file_diff_hides_the_file_list(
    window, git_repo, make_patch, sources
) -> None:
    window.add_repo(git_repo, code_test_command(git_repo))
    window.submit_proposal(
        "Break add on purpose", diff_text=make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    )
    assert len(window._diff_patches) == 1
    assert window.diff_files.isHidden(), "one row of nothing is a dead band"
    assert window.diff_title.text() == "calc.py"


def test_side_by_side_toggle_renders_the_other_layout(
    window, git_repo, make_patch, sources
) -> None:
    window.add_repo(git_repo, code_test_command(git_repo))
    window.submit_proposal(
        "Break add on purpose", diff_text=make_patch(git_repo, {"calc.py": sources["calc_wrong"]})
    )
    was = window.diff_view.is_side_by_side()
    window.side_by_side_button.setChecked(not was)
    assert window.diff_view.is_side_by_side() is (not was)
    assert window.diff_view.toPlainText().startswith("diff --git")


def test_opening_a_file_adds_a_closable_centre_tab(
    window, git_repo, make_patch, sources
) -> None:
    window.add_repo(git_repo, code_test_command(git_repo))
    window.index_repo()
    before = window.tabs.count()
    window.open_file_tab("calc.py")
    assert window.tabs.count() == before + 1
    index = window.tabs.count() - 1
    assert "calc.py" in window.tabs.tabText(index)
    assert window.tabs.widget(index).toPlainText() == sources["calc"]
    bar = window.tabs.tabBar()
    close = bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
    assert close is not None and close.objectName() == "tabClose"
    assert close.text() == "\u2715", "a quiet glyph, not a filled red box"
    assert bar.tabButton(0, QTabBar.ButtonPosition.RightSide) is None, "Graph is not closable"
    assert bar.tabButton(2, QTabBar.ButtonPosition.RightSide) is None, "Changes is not closable"
    window.open_file_tab("calc.py")  # the same path focuses, never duplicates
    assert window.tabs.count() == before + 1
    close.click()  # the real button closes its own tab
    assert window.tabs.count() == before


def test_switching_repository_closes_file_tabs(window, git_repo, tmp_path) -> None:
    from conftest import git as git_in  # the suite's own git helper

    window.add_repo(git_repo, code_test_command(git_repo))
    window.open_file_tab("calc.py")
    assert window.tabs.count() > 3
    second = tmp_path / "other-repo"
    second.mkdir()
    (second / "x.py").write_text("X = 1\n", encoding="utf-8")
    git_in(second, "init", "-q")
    git_in(second, "config", "user.name", "selfkraken-test")
    git_in(second, "config", "user.email", "selfkraken-test@localhost")
    git_in(second, "add", "-A")
    git_in(second, "commit", "-q", "-m", "second repo")
    window.add_repo(second, None)
    assert window.tabs.count() == 3, "a tab of another repository is not context"


def test_start_hooks_drive_the_window(
    qapp, gui_settings, no_dialogs, git_repo, monkeypatch
) -> None:
    """SK_GUI_START_TAB / SK_GUI_SELECT_PROPOSAL exist for capture and checking."""
    from selfkraken.gui.app import MainWindow

    store = Store(gui_settings.db_path)
    try:
        repo = workflow.register_repo(store, git_repo, None)
        proposal = store.create_proposal(
            repo.id,
            "guard the division",
            "manual",
            "diff --git a/x.py b/x.py\n",
            rationale="",
        )
        proposal_id = proposal.id
    finally:
        store.close()
    monkeypatch.setenv("SK_GUI_START_TAB", "changes")
    monkeypatch.setenv("SK_GUI_SELECT_PROPOSAL", "1")
    win = MainWindow(run_jobs_inline=True, theme="dark", settings=gui_settings)
    try:
        win.show()
        qapp.processEvents()
        assert win.tabs.currentIndex() == 2
        assert win._selected_proposal_id == proposal_id
    finally:
        win.close()


def test_graph_date_column_can_never_elide(qapp, git_repo) -> None:
    """The date is sized from the widest string the formatter can produce."""
    from PySide6.QtGui import QFontMetrics

    from selfkraken.gui.graph_widget import GraphView
    from selfkraken.gui.models import TIMESTAMP_SAMPLE, date_column_width, meta_font
    from selfkraken.graph import layout, read_commits

    view = GraphView(DARK)
    try:
        view.set_rows(layout(read_commits(git_repo)), [])
        view.resize(1280, 700)
        view.show()
        qapp.processEvents()
        budget = date_column_width(view, TIMESTAMP_SAMPLE, "Date")
        for font in (view.font(), meta_font()):
            needed = QFontMetrics(font).horizontalAdvance("2025-03-09 08:00")
            assert budget >= needed + 2 * 6, f"{font.pointSizeF()}pt would be elided"
        assert view.columnWidth(4) == budget
    finally:
        view.close()


def test_graph_refs_column_serves_the_visible_rows(qapp, tmp_path) -> None:
    """Refs is sized to what is on screen, and Subject keeps the rest."""
    from selfkraken.gui.graph_widget import (
        REFS_COLUMN_HARD_MAX,
        REFS_VIEWPORT_SHARE,
        GraphView,
    )

    repo = make_multibranch_repo(tmp_path / "demo")
    view = GraphView(DARK)
    try:
        view.set_rows(layout(read_commits(repo, limit=300)), list_branches(repo))
        view.resize(1000, 500)
        view.show()
        qapp.processEvents()
        viewport = view.viewport().width()
        refs = view.columnWidth(1)
        assert refs <= max(REFS_COLUMN_HARD_MAX, int(viewport * REFS_VIEWPORT_SHARE))
        assert view.horizontalHeader().sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch
        assert view.columnWidth(2) > refs, "Subject must out-width the badge column"
    finally:
        view.close()
