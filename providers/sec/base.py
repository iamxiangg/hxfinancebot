from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from providers.sec.models import (
    CompanyFacts,
    CompanyProfile,
    FilingDocument,
    FilingDocumentMetadata,
    FilingMetadata,
    SECInsiderTransaction,
)


@runtime_checkable
class SECProvider(Protocol):
    def company_profile(self, ticker: str) -> CompanyProfile:
        ...

    def recent_filings(
        self,
        ticker: str,
        *,
        forms: set[str] | None = None,
        filed_after: date | None = None,
    ) -> list[FilingMetadata]:
        ...

    def daily_index_filings(
        self,
        day: date,
        *,
        forms: set[str] | None = None,
    ) -> list[FilingMetadata]:
        ...

    def company_facts(
        self,
        ticker: str,
        *,
        as_of: datetime | None = None,
    ) -> CompanyFacts:
        ...

    def filing_documents(self, filing: FilingMetadata) -> list[FilingDocumentMetadata]:
        ...

    def filing_text(
        self,
        filing: FilingMetadata,
        *,
        document_name: str | None = None,
    ) -> FilingDocument:
        ...

    def form4_transactions(self, filing: FilingMetadata) -> list[SECInsiderTransaction]:
        ...

