from __future__ import annotations

import html
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

from providers.regulatory.base import SourceBatch
from providers.sec import get_sec_provider
from providers.sec.base import SECProvider
from providers.sec.errors import SECAccessDeniedError, SECNotFoundError, SECRequestError
from providers.sec.models import FilingMetadata
from research.regulatory.config import RegulatoryMonitorConfig
from research.regulatory.event_types import SEC_EXACT_PHRASES
from research.regulatory.identifiers import build_raw_event_id
from research.regulatory.models import RawRegulatoryRecord, SourceTier


class SECRegulatoryProvider:
    source_name = "sec"

    HEALTHCARE_NAME_HINTS = (
        "THERAPEUT",
        "PHARMA",
        "BIO",
        "MEDICAL",
        "HEALTH",
        "ONCO",
        "DIAGNOST",
        "VACCINE",
        "LIFE SCI",
        "DEVICE",
        "GENE",
        "CELL",
    )

    def __init__(self, *, config: RegulatoryMonitorConfig | None = None, sec_provider: SECProvider | None = None) -> None:
        self.config = config or RegulatoryMonitorConfig.from_env()
        self.sec = sec_provider or get_sec_provider()
        try:
            self.max_filings_per_run = max(1, int(str(os.getenv("REGULATORY_SEC_MAX_FILINGS", "40")).strip()))
        except ValueError:
            self.max_filings_per_run = 40

    def _looks_healthcare(self, company_name: str) -> bool:
        normalized = str(company_name or "").strip().upper()
        return any(hint in normalized for hint in self.HEALTHCARE_NAME_HINTS)

    def _candidate_filings(self, since: datetime, until: datetime) -> list[FilingMetadata]:
        forms = {"8-K", "6-K"}
        rows: list[FilingMetadata] = []
        day = since.date()
        while day <= until.date():
            try:
                for filing in self.sec.daily_index_filings(day, forms=forms):
                    if not self._looks_healthcare(getattr(filing, "company_name", "")):
                        continue
                    rows.append(filing)
                    if len(rows) >= self.max_filings_per_run:
                        return rows
            except FileNotFoundError:
                pass
            except (SECAccessDeniedError, SECRequestError):
                pass
            day += timedelta(days=1)
        return rows

    @staticmethod
    def _readable_text(text: str) -> bool:
        sample = str(text or "")[:12000]
        if len(sample) < 500:
            return False
        printable = sum(1 for char in sample if char.isprintable())
        alpha_or_space = sum(1 for char in sample if char.isalpha() or char.isspace())
        high_noise = sum(1 for char in sample if char in {"\\", "^", "_", "[", "]", "{", "}", "|"})
        printable_ratio = printable / max(len(sample), 1)
        alpha_ratio = alpha_or_space / max(len(sample), 1)
        noise_ratio = high_noise / max(len(sample), 1)
        return printable_ratio >= 0.95 and alpha_ratio >= 0.55 and noise_ratio <= 0.05

    @staticmethod
    def _clean_text(text: str) -> str:
        value = html.unescape(str(text or ""))
        value = value.replace("\u25aa", "- ").replace("\u2013", "- ").replace("\u2014", "- ")
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        try:
            filings = self._candidate_filings(since, until)
        except Exception as exc:
            return SourceBatch(source_status="ERROR", errors=[f"SEC daily index failed: {exc.__class__.__name__}"])
        records: list[RawRegulatoryRecord] = []
        errors: list[str] = []
        phrase_bank = tuple(phrase for phrases in SEC_EXACT_PHRASES.values() for phrase in phrases)
        for filing in filings:
            try:
                document = self.sec.filing_text(filing)
            except (SECRequestError, SECNotFoundError, SECAccessDeniedError) as exc:
                errors.append(f"{filing.accession}:{exc.__class__.__name__}")
                continue
            if not self._readable_text(document.text):
                continue
            cleaned_text = self._clean_text(document.text)
            lowered = cleaned_text.lower()
            if not any(phrase in lowered for phrase in phrase_bank):
                continue
            raw_event_id = build_raw_event_id(
                source=self.source_name,
                source_record_id=filing.accession,
                source_event_type=filing.form,
                source_publication_date=filing.filed_at.date().isoformat(),
            )
            records.append(
                RawRegulatoryRecord(
                    raw_event_id=raw_event_id,
                    source_name=self.source_name,
                    source_record_id=filing.accession,
                    source_url=document.source_url,
                    source_document_type=filing.form,
                    source_tier=SourceTier.TIER_1,
                    published_at=filing.filed_at.date().isoformat(),
                    observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    event_type="SEC_FILING",
                    ticker=filing.ticker,
                    cik=filing.cik,
                    exact_text=cleaned_text,
                    raw_payload={"form": filing.form, "accession": filing.accession, "text_preview": cleaned_text[:5000]},
                    structured_data={"phase": "", "form": filing.form},
                )
            )
        return SourceBatch(records=records, errors=errors, metadata={"record_count": len(records), "filings_scanned": len(filings)})
