"""Run a repository's test command and keep only what is worth storing.

The command comes from the database (set by the user with ``skraken set-test``),
so it is split with :mod:`shlex` and executed as an argv list -- never through a
shell.  Output is stdout+stderr merged and tail-trimmed, and a hung run is
turned into exit code 124 instead of hanging the CLI.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError

__all__ = ["TestResult", "run_tests"]

#: conventional shell codes, kept so the UI can explain what happened
EXIT_TIMEOUT = 124
EXIT_MISSING_EXECUTABLE = 127
EXIT_NOT_EXECUTABLE = 126


@dataclass
class TestResult:
    """One execution of a repository's test command."""

    command: str
    exit_code: int
    duration_ms: int
    output_tail: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_tests(
    repo_path: Path,
    command: str,
    timeout: int,
    tail_chars: int = 4000,
) -> TestResult:
    """Execute *command* inside *repo_path* with a hard *timeout*.

    Returns a :class:`TestResult` instead of raising for the expected failures:
    timeout (exit 124) and a missing executable (exit 127).
    """
    if not str(command or "").strip():
        raise ValidationError("test command is empty")
    if timeout is None or int(timeout) <= 0:
        raise ValidationError("test timeout must be a positive number of seconds")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValidationError(f"test command could not be parsed: {exc}") from exc
    if not argv:
        raise ValidationError("test command is empty")

    limit = int(timeout)
    keep = max(int(tail_chars), 0)
    if not Path(repo_path).is_dir():  # repo moved/deleted since it was registered
        return TestResult(
            command=command,
            exit_code=EXIT_MISSING_EXECUTABLE,
            duration_ms=0,
            output_tail=f"[test] repository directory not found: {repo_path}",
            timed_out=False,
        )
    started = time.monotonic()
    timed_out = False
    exit_code = 0

    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv,
            shell=False,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=limit,
        )
        exit_code = proc.returncode
        output = _as_text(proc.stdout) + _as_text(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = EXIT_TIMEOUT
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
        output = (
            output
            + f"\n[test] timed out after {limit}s and was terminated: {command}"
        )
    except FileNotFoundError:
        exit_code = EXIT_MISSING_EXECUTABLE
        output = f"[test] command not found: {argv[0]}"
    except PermissionError:
        exit_code = EXIT_NOT_EXECUTABLE
        output = f"[test] not executable: {argv[0]}"
    except NotADirectoryError:
        exit_code = EXIT_MISSING_EXECUTABLE
        output = f"[test] repository directory not found: {repo_path}"
    except OSError as exc:
        exit_code = EXIT_MISSING_EXECUTABLE
        output = f"[test] could not run command: {exc}"

    duration_ms = int((time.monotonic() - started) * 1000)
    tail = output[-keep:] if keep else ""
    return TestResult(
        command=command,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output_tail=tail,
        timed_out=timed_out,
    )
