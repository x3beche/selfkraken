"""One OpenAI-compatible chat endpoint, stdlib only.

Design notes that matter:

* the HTTP key is never part of an error message: every message built here goes
  through :meth:`OpenAICompatibleClient._sanitize`, which strips the key, bearer
  tokens, ``api_key``-looking pairs and ``user:pass@`` URL secrets;
* responses are size-capped and only the completion text is returned, so model
  output (and anything the endpoint echoes) cannot fill memory;
* :func:`make_client` returns ``None`` when no model is configured -- the rest
  of the app then runs in degraded (manual ``--diff-file``) mode.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Protocol

from .errors import LLMError, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only, module owned by agent A
    from .config import Settings

__all__ = [
    "LLMClient",
    "OpenAICompatibleClient",
    "make_client",
    "SYSTEM_PROMPT",
    "build_user_prompt",
    "split_rationale",
]

#: never slurp more than this from a completion endpoint
MAX_RESPONSE_BYTES = 4_000_000
#: rationale kept alongside a proposal is a summary, not a transcript
MAX_RATIONALE_CHARS = 2000
#: how much of an endpoint error body is echoed back into the message
_ERROR_DETAIL_CHARS = 300

SYSTEM_PROMPT = (
    "You are selfkraken, a precise coding assistant that works inside one git "
    "repository. You change code only by proposing a patch.\n"
    "\n"
    "Answer in exactly two parts:\n"
    "1. A short rationale: one paragraph of plain prose (max ~120 words) saying "
    "what you change and why. No bullet lists, no headings.\n"
    "2. Then exactly ONE fenced code block tagged ```diff containing a single "
    "unified diff in `git diff` format, and nothing after it.\n"
    "\n"
    "The diff block must be strictly valid:\n"
    "* per file: `diff --git a/<path> b/<path>`, then `--- a/<path>` and "
    "`+++ b/<path>` (`--- /dev/null` for new files, `+++ /dev/null` for deletions)\n"
    "* hunk headers `@@ -<old_start>,<old_count> +<new_start>,<new_count> @@` "
    "with line counts that exactly match the body of the hunk\n"
    "* context lines start with a space, removed lines with -, added lines with +\n"
    "* paths are relative to the repository root, with the a/ and b/ prefixes; "
    "never absolute paths, never `..`, never anything under .git/\n"
    "* base your hunks on the supplied repository context, keep the patch minimal, "
    "and do not invent files you were not shown\n"
    "\n"
    "Do not emit any other fenced code block, and do not wrap the diff in "
    "backticks of another language."
)


def build_user_prompt(request: str, context: str, repo_name: str) -> str:
    """Compose the user turn: repo name, the request, then the retrieved context."""
    parts = [
        f"Repository: {repo_name}",
        "",
        "Request:",
        str(request).strip(),
        "",
        "Relevant files from the repository:",
        context.strip() or "(no context retrieved - rely on the request alone)",
        "",
        "Reply with one short rationale paragraph and then exactly one ```diff "
        "fenced unified diff for the request above.",
    ]
    return "\n".join(parts)


def split_rationale(text: str) -> str:
    """The prose before the first code fence, stripped and capped at 2000 chars."""
    if not isinstance(text, str):
        return ""
    index = text.find("```")
    head = text if index < 0 else text[:index]
    return head.strip()[:MAX_RATIONALE_CHARS]


class LLMClient(Protocol):
    """Anything that can answer one system+user turn with text."""

    name: str

    def complete(self, system: str, user: str) -> str: ...


def _one_line(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return " ".join((text or "").split())


class OpenAICompatibleClient:
    """``POST {base_url}/chat/completions`` with a JSON chat body."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120,
    ) -> None:
        url = str(base_url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValidationError(
                "SK_LLM_BASE_URL must start with http:// or https://"
            )
        if not str(model or "").strip():
            raise ValidationError("model name is empty")
        self.base_url = url
        self.model = str(model).strip()
        self.timeout = float(timeout)
        self.temperature = 0.2
        self._api_key = str(api_key) if api_key else None

    @property
    def name(self) -> str:
        return self.model

    def __repr__(self) -> str:  # never the key
        return (
            f"OpenAICompatibleClient(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout={self.timeout!r}, "
            f"api_key={'set' if self._api_key else 'not set'})"
        )

    def _sanitize(self, text: str) -> str:
        """Collapse to one line and remove anything that could be a secret."""
        out = _one_line(text)[:_ERROR_DETAIL_CHARS]
        if self._api_key and len(self._api_key) >= 4:
            out = out.replace(self._api_key, "***")
        out = re.sub(r"(?i)bearer\s+[^\s,;]+", "bearer ***", out)
        out = re.sub(
            r"(?i)(api[_-]?key|authorization|client[_-]?secret|token)"
            r"(['\"]?)\s*[:=]\s*(['\"]?)[^\s,'\"};]+",
            r"\1\2\3***",
            out,
        )
        out = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1***:***@", out)
        return out.strip()

    def _fail(self, reason: str, detail: str = "") -> LLMError:
        message = self._sanitize(f"{reason}: {detail}" if detail else reason)
        return LLMError(message or reason)

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def complete(self, system: str, user: str) -> str:
        """Send one chat completion request and return the assistant text."""
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(), data=payload, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:  # 4xx / 5xx
            detail = ""
            try:
                detail = _one_line(exc.read(4096))
            except Exception:  # pragma: no cover - broken error body
                detail = ""
            raise self._fail(
                f"model endpoint returned HTTP {exc.code}", detail
            ) from None
        except TimeoutError as exc:
            raise self._fail(
                f"model endpoint timed out after {self.timeout:g}s"
            ) from exc
        except urllib.error.URLError as exc:
            reason = _one_line(str(getattr(exc, "reason", exc)))
            if "timed out" in reason.lower() or "timeout" in reason.lower():
                raise self._fail(
                    f"model endpoint timed out after {self.timeout:g}s"
                ) from exc
            raise self._fail("model endpoint is unreachable", reason) from exc
        except OSError as exc:  # socket reset mid-read, ...
            raise self._fail("model endpoint request failed", _one_line(str(exc))) from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise self._fail("model endpoint response is too large")

        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeError) as exc:
            raise self._fail("model endpoint returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise self._fail("model endpoint returned an unexpected JSON shape")

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._fail("model endpoint returned no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise self._fail("model endpoint returned a malformed choice")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            alternative = first.get("text")
            content = alternative if isinstance(alternative, str) else None
        if not isinstance(content, str) or not content.strip():
            raise self._fail("model endpoint returned an empty completion")
        return content


def make_client(settings: Settings) -> LLMClient | None:
    """A client for the configured model, or ``None`` in degraded mode."""
    if not settings.llm_enabled:
        return None
    base_url = getattr(settings, "llm_base_url", None)
    model = getattr(settings, "llm_model", None)
    if not base_url or not model:  # pragma: no cover - llm_enabled covers it
        return None
    return OpenAICompatibleClient(
        base_url,
        model,
        api_key=getattr(settings, "llm_api_key", None),
        timeout=float(getattr(settings, "llm_timeout", 120) or 120),
    )
