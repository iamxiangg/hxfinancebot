from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from providers.sec.base import SECProvider
from providers.sec.cache import JSONDiskCache
from providers.sec.errors import MissingSECUserAgentError, SECNotFoundError, SECRequestError
from providers.sec.models import (
    CompanyFacts,
    CompanyProfile,
    EvidenceReference,
    FilingDocument,
    FilingDocumentMetadata,
    FilingMetadata,
    FinancialFact,
    SECInsiderTransaction,
)
from scanners.insider.parser import find_ownership_xml_filename, parse_master_index, parse_ownership_xml


logger = logging.getLogger(__name__)

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"
TICKER_MAP_URL = f"{SEC_BASE_URL}/files/company_tickers.json"

DOCUMENT_PATTERN = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.IGNORECASE | re.DOTALL)


def _normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper().replace(".", "-")


def _normalize_cik(value: str | int) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(10)


def _normalize_accession(value: str) -> str:
    text = str(value or "").strip().replace(".txt", "").replace(".xml", "")
    if not text:
        return ""
    if "-" in text:
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 18:
        return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
    return text


def _coerce_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _coerce_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    parsed_date = _coerce_date(text)
    if parsed_date is None:
        return None
    return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)


def _to_float(value: Any) -> float | int | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _standardize_concept(taxonomy: str, concept: str) -> str:
    suffix = str(concept or "").strip()
    if not suffix:
        return ""
    namespace = str(taxonomy or "").strip().lower()
    return suffix if not namespace else f"{namespace}:{suffix}"


def _fact_sort_key(fact: FinancialFact) -> tuple[datetime, str]:
    return fact.filed_at, fact.accession


