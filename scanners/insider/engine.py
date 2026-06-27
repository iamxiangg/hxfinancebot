from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests
import yfinance as yf

from scanners.insider.parser import (
    MasterIndexEntry,
    NonDerivativeTransaction,
    ParsedOwnershipFiling,
    ReportingOwner,
    find_ownership_xml_filename,
    parse_master_index,
    parse_ownership_xml,
)
from scanners.insider.sec_client import SECClient


logger = logging.getLogger(__name__)

MODEL_VERSION = "2026-06-26-insider-v1"

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


def _today() -> date:
    return datetime.now(UTC).date()


def _median_dollar_volume(ticker: str) -> tuple[float | None, float | None, list[str]]:
    risk_flags: list[str] = []
    try:
        history = yf.download(ticker, period="6mo", auto_adjust=True, progress=False, threads=False)
    except Exception:
        return None, None, ["market_data_unavailable"]
    if history is None or history.empty or "Close" not in history or "Volume" not in history:
        return None, None, ["market_data_unavailable"]
    current_price = float(history["Close"].iloc[-1])
    series = (history["Close"] * history["Volume"]).tail(30)
    if series.empty:
        return current_price, None, ["market_data_unavailable"]
    return current_price, float(series.median()), risk_flags


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


def parse_filing_purchases(filing: ParsedOwnershipFiling) -> tuple[list[QualifyingPurchase], list[dict[str, Any]]]:
    purchases: list[QualifyingPurchase] = []
    ledger_rows: list[dict[str, Any]] = []
    owners = [owner for owner in filing.reporting_owners if owner_is_eligible(owner)]
    if not owners:
        ledger_rows.append(
            {
                "accession": filing.accession,
                "ticker": filing.issuer_ticker,
                "decision": "EXCLUDED",
                "reason": "no_eligible_owner",
            }
        )
        return purchases, ledger_rows

    for owner in owners:
        role = owner_role(owner)
        for transaction in filing.transactions:
            transaction_value = transaction.shares * transaction.price_per_share
            reason = transaction_exclusion_reason(transaction)
            if reason:
                ledger_rows.append(
                    {
                        "accession": filing.accession,
                        "ticker": filing.issuer_ticker,
                        "owner_cik": owner.cik,
                        "owner_name": owner.name,
                        "transaction_date": transaction.transaction_date,
                        "security_title": transaction.security_title,
                        "shares": transaction.shares,
                        "price_per_share": transaction.price_per_share,
                        "transaction_value": transaction_value,
                        "direct_or_indirect": transaction.direct_or_indirect.upper(),
                        "decision": "EXCLUDED",
                        "reason": reason,
                        "confidence": "",
                    }
                )
                continue

            confidence = purchase_confidence(transaction)
            if confidence not in {"OPEN_MARKET_HIGH_CONFIDENCE", "OPEN_MARKET_MEDIUM_CONFIDENCE"}:
                ledger_rows.append(
                    {
                        "accession": filing.accession,
                        "ticker": filing.issuer_ticker,
                        "owner_cik": owner.cik,
                        "owner_name": owner.name,
                        "transaction_date": transaction.transaction_date,
                        "security_title": transaction.security_title,
                        "shares": transaction.shares,
                        "price_per_share": transaction.price_per_share,
                        "transaction_value": transaction_value,
                        "direct_or_indirect": transaction.direct_or_indirect.upper(),
                        "decision": "EXCLUDED",
                        "reason": confidence.lower(),
                        "confidence": confidence,
                    }
                )
                continue

            ledger_rows.append(
                {
                    "accession": filing.accession,
                    "ticker": filing.issuer_ticker,
                    "owner_cik": owner.cik,
                    "owner_name": owner.name,
                    "transaction_date": transaction.transaction_date,
                    "security_title": transaction.security_title,
                    "shares": transaction.shares,
                    "price_per_share": transaction.price_per_share,
                    "transaction_value": transaction_value,
                    "direct_or_indirect": transaction.direct_or_indirect.upper(),
                    "decision": "QUALIFIED",
                    "reason": role,
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
                    transaction_date=date.fromisoformat(transaction.transaction_date[:10]),
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
                }
            )
        )
    return consolidated


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


def score_cluster(ticker: str, purchases: list[QualifyingPurchase], config: InsiderConfig) -> InsiderTickerResult | None:
    if not purchases:
        return None
    purchases = sorted(purchases, key=lambda item: (item.transaction_date, item.owner_cik))
    latest_date = max(item.transaction_date for item in purchases)
    clustered = [item for item in purchases if (latest_date - item.transaction_date).days <= config.cluster_days]
    if not clustered:
        return None

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


def run_insider_scan(
    *,
    config: InsiderConfig | None = None,
    observed_at: str | None = None,
    sec_client: SECClient | None = None,
    prior_accessions: set[str] | None = None,
) -> tuple[list[InsiderTickerResult], dict[str, Any]]:
    config = config or InsiderConfig.from_env()
    if not config.enable:
        return [], {"scanned_entries": 0, "qualifying_purchases": 0, "processed_accessions": []}
    sec_client = sec_client or SECClient(
        timeout_seconds=config.request_timeout,
        max_requests_per_second=config.max_sec_requests_per_second,
    )
    observed = datetime.fromisoformat((observed_at or datetime.now(UTC).isoformat()).replace("Z", "+00:00"))
    seen_accessions = set(prior_accessions or set())
    purchases_by_ticker: dict[str, list[QualifyingPurchase]] = {}
    processed_accessions: set[str] = set()
    ledger_rows: list[dict[str, Any]] = []
    scanned_entries = 0

    for offset in range(config.lookback_days):
        day = (observed.date() - timedelta(days=offset))
        try:
            index = sec_client.fetch_daily_master_index(day)
        except FileNotFoundError:
            continue
        entries = [entry for entry in parse_master_index(index.text) if entry.form_type in {"4", "4/A"}]
        for entry in entries:
            accession = entry.archive_path.rsplit("/", 1)[-1].replace(".txt", "")
            if accession in seen_accessions:
                continue
            scanned_entries += 1
            filing_text = sec_client.fetch_filing_text(entry.archive_path)
            xml_name = find_ownership_xml_filename(filing_text)
            xml_text = filing_text if xml_name is None and "<ownershipDocument" in filing_text else sec_client.fetch_filing_text(
                f"{entry.archive_path.rsplit('/', 1)[0]}/{xml_name}"
            )
            filing = parse_ownership_xml(xml_text, accession=accession)
            processed_accessions.add(accession)
            parsed_purchases, parsed_ledger_rows = parse_filing_purchases(filing)
            ledger_rows.extend(parsed_ledger_rows)
            for purchase in consolidate_purchases(parsed_purchases):
                purchases_by_ticker.setdefault(purchase.ticker, []).append(purchase)

    results = [
        score_cluster(ticker, purchases, config)
        for ticker, purchases in purchases_by_ticker.items()
    ]
    retained = [result for result in results if result is not None]
    retained.sort(key=lambda item: (item.total_score, item.conviction_score, item.commitment_score), reverse=True)
    return retained[: config.max_results], {
        "scanned_entries": scanned_entries,
        "qualifying_purchases": sum(len(items) for items in purchases_by_ticker.values()),
        "processed_accessions": sorted(processed_accessions),
        "ledger_rows": ledger_rows,
    }
