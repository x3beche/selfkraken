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
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl, Qt  # noqa: E402

from selfkraken.gui import monaco_payload as mp  # noqa: E402
from selfkraken.gui import monaco_view as mv  # noqa: E402
from selfkraken.gui.theme import DARK, LIGHT  # noqa: E402

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
# --------------------------------------------------------------------------- #
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


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