class OfficialSECProvider(SECProvider):
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        session: requests.Session | Any | None = None,
        max_requests_per_second: float | None = None,
        timeout_seconds: float | None = None,
        retry_limit: int = 2,
        cache: JSONDiskCache | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self.user_agent = str(user_agent or os.getenv("SEC_USER_AGENT", "")).strip()
        if not self.user_agent:
            raise MissingSECUserAgentError(
                "SEC_USER_AGENT is required for SEC requests. Set it to a descriptive contact string."
            )
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update({"User-Agent": self.user_agent})
        self.max_requests_per_second = max(0.1, float(max_requests_per_second or os.getenv("SEC_MAX_REQUESTS_PER_SECOND", 5)))
        self.timeout_seconds = float(timeout_seconds or os.getenv("SEC_REQUEST_TIMEOUT", 30))
        self.retry_limit = max(0, int(retry_limit))
        ttl_hours = float(os.getenv("SEC_CACHE_TTL_HOURS", 24))
        cache_root = Path(os.getenv("SEC_CACHE_DIR", "funnel_output/sec_cache"))
        self.cache = cache or JSONDiskCache(cache_root, default_ttl=timedelta(hours=ttl_hours), enabled=cache_enabled)
        self._last_request_at = 0.0

    def company_profile(self, ticker: str) -> CompanyProfile:
        normalized = _normalize_ticker(ticker)
        record = self._ticker_record(normalized)
        return CompanyProfile(
            ticker=normalized,
            cik=_normalize_cik(record.get("cik_str", "")),
            name=str(record.get("title", "")).strip(),
            source_url=TICKER_MAP_URL,
        )

    def recent_filings(
        self,
        ticker: str,
        *,
        forms: set[str] | None = None,
        filed_after: date | None = None,
    ) -> list[FilingMetadata]:
        profile = self.company_profile(ticker)
        url = f"{SEC_DATA_URL}/submissions/CIK{profile.cik}.json"
        payload = self._get_json(url, cache_key=f"submissions:{profile.cik}", immutable=False)
        recent = payload.get("filings", {}).get("recent", {})
        forms_filter = {item.upper() for item in forms} if forms else None
        count = len(recent.get("accessionNumber", []))
        rows: list[FilingMetadata] = []
        for index in range(count):
            accession = _normalize_accession(recent.get("accessionNumber", [""])[index])
            form = str(recent.get("form", [""])[index]).strip()
            filed_at = _coerce_datetime(recent.get("filingDate", [""])[index])
            if not accession or not form or filed_at is None:
                continue
            if forms_filter and form.upper() not in forms_filter:
                continue
            if filed_after is not None and filed_at.date() <= filed_after:
                continue
            report_date = _coerce_date(recent.get("reportDate", [""])[index])
            primary_document = str(recent.get("primaryDocument", [""])[index]).strip()
            rows.append(
                FilingMetadata(
                    ticker=profile.ticker,
                    cik=profile.cik,
                    accession=accession,
                    form=form,
                    filed_at=filed_at,
                    report_date=report_date,
                    primary_document=primary_document,
                    is_amendment=form.endswith("/A"),
                    source_url=self._filing_txt_url(profile.cik, accession),
                )
            )
        return rows

    def daily_index_filings(
        self,
        day: date,
        *,
        forms: set[str] | None = None,
    ) -> list[FilingMetadata]:
        forms_filter = {item.upper() for item in forms} if forms else None
        try:
            index_text = self._get_text(
                self.daily_master_index_url(day),
                cache_key=f"daily-index:{day.isoformat()}",
                immutable=True,
            )
        except SECNotFoundError as exc:
            raise FileNotFoundError(str(day)) from exc
        rows: list[FilingMetadata] = []
        for entry in parse_master_index(index_text):
            form = str(entry.form_type or "").strip().upper()
            if forms_filter and form not in forms_filter:
                continue
            accession = _normalize_accession(entry.archive_path.rsplit("/", 1)[-1])
            filed_at = _coerce_datetime(entry.date_filed) or datetime(day.year, day.month, day.day, tzinfo=UTC)
            archive_path = entry.archive_path.lstrip("/")
            rows.append(
                FilingMetadata(
                    ticker="",
                    cik=_normalize_cik(entry.cik),
                    accession=accession,
                    form=form,
                    filed_at=filed_at,
                    report_date=_coerce_date(entry.date_filed),
                    primary_document=archive_path.rsplit("/", 1)[-1],
                    is_amendment=form.endswith("/A"),
                    source_url=f"{SEC_BASE_URL}/Archives/{archive_path}",
                )
            )
        return rows

    def company_facts(
        self,
        ticker: str,
        *,
        as_of: datetime | None = None,
    ) -> CompanyFacts:
        profile = self.company_profile(ticker)
        url = f"{SEC_DATA_URL}/api/xbrl/companyfacts/CIK{profile.cik}.json"
        payload = self._get_json(url, cache_key=f"company-facts:{profile.cik}", immutable=False)
        facts_by_concept: dict[str, list[FinancialFact]] = {}
        as_of_utc = None
        if as_of is not None:
            as_of_utc = as_of.astimezone(UTC) if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        for taxonomy, concepts in payload.get("facts", {}).items():
            for original_concept, concept_payload in concepts.items():
                concept_name = _standardize_concept(taxonomy, original_concept)
                deduped: dict[tuple[Any, ...], FinancialFact] = {}
                for unit, observations in concept_payload.get("units", {}).items():
                    for item in observations:
                        filed_at = _coerce_datetime(item.get("filed"))
                        if filed_at is None:
                            continue
                        if as_of_utc is not None and filed_at > as_of_utc:
                            continue
                        value = _to_float(item.get("val"))
                        if value is None:
                            continue
                        accession = _normalize_accession(str(item.get("accn", "")))
                        fact = FinancialFact(
                            concept_name=concept_name,
                            original_concept=original_concept,
                            value=value,
                            unit=str(unit or ""),
                            period_start=_coerce_date(item.get("start")),
                            period_end=_coerce_date(item.get("end")),
                            filed_at=filed_at,
                            form=str(item.get("form", "")),
                            accession=accession,
                            fiscal_year=int(item["fy"]) if str(item.get("fy", "")).strip().isdigit() else None,
                            fiscal_period=str(item.get("fp", "")),
                            frame=str(item.get("frame", "")),
                            source_provider="official",
                            evidence=EvidenceReference(
                                provider="official",
                                source_url=url,
                                accession=accession,
                                note=concept_name,
                            ),
                        )
                        dedupe_key = (
                            fact.original_concept,
                            fact.unit,
                            fact.period_start.isoformat() if fact.period_start else "",
                            fact.period_end.isoformat() if fact.period_end else "",
                            fact.fiscal_year,
                            fact.fiscal_period,
                            fact.frame,
                        )
                        existing = deduped.get(dedupe_key)
                        if existing is None or _fact_sort_key(fact) >= _fact_sort_key(existing):
                            deduped[dedupe_key] = fact
                if deduped:
                    facts_by_concept[concept_name] = sorted(deduped.values(), key=_fact_sort_key)
        return CompanyFacts(
            ticker=profile.ticker,
            cik=profile.cik,
            facts=facts_by_concept,
            source_provider="official",
        )

    def filing_documents(self, filing: FilingMetadata) -> list[FilingDocumentMetadata]:
        filing_text = self._get_text(filing.source_url, cache_key=f"filing-index:{filing.accession}", immutable=True)
        documents: list[FilingDocumentMetadata] = []
        for block in DOCUMENT_PATTERN.findall(filing_text):
            doc_type = self._extract_tag(block, "TYPE")
            filename = self._extract_tag(block, "FILENAME")
            if not filename:
                continue
            documents.append(
                FilingDocumentMetadata(
                    filing_accession=filing.accession,
                    document_name=filename,
                    document_type=doc_type,
                    sequence=self._extract_tag(block, "SEQUENCE"),
                    description=self._extract_tag(block, "DESCRIPTION"),
                    is_primary=filename == filing.primary_document,
                    source_url=self._document_url(filing, filename),
                )
            )
        return documents

    def filing_text(
        self,
        filing: FilingMetadata,
        *,
        document_name: str | None = None,
    ) -> FilingDocument:
        target_name = str(document_name or filing.primary_document or "").strip()
        target_url = filing.source_url if not target_name else self._document_url(filing, target_name)
        try:
            text = self._get_text(
                target_url,
                cache_key=f"filing-text:{filing.accession}:{target_name or '__index__'}",
                immutable=True,
            )
        except SECNotFoundError:
            text = self._get_text(
                filing.source_url,
                cache_key=f"filing-text:{filing.accession}:__index__",
                immutable=True,
            )
            target_name = target_name or filing.primary_document or filing.source_url.rsplit("/", 1)[-1]
            target_url = filing.source_url
        return FilingDocument(
            filing_accession=filing.accession,
            document_name=target_name,
            text=text,
            source_url=target_url,
            is_primary=(target_name == filing.primary_document),
            content_type="text/plain",
        )

    def form4_transactions(self, filing: FilingMetadata) -> list[SECInsiderTransaction]:
        filing_index = self._get_text(filing.source_url, cache_key=f"form4-index:{filing.accession}", immutable=True)
        xml_name = find_ownership_xml_filename(filing_index)
        xml_document = (
            FilingDocument(
                filing_accession=filing.accession,
                document_name=filing.primary_document or filing.source_url.rsplit("/", 1)[-1],
                text=filing_index,
                source_url=filing.source_url,
                is_primary=True,
            )
            if xml_name is None and "<ownershipDocument" in filing_index
            else self.filing_text(filing, document_name=xml_name)
        )
        parsed = parse_ownership_xml(xml_document.text, accession=filing.accession)
        report_date = filing.report_date or _coerce_date(parsed.acceptance_datetime)
        rows: list[SECInsiderTransaction] = []
        for owner in parsed.reporting_owners:
            for transaction in parsed.transactions:
                rows.append(
                    SECInsiderTransaction(
                        ticker=(parsed.issuer_ticker or filing.ticker).upper(),
                        issuer_cik=parsed.issuer_cik,
                        accession=parsed.accession,
                        owner_cik=owner.cik,
                        owner_name=owner.name,
                        owner_is_director=owner.is_director,
                        owner_is_officer=owner.is_officer,
                        owner_is_ten_percent_owner=owner.is_ten_percent_owner,
                        officer_title=owner.officer_title,
                        security_title=transaction.security_title,
                        transaction_date=_coerce_date(transaction.transaction_date),
                        transaction_code=transaction.transaction_code,
                        acquired_disposed=transaction.acquired_disposed,
                        shares=float(transaction.shares or 0.0),
                        price_per_share=float(transaction.price_per_share or 0.0),
                        shares_owned_after=transaction.shares_owned_after,
                        direct_or_indirect=transaction.direct_or_indirect,
                        footnotes=list(transaction.footnotes),
                        filed_at=filing.filed_at,
                        report_date=report_date,
                        evidence=EvidenceReference(
                            provider="official",
                            source_url=xml_document.source_url,
                            accession=filing.accession,
                            document_name=xml_document.document_name,
                        ),
                    )
                )
        return rows

    def daily_master_index_url(self, day: date) -> str:
        quarter = ((day.month - 1) // 3) + 1
        return (
            f"{SEC_BASE_URL}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/master.{day.strftime('%Y%m%d')}.idx"
        )

    def _ticker_record(self, ticker: str) -> dict[str, Any]:
        payload = self._get_json(TICKER_MAP_URL, cache_key="ticker-map", immutable=False)
        for item in payload.values():
            if _normalize_ticker(str(item.get("ticker", ""))) == ticker:
                return item
        raise SECNotFoundError(f"Ticker not found in SEC mapping: {ticker}")

    def _filing_txt_url(self, cik: str, accession: str) -> str:
        cik_number = str(int(_normalize_cik(cik)))
        accession_dir = _normalize_accession(accession).replace("-", "")
        filename = f"{accession}.txt"
        return f"{SEC_BASE_URL}/Archives/edgar/data/{cik_number}/{accession_dir}/{filename}"

    def _document_url(self, filing: FilingMetadata, document_name: str) -> str:
        cik_number = str(int(_normalize_cik(filing.cik)))
        accession_dir = filing.accession_no_dashes
        return f"{SEC_BASE_URL}/Archives/edgar/data/{cik_number}/{accession_dir}/{document_name}"

    def _extract_tag(self, block: str, tag: str) -> str:
        match = re.search(rf"<{tag}>([^<]*)", block, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    def _throttle(self) -> None:
        minimum_interval = 1.0 / self.max_requests_per_second
        wait = minimum_interval - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get_json(self, url: str, *, cache_key: str, immutable: bool) -> dict[str, Any]:
        cached = self.cache.get(cache_key, ttl=None if immutable else self.cache.default_ttl)
        if cached is not None:
            return dict(cached)
        payload = self._request(url, expect_json=True)
        self.cache.set(cache_key, payload, ttl=None if immutable else self.cache.default_ttl)
        return payload

    def _get_text(self, url: str, *, cache_key: str, immutable: bool) -> str:
        cached = self.cache.get(cache_key, ttl=None if immutable else self.cache.default_ttl)
        if cached is not None:
            return str(cached)
        payload = self._request(url, expect_json=False)
        self.cache.set(cache_key, payload, ttl=None if immutable else self.cache.default_ttl)
        return str(payload)

    def _request(self, url: str, *, expect_json: bool) -> Any:
        for attempt in range(self.retry_limit + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
                self._last_request_at = time.time()
            except requests.RequestException as exc:
                if attempt >= self.retry_limit:
                    raise SECRequestError(f"SEC request failed for {url}: {exc.__class__.__name__}") from exc
                time.sleep(min(2 ** attempt, 5))
                continue
            status = int(getattr(response, "status_code", 0))
            if status == 404:
                raise SECNotFoundError(url)
            if status == 429 or 500 <= status < 600:
                if attempt >= self.retry_limit:
                    raise SECRequestError(f"SEC retryable request failed for {url}: HTTP {status}")
                time.sleep(min(2 ** attempt, 5))
                continue
            if 400 <= status < 500:
                raise SECRequestError(f"SEC request failed for {url}: HTTP {status}")
            if expect_json:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SECRequestError(f"SEC returned invalid JSON for {url}") from exc
            return str(getattr(response, "text", ""))
        raise SECRequestError(f"SEC request failed for {url}")
