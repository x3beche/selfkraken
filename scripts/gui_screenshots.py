"""Generate desktop-GUI screenshots (offscreen Qt, throwaway SK_DATA_DIR).

Usage:  .venv/bin/python scripts/gui_screenshots.py [--out docs/screenshots]

Builds the multibranch demo repo, registers it (with a real test command),
applies one manual proposal (tests + commit) and leaves a second proposal in
'proposed' state — then captures the window at 1440x900:

  gui-empty.png      fresh data dir, guidance empty state
  gui-graph.png      commit graph tab (dark theme)
  gui-proposal.png   proposal detail with diff + test output
  gui-light.png      commit graph tab (light theme)
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _datadir_guard import refuse_real_data_dir  # noqa: E402

os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _git(repo: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c", "user.name=Kraken Fixture",
            "-c", "user.email=fixture@selfkraken.local",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _make_patch(repo: Path, rel: str, new_content: str) -> str:
    """Diff that turns the committed *rel* into *new_content*; tree stays clean."""
    target = repo / rel
    original = target.read_text(encoding="utf-8")
    target.write_text(new_content, encoding="utf-8")
    diff = _git(repo, "diff", "--", rel)
    target.write_text(original, encoding="utf-8")
    if not diff.strip():
        raise SystemExit(f"empty diff for {rel}")
    return diff


def main() -> int:
    parser = argparse.ArgumentParser(description="selfkraken GUI screenshots")
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # never read from (or hand anything that could write to) the real user data
    if os.environ.get("SK_DATA_DIR"):
        refuse_real_data_dir(os.environ["SK_DATA_DIR"], "the SK_DATA_DIR you exported")

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    with tempfile.TemporaryDirectory(prefix="selfkraken-guishots-") as tmp:
        work = Path(tmp)
        os.environ["SK_DATA_DIR"] = str(work / "data")
        refuse_real_data_dir(work / "data", "the screenshot data dir (check TMPDIR)")
        for variable in ("SK_LLM_BASE_URL", "SK_LLM_API_KEY", "SK_LLM_MODEL"):
            os.environ.pop(variable, None)
        # commits made through the GUI must never use a personal git identity
        os.environ.update(
            {
                "GIT_AUTHOR_NAME": "Demo User",
                "GIT_AUTHOR_EMAIL": "demo@localhost",
                "GIT_COMMITTER_NAME": "Demo User",
                "GIT_COMMITTER_EMAIL": "demo@localhost",
            }
        )

        from selfkraken.gui.app import MainWindow, apply_theme
        from selfkraken.gui.theme import LIGHT, resolve
        from fixtures_git import make_multibranch_repo

        # -- 1. empty state (fresh data dir) --------------------------------
        apply_theme(app, resolve("dark"))
        empty = MainWindow(run_jobs_inline=True, theme="dark")
        empty.resize(1440, 900)
        empty.show()
        app.processEvents()
        _save(empty, out / "gui-empty.png")
        empty.close()

        # -- 2. demo repo + full loop ----------------------------------------
        repo = make_multibranch_repo(work / "demo-app")
        # keep the file tree free of test-run noise
        (repo / ".gitignore").write_text(
            "__pycache__/\n*.py[cod]\n.pytest_cache/\n", encoding="utf-8"
        )
        _git(repo, "add", "--", ".gitignore")
        _git(repo, "commit", "-q", "-m", "Ignore Python caches")
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        test_command = f"{shlex.quote(sys.executable)} -m pytest -q"

        core = (repo / "src" / "app" / "core.py").read_text(encoding="utf-8")
        patch_core = _make_patch(
            repo,
            "src/app/core.py",
            core + '\n\ndef proposal_note() -> str:\n    """Added by a selfkraken GUI proposal."""\n    return "applied via skraken gui"\n',
        )
        readme = (repo / "README.md").read_text(encoding="utf-8")
        patch_readme = _make_patch(
            repo,
            "README.md",
            readme + "\n## Proposals\n\n- applied via the desktop GUI\n",
        )

        apply_theme(app, resolve("dark"))
        win = MainWindow(run_jobs_inline=True, theme="dark")
        win.confirm_handler = lambda title, text: True
        win.resize(1440, 900)
        win.show()
        app.processEvents()

        win.add_repo(repo, test_command)
        win.index_repo()
        win.submit_proposal(
            "Add a proposal_note() helper to src/app/core.py",
            diff_text=(
                "Add a small proposal_note() helper to src/app/core.py so the "
                "proposal pipeline has something to apply in the demo.\n\n"
                + patch_core
            ),
        )
        applied_id = win._selected_proposal_id
        win.apply_proposal(applied_id, run_tests=True, commit=True, keep_on_failure=False)
        win.submit_proposal(
            "Document the proposals workflow in README.md",
            diff_text=patch_readme,
        )
        app.processEvents()

        # -- 3. graph screenshot (dark) --------------------------------------
        win.tabs.setCurrentIndex(0)
        win.graph_view.select_row(0)
        app.processEvents()
        _save(win, out / "gui-graph.png")

        # -- 4. proposal detail: applied proposal with diff + test output ----
        win.tabs.setCurrentIndex(1)
        win.commit_group.setVisible(False)
        win.select_proposal(applied_id)
        win.tests_toggle.setChecked(True)
        win.files_view.expandAll()
        app.processEvents()
        diff_scroll = win.diff_view.verticalScrollBar()
        diff_scroll.setValue(int(diff_scroll.maximum() * 0.4))  # @@ + context + adds
        scrollbar = win.right_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        app.processEvents()
        _save(win, out / "gui-proposal.png")
        scrollbar.setValue(0)
        win.files_view.collapseAll()

        # -- 5. light theme ----------------------------------------------------
        apply_theme(app, LIGHT)
        light = MainWindow(run_jobs_inline=True, theme="light")
        light.resize(1440, 900)
        light.show()
        app.processEvents()
        light.tabs.setCurrentIndex(0)
        light.graph_view.select_row(0)
        app.processEvents()
        _save(light, out / "gui-light.png")
        light.close()
        win.close()

    print("written:")
    for name in ("gui-empty.png", "gui-graph.png", "gui-proposal.png", "gui-light.png"):
        print(f"  {out / name}")
    return 0


def _save(widget, path: Path) -> None:
    image = widget.grab()
    image.save(str(path))
    print(f"wrote {path} ({image.width()}x{image.height()})")


if __name__ == "__main__":
    raise SystemExit(main())
