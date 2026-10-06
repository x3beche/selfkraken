"""Deterministic demo repositories for selfkraken tests and screenshots.

Importable by pytest (`from fixtures_git import make_multibranch_repo`) and by
scripts/screenshots.py. Every builder creates a real git repo with fixed,
incrementing commit dates (GIT_AUTHOR_DATE/GIT_COMMITTER_DATE) and local
user identity, so history shape and ordering are reproducible.

The richer multi-branch demo that *ships* with the app lives in the package
(``selfkraken.sample._build_multibranch``, reached by
``skraken sample load --kind multibranch``).  This fixture deliberately keeps
its own leaner shape: ``tests/test_graph.py``, ``tests/test_views.py``,
``tests/test_gui.py`` and ``tests/test_multibranch_workflow.py`` pin its commit
count, subjects, file contents and the ``Kraken Fixture`` identity, so it is not
a thin wrapper around the shipped builder.

Every builder puts its fake bare remote in an *explicit* location: never a
generic ``origin.git`` next to the repository (that used to litter whatever
folder a demo was built in), but ``<repo-name>-origin.git`` by default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GIT_BASE = 1_700_000_000  # arbitrary fixed epoch: 2023-11-14
_DATE_STEP = 3600  # one hour between commits: stable newest-first ordering


def _run(repo: Path, step: int, *args: str) -> str:
    """Run one git command for the fixture with pinned identity and dates."""
    stamp = f"@{_GIT_BASE + step * _DATE_STEP} +0000"
    env = {
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": "Kraken Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@selfkraken.local",
        "GIT_COMMITTER_NAME": "Kraken Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@selfkraken.local",
        "HOME": str(repo.parent),
        "PATH": "/usr/bin:/bin",
    }
    cmd = [
        "git",
        "-C",
        str(repo),
        "-c", "user.name=Kraken Fixture",
        "-c", "user.email=fixture@selfkraken.local",
        "-c", "commit.gpgsign=false",
        "-c", "tag.gpgsign=false",
        "-c", "core.autocrlf=false",
        *args,
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"fixture git {' '.join(args)} failed ({proc.returncode}):\n"
            f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        )
    return proc.stdout


class _Repo:
    """Tiny scripted wrapper that keeps a commit-date counter per repo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._step = 0

    def _next(self) -> int:
        self._step += 1
        return self._step

    def init(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        _run(self.path, self._next(), "init", "-b", "main")

    def write(self, rel: str, content: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str, *paths: str) -> str:
        step = self._next()
        if paths:
            _run(self.path, step, "add", "--", *paths)
        else:
            _run(self.path, step, "add", "-A")
        _run(self.path, step, "commit", "-m", message)
        return _run(self.path, step, "rev-parse", "HEAD").strip()

    def branch(self, name: str, start: str | None = None) -> None:
        step = self._next()
        args = ["branch", name] + ([start] if start else [])
        _run(self.path, step, *args)

    def checkout(self, name: str, *, create: bool = False) -> None:
        step = self._next()
        if create:
            _run(self.path, step, "checkout", "-b", name)
        else:
            _run(self.path, step, "checkout", name)

    def merge(self, name: str, message: str) -> str:
        step = self._next()
        _run(self.path, step, "merge", "--no-ff", "-m", message, name)
        return _run(self.path, step, "rev-parse", "HEAD").strip()

    def merge_octopus(self, names: list[str], message: str) -> str:
        step = self._next()
        _run(self.path, step, "merge", "--no-ff", "-m", message, *names)
        return _run(self.path, step, "rev-parse", "HEAD").strip()

    def tag(self, name: str, message: str) -> None:
        step = self._next()
        _run(self.path, step, "tag", "-a", name, "-m", message)

    def rev(self, ref: str) -> str:
        return _run(self.path, self._next(), "rev-parse", ref).strip()


_CORE_V0 = '''"""Core utilities for the demo app."""


def greet(name: str) -> str:
    return f"hello {name}"
'''

_CORE_V1 = '''"""Core utilities for the demo app."""


def greet(name: str) -> str:
    return f"hello {name}"


def compute(a: int, b: int) -> int:
    return a * 3 + b


def divide(a: int, b: int) -> float:
    return a / b
'''

_CORE_V2 = '''"""Core utilities for the demo app."""


def greet(name: str) -> str:
    return f"hello {name}"


def compute(a: int, b: int) -> int:
    return a * 3 + b


def divide(a: int, b: int) -> float:
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''


def make_multibranch_repo(path: Path, origin_path: Path | None = None) -> Path:
    """Realistic multi-branch history: parallel features, merges, hotfix, tag.

    ``origin_path`` is where the fake bare remote is created; the default is
    ``<path>-origin.git`` (a sibling named after the repository, never a
    generic ``origin.git`` that escapes the caller's directory).

    Shape (newest on top):
      main with scaffold + compute; feature/login (2 commits) and
      feature/search (2 commits) branched in parallel; main gets a commit;
      feature/search merges main (--no-ff); both features merged into main
      (--no-ff); hotfix/div-zero branched and merged; experiment/cache left
      unmerged; annotated tag v0.1.0; a bare ``origin`` remote is added and
      pushed, then main gets one more commit so main is 1 ahead of upstream.
    """
    repo = _Repo(Path(path))
    repo.init()

    # --- main: scaffold -----------------------------------------------------
    repo.write("README.md", "# Demo App\n\nA small project used for selfkraken demos.\n")
    repo.write("src/app/__init__.py", '"""Demo application package."""\n')
    repo.write("src/app/core.py", _CORE_V0)
    repo.write("tests/test_core.py", "from src.app.core import greet\n\n\ndef test_greet():\n    assert greet('x') == 'hello x'\n")
    repo.commit("Initial project scaffold", "README.md", "src/app/__init__.py", "src/app/core.py", "tests/test_core.py")
    repo.write("src/app/core.py", _CORE_V1)
    repo.commit("Add compute function", "src/app/core.py")

    # --- two parallel feature branches --------------------------------------
    repo.checkout("feature/login", create=True)
    repo.write("src/app/auth.py", '"""Login helpers."""\n\n\ndef login(user: str, password: str) -> bool:\n    return bool(user) and bool(password)\n')
    repo.write("tests/test_auth.py", "from src.app.auth import login\n\n\ndef test_login_ok():\n    assert login('ada', 'pw')\n")
    repo.commit("Add login module", "src/app/auth.py", "tests/test_auth.py")
    repo.write("src/app/auth.py", '"""Login helpers."""\n\n\ndef login(user: str, password: str) -> bool:\n    if not password:\n        return False\n    return bool(user)\n')
    repo.commit("Reject empty password", "src/app/auth.py")

    repo.checkout("main")
    repo.checkout("feature/search", create=True)
    repo.write("src/app/search.py", '"""Search prototype."""\n\n\ndef find(items: list[str], needle: str) -> list[str]:\n    return [i for i in items if needle in i]\n')
    repo.commit("Add search prototype", "src/app/search.py")
    repo.write("src/app/search.py", '"""Search prototype with simple ranking."""\n\n\ndef find(items: list[str], needle: str) -> list[str]:\n    scored = [(i.count(needle), i) for i in items if needle in i]\n    return [i for _, i in sorted(scored, reverse=True)]\n')
    repo.write("tests/test_search.py", "from src.app.search import find\n\n\ndef test_find_ranks():\n    assert find(['aa', 'a'], 'a') == ['aa', 'a']\n")
    repo.commit("Rank search results", "src/app/search.py", "tests/test_search.py")

    # --- main moves on, feature/search merges main --------------------------
    repo.checkout("main")
    repo.write("README.md", "# Demo App\n\nA small project used for selfkraken demos.\n\n## Roadmap\n\n- search\n- login\n- cache\n")
    repo.commit("Document roadmap", "README.md")

    repo.checkout("feature/search")
    repo.merge("main", "Merge main into feature/search")
    repo.write("src/app/search.py", '"""Search prototype with simple ranking."""\n\n\ndef find(items: list[str], needle: str) -> list[str]:\n    scored = [(i.count(needle), i) for i in items if needle.lower() in i.lower()]\n    return [i for _, i in sorted(scored, reverse=True)]\n')
    repo.commit("Tweak search scoring", "src/app/search.py")

    # --- both features merged into main -------------------------------------
    repo.checkout("main")
    repo.merge("feature/login", "Merge feature/login into main")
    repo.merge("feature/search", "Merge feature/search into main")

    # --- hotfix branched from main and merged --------------------------------
    repo.checkout("hotfix/div-zero", create=True)
    repo.write("src/app/core.py", _CORE_V2)
    repo.write("tests/test_core.py", "import pytest\n\nfrom src.app.core import divide, greet\n\n\ndef test_greet():\n    assert greet('x') == 'hello x'\n\n\ndef test_divide_guard():\n    with pytest.raises(ValueError):\n        divide(1, 0)\n")
    repo.commit("Guard against division by zero", "src/app/core.py", "tests/test_core.py")
    repo.checkout("main")
    repo.merge("hotfix/div-zero", "Merge hotfix/div-zero into main")

    # --- an experiment left open (unmerged) ----------------------------------
    repo.checkout("experiment/cache", create=True)
    repo.write("src/app/cache.py", '"""Naive caching layer (experimental)."""\n\n_CACHE: dict[str, str] = {}\n\n\ndef remember(key: str, value: str) -> None:\n    _CACHE[key] = value\n')
    repo.write("tests/test_cache.py", "from src.app.cache import remember, _CACHE\n\n\ndef test_remember():\n    remember('k', 'v')\n    assert _CACHE['k'] == 'v'\n")
    repo.commit("Draft cache layer", "src/app/cache.py", "tests/test_cache.py")
    repo.checkout("main")

    repo.tag("v0.1.0", "Release 0.1.0")

    # --- origin remote + tracking so list_branches sees ahead/behind ---------
    # An explicit location, never a generic "<parent>/origin.git": building a
    # demo into ~/Documents/Github/x used to litter ~/Documents/Github/origin.git.
    if origin_path is None:
        bare = Path(path).with_name(Path(path).name + "-origin.git")
    else:
        bare = Path(origin_path)
    bare = bare.expanduser().resolve()
    _run(repo.path, repo._next(), "init", "--bare", "-b", "main", str(bare))
    _run(repo.path, repo._next(), "remote", "add", "origin", str(bare))
    _run(repo.path, repo._next(), "push", "--all", "origin")
    _run(repo.path, repo._next(), "push", "-u", "origin", "main")
    repo.write("README.md", "# Demo App\n\nA small project used for selfkraken demos.\n\n## Roadmap\n\n- done: search, login\n- next: cache\n")
    repo.commit("Bump release metadata", "README.md")
    return repo.path


def make_octopus_repo(path: Path) -> Path:
    """main + three side branches joined by one 4-parent octopus merge."""
    repo = _Repo(Path(path))
    repo.init()
    repo.write("README.md", "# Octopus demo\n")
    repo.write("src/app/__init__.py", "")
    repo.commit("Initial commit", "README.md", "src/app/__init__.py")

    for name, body in (
        ("topic/one", 'ONE = 1\n'),
        ("topic/two", 'TWO = 2\n'),
        ("topic/three", 'THREE = 3\n'),
    ):
        repo.checkout(name, create=True)
        repo.write(f"src/app/{name.split('/')[-1]}.py", body)
        repo.commit(f"Add {name}", f"src/app/{name.split('/')[-1]}.py")
        repo.checkout("main")

    repo.merge_octopus(
        ["topic/one", "topic/two", "topic/three"],
        "Merge topics one, two and three (octopus)",
    )
    return repo.path


def make_criss_cross_repo(path: Path) -> Path:
    """Criss-cross history: left merges right, right merges left, both into main."""
    repo = _Repo(Path(path))
    repo.init()
    repo.write("README.md", "# Criss-cross demo\n")
    repo.write("src/app/__init__.py", "")
    repo.commit("Root commit", "README.md", "src/app/__init__.py")

    repo.checkout("left", create=True)
    repo.write("src/app/left.py", "LEFT = True\n")
    repo.commit("Work on left", "src/app/left.py")

    repo.checkout("main")
    repo.checkout("right", create=True)
    repo.write("src/app/right.py", "RIGHT = True\n")
    repo.commit("Work on right", "src/app/right.py")

    repo.checkout("left")
    repo.merge("right", "left merges right")
    repo.checkout("right")
    repo.merge("left", "right merges left")

    repo.checkout("main")
    repo.merge("left", "Merge left into main")
    repo.merge("right", "Merge right into main")
    return repo.path
