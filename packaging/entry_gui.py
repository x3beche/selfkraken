"""Entry point for the frozen ``skraken-gui`` desktop app."""

import sys

from selfkraken.gui.app import run

if __name__ == "__main__":
    sys.exit(run())
