from __future__ import annotations

import logging
import os
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests
import yfinance as yf

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from providers.sec import get_sec_provider
from providers.sec.base import SECProvider
from providers.sec.errors import SECAccessDeniedError, SECNotFoundError, SECRequestError
from providers.sec.models import FilingMetadata, SECInsiderTransaction
from scanners.insider.parser import (
    MasterIndexEntry,
    NonDerivativeTransaction,
    ParsedOwnershipFiling,
    ReportingOwner,
    parse_ownership_xml,
)


logger = logging.getLogger(__name__)

MODEL_VERSION = "2026-06-27-insider-v2"

EXCLUDED_CODES = {"A", "C", "F", "G", "M", "W", "X"}
EXCLUDED_TEXT_TOKENS = (
    "private placement",
    "subscription agreement",
    "purchase agreement",
    "pipe",
    "registered direct",
    "financing",
    "convertible note",
    "conversion",
    "option exercise",
    "restricted stock",
    "stock award",
    "vesting",
    "tax withholding",
    "gift",
    "inheritance",
    "dividend reinvestment",
    "warrant",
    "preferred",
)
COMMON_STOCK_TOKENS = ("common", "ordinary", "class a", "class b")


@dataclass(frozen=True)
class InsiderConfig:
    enable: bool = True
    lookback_days: int = 7
    cluster_days: int = 21
    valid_days: int = 14
    history_days: int = 365
    min_price: float = 3.0
    min_median_dollar_volume: float = 5_000_000.0
    max_sec_requests_per_second: float = 5.0
    request_timeout: float = 30.0
    single_buy_min_value: float = 250_000.0
    max_results: int = 20

    @classmethod
    def from_env(cls) -> "InsiderConfig":
        return cls(
            enable=_env_bool("INSIDER_ENABLE", True),
            lookback_days=_env_int("INSIDER_LOOKBACK_DAYS", 7),
            cluster_days=_env_int("INSIDER_CLUSTER_DAYS", 21),
            valid_days=_env_int("INSIDER_VALID_DAYS", 14),
            history_days=_env_int("INSIDER_HISTORY_DAYS", 365),
            min_price=_env_float("INSIDER_MIN_PRICE", 3.0),
            min_median_dollar_volume=_env_float("INSIDER_MIN_MEDIAN_DOLLAR_VOLUME", 5_000_000.0),
            max_sec_requests_per_second=_env_float("INSIDER_MAX_SEC_REQUESTS_PER_SECOND", 5.0),
            request_timeout=_env_float("INSIDER_REQUEST_TIMEOUT", 30.0),
            single_buy_min_value=_env_float("INSIDER_SINGLE_BUY_MIN_VALUE", 250_000.0),
            max_results=_env_int("INSIDER_MAX_RESULTS", 20),
        )


@dataclass(frozen=True)
class QualifyingPurchase:
    ticker: str
    issuer_cik: str
    accession: str
    owner_cik: str
    owner_name: str
    owner_role: str
    owner_is_operating: bool
    transaction_date: date
    security_title: str
    shares: float
    price_per_share: float
    transaction_value: float
    direct_or_indirect: str
    plan_10b5_1: bool
    confidence: str
    shares_owned_after: float | None
    transaction_row_count: int
    footnotes: list[str]
    owner_is_director: bool = False
    owner_is_officer: bool = False
    owner_is_ten_percent_owner: bool = False
    officer_title: str = ""
    filing_date: date | None = None
    qualification_decision: str = "QUALIFIED"
    qualification_reason: str = ""
    observed_at: str = ""
    transaction_key: str = ""
    transaction_group_key: str = ""
    source_fingerprint: str = ""
    is_current_trigger: bool = False


@dataclass
class InsiderTickerResult:
    ticker: str
    classification: str
    total_score: float
    conviction_score: float
    commitment_score: float
    market_context_score: float
    unique_insiders: int
    operating_insiders: int
    director_count: int
    purchase_event_count: int
    transaction_row_count: int
    aggregate_purchase_value: float
    largest_individual_purchase: float
    weighted_purchase_price: float | None
    cluster_span_days: int
    insider_names: list[str]
    insider_roles: list[str]
    direct_purchase_count: int
    indirect_purchase_count: int
    plan_10b5_1_count: int
    entry_state: str
    data_confidence: str
    reason: str
    risk_flags: list[str]
    valid_for_days: int
    source_accessions: list[str]
    details: dict[str, Any] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _canonical_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _bool_text(value: bool) -> str:
    return "1" if value else "0"


