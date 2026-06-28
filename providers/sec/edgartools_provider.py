from __future__ import annotations

from datetime import date, datetime
from typing import Any

from providers.sec.base import SECProvider
from providers.sec.errors import SECProviderUnavailableError
from providers.sec.models import (
    CompanyFacts,
    CompanyProfile,
    FilingDocument,
    FilingDocumentMetadata,
    FilingMetadata,
    SECInsiderTransaction,
)
from providers.sec.official import OfficialSECProvider


class EdgarToolsSECProvider(SECProvider):
    def __init__(self, **official_kwargs: Any) -> None:
        try:
            self._edgar = __import__("edgar")
        except ImportError:
            try:
                self._edgar = __import__("edgartools")
            except ImportError as exc:
                raise SECProviderUnavailableError(
                    "SEC_PROVIDER=edgartools requires the EdgarTools package to be installed."
                ) from exc
        # Keep the repository-facing contract stable by normalizing through the shared
        # official provider path until EdgarTools-specific enrichments are introduced.
        self._official = OfficialSECProvider(**official_kwargs)

    def company_profile(self, ticker: str) -> CompanyProfile:
        return self._official.company_profile(ticker)

    def recent_filings(
        self,
        ticker: str,
        *,
        forms: set[str] | None = None,
        filed_after: date | None = None,
    ) -> list[FilingMetadata]:
        return self._official.recent_filings(ticker, forms=forms, filed_after=filed_after)

    def daily_index_filings(
        self,
        day: date,
        *,
        forms: set[str] | None = None,
    ) -> list[FilingMetadata]:
        return self._official.daily_index_filings(day, forms=forms)

    def company_facts(self, ticker: str, *, as_of: datetime | None = None) -> CompanyFacts:
        return self._official.company_facts(ticker, as_of=as_of)

    def filing_documents(self, filing: FilingMetadata) -> list[FilingDocumentMetadata]:
        return self._official.filing_documents(filing)

    def filing_text(
        self,
        filing: FilingMetadata,
        *,
        document_name: str | None = None,
    ) -> FilingDocument:
        return self._official.filing_text(filing, document_name=document_name)

    def form4_transactions(self, filing: FilingMetadata) -> list[SECInsiderTransaction]:
        return self._official.form4_transactions(filing)
