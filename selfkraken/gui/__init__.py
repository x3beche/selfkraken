"""Desktop GUI for selfkraken (PySide6 / Qt).

Importing this package alone does **not** require PySide6 — every Qt import
lives inside the submodules (``app``, ``graph_widget``, ``diff_view``,
``models``, ``worker``), which are imported lazily by ``skraken gui``.
Install the extra with ``pip install 'selfkraken[gui]'``.
"""

from __future__ import annotations

GUI_MISSING_MESSAGE = (
    "the desktop GUI needs PySide6 — install with: pip install 'selfkraken[gui]'"
)

__all__ = ["GUI_MISSING_MESSAGE"]