def _safe_iso_date(raw: str | None) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _stable_digest(parts: list[str], *, prefix: str) -> str:
    payload = "|".join(parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def build_transaction_group_key(
    *,
    issuer_cik: str,
    owner_cik: str,
    transaction_date: date,
    security_title: str,
    direct_or_indirect: str,
) -> str:
    return _stable_digest(
        [
            str(issuer_cik or "").strip(),
            str(owner_cik or "").strip(),
            transaction_date.isoformat(),
            _canonical_text(security_title),
            str(direct_or_indirect or "").strip().upper(),
        ],
        prefix="grp",
    )


def build_transaction_key(
    *,
    issuer_cik: str,
    owner_cik: str,
    accession: str,
    transaction_date: date,
    security_title: str,
    direct_or_indirect: str,
    shares: float,
    price_per_share: float,
) -> str:
    return _stable_digest(
        [
            str(issuer_cik or "").strip(),
            str(owner_cik or "").strip(),
            str(accession or "").strip(),
            transaction_date.isoformat(),
            _canonical_text(security_title),
            str(direct_or_indirect or "").strip().upper(),
            f"{float(shares):.8f}",
            f"{float(price_per_share):.8f}",
        ],
        prefix="txn",
    )


def build_source_fingerprint(
    *,
    accession: str,
    issuer_cik: str,
    owner_cik: str,
    transaction_date: date,
    security_title: str,
    shares: float,
    price_per_share: float,
    direct_or_indirect: str,
    footnotes: list[str],
) -> str:
    return _stable_digest(
        [
            str(accession or "").strip(),
            str(issuer_cik or "").strip(),
            str(owner_cik or "").strip(),
            transaction_date.isoformat(),
            _canonical_text(security_title),
            f"{float(shares):.8f}",
            f"{float(price_per_share):.8f}",
            str(direct_or_indirect or "").strip().upper(),
            "|".join(_canonical_text(item) for item in footnotes),
        ],
        prefix="src",
    )


def _today() -> date:
    return datetime.now(UTC).date()


def _ny_business_dates_before(reference: datetime, count: int) -> list[date]:
    """Yield up to `count` US business days (Mon-Fri) before `reference` in Eastern time.

    The current day is included only if it is a business day.  Weekends are skipped
    without consuming a slot.
    """
    ny = reference.astimezone(ZoneInfo("America/New_York")) if reference.tzinfo is not None else reference
    day = ny.date()
    results: list[date] = []
    while len(results) < count:
        if day.weekday() < 5:
            results.append(day)
        day = day - timedelta(days=1)
    return results


def _median_dollar_volume(ticker: str) -> tuple[float | None, float | None, list[str]]:
    risk_flags: list[str] = []
    try:
        history = yf.download(ticker, period="6mo", auto_adjust=True, progress=False, threads=False)
    except Exception:
        return None, None, ["market_data_unavailable"]
    if history is None or history.empty or "Close" not in history or "Volume" not in history:
        return None, None, ["market_data_unavailable"]
    # ``yfinance`` can return a multi-column DataFrame for certain tickers
    # (e.g. when the exchange broadcasts via a fund wrapper).  ``iloc[-1]``
    # then produces a ``Series`` rather than a scalar, and ``float(Series)``
    # raises ``TypeError``.  ``.squeeze()`` safely reduces either a 1-element
    # ``Series`` or a (1,1)-shaped ``DataFrame`` down to a Python scalar.
    current_price = float(history["Close"].iloc[-1].squeeze())
    series = (history["Close"] * history["Volume"]).tail(30)
    if series.empty:
        return current_price, None, ["market_data_unavailable"]
    # ``series.median()`` can also return a ``Series`` (one median per column)
    # when ``history["Close"] * history["Volume"]`` is a multi-column
    # ``DataFrame`` rather than a single-column ``Series``.  Guard identically.
    return current_price, float(series.median().squeeze()), risk_flags


def owner_role(owner: ReportingOwner) -> str:
    title = owner.officer_title.strip()
    upper = title.upper()
    if owner.is_officer:
        if "CEO" in upper or "CHIEF EXECUTIVE" in upper or "FOUNDER" in upper:
            return "CEO"
        if "CFO" in upper or "CHIEF FINANCIAL" in upper:
            return "CFO"
        if "COO" in upper or "CHIEF OPERATING" in upper:
            return "COO"
        if "PRESIDENT" in upper:
            return "President"
        return title or "Officer"
    if owner.is_director:
        return "Director"
    if owner.is_ten_percent_owner:
        return "10% Owner"
    return "Other"


def owner_is_eligible(owner: ReportingOwner) -> bool:
    if owner.is_officer or owner.is_director:
        return True
    return False


def owner_is_operating(owner: ReportingOwner) -> bool:
    role = owner_role(owner).lower()
    return owner.is_officer and ("director" not in role)


def security_is_common_equity(title: str) -> bool:
    normalized = title.strip().lower()
    if not normalized:
        return False
    if not any(token in normalized for token in COMMON_STOCK_TOKENS):
        return False
    return not any(token in normalized for token in EXCLUDED_TEXT_TOKENS)


def transaction_exclusion_reason(transaction: NonDerivativeTransaction) -> str | None:
    if transaction.transaction_code in EXCLUDED_CODES:
        return f"excluded_code_{transaction.transaction_code}"
    if transaction.transaction_code != "P":
        return "unsupported_code"
    if transaction.acquired_disposed.upper() != "A":
        return "not_acquired"
    if transaction.shares <= 0 or transaction.price_per_share <= 0:
        return "invalid_shares_or_price"
    text = " | ".join([transaction.security_title] + list(transaction.footnotes)).lower()
    if any(token in text for token in EXCLUDED_TEXT_TOKENS):
        return "excluded_context"
    if not security_is_common_equity(transaction.security_title):
        return "non_common_equity"
    return None


def purchase_confidence(transaction: NonDerivativeTransaction) -> str:
    text = " | ".join([transaction.security_title] + list(transaction.footnotes)).lower()
    if any(token in text for token in ("private placement", "subscription agreement", "purchase agreement", "pipe", "registered direct")):
        return "PRIVATE_OR_FINANCING"
    if "10b5-1" in text or "10b5" in text:
        return "OPEN_MARKET_MEDIUM_CONFIDENCE"
    if transaction.direct_or_indirect.upper() == "I":
        return "OPEN_MARKET_MEDIUM_CONFIDENCE"
    return "OPEN_MARKET_HIGH_CONFIDENCE"


def _provider_owner_role(transaction: SECInsiderTransaction) -> str:
    title = transaction.officer_title.strip()
    upper = title.upper()
    if transaction.owner_is_officer:
        if "CEO" in upper or "CHIEF EXECUTIVE" in upper or "FOUNDER" in upper:
            return "CEO"
        if "CFO" in upper or "CHIEF FINANCIAL" in upper:
            return "CFO"
        if "COO" in upper or "CHIEF OPERATING" in upper:
            return "COO"
        if "PRESIDENT" in upper:
            return "President"
        return title or "Officer"
    if transaction.owner_is_director:
        return "Director"
    if transaction.owner_is_ten_percent_owner:
        return "10% Owner"
    return "Other"


def _provider_owner_is_eligible(transaction: SECInsiderTransaction) -> bool:
    return transaction.owner_is_officer or transaction.owner_is_director


def _provider_owner_is_operating(transaction: SECInsiderTransaction) -> bool:
    role = _provider_owner_role(transaction).lower()
    return transaction.owner_is_officer and ("director" not in role)


def _provider_transaction_exclusion_reason(transaction: SECInsiderTransaction) -> str | None:
    if transaction.transaction_code in EXCLUDED_CODES:
        return f"excluded_code_{transaction.transaction_code}"
    if transaction.transaction_code != "P":
        return "unsupported_code"
    if transaction.acquired_disposed.upper() != "A":
        return "not_acquired"
    if transaction.shares <= 0 or transaction.price_per_share <= 0:
        return "invalid_shares_or_price"
    text = " | ".join([transaction.security_title] + list(transaction.footnotes)).lower()
    if any(token in text for token in EXCLUDED_TEXT_TOKENS):
        return "excluded_context"
    if not security_is_common_equity(transaction.security_title):
        return "non_common_equity"
    return None


def _provider_purchase_confidence(transaction: SECInsiderTransaction) -> str:
    text = " | ".join([transaction.security_title] + list(transaction.footnotes)).lower()
    if any(token in text for token in ("private placement", "subscription agreement", "purchase agreement", "pipe", "registered direct")):
        return "PRIVATE_OR_FINANCING"
    if "10b5-1" in text or "10b5" in text:
        return "OPEN_MARKET_MEDIUM_CONFIDENCE"
    if transaction.direct_or_indirect.upper() == "I":
        return "OPEN_MARKET_MEDIUM_CONFIDENCE"
    return "OPEN_MARKET_HIGH_CONFIDENCE"


def parse_filing_purchases(
    filing: ParsedOwnershipFiling,
    *,
    filing_date: date | None = None,
    observed_at: str = "",
) -> tuple[list[QualifyingPurchase], list[dict[str, Any]]]:
    purchases: list[QualifyingPurchase] = []
    ledger_rows: list[dict[str, Any]] = []
    owners = [owner for owner in filing.reporting_owners if owner_is_eligible(owner)]
    fallback_filing_date = filing_date or _safe_iso_date(filing.acceptance_datetime) or _today()
    if not owners:
        ledger_rows.append(
            {
                "accession": filing.accession,
                "issuer_cik": filing.issuer_cik,
                "ticker": filing.issuer_ticker,
                "filing_date": fallback_filing_date.isoformat(),
                "decision": "EXCLUDED",
                "reason": "no_eligible_owner",
                "qualification_decision": "EXCLUDED",
                "qualification_reason": "no_eligible_owner",
                "observed_at": observed_at,
            }
        )
        return purchases, ledger_rows

    for owner in owners:
        role = owner_role(owner)
        for transaction in filing.transactions:
            transaction_date = _safe_iso_date(transaction.transaction_date)
            if transaction_date is None:
                ledger_rows.append(
                    {
                        "accession": filing.accession,
                        "issuer_cik": filing.issuer_cik,
                        "ticker": filing.issuer_ticker,
                        "owner_cik": owner.cik,
                        "owner_name": owner.name,
                        "owner_role": role,
                        "officer_title": owner.officer_title,
                        "filing_date": fallback_filing_date.isoformat(),
                        "transaction_date": transaction.transaction_date,
                        "security_title": transaction.security_title,
                        "shares": transaction.shares,
                        "price_per_share": transaction.price_per_share,
                        "transaction_value": transaction.shares * transaction.price_per_share,
                        "direct_or_indirect": transaction.direct_or_indirect.upper(),
                        "decision": "EXCLUDED",
                        "reason": "invalid_transaction_date",
                        "qualification_decision": "EXCLUDED",
                        "qualification_reason": "invalid_transaction_date",
                        "confidence": "",
                        "observed_at": observed_at,
                    }
                )
                continue
            transaction_value = transaction.shares * transaction.price_per_share
            reason = transaction_exclusion_reason(transaction)
            transaction_group_key = build_transaction_group_key(
                issuer_cik=filing.issuer_cik,
                owner_cik=owner.cik,
                transaction_date=transaction_date,
                security_title=transaction.security_title,
                direct_or_indirect=transaction.direct_or_indirect,
            )
            transaction_key = build_transaction_key(
                issuer_cik=filing.issuer_cik,
                owner_cik=owner.cik,
                accession=filing.accession,
                transaction_date=transaction_date,
                security_title=transaction.security_title,
                direct_or_indirect=transaction.direct_or_indirect,
                shares=transaction.shares,
                price_per_share=transaction.price_per_share,
            )
            source_fingerprint = build_source_fingerprint(
                accession=filing.accession,
                issuer_cik=filing.issuer_cik,
                owner_cik=owner.cik,
                transaction_date=transaction_date,
                security_title=transaction.security_title,
                shares=transaction.shares,
                price_per_share=transaction.price_per_share,
                direct_or_indirect=transaction.direct_or_indirect,
                footnotes=transaction.footnotes,
            )
            row_base = {
                "transaction_key": transaction_key,
                "transaction_group_key": transaction_group_key,
                "source_fingerprint": source_fingerprint,
                "accession": filing.accession,
                "issuer_cik": filing.issuer_cik,
                "ticker": filing.issuer_ticker,
                "owner_cik": owner.cik,
                "owner_name": owner.name,
                "owner_role": role,
                "officer_title": owner.officer_title,
                "owner_is_operating": owner_is_operating(owner),
                "owner_is_director": owner.is_director,
                "owner_is_officer": owner.is_officer,
                "owner_is_ten_percent_owner": owner.is_ten_percent_owner,
                "transaction_date": transaction_date.isoformat(),
                "filing_date": fallback_filing_date.isoformat(),
                "security_title": transaction.security_title,
                "shares": transaction.shares,
                "price_per_share": transaction.price_per_share,
                "transaction_value": transaction_value,
                "direct_or_indirect": transaction.direct_or_indirect.upper(),
                "plan_10b5_1": any("10b5" in footnote.lower() for footnote in transaction.footnotes),
                "shares_owned_after": transaction.shares_owned_after,
                "observed_at": observed_at,
            }
            if reason:
                ledger_rows.append(
                    {
                        **row_base,
                        "decision": "EXCLUDED",
                        "reason": reason,
                        "qualification_decision": "EXCLUDED",
                        "qualification_reason": reason,
                        "confidence": "",
                    }
                )
                continue

            confidence = purchase_confidence(transaction)
            if confidence not in {"OPEN_MARKET_HIGH_CONFIDENCE", "OPEN_MARKET_MEDIUM_CONFIDENCE"}:
                ledger_rows.append(
                    {
                        **row_base,
                        "decision": "EXCLUDED",
                        "reason": confidence.lower(),
                        "qualification_decision": "EXCLUDED",
                        "qualification_reason": confidence.lower(),
                        "confidence": confidence,
                    }
                )
                continue

            ledger_rows.append(
                {
                    **row_base,
                    "decision": "QUALIFIED",
                    "reason": role,
                    "qualification_decision": "QUALIFIED",
                    "qualification_reason": role,
                    "confidence": confidence,
                }
            )

            purchases.append(
                QualifyingPurchase(
                    ticker=filing.issuer_ticker.upper(),
                    issuer_cik=filing.issuer_cik,
                    accession=filing.accession,
                    owner_cik=owner.cik,
                    owner_name=owner.name,
                    owner_role=role,
                    owner_is_operating=owner_is_operating(owner),
                    owner_is_director=owner.is_director,
                    owner_is_officer=owner.is_officer,
                    owner_is_ten_percent_owner=owner.is_ten_percent_owner,
                    officer_title=owner.officer_title,
                    transaction_date=transaction_date,
                    filing_date=fallback_filing_date,
                    security_title=transaction.security_title,
                    shares=transaction.shares,
                    price_per_share=transaction.price_per_share,
                    transaction_value=transaction_value,
                    direct_or_indirect=transaction.direct_or_indirect.upper(),
                    plan_10b5_1=any("10b5" in footnote.lower() for footnote in transaction.footnotes),
                    confidence=confidence,
                    shares_owned_after=transaction.shares_owned_after,
                    transaction_row_count=1,
                    footnotes=transaction.footnotes,
                    qualification_decision="QUALIFIED",
                    qualification_reason=role,
                    observed_at=observed_at,
                    transaction_key=transaction_key,
                    transaction_group_key=transaction_group_key,
                    source_fingerprint=source_fingerprint,
                    is_current_trigger=True,
                )
            )
    return purchases, ledger_rows


def parse_provider_filing_purchases(
    filing: FilingMetadata,
    transactions: list[SECInsiderTransaction],
    *,
    observed_at: str = "",
) -> tuple[list[QualifyingPurchase], list[dict[str, Any]]]:
    purchases: list[QualifyingPurchase] = []
    ledger_rows: list[dict[str, Any]] = []
    fallback_filing_date = filing.report_date or filing.filed_at.date()
    eligible_transactions = [item for item in transactions if _provider_owner_is_eligible(item)]
    ticker = next((item.ticker for item in transactions if item.ticker), filing.ticker).upper()
    issuer_cik = next((item.issuer_cik for item in transactions if item.issuer_cik), filing.cik)
    if not eligible_transactions:
        ledger_rows.append(
            {
                "accession": filing.accession,
                "issuer_cik": issuer_cik,
                "ticker": ticker,
                "filing_date": fallback_filing_date.isoformat(),
                "decision": "EXCLUDED",
                "reason": "no_eligible_owner",
                "qualification_decision": "EXCLUDED",
                "qualification_reason": "no_eligible_owner",
                "observed_at": observed_at,
            }
        )
        return purchases, ledger_rows

    for transaction in eligible_transactions:
        role = _provider_owner_role(transaction)
        transaction_date = transaction.transaction_date
        if transaction_date is None:
            ledger_rows.append(
                {
                    "accession": filing.accession,
                    "issuer_cik": issuer_cik,
                    "ticker": ticker,
                    "owner_cik": transaction.owner_cik,
                    "owner_name": transaction.owner_name,
                    "owner_role": role,
                    "officer_title": transaction.officer_title,
                    "filing_date": fallback_filing_date.isoformat(),
                    "transaction_date": "",
                    "security_title": transaction.security_title,
                    "shares": transaction.shares,
                    "price_per_share": transaction.price_per_share,
                    "transaction_value": transaction.shares * transaction.price_per_share,
                    "direct_or_indirect": transaction.direct_or_indirect.upper(),
                    "decision": "EXCLUDED",
                    "reason": "invalid_transaction_date",
                    "qualification_decision": "EXCLUDED",
                    "qualification_reason": "invalid_transaction_date",
                    "confidence": "",
                    "observed_at": observed_at,
                }
            )
            continue
        transaction_value = transaction.shares * transaction.price_per_share
        reason = _provider_transaction_exclusion_reason(transaction)
        transaction_group_key = build_transaction_group_key(
            issuer_cik=issuer_cik,
            owner_cik=transaction.owner_cik,
            transaction_date=transaction_date,
            security_title=transaction.security_title,
            direct_or_indirect=transaction.direct_or_indirect,
        )
        transaction_key = build_transaction_key(
            issuer_cik=issuer_cik,
            owner_cik=transaction.owner_cik,
            accession=filing.accession,
            transaction_date=transaction_date,
            security_title=transaction.security_title,
            direct_or_indirect=transaction.direct_or_indirect,
            shares=transaction.shares,
            price_per_share=transaction.price_per_share,
        )
        source_fingerprint = build_source_fingerprint(
            accession=filing.accession,
            issuer_cik=issuer_cik,
            owner_cik=transaction.owner_cik,
            transaction_date=transaction_date,
            security_title=transaction.security_title,
            shares=transaction.shares,
            price_per_share=transaction.price_per_share,
            direct_or_indirect=transaction.direct_or_indirect,
            footnotes=transaction.footnotes,
        )
        row_base = {
            "transaction_key": transaction_key,
            "transaction_group_key": transaction_group_key,
            "source_fingerprint": source_fingerprint,
            "accession": filing.accession,
            "issuer_cik": issuer_cik,
            "ticker": ticker,
            "owner_cik": transaction.owner_cik,
            "owner_name": transaction.owner_name,
            "owner_role": role,
            "officer_title": transaction.officer_title,
            "owner_is_operating": _provider_owner_is_operating(transaction),
            "owner_is_director": transaction.owner_is_director,
            "owner_is_officer": transaction.owner_is_officer,
            "owner_is_ten_percent_owner": transaction.owner_is_ten_percent_owner,
            "transaction_date": transaction_date.isoformat(),
            "filing_date": fallback_filing_date.isoformat(),
            "security_title": transaction.security_title,
            "shares": transaction.shares,
            "price_per_share": transaction.price_per_share,
            "transaction_value": transaction_value,
            "direct_or_indirect": transaction.direct_or_indirect.upper(),
            "plan_10b5_1": any("10b5" in footnote.lower() for footnote in transaction.footnotes),
            "shares_owned_after": transaction.shares_owned_after,
            "observed_at": observed_at,
        }
        if reason:
            ledger_rows.append(
                {
                    **row_base,
                    "decision": "EXCLUDED",
                    "reason": reason,
                    "qualification_decision": "EXCLUDED",
                    "qualification_reason": reason,
                    "confidence": "",
                }
            )
            continue

        confidence = _provider_purchase_confidence(transaction)
        if confidence not in {"OPEN_MARKET_HIGH_CONFIDENCE", "OPEN_MARKET_MEDIUM_CONFIDENCE"}:
            ledger_rows.append(
                {
                    **row_base,
                    "decision": "EXCLUDED",
                    "reason": confidence.lower(),
                    "qualification_decision": "EXCLUDED",
                    "qualification_reason": confidence.lower(),
                    "confidence": confidence,
                }
            )
            continue

        ledger_rows.append(
            {
                **row_base,
                "decision": "QUALIFIED",
                "reason": role,
                "qualification_decision": "QUALIFIED",
                "qualification_reason": role,
                "confidence": confidence,
            }
        )
        purchases.append(
            QualifyingPurchase(
                ticker=ticker,
                issuer_cik=issuer_cik,
                accession=filing.accession,
                owner_cik=transaction.owner_cik,
                owner_name=transaction.owner_name,
                owner_role=role,
                owner_is_operating=_provider_owner_is_operating(transaction),
                owner_is_director=transaction.owner_is_director,
                owner_is_officer=transaction.owner_is_officer,
                owner_is_ten_percent_owner=transaction.owner_is_ten_percent_owner,
                officer_title=transaction.officer_title,
                transaction_date=transaction_date,
                filing_date=fallback_filing_date,
                security_title=transaction.security_title,
                shares=transaction.shares,
                price_per_share=transaction.price_per_share,
                transaction_value=transaction_value,
                direct_or_indirect=transaction.direct_or_indirect.upper(),
                plan_10b5_1=any("10b5" in footnote.lower() for footnote in transaction.footnotes),
                confidence=confidence,
                shares_owned_after=transaction.shares_owned_after,
                transaction_row_count=1,
                footnotes=transaction.footnotes,
                qualification_decision="QUALIFIED",
                qualification_reason=role,
                observed_at=observed_at,
                transaction_key=transaction_key,
                transaction_group_key=transaction_group_key,
                source_fingerprint=source_fingerprint,
                is_current_trigger=True,
            )
        )
    return purchases, ledger_rows


def consolidate_purchases(purchases: list[QualifyingPurchase]) -> list[QualifyingPurchase]:
    grouped: dict[tuple[Any, ...], list[QualifyingPurchase]] = {}
    for purchase in purchases:
        key = (
            purchase.issuer_cik,
            purchase.owner_cik,
            purchase.transaction_date.isoformat(),
            purchase.security_title.lower(),
            purchase.direct_or_indirect,
            purchase.accession,
        )
        grouped.setdefault(key, []).append(purchase)

    consolidated: list[QualifyingPurchase] = []
    for group in grouped.values():
        representative = group[0]
        total_shares = sum(item.shares for item in group)
        total_value = sum(item.transaction_value for item in group)
        weighted_price = total_value / total_shares if total_shares > 0 else representative.price_per_share
        consolidated.append(
            QualifyingPurchase(
                **{
                    **representative.__dict__,
                    "shares": total_shares,
                    "price_per_share": weighted_price,
                    "transaction_value": total_value,
                    "transaction_row_count": sum(item.transaction_row_count for item in group),
                    "transaction_key": build_transaction_key(
                        issuer_cik=representative.issuer_cik,
                        owner_cik=representative.owner_cik,
                        accession=representative.accession,
                        transaction_date=representative.transaction_date,
                        security_title=representative.security_title,
                        direct_or_indirect=representative.direct_or_indirect,
                        shares=total_shares,
                        price_per_share=weighted_price,
                    ),
                    "source_fingerprint": build_source_fingerprint(
                        accession=representative.accession,
                        issuer_cik=representative.issuer_cik,
                        owner_cik=representative.owner_cik,
                        transaction_date=representative.transaction_date,
                        security_title=representative.security_title,
                        shares=total_shares,
                        price_per_share=weighted_price,
                        direct_or_indirect=representative.direct_or_indirect,
                        footnotes=representative.footnotes,
                    ),
                }
            )
        )
    return consolidated


def _purchase_priority(purchase: QualifyingPurchase) -> tuple[int, str, str, int, float, float]:
    filing_text = purchase.filing_date.isoformat() if purchase.filing_date is not None else ""
    return (
        1 if purchase.qualification_decision == "QUALIFIED" else 0,
        filing_text,
        str(purchase.observed_at or ""),
        1 if purchase.is_current_trigger else 0,
        float(purchase.shares or 0.0),
        float(purchase.price_per_share or 0.0),
    )


def merge_purchase_history(
    historical: list[QualifyingPurchase],
    current: list[QualifyingPurchase],
    *,
    since: date,
) -> tuple[list[QualifyingPurchase], set[str]]:
    trigger_group_keys = {
        purchase.transaction_group_key
        for purchase in current
        if purchase.transaction_date >= since
    }
    grouped: dict[str, QualifyingPurchase] = {}
    for purchase in historical + current:
        if purchase.transaction_date < since:
            continue
        existing = grouped.get(purchase.transaction_group_key)
        if existing is None or _purchase_priority(purchase) >= _purchase_priority(existing):
            grouped[purchase.transaction_group_key] = purchase
    merged = sorted(
        grouped.values(),
        key=lambda item: (item.transaction_date, item.owner_cik, item.transaction_group_key),
    )
    return merged, trigger_group_keys


def _mapped_points(value: float, steps: list[tuple[float, float]]) -> float:
    result = 0.0
    for threshold, points in steps:
        if value >= threshold:
            result = points
    return result


def _entry_state(current_price: float | None, weighted_purchase_price: float | None) -> str:
    if current_price in (None, 0.0) or weighted_purchase_price in (None, 0.0):
        return "neutral"
    ratio = current_price / weighted_purchase_price
    if ratio >= 1.25:
        return "overextended"
    if ratio >= 1.05:
        return "trend_confirmed"
    if ratio >= 0.95:
        return "neutral"
    if ratio >= 0.85:
        return "drawdown_purchase"
    return "breakdown_risk"


def _score_purchase_window(ticker: str, clustered: list[QualifyingPurchase], config: InsiderConfig) -> InsiderTickerResult | None:
    if not clustered:
        return None

    latest_date = max(item.transaction_date for item in clustered)
    unique_owners = {item.owner_cik: item for item in clustered}
    unique_insiders = len(unique_owners)
    operating_insiders = sum(item.owner_is_operating for item in unique_owners.values())
    director_count = sum(item.owner_role == "Director" for item in unique_owners.values())
    purchase_event_count = len(clustered)
    transaction_row_count = sum(item.transaction_row_count for item in clustered)
    aggregate_purchase_value = sum(item.transaction_value for item in clustered)
    largest_individual_purchase = max(item.transaction_value for item in clustered)
    total_shares = sum(item.shares for item in clustered)
    weighted_purchase_price = aggregate_purchase_value / total_shares if total_shares > 0 else None
    cluster_span_days = (latest_date - min(item.transaction_date for item in clustered)).days
    direct_purchase_count = sum(item.direct_or_indirect == "D" for item in clustered)
    indirect_purchase_count = sum(item.direct_or_indirect == "I" for item in clustered)
    plan_count = sum(item.plan_10b5_1 for item in clustered)

    breadth_score = {1: 4.0, 2: 10.0, 3: 15.0}.get(unique_insiders, 20.0 if unique_insiders >= 4 else 0.0)
    primary_role_points = 0.0
    roles = []
    for item in unique_owners.values():
        roles.append(item.owner_role)
        role_upper = item.owner_role.upper()
        if role_upper == "CEO":
            primary_role_points += 10.0
        elif role_upper in {"CFO", "COO", "PRESIDENT"}:
            primary_role_points += 8.0
        elif role_upper == "DIRECTOR":
            primary_role_points += 4.0
        else:
            primary_role_points += 6.0 if item.owner_is_operating else 3.0
    role_strength = min(15.0, primary_role_points + min(5.0, float(len(set(roles)) - 1) * 2.0))
    recurrence = min(10.0, purchase_event_count * 2.0 + (3.0 if unique_insiders > 1 else 0.0))
    confidence_score = 5.0
    if plan_count:
        confidence_score -= 1.5
    if indirect_purchase_count and not direct_purchase_count:
        confidence_score -= 1.0
    conviction_score = round(min(50.0, breadth_score + role_strength + recurrence + max(0.0, confidence_score)), 2)

    aggregate_points = _mapped_points(
        aggregate_purchase_value,
        [(25_000.0, 3.0), (100_000.0, 5.0), (250_000.0, 7.0), (500_000.0, 9.0), (1_000_000.0, 12.0)],
    )
    largest_points = _mapped_points(
        largest_individual_purchase,
        [(10_000.0, 2.0), (50_000.0, 3.0), (100_000.0, 5.0), (250_000.0, 6.0), (500_000.0, 8.0)],
    )
    position_points_list: list[float] = []
    for item in clustered:
        if item.shares_owned_after is None:
            continue
        prior_shares = item.shares_owned_after - item.shares
        if prior_shares <= 0:
            continue
        ratio = item.shares / prior_shares
        if ratio >= 0.10:
            position_points_list.append(10.0)
        elif ratio >= 0.05:
            position_points_list.append(7.0)
        elif ratio >= 0.01:
            position_points_list.append(4.0)
        else:
            position_points_list.append(1.0)
    commitment_subtotal = aggregate_points + largest_points
    available_commitment_max = 20.0
    if position_points_list:
        commitment_subtotal += max(position_points_list)
        available_commitment_max = 30.0
    commitment_score = round((commitment_subtotal / available_commitment_max) * 30.0, 2) if available_commitment_max else 0.0

    current_price, median_dollar_volume, risk_flags = _median_dollar_volume(ticker)
    entry_state = _entry_state(current_price, weighted_purchase_price)
    price_context = {"trend_confirmed": 5.0, "neutral": 3.0, "drawdown_purchase": 4.0, "overextended": 1.0, "breakdown_risk": 0.0}.get(entry_state, 3.0)
    liquidity_points = 0.0
    if median_dollar_volume is not None:
        if median_dollar_volume >= config.min_median_dollar_volume * 3:
            liquidity_points = 5.0
        elif median_dollar_volume >= config.min_median_dollar_volume:
            liquidity_points = 4.0
        else:
            risk_flags.append("low_liquidity")
            liquidity_points = 1.0
    trend_points = 4.0 if entry_state in {"trend_confirmed", "drawdown_purchase"} else 1.0
    safety_points = 5.0
    if plan_count:
        safety_points -= 1.0
    if risk_flags:
        safety_points -= 2.0
    market_context_score = round(max(0.0, min(20.0, price_context + liquidity_points + trend_points + max(0.0, safety_points))), 2)

    total_score = round(conviction_score + commitment_score + market_context_score, 2)
    data_confidence = "high" if not plan_count and direct_purchase_count >= indirect_purchase_count else "medium"
    if risk_flags:
        data_confidence = "medium"

    qualifies_single_exception = (
        unique_insiders == 1
        and any(role in {"CEO", "CFO", "President"} for role in roles)
        and largest_individual_purchase >= config.single_buy_min_value
        and any(points >= 7.0 for points in position_points_list)
    )
    classification = "excluded"
    if (
        total_score >= 75.0
        and conviction_score >= 35.0
        and commitment_score >= 18.0
        and market_context_score >= 10.0
        and data_confidence in {"medium", "high"}
        and (unique_insiders >= 2 or qualifies_single_exception)
        and entry_state != "breakdown_risk"
    ):
        classification = "actionable"
    elif total_score >= 65.0:
        classification = "wait"
    elif total_score >= 50.0:
        classification = "near_miss"
    else:
        return None

    reason = (
        f"{unique_insiders} insider(s), ${aggregate_purchase_value:,.0f} aggregate, "
        f"score {total_score:.1f}, {entry_state.replace('_', ' ')}"
    )
    return InsiderTickerResult(
        ticker=ticker,
        classification=classification,
        total_score=total_score,
        conviction_score=conviction_score,
        commitment_score=commitment_score,
        market_context_score=market_context_score,
        unique_insiders=unique_insiders,
        operating_insiders=operating_insiders,
        director_count=director_count,
        purchase_event_count=purchase_event_count,
        transaction_row_count=transaction_row_count,
        aggregate_purchase_value=aggregate_purchase_value,
        largest_individual_purchase=largest_individual_purchase,
        weighted_purchase_price=weighted_purchase_price,
        cluster_span_days=cluster_span_days,
        insider_names=sorted({item.owner_name for item in clustered}),
        insider_roles=sorted(set(roles)),
        direct_purchase_count=direct_purchase_count,
        indirect_purchase_count=indirect_purchase_count,
        plan_10b5_1_count=plan_count,
        entry_state=entry_state,
        data_confidence=data_confidence,
        reason=reason,
        risk_flags=sorted(set(risk_flags)),
        valid_for_days=config.valid_days,
        source_accessions=sorted({item.accession for item in clustered}),
        details={
            "model_version": MODEL_VERSION,
            "aggregate_purchase_value": aggregate_purchase_value,
            "weighted_purchase_price": weighted_purchase_price,
        },
    )


def score_cluster(
    ticker: str,
    purchases: list[QualifyingPurchase],
    config: InsiderConfig,
    *,
    current_trigger_group_keys: set[str] | None = None,
) -> InsiderTickerResult | None:
    if not purchases:
        return None
    ordered = sorted(purchases, key=lambda item: (item.transaction_date, item.owner_cik, item.transaction_group_key))
    best: InsiderTickerResult | None = None
    for end_index, end_purchase in enumerate(ordered):
        window = [
            item
            for item in ordered[: end_index + 1]
            if 0 <= (end_purchase.transaction_date - item.transaction_date).days <= config.cluster_days
        ]
        if not window:
            continue
        if current_trigger_group_keys is not None and not any(
            item.transaction_group_key in current_trigger_group_keys
            for item in window
        ):
            continue
        candidate = _score_purchase_window(ticker, window, config)
        if candidate is None:
            continue
        if best is None:
            best = candidate
            continue
        candidate_rank = (
            candidate.total_score,
            candidate.conviction_score,
            candidate.commitment_score,
            candidate.market_context_score,
            candidate.cluster_span_days,
            candidate.aggregate_purchase_value,
        )
        best_rank = (
            best.total_score,
            best.conviction_score,
            best.commitment_score,
            best.market_context_score,
            best.cluster_span_days,
            best.aggregate_purchase_value,
        )
        if candidate_rank > best_rank:
            best = candidate
    return best


def run_insider_scan(
    *,
    config: InsiderConfig | None = None,
    observed_at: str | None = None,
    sec_provider: SECProvider | None = None,
    prior_accessions: set[str] | None = None,
    prior_purchases: list[QualifyingPurchase] | None = None,
) -> tuple[list[InsiderTickerResult], dict[str, Any]]:
    config = config or InsiderConfig.from_env()
    if not config.enable:
        return [], {"scanned_entries": 0, "qualifying_purchases": 0, "processed_accessions": []}
    sec_provider = sec_provider or get_sec_provider()
    observed = datetime.fromisoformat((observed_at or datetime.now(UTC).isoformat()).replace("Z", "+00:00"))
    history_since = observed.date() - timedelta(days=max(0, config.history_days))

    agent_value = str(getattr(sec_provider, "user_agent", "") or os.getenv("SEC_USER_AGENT", "")).strip()
    user_agent_configured = bool(agent_value and agent_value != "hxfinancebot/1.0 (contact@hxfinancebot.dev)")

    observed_ny = observed.astimezone(ZoneInfo("America/New_York")) if observed.tzinfo is not None else observed
    candidate_dates = _ny_business_dates_before(observed, max(1, int(config.lookback_days)))
    latest_candidate = candidate_dates[0] if candidate_dates else None

    logger.info(
        "Insider scan starting: lookback_business_days=%d history_days=%d cluster_days=%d "
        "latest_candidate_index_date=%s user_agent_configured=%s",
        config.lookback_days,
        config.history_days,
        config.cluster_days,
        latest_candidate.isoformat() if latest_candidate else "none",
        user_agent_configured,
    )

    seen_accessions = set(prior_accessions or set())
    historical_by_ticker: dict[str, list[QualifyingPurchase]] = {}
    for purchase in prior_purchases or []:
        if purchase.transaction_date >= history_since:
            historical_by_ticker.setdefault(purchase.ticker, []).append(
                QualifyingPurchase(**{**purchase.__dict__, "is_current_trigger": False})
            )
    new_purchases_by_ticker: dict[str, list[QualifyingPurchase]] = {}
    processed_accessions: set[str] = set()
    ledger_rows: list[dict[str, Any]] = []
    scanned_entries = 0
    persistent_403_count = 0
    dates_attempted = 0
    dates_loaded = 0

    for day in candidate_dates:
        dates_attempted += 1
        is_today_or_tomorrow = day >= observed_ny.date() - timedelta(days=1)
        try:
            entries = sec_provider.daily_index_filings(day, forms={"4", "4/A"})
        except FileNotFoundError:
            if day.weekday() >= 5:
                logger.debug("Insider SEC index skipped: date=%s reason=weekend", day.isoformat())
            elif is_today_or_tomorrow:
                logger.info("Insider SEC index skipped: date=%s reason=current_index_not_yet_complete", day.isoformat())
            else:
                logger.info("Insider SEC index skipped: date=%s reason=index_file_not_found", day.isoformat())
            continue
        except SECAccessDeniedError:
            persistent_403_count += 1
            if is_today_or_tomorrow:
                logger.info("Insider SEC index skipped: date=%s reason=current_index_not_complete", day.isoformat())
            else:
                logger.warning("Insider SEC index access denied: date=%s reason=access_denied", day.isoformat())
            continue
        except SECNotFoundError:
            logger.info("Insider SEC index skipped: date=%s reason=index_file_not_found", day.isoformat())
            continue
        except SECRequestError as exc:
            logger.warning("Insider SEC index request failed: date=%s error=%s", day.isoformat(), exc.__class__.__name__)
            continue

        dates_loaded += 1
        form4_count = len(entries)
        logger.info("Insider SEC index loaded: date=%s form4_entries=%d", day.isoformat(), form4_count)
        for filing_metadata in entries:
            accession = filing_metadata.accession
            if accession in seen_accessions:
                continue
            try:
                scanned_entries += 1
                transactions = sec_provider.form4_transactions(filing_metadata)
                processed_accessions.add(accession)
                parsed_purchases, parsed_ledger_rows = parse_provider_filing_purchases(
                    filing_metadata,
                    transactions,
                    observed_at=observed.replace(microsecond=0).isoformat(),
                )
                ledger_rows.extend(parsed_ledger_rows)
                for purchase in consolidate_purchases(parsed_purchases):
                    new_purchases_by_ticker.setdefault(purchase.ticker, []).append(purchase)
            except Exception as exc:
                logger.warning("Insider filing failed for %s: %r", accession, exc)
                continue

    if dates_loaded == 0 and persistent_403_count > 0:
        raise SECRequestError(
            f"Insider scan failed: persistent SEC access denial across {persistent_403_count} historical business dates. "
            "Check SEC_USER_AGENT configuration."
        )

    new_accessions = len(processed_accessions)
    qualifying_purchases = sum(len(items) for items in new_purchases_by_ticker.values())

    all_tickers = sorted(set(historical_by_ticker).union(new_purchases_by_ticker))
    merged_purchases_by_ticker: dict[str, list[QualifyingPurchase]] = {}
    trigger_group_keys_by_ticker: dict[str, set[str]] = {}
    for ticker in all_tickers:
        merged, trigger_keys = merge_purchase_history(
            historical_by_ticker.get(ticker, []),
            new_purchases_by_ticker.get(ticker, []),
            since=history_since,
        )
        if merged:
            merged_purchases_by_ticker[ticker] = merged
        if trigger_keys:
            trigger_group_keys_by_ticker[ticker] = trigger_keys

    results = [
        score_cluster(
            ticker,
            purchases,
            config,
            current_trigger_group_keys=trigger_group_keys_by_ticker.get(ticker, set()),
        )
        for ticker, purchases in merged_purchases_by_ticker.items()
        if trigger_group_keys_by_ticker.get(ticker)
    ]
    retained = [result for result in results if result is not None]
    retained.sort(key=lambda item: (item.total_score, item.conviction_score, item.commitment_score), reverse=True)

    tickers_scored = len({r.ticker for r in retained})
    signals_retained = len(retained)
    logger.info(
        "Insider engine scan: index_dates_attempted=%d index_dates_loaded=%d entries=%d "
        "new_accessions=%d qualifying_purchases=%d tickers_scored=%d signals_retained=%d",
        dates_attempted,
        dates_loaded,
        scanned_entries,
        new_accessions,
        qualifying_purchases,
        tickers_scored,
        signals_retained,
    )

    return retained[: config.max_results], {
        "scanned_entries": scanned_entries,
        "qualifying_purchases": qualifying_purchases,
        "processed_accessions": sorted(processed_accessions),
        "ledger_rows": ledger_rows,
        "dates_attempted": dates_attempted,
        "dates_loaded": dates_loaded,
        "new_accessions": new_accessions,
        "tickers_scored": tickers_scored,
    }
