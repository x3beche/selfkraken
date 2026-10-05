"""Model client tests against a local OpenAI-compatible stub (127.0.0.1)."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from selfkraken import llm
from selfkraken.errors import LLMError, ValidationError

API_KEY = "sk-TESTONLY-9d4f2a7c1b8e"
ANSWER = (
    "Guard the divisor.\n\n```diff\ndiff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def div(a, b):\n+    if b == 0: raise\n"
    "     return a // b\n```"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        return

    def _reply(self, status: int, payload: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:  # client gave up (timeout) - nothing to report
            return

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        server.requests.append(
            {
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        mode = server.mode
        if mode == "ok":
            payload = json.dumps(
                {"choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}}]}
            ).encode()
            self._reply(200, payload)
        elif mode == "leaky_error":
            # a chatty endpoint echoing the request headers back at us
            echoed = f"internal failure; saw {self.headers.get('Authorization')}"
            payload = json.dumps({"error": {"message": echoed, "type": "server_error"}}).encode()
            self._reply(500, payload)
        elif mode == "bad_json":
            self._reply(200, b"<html>definitely not json</html>")
        elif mode == "no_choices":
            self._reply(200, json.dumps({"model": "gpt-test", "usage": {}}).encode())
        elif mode == "empty_content":
            self._reply(
                200, json.dumps({"choices": [{"message": {"content": "   "}}]}).encode()
            )
        elif mode == "slow":
            time.sleep(1.0)
            self._reply(200, b"{}")
        else:  # pragma: no cover
            self._reply(404, b"{}")


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = "ok"
        self.requests: list[dict] = []


@pytest.fixture()
def server():
    stub = StubServer()
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        stub.shutdown()
        stub.server_close()
        thread.join(timeout=5)


def client_for(stub: StubServer, mode: str = "ok", *, api_key: str | None = API_KEY, timeout: float = 5.0) -> llm.OpenAICompatibleClient:
    stub.mode = mode
    port = stub.server_address[1]
    return llm.OpenAICompatibleClient(f"http://127.0.0.1:{port}/v1", "gpt-test", api_key=api_key, timeout=timeout)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------- happy path


def test_complete_posts_chat_completions_and_returns_content(server: StubServer) -> None:
    client = client_for(server)
    assert client.name == "gpt-test"
    out = client.complete("SYS", "USR")
    assert out == ANSWER

    (request,) = server.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["content_type"] == "application/json"
    assert request["auth"] == f"Bearer {API_KEY}"
    body = request["body"]
    assert body["model"] == "gpt-test"
    assert body["temperature"] == pytest.approx(0.2)
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "SYS"
    assert body["messages"][1]["content"] == "USR"


def test_no_authorization_header_without_api_key(server: StubServer) -> None:
    client = client_for(server, api_key=None)
    assert client.complete("s", "u") == ANSWER
    (request,) = server.requests
    assert request["auth"] is None


def test_base_url_trailing_slash_is_normalised(server: StubServer) -> None:
    port = server.server_address[1]
    client = llm.OpenAICompatibleClient(f"http://127.0.0.1:{port}/v1/", "m", None, 5)
    client.complete("s", "u")
    assert server.requests[-1]["path"] == "/v1/chat/completions"


# ---------------------------------------------------------------- failure modes


def test_http_500_raises_llmerror_without_leaking_the_key(server: StubServer) -> None:
    client = client_for(server, "leaky_error")
    with pytest.raises(LLMError) as excinfo:
        client.complete("s", "u")
    message = str(excinfo.value)
    assert "500" in message
    assert API_KEY not in message
    assert API_KEY.split("-")[-1] not in message
    assert "sk-TESTONLY" not in message


@pytest.mark.parametrize(
    "mode,expectation",
    [
        ("bad_json", "json"),
        ("no_choices", "choices"),
        ("empty_content", "empty"),
    ],
)
def test_unusable_responses_raise_llmerror(
    server: StubServer, mode: str, expectation: str
) -> None:
    client = client_for(server, mode)
    with pytest.raises(LLMError) as excinfo:
        client.complete("s", "u")
    assert expectation in str(excinfo.value).lower()
    assert API_KEY not in str(excinfo.value)


def test_timeout_raises_llmerror(server: StubServer) -> None:
    client = client_for(server, "slow", timeout=0.2)
    started = time.monotonic()
    with pytest.raises(LLMError) as excinfo:
        client.complete("s", "u")
    assert "timed out" in str(excinfo.value).lower()
    assert API_KEY not in str(excinfo.value)
    assert time.monotonic() - started < 5


def test_unreachable_endpoint_raises_llmerror() -> None:
    client = llm.OpenAICompatibleClient(
        f"http://127.0.0.1:{free_port()}/v1", "gpt-test", API_KEY, 2
    )
    with pytest.raises(LLMError) as excinfo:
        client.complete("s", "u")
    assert "unreachable" in str(excinfo.value).lower()
    assert API_KEY not in str(excinfo.value)


def test_client_rejects_bad_configuration() -> None:
    with pytest.raises(ValidationError):
        llm.OpenAICompatibleClient("ftp://example.invalid", "m")
    with pytest.raises(ValidationError):
        llm.OpenAICompatibleClient("http://127.0.0.1:1/v1", "  ")
    client = llm.OpenAICompatibleClient("http://127.0.0.1:1/v1", "m", API_KEY)
    assert API_KEY not in repr(client)
    assert "api_key=set" in repr(client)


# ---------------------------------------------------------------- make_client


def settings_stub(**overrides: object) -> SimpleNamespace:
    base = dict(
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="llama3.1",
        llm_api_key=API_KEY,
        llm_timeout=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_make_client_returns_none_when_unconfigured() -> None:
    assert llm.make_client(settings_stub(llm_enabled=False)) is None


def test_make_client_uses_settings() -> None:
    client = llm.make_client(settings_stub())
    assert isinstance(client, llm.OpenAICompatibleClient)
    assert client is not None
    assert client.name == "llama3.1"
    assert client.base_url == "http://127.0.0.1:11434/v1"
    assert client.timeout == 30


# ------------------------------------------------------- prompts and rationale


def test_system_prompt_demands_one_diff_block() -> None:
    prompt = llm.SYSTEM_PROMPT
    assert "```diff" in prompt
    assert "rationale" in prompt.lower()
    assert "a/" in prompt and "b/" in prompt
    assert "@@" in prompt  # hunk headers must be correct
    assert "relative" in prompt.lower()


def test_build_user_prompt_includes_everything() -> None:
    text = llm.build_user_prompt("  fix div  ", "### calc.py\n```\nx\n```", "demo-repo")
    assert "demo-repo" in text
    assert "fix div" in text
    assert "### calc.py" in text
    assert "```diff" in text


def test_build_user_prompt_without_context_still_works() -> None:
    text = llm.build_user_prompt("do it", "", "repo")
    assert "no context" in text.lower()


def test_split_rationale() -> None:
    assert llm.split_rationale(ANSWER) == "Guard the divisor."
    assert llm.split_rationale("no fences here\n") == "no fences here"
    assert llm.split_rationale("```diff\nx\n```") == ""
    long = "a" * 5000 + "\n```diff\n"
    assert len(llm.split_rationale(long)) == 2000
