"""Tests for selfkraken.config (owner A)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from selfkraken.config import (
    ALL_ENV_VARS,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_TEST_TIMEOUT,
    ENV_DATA_DIR,
    ENV_LLM_API_KEY,
    ENV_LLM_BASE_URL,
    ENV_LLM_MODEL,
    ENV_LLM_TIMEOUT,
    ENV_MAX_CONTEXT_CHARS,
    ENV_TEST_TIMEOUT,
    Settings,
    load_settings,
    redact,
)
from selfkraken.errors import ValidationError

SECRET = "sk-sup3rsecret-0123456789"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SK_* / XDG leakage from the developer's shell into these tests."""
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def test_defaults() -> None:
    settings = load_settings({}, load_dotenv_file=False)
    assert settings.data_dir == Path.home() / ".local" / "share" / "selfkraken"
    assert settings.db_path == settings.data_dir / "selfkraken.db"
    assert settings.llm_base_url is None
    assert settings.llm_api_key is None
    assert settings.llm_model is None
    assert settings.llm_enabled is False
    assert settings.llm_timeout == DEFAULT_LLM_TIMEOUT == 120.0
    assert settings.max_context_chars == DEFAULT_MAX_CONTEXT_CHARS == 12000
    assert settings.test_timeout == DEFAULT_TEST_TIMEOUT == 600


def test_xdg_data_home_is_used(tmp_path: Path) -> None:
    settings = load_settings({"XDG_DATA_HOME": str(tmp_path)}, load_dotenv_file=False)
    assert settings.data_dir == tmp_path / "selfkraken"
    assert settings.db_path == tmp_path / "selfkraken" / "selfkraken.db"


def test_data_dir_override(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    settings = load_settings({ENV_DATA_DIR: str(target)}, load_dotenv_file=False)
    assert settings.data_dir == target
    assert settings.db_path == target / "selfkraken.db"


def test_relative_data_dir_is_anchored_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings({ENV_DATA_DIR: "relative/data"}, load_dotenv_file=False)
    assert settings.data_dir == tmp_path / "relative" / "data"


def test_blank_values_count_as_unset() -> None:
    settings = load_settings(
        {ENV_LLM_MODEL: "   ", ENV_LLM_BASE_URL: "", ENV_LLM_API_KEY: "  "},
        load_dotenv_file=False,
    )
    assert settings.llm_model is None
    assert settings.llm_base_url is None
    assert settings.llm_api_key is None
    assert settings.llm_enabled is False


def test_all_overrides() -> None:
    settings = load_settings(
        {
            ENV_DATA_DIR: "/tmp/sk-data",
            ENV_LLM_BASE_URL: "http://localhost:11434/v1",
            ENV_LLM_API_KEY: SECRET,
            ENV_LLM_MODEL: "qwen-test",
            ENV_LLM_TIMEOUT: "45.5",
            ENV_MAX_CONTEXT_CHARS: "4000",
            ENV_TEST_TIMEOUT: "30",
        },
        load_dotenv_file=False,
    )
    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.llm_api_key == SECRET
    assert settings.llm_model == "qwen-test"
    assert settings.llm_timeout == 45.5
    assert settings.max_context_chars == 4000
    assert settings.test_timeout == 30
    assert settings.llm_enabled is True


@pytest.mark.parametrize(
    "env",
    [
        {ENV_LLM_BASE_URL: "http://localhost:11434/v1"},
        {ENV_LLM_MODEL: "qwen-test"},
        {},
    ],
)
def test_llm_enabled_needs_url_and_model(env: dict[str, str]) -> None:
    assert load_settings(env, load_dotenv_file=False).llm_enabled is False


def test_https_base_url_allowed() -> None:
    settings = load_settings(
        {ENV_LLM_BASE_URL: "https://example.test/v1", ENV_LLM_MODEL: "m"},
        load_dotenv_file=False,
    )
    assert settings.llm_enabled is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (ENV_LLM_TIMEOUT, "not-a-number"),
        (ENV_LLM_TIMEOUT, "0"),
        (ENV_LLM_TIMEOUT, "-3"),
        (ENV_MAX_CONTEXT_CHARS, "12.5"),
        (ENV_MAX_CONTEXT_CHARS, "abc"),
        (ENV_MAX_CONTEXT_CHARS, "0"),
        (ENV_TEST_TIMEOUT, "sixty"),
        (ENV_TEST_TIMEOUT, "-1"),
    ],
)
def test_invalid_numbers_name_variable_not_value(name: str, value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_settings({name: value}, load_dotenv_file=False)
    message = str(excinfo.value)
    assert name in message
    assert value not in message


@pytest.mark.parametrize("value", ["localhost:11434/v1", "ftp://example.test", "a b c"])
def test_invalid_base_url_names_variable_not_value(value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_settings({ENV_LLM_BASE_URL: value}, load_dotenv_file=False)
    message = str(excinfo.value)
    assert ENV_LLM_BASE_URL in message
    assert value not in message


def test_api_key_never_in_repr_or_str() -> None:
    settings = load_settings({ENV_LLM_API_KEY: SECRET}, load_dotenv_file=False)
    assert settings.llm_api_key == SECRET
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in f"{settings}"


def test_settings_are_frozen() -> None:
    settings = load_settings({}, load_dotenv_file=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.llm_model = "nope"  # type: ignore[misc]


def test_redact_replaces_key() -> None:
    settings = load_settings({ENV_LLM_API_KEY: SECRET}, load_dotenv_file=False)
    redacted = redact(f"Authorization: Bearer {SECRET} sent", settings)
    assert SECRET not in redacted
    assert "***" in redacted


def test_redact_leaves_other_text_alone() -> None:
    settings = load_settings({ENV_LLM_API_KEY: SECRET}, load_dotenv_file=False)
    assert redact("nothing secret here", settings) == "nothing secret here"


def test_redact_without_key_or_with_short_key() -> None:
    plain = load_settings({}, load_dotenv_file=False)
    assert redact("text", plain) == "text"
    short = load_settings({ENV_LLM_API_KEY: "ab"}, load_dotenv_file=False)
    assert redact("prefix ab suffix", short) == "prefix ab suffix"


def test_dotenv_file_is_loaded_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"{ENV_LLM_MODEL}=from-dotenv\n{ENV_MAX_CONTEXT_CHARS}=777\n", encoding="utf-8"
    )
    settings = load_settings()
    assert settings.llm_model == "from-dotenv"
    assert settings.max_context_chars == 777


def test_real_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{ENV_LLM_MODEL}=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv(ENV_LLM_MODEL, "real-env")
    assert load_settings().llm_model == "real-env"


def test_dotenv_skipped_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{ENV_LLM_MODEL}=from-dotenv\n", encoding="utf-8")
    assert load_settings(load_dotenv_file=False).llm_model is None


def test_dotenv_skipped_when_env_supplied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{ENV_LLM_MODEL}=from-dotenv\n", encoding="utf-8")
    assert load_settings({ENV_LLM_MODEL: "explicit"}).llm_model == "explicit"


def test_missing_dotenv_file_is_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.llm_model is None


def test_settings_type_contract(tmp_path: Path) -> None:
    settings = load_settings({ENV_DATA_DIR: str(tmp_path)}, load_dotenv_file=False)
    assert isinstance(settings, Settings)
    assert isinstance(settings.data_dir, Path)
    assert isinstance(settings.db_path, Path)
