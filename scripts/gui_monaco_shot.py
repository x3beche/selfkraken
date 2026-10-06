"""Capture the Monaco views from the **real display** into docs/screenshots.

Why a separate script: QtWebEngine cannot paint under ``QT_QPA_PLATFORM=offscreen``
— offscreen the GUI shows its plain-text fallback, which is what
``scripts/gui_screenshots.py`` records.  The pictures here are the same states
with the editor actually running, so they need a desktop session (``DISPLAY``).

Like every capture here it grabs **its own window** and never the screen:
``QWidget.grab()`` renders the widget tree, which is also the only way to see the
web-engine area at all (an X11 window capture shows black there, because the page
is a separate GPU surface).

Usage:  .venv/bin/python scripts/gui_monaco_shot.py [--out docs/screenshots]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _datadir_guard import refuse_real_data_dir  # noqa: E402

#: how long to let the page paint before grabbing the window
PAINT_MS = 2500


def main() -> int:
    parser = argparse.ArgumentParser(description="selfkraken Monaco screenshots")
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    parser.add_argument(
        "--size", default="1440x900", help="window size, e.g. 1440x900 or 1280x800"
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print(
            "no display: these screenshots need a desktop session (the offscreen\n"
            "fallback is captured by scripts/gui_screenshots.py instead).",
            file=sys.stderr,
        )
        return 1
    os.environ.pop("QT_QPA_PLATFORM", None)
    width, _, height = args.size.partition("x")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    with tempfile.TemporaryDirectory(prefix="selfkraken-monaco-shots-") as tmp:
        work = Path(tmp)
        os.environ["SK_DATA_DIR"] = str(work / "data")
        refuse_real_data_dir(work / "data", "the screenshot data dir (check TMPDIR)")
        for variable in ("SK_LLM_BASE_URL", "SK_LLM_API_KEY", "SK_LLM_MODEL"):
            os.environ.pop(variable, None)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        from selfkraken import sample
        from selfkraken.config import load_settings
        from selfkraken.db import Store
        from selfkraken.gui.app import MainWindow, apply_theme
        from selfkraken.gui.theme import resolve

        settings = load_settings()
        store = Store(settings.db_path)
        try:
            repo = sample.load_multibranch_sample(store, settings)
            proposals = store.list_proposals(repo_id=repo.id)
        finally:
            store.close()

        apply_theme(app, resolve("dark", prefers_dark=True))
        window = MainWindow(theme="dark", run_jobs_inline=True)
        window.resize(int(width), int(height))
        window.show()

        results: list[Path] = []

        def wait_then(action, ms: int = PAINT_MS) -> None:
            QTimer.singleShot(ms, action)

        def select_proposal() -> None:
            if proposals:
                window.select_proposal(proposals[0].id)
            window.tabs.setCurrentIndex(2)  # Changes
            wait_then(lambda: shoot("gui-monaco.png", select_done))

        def select_done() -> None:
            window.open_file_tab("src/app/core.py")
            wait_then(lambda: shoot("gui-file.png", finish))

        def shoot(name: str, then) -> None:
            image = window.grab()
            target = out / name
            ok = image.save(str(target))
            print(f"{'wrote' if ok else 'FAILED'} {target} "
                  f"({image.width()}x{image.height()})")
            if ok:
                results.append(target)
            then()

        def finish() -> None:
            window.close()
            app.quit()

        wait_then(select_proposal, 1200)  # let the boot and the first diff land
        app.exec()

    if len(results) != 2:
        return 1
    print("written:")
    for target in results:
        print(f"  {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
