from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class EvidenceReference:
    provider: str
    source_url: str
    accession: str = ""
    document_name: str = ""
    archive_path: str = ""
    note: str = ""


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    cik: str
    name: str
    sic: str = ""
    sic_description: str = ""
    entity_type: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class FilingMetadata:
    ticker: str
    cik: str
    accession: str
    form: str
    filed_at: datetime
    report_date: date | None
    primary_document: str
    is_amendment: bool
    source_url: str
    company_name: str = ""

    @property
    def accession_no_dashes(self) -> str:
        return self.accession.replace("-", "")


@dataclass(frozen=True)
class FilingDocumentMetadata:
    filing_accession: str
    document_name: str
    document_type: str
    sequence: str = ""
    description: str = ""
    is_primary: bool = False
    source_url: str = ""


@dataclass(frozen=True)
class FilingDocument:
    filing_accession: str
    document_name: str
    text: str
    source_url: str
    is_primary: bool = False
    content_type: str = "text/plain"


@dataclass(frozen=True)
class FinancialFact:
    concept_name: str
    original_concept: str
    value: float | int
    unit: str
    period_start: date | None
    period_end: date | None
    filed_at: datetime
    form: str
    accession: str
    fiscal_year: int | None
    fiscal_period: str
    frame: str = ""
    source_provider: str = "official"
    evidence: EvidenceReference | None = None


@dataclass(frozen=True)
class CompanyFacts:
    ticker: str
    cik: str
    facts: dict[str, list[FinancialFact]] = field(default_factory=dict)
    source_provider: str = "official"

    def all_facts(self) -> list[FinancialFact]:
        rows: list[FinancialFact] = []
        for items in self.facts.values():
            rows.extend(items)
        return rows


@dataclass(frozen=True)
class SECInsiderTransaction:
    ticker: str
    issuer_cik: str
    accession: str
    owner_cik: str
    owner_name: str
    owner_is_director: bool
    owner_is_officer: bool
    owner_is_ten_percent_owner: bool
    officer_title: str
    security_title: str
    transaction_date: date | None
    transaction_code: str
    acquired_disposed: str
    shares: float
    price_per_share: float
    shares_owned_after: float | None
    direct_or_indirect: str
    footnotes: list[str] = field(default_factory=list)
    filed_at: datetime | None = None
    report_date: date | None = None
    evidence: EvidenceReference | None = None

