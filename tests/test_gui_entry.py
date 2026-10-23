"""The GUI binary also answers the CLI when asked with -c/--cli."""

from __future__ import annotations

import sys

import pytest

from selfkraken.gui import entry


def test_dash_c_runs_the_cli_with_the_remaining_arguments(monkeypatch) -> None:
    """``skraken-gui -c doctor`` must reach typer exactly as ``skraken doctor``."""
    import selfkraken.cli as cli

    seen: dict[str, list[str]] = {}

    def fake_app(**_kwargs: object) -> None:
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["skraken-gui"])

    assert entry.main(["-c", "doctor", "--repo", "/tmp/x"]) == 0
    assert seen["argv"][1:] == ["doctor", "--repo", "/tmp/x"]


def test_long_flag_is_the_same_switch(monkeypatch) -> None:
    import selfkraken.cli as cli

    seen: dict[str, list[str]] = {}

    def fake_app(**_kwargs: object) -> None:
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["skraken-gui"])

    assert entry.main(["--cli", "log"]) == 0
    assert seen["argv"][1:] == ["log"]


def test_without_the_flag_the_window_opens(monkeypatch) -> None:
    pytest.importorskip("PySide6")
    import selfkraken.gui.app as gui_app

    monkeypatch.setattr(gui_app, "run", lambda: 7)

    assert entry.main([]) == 7
    assert entry.main(["something-qt"]) == 7


def test_a_command_named_like_the_flag_still_works(monkeypatch) -> None:
    """Only the *first* argument is the switch; later ones are passed through."""
    import selfkraken.cli as cli

    seen: dict[str, list[str]] = {}

    def fake_app(**_kwargs: object) -> None:
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["skraken-gui"])

    entry.main(["-c", "propose", "-c"])
    assert seen["argv"][1:] == ["propose", "-c"]
