"""Environment / settings loading for selfkraken.

`.env` in the current directory is loaded with python-dotenv using
``override=False`` so the real process environment always wins.  Every value is
validated; an invalid value raises :class:`~selfkraken.errors.ValidationError`
that names the *variable* but never echoes the offending value (values can be
secrets or contain them).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from .errors import ValidationError

# Environment variable names (see CONTRACT.md §Environment / settings)
ENV_DATA_DIR: Final[str] = "SK_DATA_DIR"
ENV_LLM_BASE_URL: Final[str] = "SK_LLM_BASE_URL"
ENV_LLM_API_KEY: Final[str] = "SK_LLM_API_KEY"
ENV_LLM_MODEL: Final[str] = "SK_LLM_MODEL"
ENV_LLM_TIMEOUT: Final[str] = "SK_LLM_TIMEOUT"
ENV_MAX_CONTEXT_CHARS: Final[str] = "SK_MAX_CONTEXT_CHARS"
ENV_TEST_TIMEOUT: Final[str] = "SK_TEST_TIMEOUT"

ALL_ENV_VARS: Final[tuple[str, ...]] = (
    ENV_DATA_DIR,
    ENV_LLM_BASE_URL,
    ENV_LLM_API_KEY,
    ENV_LLM_MODEL,
    ENV_LLM_TIMEOUT,
    ENV_MAX_CONTEXT_CHARS,
    ENV_TEST_TIMEOUT,
)

DEFAULT_APP_DIRNAME: Final[str] = "selfkraken"
DEFAULT_DB_FILENAME: Final[str] = "selfkraken.db"
DEFAULT_LLM_TIMEOUT: Final[float] = 120.0
DEFAULT_MAX_CONTEXT_CHARS: Final[int] = 12000
DEFAULT_TEST_TIMEOUT: Final[int] = 600

URL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
REDACTION: Final[str] = "***"
_MIN_REDACT_KEY_LEN: Final[int] = 4


@dataclass(frozen=True)
class Settings:
    """Immutable, validated view of the environment."""

    data_dir: Path
    db_path: Path
    llm_base_url: str | None
    llm_api_key: str | None = field(repr=False, default=None)
    llm_model: str | None = None
    llm_timeout: float = DEFAULT_LLM_TIMEOUT
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    test_timeout: int = DEFAULT_TEST_TIMEOUT

    @property
    def llm_enabled(self) -> bool:
        """True when both a base URL and a model name are configured."""
        return bool(self.llm_base_url) and bool(self.llm_model)


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_DATA_HOME/selfkraken`` or ``~/.local/share/selfkraken``."""
    source: Mapping[str, str] = os.environ if env is None else env
    base = (source.get("XDG_DATA_HOME") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / DEFAULT_APP_DIRNAME


def _value(source: Mapping[str, str], name: str) -> str | None:
    """Return a stripped value, or None when unset/blank."""
    raw = source.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _as_path(raw: str, name: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _as_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = _value(source, name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:  # value deliberately not echoed
        raise ValidationError(f"{name} must be a whole number") from exc
    if parsed <= 0:
        raise ValidationError(f"{name} must be greater than zero")
    return parsed


def _as_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = _value(source, name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:  # value deliberately not echoed
        raise ValidationError(f"{name} must be a number of seconds") from exc
    if parsed <= 0:
        raise ValidationError(f"{name} must be greater than zero")
    return parsed


def _as_url(source: Mapping[str, str], name: str) -> str | None:
    raw = _value(source, name)
    if raw is None:
        return None
    if any(char.isspace() for char in raw):
        raise ValidationError(f"{name} must not contain whitespace")
    if not raw.lower().startswith(URL_SCHEMES):
        raise ValidationError(f"{name} must start with http:// or https://")
    return raw


def load_settings(
    env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True
) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    A ``.env`` file in the current directory is merged only when ``env`` is
    None and ``load_dotenv_file`` is True, and never overrides real environment
    variables (``override=False``).
    """
    source: Mapping[str, str]
    if env is not None:
        source = env
    else:
        if load_dotenv_file:
            load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        source = os.environ

    data_dir_raw = _value(source, ENV_DATA_DIR)
    data_dir = _as_path(data_dir_raw, ENV_DATA_DIR) if data_dir_raw else default_data_dir(source)

    return Settings(
        data_dir=data_dir,
        db_path=data_dir / DEFAULT_DB_FILENAME,
        llm_base_url=_as_url(source, ENV_LLM_BASE_URL),
        llm_api_key=_value(source, ENV_LLM_API_KEY),
        llm_model=_value(source, ENV_LLM_MODEL),
        llm_timeout=_as_float(source, ENV_LLM_TIMEOUT, DEFAULT_LLM_TIMEOUT),
        max_context_chars=_as_int(source, ENV_MAX_CONTEXT_CHARS, DEFAULT_MAX_CONTEXT_CHARS),
        test_timeout=_as_int(source, ENV_TEST_TIMEOUT, DEFAULT_TEST_TIMEOUT),
    )


def redact(text: str, settings: Settings) -> str:
    """Replace any occurrence of the API key in ``text`` with ``***``."""
    key = settings.llm_api_key
    if not key or len(key) < _MIN_REDACT_KEY_LEN:
        return text
    return text.replace(key, REDACTION)
