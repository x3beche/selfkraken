"""Shared exception types. Every user-facing failure derives from SKError."""


class SKError(Exception):
    """Base class for expected, user-facing errors (CLI prints message, exit 1)."""


class ValidationError(SKError):
    """Untrusted input (user args, model output, import files, diffs) was rejected."""


class NotFoundError(SKError):
    """A repo, proposal or other record does not exist."""


class GitError(SKError):
    """A git command failed."""


class LLMUnavailable(SKError):
    """No model is configured (degraded mode)."""


class LLMError(SKError):
    """The configured model endpoint failed or returned unusable output."""
