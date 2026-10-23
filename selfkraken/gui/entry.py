"""Entry point for the frozen desktop app.

The GUI build also answers the CLI: ``skraken-gui -c doctor`` runs exactly what
``skraken doctor`` runs. One download then covers both, which matters because
the window is where most people start and fetching a second file for a single
command is friction for no benefit.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

#: leading arguments that mean "act as the CLI instead of opening the window"
CLI_FLAGS = ("-c", "--cli")


def _attach_console() -> None:
    """Give the windowed Windows build somewhere to print.

    PyInstaller builds the GUI with ``console=False``, so ``sys.stdout`` and
    ``sys.stderr`` are ``None`` and a CLI command's output would go nowhere.
    Attach to the console that started us; if there is none (the exe was
    double-clicked), open one so the output is still readable.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        attach_parent_process = -1
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.AttachConsole(attach_parent_process):
            kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:  # pragma: no cover - only reachable on a real Windows box
        # No console is a cosmetic loss, not a reason to abort the command.
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI when asked with ``-c``/``--cli``, otherwise the window."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in CLI_FLAGS:
        _attach_console()
        # imported here so the GUI build starts without pulling in typer/rich
        from selfkraken.cli import app as cli_app

        sys.argv = [sys.argv[0], *args[1:]]
        # the usage line must show the switch, or it reads as if the bare
        # binary took commands directly
        cli_app(prog_name="skraken-gui -c")
        return 0

    from selfkraken.gui.app import run

    return run()
