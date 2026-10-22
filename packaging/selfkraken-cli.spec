# -*- mode: python ; coding: utf-8 -*-
"""Frozen single-file ``skraken`` CLI.

Core-only on purpose: the GUI extra (PySide6, ~200 MB) is excluded, so this
binary stays around 15-25 MB and every command except ``skraken gui`` works.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "PyQt5",
        "PyQt6",
        "shiboken6",
        "tkinter",
        "pytest",
        "setuptools",
        "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="skraken",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
