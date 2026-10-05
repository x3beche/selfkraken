"""Indexer tests: skip rules, chunking, incremental indexing, context budget.

``selfkraken.db`` is owned by another agent, so these tests drive
:class:`FakeStore` -- an in-test stand-in that implements exactly the four
store methods the indexer uses.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from selfkraken import indexer
from selfkraken.errors import ValidationError

# ------------------------------------------------------------------ fake store


@dataclass
class FakeChunk:
    path: str
    start_line: int
    content: str
    score: float


class FakeStore:
    """get_file_shas / replace_file_index / remove_files_not_in / search_chunks."""

    def __init__(self) -> None:
        self.shas: dict[str, tuple[str, int]] = {}
        self.chunks: list[tuple[str, int, str]] = []  # (path, start_line, content)
        self.replaced: list[str] = []

    def get_file_shas(self, repo_id: int) -> dict[str, str]:
        return {path: sha for path, (sha, _size) in self.shas.items()}

    def replace_file_index(
        self,
        repo_id: int,
        path: str,
        sha: str,
        size: int,
        chunks: list[tuple[int, str]],
    ) -> None:
        self.replaced.append(path)
        self.shas[path] = (sha, size)
        self.chunks = [row for row in self.chunks if row[0] != path]
        self.chunks.extend((path, start, content) for start, content in chunks)

    def remove_files_not_in(self, repo_id: int, keep: set[str]) -> int:
        doomed = [path for path in self.shas if path not in keep]
        for path in doomed:
            del self.shas[path]
        self.chunks = [row for row in self.chunks if row[0] not in doomed]
        return len(doomed)

    def search_chunks(
        self, repo_id: int, query: str, limit: int = 8
    ) -> list[FakeChunk]:
        words = re.findall(r"[A-Za-z0-9_]+", query or "")
        hits: list[FakeChunk] = []
        for path, start, content in self.chunks:
            if not words or any(word.lower() in content.lower() for word in words):
                hits.append(FakeChunk(path=path, start_line=start, content=content, score=1.0))
        return hits[:limit]


def make_repo(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id=1, path=str(root), name="repo", test_command=None, is_sample=False,
        created_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    (root / "calc.py").write_text("def div(a, b):\n    return a // b\n")
    (root / "notes.md").write_text("# notes\n\nsome prose\n")
    return root


# ------------------------------------------------------------------ skip rules


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env.production",
        ".env.local",
        "keys/server.pem",
        "app.key",
        "id_rsa",
        ".ssh/id_rsa",
        "certs/bundle.p12",
        "certs/old.pfx",
        "secrets.yaml",
        "var/app.sqlite",
        "cache/db.db",
    ],
)
def test_should_skip_blocks_secret_like_files(path: str) -> None:
    assert indexer.should_skip(path) is True


@pytest.mark.parametrize(
    "path", ["calc.py", "README.md", "src/checkout.py", "keys.txt", ".envrc", "db.py"]
)
def test_should_skip_allows_ordinary_files(path: str) -> None:
    assert indexer.should_skip(path) is False


def test_secret_patterns_are_the_contract_tuple() -> None:
    assert indexer.SECRET_PATTERNS == (
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa*",
        "*.p12",
        "*.pfx",
        "secrets.*",
        "*.sqlite",
        "*.db",
    )
    assert (indexer.MAX_FILE_BYTES, indexer.CHUNK_LINES) == (200_000, 60)


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"", False),
        (b"print('hi')\n", False),
        (b"caf\xc3\xa9\n", False),
        (b"\x00def", True),
        (b"x" * 9000 + b"\x00" + b"y", False),  # NUL beyond the sniff window
        (b"\xff\xfe\x00\x00binary", True),
    ],
)
def test_is_probably_binary(data: bytes, expected: bool) -> None:
    assert indexer.is_probably_binary(data) is expected


# --------------------------------------------------------------------- chunking


def test_chunk_text_uses_one_based_start_lines() -> None:
    lines = [f"line {n}" for n in range(1, 131)]  # 130 lines
    chunks = indexer.chunk_text("\n".join(lines) + "\n")
    assert [start for start, _ in chunks] == [1, 61, 121]
    assert chunks[0][1].splitlines()[0] == "line 1"
    assert chunks[1][1].splitlines()[0] == "line 61"
    assert len(chunks[1][1].splitlines()) == 60
    assert chunks[2][1].splitlines()[0] == "line 121"
    assert len(chunks[2][1].splitlines()) == 10


def test_chunk_text_small_and_empty_input() -> None:
    assert indexer.chunk_text("") == []
    assert indexer.chunk_text("only one line\n") == [(1, "only one line")]
    assert indexer.chunk_text("a\nb\nc\nd\ne", chunk_lines=2) == [
        (1, "a\nb"),
        (3, "c\nd"),
        (5, "e"),
    ]


def test_list_repo_files_skips_ignored_and_git_dir(repo: Path) -> None:
    assert indexer.list_repo_files(repo) == ["calc.py", "notes.md"]
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert indexer.list_repo_files(repo) == ["calc.py", "notes.md"]
    (repo / "scratch.log").write_text("noise\n")
    assert "scratch.log" in indexer.list_repo_files(repo)  # untracked, not ignored
    (repo / ".gitignore").write_text("ignored/\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "big.bin").write_bytes(b"\x00\x01")
    files = indexer.list_repo_files(repo)
    assert "ignored/big.bin" not in files
    assert not any(f.startswith(".git/") for f in files)


# ------------------------------------------------------------- index_repo counts


def test_index_repo_is_incremental(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)

    first = indexer.index_repo(store, record)
    assert (first.scanned, first.indexed, first.unchanged, first.removed) == (2, 2, 0, 0)
    assert set(store.shas) == {"calc.py", "notes.md"}
    assert len(store.chunks) == 2

    second = indexer.index_repo(store, record)
    assert (second.indexed, second.unchanged, second.scanned) == (0, 2, 2)

    (repo / "calc.py").write_text("def div(a, b):\n    return a / b\n")
    third = indexer.index_repo(store, record)
    assert (third.indexed, third.unchanged, third.removed) == (1, 1, 0)

    (repo / "notes.md").unlink()
    fourth = indexer.index_repo(store, record)
    assert fourth.removed == 1
    assert set(store.shas) == {"calc.py"}
    assert [path for path, _, _ in store.chunks] == ["calc.py"]


def test_index_repo_never_indexes_secrets_or_binaries(repo: Path) -> None:
    (repo / ".env").write_text("SK_LLM_API_KEY=super-secret\n")
    (repo / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nnope\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
    (repo / "huge.py").write_text("x = 1\n" + ("# pad\n" * 40_000))

    store, record = FakeStore(), make_repo(repo)
    stats = indexer.index_repo(store, record)
    assert stats.scanned == 6
    assert stats.indexed == 2 and stats.skipped == 4
    assert set(store.shas) == {"calc.py", "notes.md"}
    assert not any("super-secret" in content for _, _, content in store.chunks)


def test_index_repo_drops_a_file_that_became_a_secret(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)
    (repo / "calc.py").rename(repo / "calc.db")  # now a skipped pattern
    stats = indexer.index_repo(store, record)
    assert stats.removed == 1
    assert set(store.shas) == {"notes.md"}


def test_index_repo_reports_progress_paths(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    seen: list[str] = []
    indexer.index_repo(store, record, progress=seen.append)
    assert sorted(seen) == ["calc.py", "notes.md"]


# ------------------------------------------------------------------ build_context


def test_build_context_reads_extra_paths_first(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)

    context = indexer.build_context(
        store, record, "div prose", 8000, extra_paths=["calc.py"]
    )
    assert context.startswith("### calc.py (from line 1)\n```\n")
    assert "def div(a, b):" in context
    assert context.endswith("```\n")
    # the file is shown once: the full read wins over the matching search hit
    assert context.count("### calc.py") == 1
    assert "### notes.md (from line 1)" in context


def test_build_context_from_search_only(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)
    context = indexer.build_context(store, record, "prose", 4000)
    assert "### notes.md (from line 1)" in context
    assert "some prose" in context
    assert "def div" not in context
    assert indexer.build_context(store, record, "   ", 4000) == ""


def test_build_context_respects_max_chars(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)
    for budget in (10, 40, 120, 400, 4000):
        context = indexer.build_context(store, record, "div", budget, extra_paths=["calc.py"])
        assert len(context) <= budget
    big = indexer.build_context(store, record, "div", 120, extra_paths=["calc.py"])
    assert big.startswith("### calc.py (from line 1)")
    assert big.count("```") <= 2
    assert indexer.build_context(store, record, "div", 0) == ""


@pytest.mark.parametrize(
    "path,reason",
    [
        ("../outside.py", "outside"),
        ("/etc/passwd", "relative"),
        (".git/config", ".git"),
        ("./.git/../config", "outside"),
        ("sub/../secrets.txt", "outside"),
        (".env", "secret"),
        ("keys/id_rsa", "secret"),
    ],
)
def test_build_context_rejects_bad_extra_paths(
    repo: Path, tmp_path: Path, path: str, reason: str
) -> None:
    (tmp_path / "outside.py").write_text("x = 1\n")
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)
    with pytest.raises(ValidationError) as excinfo:
        indexer.build_context(store, record, "div", 4000, extra_paths=[path])
    assert reason in str(excinfo.value).lower()


def test_build_context_rejects_missing_file_and_symlink_escape(repo: Path, tmp_path: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    with pytest.raises(ValidationError):
        indexer.build_context(store, record, "div", 4000, extra_paths=["nope.py"])

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("AKIAexample\n")
    (repo / "link").symlink_to(outside)
    with pytest.raises(ValidationError):
        indexer.build_context(store, record, "div", 4000, extra_paths=["link/secret.txt"])


def test_build_context_is_empty_when_budget_is_small(repo: Path) -> None:
    store, record = FakeStore(), make_repo(repo)
    indexer.index_repo(store, record)
    assert indexer.build_context(store, record, "div", 3) == ""
