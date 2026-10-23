"""Capture the Settings tab (offscreen) for review."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from selfkraken.config import load_settings  # noqa: E402
from selfkraken.gui.app import MainWindow  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots/settings")
out.mkdir(parents=True, exist_ok=True)

data = Path(tempfile.mkdtemp())
settings = load_settings(env={**os.environ, "SK_DATA_DIR": str(data)}, load_dotenv_file=False)
app = QApplication.instance() or QApplication([sys.argv[0]])
for theme in ("dark", "light"):
    window = MainWindow(run_jobs_inline=True, theme=theme, settings=settings)
    window.resize(1280, 800)
    window.show()
    from selfkraken.gui.app import TAB_SETTINGS
    window.tabs.setCurrentIndex(TAB_SETTINGS)
    app.processEvents()
    path = out / f"settings-{theme}.png"
    window.grab().save(str(path))
    print("wrote", path)
    window.close()
