# -*- mode: python ; coding: utf-8 -*-
"""Frozen ``skraken-gui`` desktop app (PySide6 + vendored Monaco).

Built as a one-directory bundle: QtWebEngine ships a separate helper process,
which does not survive a one-file extraction on every platform. CI archives the
folder (``.zip`` on Windows/macOS, ``.tar.gz`` on Linux).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "selfkraken" / "gui" / "monaco"), "selfkraken/gui/monaco"),
]

hiddenimports = (
    collect_submodules("PySide6.QtWebEngineWidgets")
    + collect_submodules("PySide6.QtWebEngineCore")
    + collect_submodules("PySide6.QtWebChannel")
)

a = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

# Trim what this app never loads: Qt's 53 MB of translations, the PDF and
# Quick3D stacks, and the shader tooling behind them.
_DROP_LIBS = ("Qt6Pdf", "Qt6Quick3D", "Qt6ShaderTools", "Qt6Designer", "Qt6Help")
a.datas = [d for d in a.datas if "/translations/" not in d[0]]
a.binaries = [b for b in a.binaries if not any(s in b[0] for s in _DROP_LIBS)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="skraken-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="skraken-gui",
)
