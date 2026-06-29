from __future__ import annotations


class SECProviderError(RuntimeError):
    """Base class for SEC provider failures."""


class SECConfigurationError(SECProviderError):
    """Raised when SEC provider configuration is invalid."""


class MissingSECUserAgentError(SECConfigurationError):
    """Raised when SEC identity headers are missing."""


class SECRequestError(SECProviderError):
    """Raised when an SEC request fails."""


class SECNotFoundError(SECRequestError):
    """Raised when an SEC resource is not found (HTTP 404)."""


class SECAccessDeniedError(SECRequestError):
    """Raised when SEC access is denied (HTTP 403)."""


class SECProviderUnavailableError(SECProviderError):
    """Raised when an optional SEC provider backend is unavailable."""

