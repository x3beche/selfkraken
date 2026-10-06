"""Pre-launch checks for the desktop GUI — pure stdlib, never imports Qt.

``QApplication`` dies hard when its platform plugin cannot load: on Ubuntu
without ``libxcb-cursor0`` Qt prints::

    qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb platform plugin.
    qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" even though it was found.
    Aborted (core dumped)

— before any Python error handler can run.  So ``skraken gui`` calls
:func:`preflight` *first* (cheap, stdlib-only), and then :func:`launch_probe`,
which lets a throwaway subprocess take the abort instead of the user's
process.  Every problem comes back as a human-readable string carrying the
exact install command.

Importing this module must never import PySide6 (``selfkraken.gui.__init__``
stays Qt-free); the checks therefore work even on a core-only install and the
CLI can report problems without touching the library that aborts.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
from typing import Optional

#: Qt's platform-plugin selector — must be set before QApplication exists.
QT_PLATFORM_VAR = "QT_QPA_PLATFORM"

#: The system library Qt 6.5+ needs to load the xcb (X11) platform plugin.
#: Debian/Ubuntu: libxcb-cursor0 · Fedora: xcb-util-cursor · Arch: xcb-util-cursor.
XCB_CURSOR_LIBRARY = "libxcb-cursor.so.0"

#: The one fix repeated on every xcb failure path.
XCB_CURSOR_HINT = (
    "Qt needs the system library libxcb-cursor0. Install it with: "
    "sudo apt install libxcb-cursor0 (Debian/Ubuntu) / "
    "sudo dnf install xcb-util-cursor (Fedora) / "
    "sudo pacman -S xcb-util-cursor (Arch)"
)

#: What the launch probe runs — the same import + construction the real
#: launch does, in a child process that is allowed to core dump.
PROBE_CODE = "from PySide6.QtWidgets import QApplication; QApplication([])"

#: A plugin that cannot initialise either aborts fast or hangs; bound both.
PROBE_TIMEOUT_SECONDS = 15

#: Qt's own explanation lines kept from the probe's stderr.
PROBE_QT_LINES_MAX = 5


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def xcb_cursor_library_present() -> bool:
    """True when the xcb Qt platform plugin can dlopen libxcb-cursor.

    ``ctypes.util.find_library`` reads the ldconfig cache, which can be stale
    in both directions (entry without a loadable file — the exact trap in
    Qt's own error text — and file without an entry), so each candidate is
    really loaded with ``ctypes.CDLL`` the same way Qt's plugin loads it.
    """
    candidates: list[str] = []
    try:
        found = ctypes.util.find_library("xcb-cursor")
    except Exception:  # pragma: no cover - find_library is already defensive
        found = None
    if found:
        candidates.append(str(found))
    candidates.append(XCB_CURSOR_LIBRARY)
    for candidate in candidates:
        try:
            ctypes.CDLL(candidate)
        except OSError:
            continue
        return True
    return False


def preflight() -> list[str]:
    """Problems that would stop Qt from opening a window; empty list is good.

    Runs **before** ``QApplication`` is created — after that a failing
    platform plugin aborts the whole process and nothing can be reported.
    On a Wayland session with no explicit platform choice this also fixes the
    environment: ``QT_QPA_PLATFORM=wayland;xcb`` makes Qt prefer Wayland, so
    the X11-specific checks (and possibly missing X11 helper libraries) are
    skipped entirely.
    """
    if not _is_linux():
        return []  # Windows and macOS ship their own Qt platform plugin

    platform = (os.environ.get(QT_PLATFORM_VAR) or "").strip().lower()
    wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    display = (os.environ.get("DISPLAY") or "").strip()

    if not platform and wayland:
        # Qt only honours this when it is set before QApplication exists.
        os.environ[QT_PLATFORM_VAR] = "wayland;xcb"
        return []

    if platform and not platform.startswith("xcb"):
        return []  # wayland / offscreen / vnc: the xcb plugin is not used

    problems: list[str] = []
    if not display:
        problems.append(
            "no graphical display: DISPLAY is not set and no Wayland session "
            "is running — run 'skraken gui' from a desktop session (or set "
            "DISPLAY / WAYLAND_DISPLAY; non-visual use: the CLI or "
            "QT_QPA_PLATFORM=offscreen)"
        )
    if not xcb_cursor_library_present():
        problems.append(XCB_CURSOR_HINT)
    return problems


def launch_probe(timeout_seconds: int = PROBE_TIMEOUT_SECONDS) -> Optional[list[str]]:
    """None when Qt can create a QApplication here, else problem lines.

    The real launch cannot survive a platform-plugin abort (it is a
    ``SIGABRT`` core dump, not a Python exception), so the attempt happens in
    a throwaway subprocess: whatever Qt would die of, the child dies of, and
    the parent turns its exit status plus the ``qt.qpa`` lines of its stderr
    into printable errors.  Skipped under ``QT_QPA_PLATFORM=offscreen``
    (nothing is opened on a display; the test suite proves that path).
    """
    platform = (os.environ.get(QT_PLATFORM_VAR) or "").strip().lower()
    if platform == "offscreen":
        return None

    header = "Qt could not start a window on this display"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", PROBE_CODE],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return [
            f"{header}: the Qt launch probe (a test QApplication start) was "
            f"still running after {timeout_seconds} s and was killed"
        ]
    except OSError as exc:  # pragma: no cover - could not even spawn Python
        return [f"{header}: the Qt launch probe could not run ({type(exc).__name__})"]

    if completed.returncode == 0:
        return None

    lines = [f"{header}: the Qt launch probe {_exit_reason(completed.returncode)}"]
    qt_lines = [
        raw.strip()
        for raw in (completed.stderr or "").splitlines()
        if raw.lstrip().startswith("qt.qpa")
    ]
    lines.extend(qt_lines[:PROBE_QT_LINES_MAX])
    lines.append(XCB_CURSOR_HINT)
    return lines


def _exit_reason(returncode: int) -> str:
    """Say *how* the probe died — a signal is Qt's abort, the classic core dump."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:  # pragma: no cover - unknown signal number
            name = f"signal {-returncode}"
        return f"was killed by {name} — Qt aborts (core dumped) when its platform plugin cannot load"
    return f"exited with code {returncode}"
