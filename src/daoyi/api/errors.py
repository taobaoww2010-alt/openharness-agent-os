"""API error types for DaoYi."""

from __future__ import annotations


class DaoYiApiError(RuntimeError):
    """Base class for upstream API failures."""


class AuthenticationFailure(DaoYiApiError):
    """Raised when the upstream service rejects the provided credentials."""


class RateLimitFailure(DaoYiApiError):
    """Raised when the upstream service rejects the request due to rate limits."""


class RequestFailure(DaoYiApiError):
    """Raised for generic request or transport failures."""
