"""Shared safety guard for the screenshot scripts.

They must write **only** inside throwaway temp dirs.  A run that fell back to
the default data dir once polluted the user's real
``~/.local/share/selfkraken`` with probe repos; every entry point now refuses
to start unless its data directory is provably outside that location.
"""

from __future__ import annotations

from pathlib import Path


def refuse_real_data_dir(path: Path | str, source: str) -> None:
    """``SystemExit`` if *path* is (or lives inside) the user's real data dir.

    Compares against both the environment-resolved default
    (``$XDG_DATA_HOME/selfkraken``) and the hard fallback
    (``~/.local/share/selfkraken``), so a stray ``XDG_DATA_HOME`` cannot make
    the real directory "allowed".
    """
    from selfkraken.config import default_data_dir

    resolved = Path(str(path)).expanduser().resolve()
    forbidden = {
        default_data_dir().resolve(),
        (Path.home() / ".local" / "share" / "selfkraken").resolve(),
    }
    for root in sorted(forbidden):
        if resolved == root or root in resolved.parents:
            raise SystemExit(
                f"refusing to run: {source} resolves to the user's real "
                f"selfkraken data dir ({root}) — these scripts always use a "
                "throwaway temp SK_DATA_DIR instead."
            )
