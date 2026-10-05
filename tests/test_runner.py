"""Test runner: argv execution, merged output tail, timeout and missing tool."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from selfkraken import runner
from selfkraken.errors import ValidationError


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return root


def command(*parts: str) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def test_passing_run(project: Path) -> None:
    result = runner.run_tests(
        project, command(sys.executable, "-c", "print('all good')"), 30
    )
    assert result.exit_code == 0
    assert result.passed is True
    assert result.timed_out is False
    assert "all good" in result.output_tail
    assert result.duration_ms >= 0
    assert result.command == command(sys.executable, "-c", "print('all good')")


def test_failing_run_merges_stdout_and_stderr(project: Path) -> None:
    script = "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(3)"
    result = runner.run_tests(project, command(sys.executable, "-c", script), 30)
    assert result.exit_code == 3
    assert result.passed is False
    assert result.timed_out is False
    assert "to stdout" in result.output_tail
    assert "to stderr" in result.output_tail


def test_runs_in_the_repository_directory(project: Path) -> None:
    script = "import os; print(os.getcwd())"
    result = runner.run_tests(project, command(sys.executable, "-c", script), 30)
    assert Path(result.output_tail.strip()).resolve() == project.resolve()


def test_timeout_kills_the_run(project: Path) -> None:
    result = runner.run_tests(
        project, command(sys.executable, "-c", "import time; time.sleep(30)"), 1
    )
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.passed is False
    assert result.duration_ms >= 900
    assert "timed out" in result.output_tail


def test_missing_executable_is_127(project: Path) -> None:
    result = runner.run_tests(project, "skraken-does-not-exist --version", 10)
    assert result.exit_code == 127
    assert result.passed is False
    assert "command not found" in result.output_tail
    assert "skraken-does-not-exist" in result.output_tail


def test_missing_repository_directory_is_reported(tmp_path: Path) -> None:
    result = runner.run_tests(tmp_path / "deleted-repo", "true", 5)
    assert result.exit_code == 127
    assert result.passed is False
    assert "repository directory not found" in result.output_tail


def test_output_tail_is_limited(project: Path) -> None:
    script = "for i in range(2000): print('line', i)"
    result = runner.run_tests(
        project, command(sys.executable, "-c", script), 60, tail_chars=200
    )
    assert result.exit_code == 0
    assert len(result.output_tail) <= 200
    assert "line 1999" in result.output_tail
    assert "line 0\n" not in result.output_tail


def test_quoted_arguments_survive_shlex(project: Path) -> None:
    result = runner.run_tests(
        project, command(sys.executable, "-c", "print('a b  c')"), 30
    )
    assert result.output_tail.strip() == "a b  c"


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_empty_command_is_a_validation_error(project: Path, bad: str) -> None:
    with pytest.raises(ValidationError):
        runner.run_tests(project, bad, 30)


def test_unbalanced_quotes_and_bad_timeout_are_validation_errors(project: Path) -> None:
    with pytest.raises(ValidationError):
        runner.run_tests(project, 'python -c "no closing quote', 30)
    with pytest.raises(ValidationError):
        runner.run_tests(project, "true", 0)
    with pytest.raises(ValidationError):
        runner.run_tests(project, "true", -5)


def test_testresult_passed_logic() -> None:
    ok = runner.TestResult("cmd", 0, 10, "", False)
    assert ok.passed is True
    assert ok.timed_out is False
    assert runner.TestResult("cmd", 1, 10, "", False).passed is False
    assert runner.TestResult("cmd", 0, 10, "", True).passed is False
