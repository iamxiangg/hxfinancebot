from __future__ import annotations

import os

from providers.sec.base import SECProvider
from providers.sec.edgartools_provider import EdgarToolsSECProvider
from providers.sec.errors import SECAccessDeniedError, SECConfigurationError, SECNotFoundError, SECRequestError
from providers.sec.official import OfficialSECProvider


def get_sec_provider() -> SECProvider:
    provider_name = str(os.getenv("SEC_PROVIDER", "official")).strip().lower()
    if provider_name in {"", "official"}:
        return OfficialSECProvider()
    if provider_name == "edgartools":
        return EdgarToolsSECProvider()
    raise SECConfigurationError(f"Unsupported SEC provider: {provider_name}")


__all__ = ["SECProvider", "OfficialSECProvider", "EdgarToolsSECProvider", "get_sec_provider", "SECAccessDeniedError", "SECNotFoundError", "SECRequestError"]
