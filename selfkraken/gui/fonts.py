"""The bundled UI and console fonts.

IBM Plex Sans for the interface, IBM Plex Mono for code, paths and the log —
the families the interface is designed around (the same ones the reference
design uses). Both are SIL OFL 1.1; the licence travels beside the files.

They ship as static TTFs inside the package, so a wheel, a PyInstaller bundle
and a source checkout all render identically and no network is involved.
"""

from __future__ import annotations

from pathlib import Path

UI_FAMILY = "Switzer"
MONO_FAMILY = "IBM Plex Mono"

FONT_DIR = Path(__file__).resolve().parent / "fonts"

_FILES = (
    "Switzer-400.ttf",
    "Switzer-500.ttf",
    "Switzer-600.ttf",
    "PlexMono-Medium.ttf",
)

_LOADED: bool | None = None


def load() -> bool:
    """Register the bundled faces with Qt once; True when they are available."""
    global _LOADED
    if _LOADED is not None:
        return _LOADED

    try:
        from PySide6.QtGui import QFontDatabase
    except Exception:  # pragma: no cover - Qt missing entirely
        _LOADED = False
        return _LOADED

    registered = 0
    for name in _FILES:
        path = FONT_DIR / name
        if not path.is_file():
            continue
        if QFontDatabase.addApplicationFont(str(path)) >= 0:
            registered += 1

    families = {name.casefold() for name in QFontDatabase.families()}
    _LOADED = registered > 0 and UI_FAMILY.casefold() in families
    return _LOADED
