"""End-to-end smoke test of the whole core loop through the CLI.

One test, one happy path: register -> index -> propose (asked of a *real* HTTP
server that speaks the OpenAI chat-completions protocol) -> apply -> tests ->
commit -> log -> graph/branches/tree.  The model call is genuine network traffic,
so the HTTP client, the diff extractor, the workflow and the CLI all have to work
together for this to pass.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest

from selfkraken.cli import app
from typer.testing import CliRunner

REQUEST = "Add a sub function and a test for it"
RATIONALE = (
    "calc.py only adds numbers, so I added a sub function next to add and a "
    "matching test in test_calc.py. Nothing else changes."
)


class ModelStub:
    """Handle of a running chat-completions stub."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.requests: list[dict] = []


@pytest.fixture
def start_model_stub() -> Iterator[Callable[[str], ModelStub]]:
    """Start background OpenAI-compatible stub servers; shut them all down after."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def _start(reply: str) -> ModelStub:
        stub = ModelStub("")

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - name required by http.server
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    stub.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                except json.JSONDecodeError:
                    stub.requests.append({})
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(404, "not found")
                    return
                body = json.dumps(
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "stub-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": reply},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return  # keep pytest output clean

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        host, port = server.server_address[:2]
        stub.base_url = f"http://{host}:{port}/v1"
        return stub

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def _model_reply(diff: str) -> str:
    """What the stub answers: a rationale paragraph then ONE ```diff block."""
    return f"{RATIONALE}\n\n```diff\n{diff}```\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_full_core_loop_through_the_cli(
    runner: CliRunner,
    cli_env: Path,
    git_repo: Path,
    test_command: str,
    make_patch: Callable[[Path, dict], str],
    sources: dict,
    start_model_stub: Callable[[str], ModelStub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init -> index -> propose over HTTP -> apply + test + commit -> log (+ graph/tree)."""
    repo = git_repo
    initial_calc = (repo / "calc.py").read_text(encoding="utf-8")
    commits_before = int(_git(repo, "rev-list", "--count", "HEAD"))
    assert commits_before == 1

    # The patch the model "writes" is a real git diff, generated and rolled back.
    diff = make_patch(
        repo,
        {"calc.py": sources["calc_with_sub"], "test_calc.py": sources["test_calc_with_sub"]},
    )
    assert "diff --git a/calc.py b/calc.py" in diff
    stub = start_model_stub(_model_reply(diff))
    monkeypatch.setenv("SK_LLM_BASE_URL", stub.base_url)
    monkeypatch.setenv("SK_LLM_MODEL", "stub-model")
    monkeypatch.delenv("SK_LLM_API_KEY", raising=False)

    # 1. register the repository, with the test command to run after a change
    init = runner.invoke(app, ["init", str(repo), "--test-cmd", test_command])
    assert init.exit_code == 0, init.output
    assert "OK" in init.output

    # 2. index it so the model gets context
    index = runner.invoke(app, ["index", "--repo", str(repo)])
    assert index.exit_code == 0, index.output
    assert "OK" in index.output

    # 3. ask for a change — this hits the stub over real HTTP
    proposed = runner.invoke(app, ["propose", REQUEST, "--repo", str(repo), "--yes"])
    assert proposed.exit_code == 0, proposed.output
    assert "OK" in proposed.output
    assert "stub-model" in proposed.output
    assert "diff --git" in proposed.output
    assert "added a sub function" in proposed.output
    assert "def sub" in proposed.output

    assert len(stub.requests) == 1, stub.requests
    payload = stub.requests[0]
    assert payload.get("model") == "stub-model"
    messages = payload.get("messages") or []
    assert any(REQUEST in str(message.get("content", "")) for message in messages)
    assert any(message.get("role") == "system" for message in messages)

    listing = runner.invoke(app, ["proposals"])
    assert listing.exit_code == 0, listing.output
    assert "proposed" in listing.output
    assert REQUEST in listing.output

    # 4. apply it, run the tests, commit the result
    applied = runner.invoke(app, ["apply", "1", "--yes", "--commit"])
    assert applied.exit_code == 0, applied.output
    assert "OK" in applied.output
    assert "tests_passed" in applied.output

    # 5. the change is really on disk and really committed
    changed_calc = (repo / "calc.py").read_text(encoding="utf-8")
    assert changed_calc != initial_calc
    assert "def sub" in changed_calc
    assert "def test_sub" in (repo / "test_calc.py").read_text(encoding="utf-8")
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == commits_before + 1
    assert _git(repo, "log", "-1", "--pretty=%s").startswith("selfkraken:")
    assert _git(repo, "status", "--porcelain") == ""

    # the applied code works
    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(repo),
        shell=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert suite.returncode == 0, suite.stdout + suite.stderr

    # 6. every step of the loop is in the audit log
    log = runner.invoke(app, ["log", "--repo", str(repo), "--limit", "50"])
    assert log.exit_code == 0, log.output
    for kind in ("register", "index", "propose", "apply", "test", "commit"):
        assert kind in log.output, f"{kind} missing from:\n{log.output}"

    # 7. the visualisation commands work on the repo we just changed
    graph = runner.invoke(app, ["graph", "--repo", str(repo)])
    assert graph.exit_code == 0, graph.output

    tree = runner.invoke(app, ["tree", "--repo", str(repo), "--proposal", "1"])
    assert tree.exit_code == 0, tree.output

    branches = runner.invoke(app, ["branches", "--repo", str(repo)])
    assert branches.exit_code == 0, branches.output
