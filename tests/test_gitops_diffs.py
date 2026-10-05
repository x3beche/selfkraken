"""Git wrappers + diff security boundary (selfkraken/gitops.py, selfkraken/diffs.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from selfkraken import diffs, gitops
from selfkraken.errors import GitError, ValidationError

CALC = '''"""Tiny calculator."""


def add(a: int, b: int) -> int:
    return a + b


def sub(a: int, b: int) -> int:
    return a - b


def div(a: int, b: int) -> int:
    return a // b
'''

CALC_PATCHED = CALC.replace(
    "def div(a: int, b: int) -> int:\n    return a // b\n",
    "def div(a: int, b: int) -> int:\n"
    "    if b == 0:\n"
    "        raise ValueError('division by zero')\n"
    "    return a // b\n",
)

# well-formed headers and hunk counts, but the pre-image is not in the file
DOES_NOT_APPLY = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,1 +1,1 @@
-this line is nowhere in calc.py
+neither is this one
"""


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.com")
    (root / "calc.py").write_text(CALC)
    (root / "README.md").write_text("# demo repo\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def real_diff(root: Path, rel: str, new_text: str) -> str:
    """A genuine ``git diff`` for *rel*, produced without dirtying the tree."""
    target = root / rel
    original = target.read_text()
    target.write_text(new_text)
    try:
        return git(root, "diff", "--", rel).stdout
    finally:
        target.write_text(original)


# --------------------------------------------------------------------------- gitops


def test_run_git_returns_output_and_raises_on_failure(repo: Path) -> None:
    proc = gitops.run_git(repo, "rev-parse", "--is-inside-work-tree")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "true"

    quiet = gitops.run_git(repo, "status", "--nonsense-flag", check=False)
    assert quiet.returncode != 0

    with pytest.raises(GitError) as excinfo:
        gitops.run_git(repo, "status", "--nonsense-flag")
    reason = str(excinfo.value)
    assert "\n" not in reason.strip()  # first line only, never a stack of stderr
    assert reason.strip()

    with pytest.raises(GitError):
        gitops.run_git(repo, "rev-parse", "--verify", "HEAD~99")


def test_is_git_repo_and_repo_root(repo: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitops.is_git_repo(repo) is True
    assert gitops.is_git_repo(plain) is False
    assert gitops.is_git_repo(repo / "missing") is False

    nested = repo / "pkg" / "deep"
    nested.mkdir(parents=True)
    assert gitops.repo_root(nested).resolve() == repo.resolve()

    with pytest.raises(GitError):
        gitops.repo_root(plain)


def test_head_sha(repo: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert gitops.head_sha(empty) is None

    sha = gitops.head_sha(repo)
    assert sha is not None
    assert len(sha) == 40
    assert all(char in "0123456789abcdef" for char in sha)


def test_changed_paths(repo: Path) -> None:
    assert gitops.changed_paths(repo) == []
    (repo / "calc.py").write_text(CALC + "\nx = 1\n")
    (repo / "fresh.py").write_text("print('hi')\n")
    paths = gitops.changed_paths(repo)
    assert "calc.py" in paths
    assert "fresh.py" in paths
    assert paths == sorted(paths)


def test_commit_paths_returns_sha_and_is_selective(repo: Path) -> None:
    before = gitops.head_sha(repo)
    (repo / "calc.py").write_text(CALC_PATCHED)
    (repo / "README.md").write_text("# changed too\n")

    sha = gitops.commit_paths(repo, ["calc.py"], "guard div")
    assert sha and sha != before and sha == gitops.head_sha(repo)
    assert gitops.changed_paths(repo) == ["README.md"]  # left in the work tree
    body = git(repo, "log", "-1", "--pretty=%B").stdout
    assert "guard div" in body
    assert "division by zero" in git(repo, "show", "HEAD:calc.py").stdout


def test_commit_paths_rejects_unsafe_input(repo: Path) -> None:
    with pytest.raises(ValidationError):
        gitops.commit_paths(repo, ["../elsewhere.py"], "escape")
    with pytest.raises(ValidationError):
        gitops.commit_paths(repo, ["/etc/passwd"], "absolute")
    with pytest.raises(ValidationError):
        gitops.commit_paths(repo, [], "nothing")
    with pytest.raises(ValidationError):
        gitops.commit_paths(repo, ["calc.py"], "   ")


def test_commit_paths_handles_new_and_many_files(repo: Path) -> None:
    (repo / "helper.py").write_text("HELPER = 42\n")
    many = []
    for index in range(120):  # past the single-argv batch size
        rel = Path("many") / f"m{index:03d}.py"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"VALUE = {index}\n")
        many.append(rel.as_posix())

    sha = gitops.commit_paths(repo, ["helper.py", *many], "bulk add")
    assert sha == gitops.head_sha(repo)
    listing = git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert "helper.py" in listing and "many/m119.py" in listing
    assert "README.md" in listing  # earlier commit is untouched
    assert gitops.changed_paths(repo) == []


# --------------------------------------------------------------------- extract_diff


def test_extract_diff_prefers_fenced_diff_block() -> None:
    text = "Here you go:\n\n```diff\n" + DOES_NOT_APPLY + "```\n\nLet me know!"
    out = diffs.extract_diff(text)
    assert out.startswith("diff --git a/calc.py")
    assert out.endswith("\n")
    assert "Let me know" not in out
    assert "```" not in out


def test_extract_diff_accepts_patch_fence_and_unclosed_fence() -> None:
    assert diffs.extract_diff("```patch\n" + DOES_NOT_APPLY + "```\n").startswith("diff")
    # a model whose output was cut off before the closing fence still works
    assert diffs.extract_diff("```diff\n" + DOES_NOT_APPLY).startswith("diff")


def test_extract_diff_raw_text_fallbacks() -> None:
    assert diffs.extract_diff("note\n" + DOES_NOT_APPLY).startswith("diff --git")
    body = DOES_NOT_APPLY.split("\n", 1)[1]  # drop the "diff --git" line
    out = diffs.extract_diff("Some prose first.\n" + body)
    assert out.startswith("--- a/calc.py")
    assert out.endswith("\n")


def test_extract_diff_rejects_output_without_a_diff() -> None:
    with pytest.raises(ValidationError) as excinfo:
        diffs.extract_diff("I cannot help with that.")
    assert "no unified diff" in str(excinfo.value)
    with pytest.raises(ValidationError):
        diffs.extract_diff("")
    with pytest.raises(ValidationError):
        diffs.extract_diff("```python\nprint(1)\n```")


# --------------------------------------------------------------------- touched_paths


def test_touched_paths_strips_prefixes_and_ignores_dev_null() -> None:
    diff = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+print('hi')
"""
    assert diffs.touched_paths(diff) == ["new.py"]


def test_touched_paths_is_sorted_unique_and_skips_hunk_bodies() -> None:
    diff = """diff --git a/z.md b/z.md
--- a/z.md
+++ b/z.md
@@ -1,3 +1,2 @@
 # notes
--- a/not-a-header.md
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,1 @@
-x
+y
"""
    assert diffs.touched_paths(diff) == ["a.py", "z.md"]


def test_touched_paths_from_real_diff(repo: Path) -> None:
    assert diffs.touched_paths(real_diff(repo, "calc.py", CALC_PATCHED)) == ["calc.py"]


# --------------------------------------------------------------------- validate_diff


def test_validate_diff_accepts_a_real_patch(repo: Path) -> None:
    diff = real_diff(repo, "calc.py", CALC_PATCHED)
    assert diffs.validate_diff(diff, repo) == ["calc.py"]
    assert diffs.validate_diff(diff, repo / ".") == ["calc.py"]


@pytest.mark.parametrize(
    "replacement,expected",
    [
        ("b/../outside.py", "outside"),
        ("b/.git/config", ".git"),
        ("b//etc/passwd", "relative"),
        ("b/..\\..\\evil.py", "outside"),
    ],
)
def test_validate_diff_rejects_dangerous_paths(
    repo: Path, replacement: str, expected: str
) -> None:
    diff = real_diff(repo, "calc.py", CALC_PATCHED).replace("b/calc.py", replacement)
    with pytest.raises(ValidationError) as excinfo:
        diffs.validate_diff(diff, repo)
    message = str(excinfo.value)
    assert expected in message.lower()
    assert "return a + b" not in message  # no file contents in the reason


def test_validate_diff_rejects_symlink_escape(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "calc.py").write_text(CALC)
    (repo / "escape").symlink_to(outside)

    diff = real_diff(repo, "calc.py", CALC_PATCHED)
    diff = diff.replace("a/calc.py", "a/escape/calc.py").replace(
        "b/calc.py", "b/escape/calc.py"
    )
    with pytest.raises(ValidationError) as excinfo:
        diffs.validate_diff(diff, repo)
    assert "repository" in str(excinfo.value).lower()
    assert (outside / "calc.py").read_text() == CALC  # nothing was touched


def test_validate_diff_rejects_non_applying_patch(repo: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        diffs.validate_diff(DOES_NOT_APPLY, repo)
    assert "return a + b" not in str(excinfo.value)
    assert gitops.changed_paths(repo) == []


def test_validate_diff_rejects_degenerate_input(repo: Path) -> None:
    good = real_diff(repo, "calc.py", CALC_PATCHED)
    with pytest.raises(ValidationError, match="empty"):
        diffs.validate_diff("", repo)
    with pytest.raises(ValidationError):
        diffs.validate_diff("   \n  ", repo)
    with pytest.raises(ValidationError):
        diffs.validate_diff("\x00" + good, repo)
    with pytest.raises(ValidationError, match="too large"):
        diffs.validate_diff(good + ("+padding\n" * 30_000), repo)
    with pytest.raises(ValidationError):
        diffs.validate_diff("prose only, no diff here", repo)


def test_validate_diff_rejects_oversize_even_before_paths(repo: Path) -> None:
    huge = "--- a/x.py\n+++ b/x.py\n" + ("+yyyy\n" * 60_000)
    with pytest.raises(ValidationError, match="too large"):
        diffs.validate_diff(huge, repo)
    assert diffs.MAX_DIFF_BYTES == 200_000


# ------------------------------------------------------------- apply / reverse


def test_apply_and_reverse_round_trip(repo: Path) -> None:
    diff = real_diff(repo, "calc.py", CALC_PATCHED)
    assert diffs.validate_diff(diff, repo) == ["calc.py"]

    diffs.apply_diff(repo, diff)
    assert (repo / "calc.py").read_text() == CALC_PATCHED
    assert diffs.can_reverse(repo, diff) is True

    diffs.reverse_diff(repo, diff)
    assert (repo / "calc.py").read_text() == CALC
    assert diffs.can_reverse(repo, diff) is False
    assert gitops.changed_paths(repo) == []


def test_apply_and_reverse_new_file(repo: Path) -> None:
    diff = """diff --git a/helper.py b/helper.py
new file mode 100644
--- /dev/null
+++ b/helper.py
@@ -0,0 +1 @@
+HELPER = 42
"""
    assert diffs.validate_diff(diff, repo) == ["helper.py"]
    diffs.apply_diff(repo, diff)
    assert (repo / "helper.py").read_text() == "HELPER = 42\n"
    diffs.reverse_diff(repo, diff)
    assert not (repo / "helper.py").exists()


def test_apply_diff_reports_giterror(repo: Path) -> None:
    with pytest.raises(GitError):
        diffs.apply_diff(repo, DOES_NOT_APPLY)
    with pytest.raises(GitError):
        diffs.reverse_diff(repo, DOES_NOT_APPLY)
    with pytest.raises(ValidationError):
        diffs.apply_diff(repo, "")
    assert diffs.can_reverse(repo, "") is False
