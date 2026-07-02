from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from providers.yahoo_throttle import create_ticker, yahoo_call, yahoo_download
from scanners.congress.asset_intent import BROAD_MARKET_ETFS, classify_asset_intent
from scanners.congress.company_classification import CompanyClassificationProvider
from scanners.congress.models import PoliticalAuditBundle, PoliticalFiler
from scanners.congress.political_roles import CongressionalRoleProvider
from scanners.congress.role_relevance import evaluate_role_relevance


logger = logging.getLogger(__name__)


MODEL_VERSION = "2026-06-28-political-disclosures-v1"
DISPLAY_SOURCE = "political_disclosures"
RAW_KADOA_URL = (
    "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
)
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
PURCHASE_DAYS = 45
LATE_DISCLOSURE_MAX_DAYS = 120
LATE_DISCLOSURE_FILING_DAYS = 14
SALE_DAYS = 90
OPTION_MATCH_DAYS = 365
CLUSTER_DAYS = 14
ACTIONABLE_C = 70.0
ACTIONABLE_E = 60.0
WAIT_C = 70.0
RISK_C = 40.0
SEVERE_DRAWDOWN = -15.0
MAX_ACTIONABLE = 8
MAX_WAIT = 6
MAX_RISK = 6
MAX_NEAREST = 5
MAX_SALE_PENALTY = 20.0
MAX_CALL_BONUS = 10.0
MAX_PUT_PENALTY = 10.0
YF_BATCH_SIZE = 20
YF_ATTEMPTS = 2
YF_FALLBACK_LIMIT = 10
YF_TIMEOUT = 30
YF_OVERRIDES = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

SUPPORTED_CATEGORIES = {
    "actionable",
    "wait",
    "risk",
}

NON_DISCRETIONARY_TERMS = (
    "dividend reinvest",
    "automatic investment",
    "automatic purchase",
    "employee stock award",
    "employee stock grant",
    "stock award",
    "vesting",
    "vested",
    "inheritance",
    "inherited",
    "estate transfer",
    "mandatory conversion",
    "required divestment",
    "mandatory divestment",
    "corporate action",
    "transfer without consideration",
    "donation",
)

FetchPayload = Callable[[], tuple[bytes, list[dict[str, Any]]]]
PriceFetcher = Callable[[list[str], date], dict[str, dict[str, pd.Series]]]


@dataclass
class PayloadMetadata:
    source_url: str
    fetched_at: str
    record_count: int
    payload_sha256: str
    payload_bytes: int


@dataclass
class TransactionRecord:
    trade_key: str
    fingerprint: str
    source_trade_id: str
    broad_outcome: str
    reason: str
    proposed_resolution: str
    manual_review_required: bool
    ticker: str = ""
    yf_ticker: str = ""
    asset_name: str = ""
    asset_type: str = ""
    transaction_type: str = ""
    transaction_date: str = ""
    filing_date: str = ""
    transaction_age: int | None = None
    filing_age: int | None = None
    days_to_file: int | None = None
    late_filing_status: str = ""
    filer_id: str = ""
    filer_name: str = ""
    owner: str = ""
    owner_relationship: str = "unknown"
    chamber: str = ""
    branch: str = ""
    party: str = ""
    state: str = ""
    agency: str = ""
    level: str = ""
    office: str = ""
    source: str = ""
    source_id: str = ""
    filing_id: str = ""
    filing_type: str = ""
    doc_url: str = ""
    ingested_at: str = ""
    amount_range_low: float = 0.0
    amount_range_mid: float = 0.0
    amount_range_high: float = 0.0
    option_side: str = ""
    strike: float | None = None
    expiry: str = ""
    comments: str = ""
    description: str = ""
    action: str = ""
    asset_class: str = ""
    asset_intent_class: str = ""
    is_new_discovery: bool = False
    is_materially_amended: bool = False
    trigger_type: str = ""
    activity_weight: float = 0.0
    discretionary_weight: float = 1.0
    company_specific: bool = False
    bullish: bool = False
    bearish: bool = False
    broad_market: bool = False
    intentionality_score: float = 0.0
    identity_resolution_status: str = ""
    bioguide_id: str = ""
    role_relevance_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CongressTickerResult:
    ticker: str
    category: str
    conviction: float
    entry: float
    base: float
    sale_penalty: float
    call_bonus: float
    put_penalty: float
    low: float
    mid: float
    high: float
    effective: float
    active_bullish_capital: float
    historical_context_capital: float
    call_mid: float
    put_mid: float
    buyers: int
    cluster_buyers: int
    weighted_age: float
    weighted_return: float
    flow: str
    names: list[str]
    unclear_sales: int
    matched_sales: int
    matched_full_sales: int
    active_trade_count: int
    active_fresh_trade_count: int
    active_late_disclosed_trade_count: int
    signal_trigger: str
    trigger_types: list[str]
    transaction_dates: list[str]
    filing_dates: list[str]
    transaction_ages: list[int]
    filing_ages: list[int]
    alertable: bool
    weighted_average_activity_weight: float
    valid_for_days: int
    source_payload_hash: str
    model_version: str = MODEL_VERSION
    display_source: str = DISPLAY_SOURCE
    intentionality_score: float = 0.0
    materiality_score: float = 0.0
    filer_abnormality_score: float = 0.0
    accumulation_score: float = 0.0
    timeliness_score: float = 0.0
    role_relevance_score: float = 0.0
    role_relevance_status: str = ""
    role_evidence: list[dict[str, Any]] = field(default_factory=list)
    seniority_classes: list[str] = field(default_factory=list)
    seniority_multipliers: list[float] = field(default_factory=list)
    committee_ids: list[str] = field(default_factory=list)
    committee_names: list[str] = field(default_factory=list)
    subcommittee_ids: list[str] = field(default_factory=list)
    agency_keys: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    chambers: list[str] = field(default_factory=list)
    filers: list[str] = field(default_factory=list)
    bioguide_ids: list[str] = field(default_factory=list)
    company_sector: str = ""
    company_industry: str = ""
    company_thematic_exposures: list[str] = field(default_factory=list)
    company_classification_source: str = ""
    company_classification_confidence: str = ""
    asset_intent_classes: list[str] = field(default_factory=list)
    cluster_type: str = "SINGLE_FILER"
    role_confirmation: str = "ROLE_UNCONFIRMED"
    high_policy_access_flag: bool = False
    active_amount_low: float = 0.0
    active_amount_mid: float = 0.0
    active_amount_high: float = 0.0
    source_ids: list[str] = field(default_factory=list)
    filing_ids: list[str] = field(default_factory=list)
    filing_types: list[str] = field(default_factory=list)
    document_urls: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CongressScanResult:
    metadata: PayloadMetadata
    transactions: list[TransactionRecord] = field(default_factory=list)
    ticker_results: list[CongressTickerResult] = field(default_factory=list)
    review_audit: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_payload: list[dict[str, Any]] = field(default_factory=list)
    audit_bundle: PoliticalAuditBundle = field(default_factory=PoliticalAuditBundle)
    scope_used: str = "all"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": asdict(self.metadata),
            "transactions": [record.to_dict() for record in self.transactions],
            "ticker_results": [record.to_dict() for record in self.ticker_results],
            "review_audit": list(self.review_audit),
            "counts": dict(self.counts),
            "ledger": dict(self.ledger),
            "raw_payload": list(self.raw_payload),
            "audit_bundle": asdict(self.audit_bundle),
            "scope_used": self.scope_used,
        }


def today() -> date:
    return datetime.now(SINGAPORE_TZ).date()


def init_yf() -> None:
    pass


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def _date_to_text(value: date | None) -> str:
    return value.isoformat() if isinstance(value, date) else ""


def pdate(value: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def surname(name: str) -> str:
    parts = str(name or "").strip().split()
    while parts and parts[-1].lower() in {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}:
        parts.pop()
    return parts[-1] if parts else "Unknown"


def ticker_code(value: Any) -> str | None:
    ticker = str(value or "").strip().upper()
    if ticker.lower() in {"", "null", "none", "--", "n/a", "nan"}:
        return None
    return ticker if re.fullmatch(r"[A-Z0-9.^=\-]+", ticker) else None


def yf_ticker(ticker: str) -> str:
    return YF_OVERRIDES.get(ticker, ticker)


def all_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in (
            "asset_type",
            "asset_name",
            "asset_description",
            "description",
            "comment",
            "comments",
            "agency",
            "office",
            "transaction_type",
        )
    ).lower()


def action(value: Any) -> str:
    text = str(value or "").lower()
    if "purchase" in text or re.search(r"\bbuy\b", text):
        return "purchase"
    if "sale" in text and "partial" in text:
        return "sale_partial"
    if "sale" in text and "full" in text:
        return "sale_full"
    if "sale" in text or re.search(r"\bsell\b", text):
        return "sale_unknown"
    return "other"


def option_record(item: dict[str, Any]) -> bool:
    text = all_text(item)
    asset_type = str(item.get("asset_type") or "").lower()
    return "option" in asset_type or "option" in text or (
        re.search(r"\b(call|put)\b", text)
        and re.search(r"\b(strike|expiry|expiration|expires|maturity|exercise)\b", text)
    )


def stock_record(item: dict[str, Any]) -> bool:
    text = f" {all_text(item)} "
    asset_type = str(item.get("asset_type") or "").strip().lower()
    if any(
        term in text
        for term in (
            " option",
            "bond",
            "debenture",
            "treasury",
            "municipal",
            "fixed income",
            "structured note",
            " note ",
            "mutual fund",
            "exchange traded fund",
            " etf",
            " fund",
            "warrant",
            "preferred stock",
            "preferred share",
            "annuity",
            "certificate of deposit",
            "cryptocurrency",
            "crypto asset",
        )
    ):
        return False
    if asset_type in {"st", "stock", "common stock", "equity", "ordinary share", "ordinary shares"}:
        return True
    return any(
        term in text
        for term in (
            "common stock",
            "class a common",
            "class b common",
            "ordinary share",
            "american depositary share",
            "american depositary receipt",
            "depositary receipt",
            " adr ",
        )
    )


def opt_side(item: dict[str, Any]) -> str | None:
    explicit = str(item.get("option_type") or item.get("put_call") or item.get("call_put") or "").strip().lower()
    if explicit in {"call", "put"}:
        return explicit
    match = re.search(r"\b(call|put)\b", all_text(item))
    return match.group(1).lower() if match else None


def opt_strike(item: dict[str, Any]) -> float | None:
    for key in ("strike", "strike_price", "option_strike"):
        value = _finite_number(item.get(key), default=float("nan"))
        if math.isfinite(value) and value > 0:
            return value
    text = all_text(item)
    for pattern in (
        r"(?:strike(?:\s+price)?|strk)\s*[:=@\-]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:strike|call|put)\b",
        r"\b(?:call|put)\s*(?:at|@)?\s*\$\s*([0-9]+(?:\.[0-9]+)?)",
    ):
        match = re.search(pattern, text)
        if match:
            value = _finite_number(match.group(1), default=float("nan"))
            if math.isfinite(value) and value > 0:
                return value
    return None


def opt_expiry(item: dict[str, Any]) -> date | None:
    for key in ("expiration_date", "expiry_date", "option_expiry", "maturity_date"):
        parsed = pdate(item.get(key))
        if parsed:
            return parsed
    text = all_text(item)
    for pattern in (
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{4}-\d{1,2}-\d{1,2})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{1,2}-\d{1,2}-\d{2,4})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"\b(20\d{2}-\d{1,2}-\d{1,2})\b",
        r"\b(\d{1,2}/\d{1,2}/20\d{2})\b",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = pdate(match.group(1))
            if parsed:
                return parsed
    return None


def amounts(item: dict[str, Any]) -> tuple[float, float, float]:
    low = _finite_number(item.get("amount_range_low"), default=float("nan"))
    high = _finite_number(item.get("amount_range_high"), default=float("nan"))
    if not math.isfinite(low) or not math.isfinite(high) or low < 0 or high < low:
        return 0.0, 0.0, 0.0
    return low, (low + high) / 2.0, high


def low_signal_weight(record: TransactionRecord) -> float:
    text = f"{record.asset_name} {record.comments} {record.description}".lower()
    return 0.25 if any(
        term in text
        for term in (
            "inherited",
            "inheritance",
            "estate",
            "mandatory divestment",
            "required divestment",
            "issuer called",
            "called by issuer",
        )
    ) else 1.0


def option_key(record: TransactionRecord) -> tuple[Any, ...] | None:
    if record.option_side not in {"call", "put"} or record.strike is None or not record.expiry:
        return None
    return (
        record.filer_id,
        record.ticker,
        record.owner_relationship,
        record.option_side,
        round(record.strike, 4),
        record.expiry,
    )


def series(value: Any) -> pd.Series:
    if not isinstance(value, pd.Series):
        return pd.Series(dtype="float64")
    result = pd.to_numeric(value, errors="coerce").dropna()
    result.index = pd.to_datetime(result.index).tz_localize(None)
    return result.sort_index()


def session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    output = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    output.mount("https://", adapter)
    output.mount("http://", adapter)
    output.headers.update({"User-Agent": "PoliticalDisclosureScanner/1.0"})
    return output


def fetch_live_payload() -> tuple[bytes, list[dict[str, Any]]]:
    response = session().get(RAW_KADOA_URL, timeout=30)
    response.raise_for_status()
    raw = response.content
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected political disclosure payload type.")
    return raw, payload


def batch_prices(symbols: list[str], start: date) -> dict[str, dict[str, pd.Series]]:
    for attempt in range(1, YF_ATTEMPTS + 1):
        try:
            logger.info(
                "Yahoo history batch: %d tickers | attempt %d/%d",
                len(symbols),
                attempt,
                YF_ATTEMPTS,
            )
            frame = yahoo_download(
                tickers=symbols,
                start=start.isoformat(),
                end=(today() + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=True,
                actions=False,
                repair=False,
                keepna=False,
                group_by="ticker",
                threads=False,
                progress=False,
                timeout=YF_TIMEOUT,
                multi_level_index=True,
            )
            found: dict[str, dict[str, pd.Series]] = {}
            one = len(symbols) == 1
            for symbol in symbols:
                if frame is None or frame.empty:
                    continue
                if one:
                    close = series(frame.get("Close"))
                    volume = series(frame.get("Volume"))
                elif isinstance(frame.columns, pd.MultiIndex) and symbol in frame.columns.get_level_values(0):
                    close = series(frame[symbol].get("Close"))
                    volume = series(frame[symbol].get("Volume"))
                else:
                    continue
                if not close.empty:
                    found[symbol] = {"close": close, "volume": volume}
            if found:
                return found
        except Exception as exc:
            logger.warning("Yahoo batch failed: %s", exc)
    return {}


def prices(symbols: list[str], earliest: date) -> dict[str, dict[str, pd.Series]]:
    deduped = sorted(set(symbols))
    start = earliest - timedelta(days=100)
    found: dict[str, dict[str, pd.Series]] = {}
    for index in range(0, len(deduped), YF_BATCH_SIZE):
        found.update(batch_prices(deduped[index : index + YF_BATCH_SIZE], start))
    for symbol in [item for item in deduped if item not in found][:YF_FALLBACK_LIMIT]:
        try:
            history = yahoo_call(
                lambda: create_ticker(symbol).history(
                    start=start.isoformat(),
                    end=(today() + timedelta(days=1)).isoformat(),
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                    repair=False,
                    keepna=False,
                    timeout=YF_TIMEOUT,
                    raise_errors=True,
                ),
                label=f"congress-history:{symbol}",
            )
            close = series(history.get("Close"))
            volume = series(history.get("Volume"))
            if not close.empty:
                found[symbol] = {"close": close, "volume": volume}
        except Exception as exc:
            logger.warning("Yahoo fallback failed for %s: %s", symbol, exc)
    return found


def cluster_score(count: int) -> float:
    return 15.0 if count >= 5 else {4: 12.0, 3: 9.0, 2: 5.0}.get(count, 0.0)


def repeat_score(transactions: int, buyers: int) -> float:
    delta = transactions - buyers
    return 10.0 if delta >= 2 else 5.0 if delta == 1 else 0.0


def price_score(ret: float) -> float:
    if ret <= -15:
        return 5.0
    if ret <= -10:
        return 25.0
    if ret <= -5:
        return 35.0
    if ret <= 2:
        return 45.0
    if ret <= 8:
        return 35.0
    if ret <= 15:
        return 20.0
    return 5.0


def trend_score(close: pd.Series) -> float:
    current = _finite_number(close.iloc[-1], default=float("nan"))
    ma20 = _finite_number(close.tail(20).mean(), default=float("nan")) if len(close) >= 20 else None
    ma50 = _finite_number(close.tail(50).mean(), default=float("nan")) if len(close) >= 50 else None
    if not math.isfinite(current):
        return 0.0
    if ma20 is not None and ma50 is not None and math.isfinite(ma20) and math.isfinite(ma50) and current > ma20 > ma50:
        return 20.0
    if ma20 is not None and math.isfinite(ma20) and current > ma20:
        return 15.0
    if ma50 is not None and math.isfinite(ma50) and current > ma50:
        return 10.0
    return 3.0


def liquidity_score(close: pd.Series, volume: pd.Series) -> float:
    common = close.index.intersection(volume.index)
    if common.empty:
        return 4.0
    value = (close.loc[common] * volume.loc[common]).tail(20).mean()
    return 15.0 if value >= 50e6 else 12.0 if value >= 10e6 else 8.0 if value >= 2e6 else 4.0


def _normalise_datetime(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("observed_at cannot be blank.")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed


def _payload_hash(raw: bytes, payload: list[dict[str, Any]]) -> tuple[str, int]:
    if raw:
        return hashlib.sha256(raw).hexdigest(), len(raw)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _resolve_scope(branch_scope: str | None) -> str:
    scope = str(branch_scope or os.getenv("POLITICAL_DISCLOSURE_SCOPE", "all")).strip().lower()
    if scope not in {"congress_only", "executive_only", "all"}:
        raise ValueError(f"Invalid POLITICAL_DISCLOSURE_SCOPE: {scope}")
    return scope


def _trade_key(
    item: dict[str, Any],
    filer_id: str,
    ticker: str | None,
    transaction_date: date | None,
    tx_action: str,
    asset_class: str,
    low: float,
    high: float,
    owner: str,
    side: str | None,
    strike: float | None,
    expiry: date | None,
) -> tuple[str, str]:
    trade_id = str(item.get("id") or item.get("trade_id") or "").strip()
    if trade_id:
        return trade_id, f"id:{trade_id}"
    fallback = (
        filer_id,
        ticker or "",
        _date_to_text(transaction_date),
        tx_action,
        asset_class,
        f"{low:.2f}",
        f"{high:.2f}",
        owner,
        side or "",
        f"{strike:.4f}" if strike is not None else "",
        _date_to_text(expiry),
    )
    return "", "sha1:" + hashlib.sha1("|".join(fallback).encode("utf-8")).hexdigest()


def _fingerprint(item: dict[str, Any], trade_key: str) -> str:
    material = {
        "trade_key": trade_key,
        "ticker": item.get("ticker"),
        "asset_name": item.get("asset_name"),
        "asset_type": item.get("asset_type"),
        "transaction_type": item.get("transaction_type") or item.get("type"),
        "transaction_date": item.get("transaction_date"),
        "filing_date": item.get("disclosure_date") or item.get("filed_date") or item.get("filing_date"),
        "amount_range_low": item.get("amount_range_low"),
        "amount_range_high": item.get("amount_range_high"),
        "owner": item.get("owner"),
        "description": item.get("description"),
        "comment": item.get("comment"),
        "source_id": item.get("source_id") or item.get("source"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _branch_from_item(item: dict[str, Any]) -> str:
    branch_text = " ".join(
        str(item.get(key) or "")
        for key in ("branch", "chamber", "source", "filing_type")
    ).lower()
    executive_text = " ".join(
        str(item.get(key) or "")
        for key in ("agency", "office", "filer_title", "branch", "source", "filing_type")
    ).lower()
    if any(term in executive_text for term in ("white house", "executive", "office of government ethics", "cabinet", "278-t")):
        return "executive"
    if any(term in branch_text for term in ("house", "senate", "congress", "legislative")):
        return "congress"
    return "unknown"


def _chamber_from_item(item: dict[str, Any]) -> str:
    chamber = str(item.get("chamber") or "").strip().lower()
    if chamber in {"house", "senate"}:
        return chamber
    text = " ".join(str(item.get(key) or "") for key in ("branch", "source", "filer_title")).lower()
    if "house" in text:
        return "house"
    if "senate" in text:
        return "senate"
    return ""


def _normalise_owner(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"self", "s", "owner"}:
        return "self"
    if text in {"sp", "spouse"}:
        return "spouse"
    if text in {"dc", "dependent", "child"}:
        return "dependent"
    if text in {"jt", "joint"}:
        return "joint"
    return "unknown"


def _in_scope(branch: str, scope: str) -> bool:
    if scope == "all":
        return branch in {"congress", "executive", "unknown"}
    if scope == "congress_only":
        return branch == "congress"
    return branch == "executive"


def _non_discretionary_reason(item: dict[str, Any]) -> str | None:
    text = all_text(item)
    for term in NON_DISCRETIONARY_TERMS:
        if term in text:
            return term
    return None


def _late_disclosure_weight(transaction_age: int) -> float:
    if transaction_age <= 90:
        return 0.35
    if transaction_age <= 120:
        return 0.15
    return 0.0


def _remaining_valid_days(record: TransactionRecord) -> int:
    if record.reason == "ACTIVE_FRESH" and record.transaction_age is not None:
        return max(1, PURCHASE_DAYS - record.transaction_age)
    if record.reason == "ACTIVE_LATE_DISCLOSED":
        tx_left = max(1, LATE_DISCLOSURE_MAX_DAYS - int(record.transaction_age or 0))
        filing_left = max(1, LATE_DISCLOSURE_FILING_DAYS - int(record.filing_age or 0))
        return max(1, min(tx_left, filing_left))
    return 3


def _resolved_public_security(item: dict[str, Any]) -> bool:
    return stock_record(item) or "common stock" in all_text(item)


def _scaled_record(record: TransactionRecord) -> TransactionRecord:
    scale = record.activity_weight * record.discretionary_weight
    clone = TransactionRecord(**record.to_dict())
    clone.amount_range_low *= scale
    clone.amount_range_mid *= scale
    clone.amount_range_high *= scale
    return clone


def _effective_amount(low: float, mid: float) -> float:
    return 0.6 * mid + 0.4 * low


def _transaction_freshness_component(record: TransactionRecord, maximum: float) -> float:
    if record.reason == "ACTIVE_FRESH" and record.transaction_age is not None:
        return maximum * max(0.0, 1.0 - float(record.transaction_age) / PURCHASE_DAYS)
    return maximum * record.activity_weight


def _weighted_average(values: Iterable[tuple[float, float]]) -> float:
    pairs = [(value, weight) for value, weight in values if weight > 0]
    if not pairs:
        return 0.0
    total_weight = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / total_weight


def _flow_label(*, cluster_type: str, asset_intents: set[str], strong_distribution: bool, role_score: float) -> str:
    if strong_distribution:
        return "Distribution signal"
    if "BROAD_MARKET_ETF" in asset_intents:
        return "Broad-market context"
    if "SECTOR_ETF" in asset_intents:
        return "Sector allocation context"
    if cluster_type == "CROSS_BRANCH_CLUSTER":
        return "Cross-branch corroboration"
    if role_score >= 14:
        return "High policy-access relevance"
    return "Political disclosure accumulation"


def _review_rows(records: list[TransactionRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.broad_outcome not in {"REQUIRES_REVIEW", "EXCLUDED"}:
            continue
        rows.append(
            {
                "trade_id": record.source_trade_id or record.trade_key,
                "ticker": record.ticker,
                "asset_name": record.asset_name,
                "transaction_date": record.transaction_date,
                "filing_date": record.filing_date,
                "reason": record.reason,
                "classification": record.broad_outcome,
                "proposed_resolution": record.proposed_resolution,
                "manual_review_required": record.manual_review_required,
            }
        )
    return rows


def _role_confirmation(status: str, score: float) -> str:
    if status in {"ROLE_SOURCE_UNAVAILABLE", "COMPANY_CLASSIFICATION_UNAVAILABLE", "HISTORICAL_ROLE_UNAVAILABLE"}:
        return "ROLE_UNAVAILABLE"
    if score > 0:
        return "ROLE_CONFIRMED"
    return "ROLE_UNCONFIRMED"


def _materiality_score(effective_amount: float) -> float:
    for threshold, score in (
        (5_000_000, 15),
        (1_000_000, 13),
        (500_000, 11),
        (250_000, 9),
        (100_000, 6),
        (50_000, 4),
        (15_000, 2),
    ):
        if effective_amount >= threshold:
            return float(score)
    return 1.0


def _sale_penalty_score(value: float) -> float:
    for threshold, score in ((1e6, 15), (500e3, 12), (250e3, 8), (100e3, 5), (50e3, 3)):
        if value >= threshold:
            return float(score)
    return 1.0


def _call_bonus_score(value: float) -> float:
    for threshold, score in ((1e6, 10), (500e3, 8), (250e3, 6), (100e3, 4), (50e3, 2)):
        if value >= threshold:
            return float(score)
    return 0.0


def _timeliness_score(days_to_file: int | None) -> float:
    if days_to_file is None:
        return 0.0
    if days_to_file <= 7:
        return 10.0
    if days_to_file <= 14:
        return 8.0
    if days_to_file <= 30:
        return 5.0
    if days_to_file <= 45:
        return 2.0
    return 0.0


def _cluster_components(active_records: list[TransactionRecord]) -> tuple[float, str, int, int]:
    by_filer: dict[str, list[TransactionRecord]] = defaultdict(list)
    recent_filers: set[str] = set()
    branches: set[str] = set()
    for record in active_records:
        by_filer[record.filer_id].append(record)
        if (record.transaction_age or CLUSTER_DAYS + 1) <= CLUSTER_DAYS:
            recent_filers.add(record.filer_id)
        if record.branch in {"congress", "executive"}:
            branches.add(record.branch)
    repeated = max((len(rows) for rows in by_filer.values()), default=0)
    same_filer_component = 6.0 if repeated >= 3 else 3.0 if repeated == 2 else 0.0
    independent = len(recent_filers)
    multi_filer_component = 6.0 if independent >= 3 else 4.0 if independent == 2 else 0.0
    cross_branch_component = 3.0 if len(branches) >= 2 and independent >= 2 else 0.0
    if len(branches) >= 2 and independent >= 2:
        cluster_type = "CROSS_BRANCH_CLUSTER"
    elif "executive" in branches and independent >= 2:
        cluster_type = "EXECUTIVE_CLUSTER"
    elif "congress" in branches and independent >= 2:
        cluster_type = "CONGRESSIONAL_CLUSTER"
    else:
        cluster_type = "SINGLE_FILER"
    return same_filer_component + multi_filer_component + cross_branch_component, cluster_type, len(by_filer), len(recent_filers)


def _abnormality_for_record(record: TransactionRecord, comparable: list[TransactionRecord]) -> tuple[float, str]:
    comparable_amounts = [
        _effective_amount(item.amount_range_low, item.amount_range_mid)
        for item in comparable
        if item.trade_key != record.trade_key and item.action == "purchase" and item.discretionary_weight > 0
    ]
    if len(comparable_amounts) < 5:
        return 4.0, "INSUFFICIENT_HISTORY"
    current = _effective_amount(record.amount_range_low, record.amount_range_mid)
    sorted_amounts = sorted(comparable_amounts)
    percentile = 100.0 * sum(amount <= current for amount in sorted_amounts) / len(sorted_amounts)
    if percentile >= 95:
        return 15.0, "P95"
    if percentile >= 80:
        return 12.0, "P80"
    if percentile >= 60:
        return 8.0, "P60"
    if percentile >= 40:
        return 5.0, "P40"
    return 2.0, "LOW_PERCENTILE"


def close_after(close: pd.Series, tx_date_text: str) -> float | None:
    tx_date = pdate(tx_date_text)
    if tx_date is None:
        return None
    eligible = close[close.index.date >= tx_date]
    return _finite_number(eligible.iloc[0], default=float("nan")) if not eligible.empty else None


def active_options(records: list[TransactionRecord]) -> tuple[list[TransactionRecord], list[TransactionRecord], int, int, int]:
    states: list[list[Any]] = []
    by_key: dict[tuple[Any, ...], list[list[Any]]] = defaultdict(list)
    unclear = 0
    matched = 0
    matched_full = 0

    sortable = sorted(records, key=lambda record: (record.transaction_date, record.trade_key))
    for record in sortable:
        if record.asset_class != "option":
            continue
        if record.action == "purchase":
            state = [record, 1.0]
            states.append(state)
            key = option_key(record)
            if key is not None:
                by_key[key].append(state)
            continue
        if record.action not in {"sale_partial", "sale_full", "sale_unknown"}:
            continue
        if (record.transaction_age or OPTION_MATCH_DAYS + 1) > SALE_DAYS:
            continue
        key = option_key(record)
        prior = [] if key is None else [
            state for state in by_key.get(key, []) if state[0].transaction_date < record.transaction_date and state[1] > 0
        ]
        if not prior or record.action == "sale_unknown":
            unclear += 1
            continue
        matched += 1
        if record.action == "sale_full":
            matched_full += 1
            for state in prior:
                state[1] = 0.0
        else:
            for state in prior:
                state[1] *= 0.5

    calls: list[TransactionRecord] = []
    puts: list[TransactionRecord] = []
    for record, fraction in states:
        if fraction <= 0:
            continue
        scaled = TransactionRecord(**record.to_dict())
        scaled.amount_range_low *= fraction
        scaled.amount_range_mid *= fraction
        scaled.amount_range_high *= fraction
        if scaled.reason.startswith("ACTIVE_"):
            scaled = _scaled_record(scaled)
        if scaled.option_side == "call":
            calls.append(scaled)
        elif scaled.option_side == "put":
            puts.append(scaled)
    return calls, puts, unclear, matched, matched_full


def sale_metrics(records: list[TransactionRecord]) -> tuple[float, float, int, int, bool]:
    prior_buys: dict[str, list[str]] = defaultdict(list)
    penalty = 0.0
    total = 0.0
    partial = 0
    full = 0
    same_full = False
    for record in records:
        if record.action == "purchase":
            prior_buys[record.filer_id].append(record.transaction_date)
    for record in records:
        if record.action not in {"sale_partial", "sale_full", "sale_unknown"}:
            continue
        if (record.transaction_age or SALE_DAYS + 1) > SALE_DAYS:
            continue
        total += record.amount_range_mid
        partial += int(record.action == "sale_partial")
        full += int(record.action == "sale_full")
        same = any(item < record.transaction_date for item in prior_buys.get(record.filer_id, []))
        penalty += (
            _sale_penalty_score(record.amount_range_mid)
            * (1.0 if record.action == "sale_full" else 0.5)
            * (1.0 if same else 0.5)
            * max(0.0, 1.0 - float(record.transaction_age or 0) / SALE_DAYS)
            * record.discretionary_weight
        )
        if record.action == "sale_full" and same:
            same_full = True
    return min(MAX_SALE_PENALTY, penalty), total, partial, full, same_full


def classify_payload_records(
    payload: list[dict[str, Any]],
    *,
    observed_on: date,
    prior_ledger: dict[str, dict[str, Any]] | None = None,
    branch_scope: str | None = None,
) -> tuple[list[TransactionRecord], dict[str, dict[str, Any]], Counter]:
    scope = _resolve_scope(branch_scope)
    prior = prior_ledger or {}
    seen_keys: set[str] = set()
    ledger_updates: dict[str, dict[str, Any]] = dict(prior)
    counts: Counter = Counter()
    records: list[TransactionRecord] = []

    for item in payload:
        counts["raw records"] += 1
        if not isinstance(item, dict):
            counts["errors"] += 1
            continue

        branch = _branch_from_item(item)
        if branch == "congress":
            counts["congressional records"] += 1
        elif branch == "executive":
            counts["executive records"] += 1

        asset_name = str(item.get("asset_name") or "").strip()
        asset_type = str(item.get("asset_type") or "").strip()
        description = str(item.get("description") or item.get("asset_description") or "").strip()
        comments = str(item.get("comment") or item.get("comments") or "").strip()
        tx_action = action(item.get("transaction_type", item.get("type")))
        option_side_value = opt_side(item)
        low, mid, high = amounts(item)
        filer_name = str(item.get("filer_name") or item.get("representative") or "Unknown").strip()
        filer_id = str(item.get("filer_id") or filer_name).strip()
        owner = str(item.get("owner") or "Unknown").strip()
        owner_relationship = _normalise_owner(owner)
        chamber = _chamber_from_item(item)
        source = str(item.get("source") or "").strip()
        source_id = str(item.get("source_id") or item.get("source") or "").strip()
        filing_id = str(item.get("filing_id") or item.get("disclosure_id") or item.get("document_id") or "").strip()
        filing_type = str(item.get("filing_type") or "").strip()
        tx_date = pdate(item.get("transaction_date"))
        filing_date = pdate(item.get("disclosure_date") or item.get("filed_date") or item.get("filing_date"))
        ticker = ticker_code(item.get("ticker"))
        strike = opt_strike(item)
        expiry = opt_expiry(item)
        state = str(item.get("state") or item.get("filer_state") or "").strip().upper()
        party = str(item.get("party") or "").strip()
        agency = str(item.get("agency") or "").strip()
        level = str(item.get("level") or item.get("position_level") or "").strip()
        office = str(item.get("office") or item.get("filer_title") or "").strip()
        doc_url = str(item.get("doc_url") or item.get("document_url") or item.get("url") or "").strip()
        ingested_at = str(item.get("ingested_at") or item.get("updated_at") or "").strip()
        intent = classify_asset_intent(item)
        if option_record(item):
            asset_class = "option"
        elif intent.intent_class in {"SECTOR_ETF", "BROAD_MARKET_ETF", "OTHER_ETF"}:
            asset_class = "etf"
        elif stock_record(item):
            asset_class = "stock"
        else:
            asset_class = "other"

        source_trade_id, trade_key = _trade_key(
            item,
            filer_id=filer_id,
            ticker=ticker,
            transaction_date=tx_date,
            tx_action=tx_action,
            asset_class=asset_class,
            low=low,
            high=high,
            owner=owner_relationship,
            side=option_side_value,
            strike=strike,
            expiry=expiry,
        )
        fingerprint = _fingerprint(item, trade_key)
        previous = prior.get(trade_key)
        is_new_discovery = previous is None
        is_materially_amended = previous is not None and previous.get("fingerprint") != fingerprint
        tx_age = (observed_on - tx_date).days if tx_date else None
        filing_age = (observed_on - filing_date).days if filing_date else None
        days_to_file = (filing_date - tx_date).days if filing_date is not None and tx_date is not None else None
        late_status = ""
        if days_to_file is not None:
            late_status = "LATE" if days_to_file > PURCHASE_DAYS else "ON_TIME"

        record = TransactionRecord(
            trade_key=trade_key,
            fingerprint=fingerprint,
            source_trade_id=source_trade_id,
            broad_outcome="EXCLUDED",
            reason="UNCLASSIFIED",
            proposed_resolution="ignore",
            manual_review_required=False,
            ticker=ticker or "",
            yf_ticker=yf_ticker(ticker) if ticker else "",
            asset_name=asset_name,
            asset_type=asset_type,
            transaction_type=str(item.get("transaction_type") or item.get("type") or ""),
            transaction_date=_date_to_text(tx_date),
            filing_date=_date_to_text(filing_date),
            transaction_age=tx_age,
            filing_age=filing_age,
            days_to_file=days_to_file,
            late_filing_status=late_status,
            filer_id=filer_id,
            filer_name=filer_name,
            owner=owner,
            owner_relationship=owner_relationship,
            chamber=chamber,
            branch=branch,
            party=party,
            state=state,
            agency=agency,
            level=level,
            office=office,
            source=source,
            source_id=source_id,
            filing_id=filing_id,
            filing_type=filing_type,
            doc_url=doc_url,
            ingested_at=ingested_at,
            amount_range_low=low,
            amount_range_mid=mid,
            amount_range_high=high,
            option_side=option_side_value or "",
            strike=strike,
            expiry=_date_to_text(expiry),
            comments=comments,
            description=description,
            action=tx_action,
            asset_class=asset_class,
            asset_intent_class=intent.intent_class,
            is_new_discovery=is_new_discovery,
            is_materially_amended=is_materially_amended,
            discretionary_weight=1.0,
            company_specific=intent.company_specific,
            bullish=intent.bullish,
            bearish=intent.bearish,
            broad_market=intent.broad_market,
            intentionality_score=intent.intentionality_score,
        )

        if trade_key in seen_keys:
            record.reason = "DUPLICATE"
            counts["errors"] += 1
            counts["duplicate_records"] += 1
            records.append(record)
            continue
        seen_keys.add(trade_key)
        ledger_updates[trade_key] = {
            "fingerprint": fingerprint,
            "ticker": record.ticker,
            "transaction_date": record.transaction_date,
            "filing_date": record.filing_date,
            "last_seen_payload_hash": "",
            "source_id": source_id,
            "filing_id": filing_id,
            "filing_type": filing_type,
            "doc_url": doc_url,
        }

        if not _in_scope(branch, scope):
            record.reason = "OUT_OF_SCOPE_BRANCH"
            counts["excluded by scope"] += 1
            records.append(record)
            continue
        counts["included by scope"] += 1

        if tx_action not in {"purchase", "sale_partial", "sale_full", "sale_unknown"}:
            record.reason = "UNSUPPORTED_ACTION"
            records.append(record)
            continue

        nondiscretionary = _non_discretionary_reason(item)
        if nondiscretionary:
            record.reason = "NON_DISCRETIONARY_TRANSACTION"
            record.proposed_resolution = f"exclude:{nondiscretionary}"
            records.append(record)
            continue

        if asset_class == "other":
            record.reason = "OUT_OF_SCOPE_ASSET"
            records.append(record)
            continue

        if record.company_specific and not ticker and _resolved_public_security(item):
            record.broad_outcome = "REQUIRES_REVIEW"
            record.reason = "UNRESOLVED_PUBLIC_SECURITY"
            record.proposed_resolution = "manual_ticker_resolution"
            record.manual_review_required = True
            records.append(record)
            continue
        if not ticker:
            record.reason = "INVALID_TICKER"
            records.append(record)
            continue
        if tx_date is None or tx_age is None or tx_age < 0:
            record.reason = "INVALID_TRANSACTION_DATE"
            records.append(record)
            continue
        if asset_class == "option" and tx_action == "purchase" and option_side_value not in {"call", "put"}:
            record.broad_outcome = "REQUIRES_REVIEW"
            record.reason = "AMBIGUOUS_OPTION"
            record.proposed_resolution = "manual_option_side_review"
            record.manual_review_required = True
            records.append(record)
            continue

        is_active_purchase = tx_action == "purchase" and (intent.bullish or intent.bearish or intent.intent_class.endswith("ETF"))
        is_fresh = is_active_purchase and tx_age <= PURCHASE_DAYS
        is_late_disclosed = (
            is_active_purchase
            and filing_age is not None
            and filing_age <= LATE_DISCLOSURE_FILING_DAYS
            and PURCHASE_DAYS < tx_age <= LATE_DISCLOSURE_MAX_DAYS
        )
        if is_fresh:
            record.broad_outcome = "RETAINED_ACTIVE"
            record.reason = "ACTIVE_FRESH"
            record.trigger_type = "fresh_transaction"
            record.activity_weight = 1.0
            counts["active_fresh_transactions"] += 1
        elif is_late_disclosed:
            record.broad_outcome = "RETAINED_ACTIVE"
            record.reason = "ACTIVE_LATE_DISCLOSED"
            record.trigger_type = "late_disclosure"
            record.activity_weight = _late_disclosure_weight(tx_age)
            counts["active_late_disclosed_transactions"] += 1
        elif tx_action in {"sale_partial", "sale_full", "sale_unknown"} and tx_age <= SALE_DAYS:
            record.broad_outcome = "RETAINED_CONTEXT"
            record.reason = "RECENT_SALE_CONTEXT"
        elif asset_class == "option" and tx_age <= OPTION_MATCH_DAYS:
            record.broad_outcome = "RETAINED_CONTEXT"
            record.reason = "OPTION_POSITION_CONTEXT"
        else:
            record.broad_outcome = "RETAINED_CONTEXT"
            record.reason = "HISTORICAL_CONTEXT"
        record.discretionary_weight = low_signal_weight(record)
        records.append(record)
    return records, ledger_updates, counts


def score_ticker(
    ticker: str,
    records: list[TransactionRecord],
    market: dict[str, pd.Series],
    *,
    payload_hash: str,
    role_provider: CongressionalRoleProvider,
    classification_provider: CompanyClassificationProvider,
    observed_at: datetime,
    history_by_filer: dict[str, list[TransactionRecord]],
    audit_bundle: PoliticalAuditBundle,
) -> tuple[CongressTickerResult | None, str | None]:
    close = market["close"]
    volume = market["volume"]
    if close.empty or not math.isfinite(_finite_number(close.iloc[-1], default=float("nan"))):
        return None, "missing_price"

    retained = [record for record in records if record.broad_outcome in {"RETAINED_ACTIVE", "RETAINED_CONTEXT"}]
    active_records = [record for record in retained if record.broad_outcome == "RETAINED_ACTIVE" and record.action == "purchase"]
    if not active_records:
        return None, "no_active_records"

    current = _finite_number(close.iloc[-1], default=float("nan"))
    active_option_records = [record for record in retained if record.asset_class == "option"]
    calls, puts, unclear_sales, matched_sales, matched_full = active_options(active_option_records)
    active_call_keys = {record.trade_key for record in active_records if record.asset_class == "option"}
    active_calls = [record for record in calls if record.trade_key in active_call_keys]
    active_stock_buys = [_scaled_record(record) for record in active_records if record.asset_class == "stock"]
    active_scaled = active_stock_buys + active_calls + [
        _scaled_record(record)
        for record in active_records
        if record.asset_class != "stock" and record.trade_key not in {item.trade_key for item in active_calls}
    ]
    bullish_company_specific = [record for record in active_scaled if record.company_specific and record.bullish]
    if not active_scaled:
        return None, "no_active_records"

    low = sum(record.amount_range_low for record in active_scaled if record.bullish and not record.bearish)
    mid = sum(record.amount_range_mid for record in active_scaled if record.bullish and not record.bearish)
    high = sum(record.amount_range_high for record in active_scaled if record.bullish and not record.bearish)
    effective = _effective_amount(low, mid)
    call_low = sum(record.amount_range_low for record in active_calls)
    call_mid = sum(record.amount_range_mid for record in active_calls)
    leverage_bonus = _call_bonus_score(_effective_amount(call_low, call_mid))
    put_mid = sum(record.amount_range_mid for record in puts if record.reason.startswith("ACTIVE_"))
    put_penalty_value = min(MAX_PUT_PENALTY, 0.5 * _sale_penalty_score(put_mid)) if put_mid else 0.0

    priced: list[tuple[TransactionRecord, float, float]] = []
    for record in [item for item in active_scaled if item.bullish and not item.bearish]:
        ref = close_after(close, record.transaction_date)
        if ref and ref > 0:
            weight = record.amount_range_mid if record.amount_range_mid > 0 else 1.0
            priced.append((record, (current - ref) / ref * 100.0, weight))
    if not priced:
        return None, "missing_price"

    weighted_return = _weighted_average((ret, weight) for _, ret, weight in priced)
    weighted_age = _weighted_average((float(record.transaction_age or 0), weight) for record, _, weight in priced)
    transaction_freshness_entry = _weighted_average(
        (_transaction_freshness_component(record, 20.0), weight)
        for record, _, weight in priced
    )
    average_activity_weight = _weighted_average((record.activity_weight, weight) for record, _, weight in priced)
    active_capital_low = low
    active_capital_mid = mid
    active_capital_high = high

    primary_intents = {record.asset_intent_class for record in active_records}
    company_specific = any(record.company_specific and record.bullish for record in active_records)
    broad_market_only = primary_intents and primary_intents.issubset({"BROAD_MARKET_ETF"})

    weighted_intentionality = _weighted_average(
        (record.intentionality_score, record.amount_range_mid or 1.0)
        for record in active_records
        if record.bullish or record.asset_intent_class.endswith("ETF")
    )
    materiality = _materiality_score(effective)
    abnormality_scores = [
        (_abnormality_for_record(record, history_by_filer.get(record.filer_id, []))[0], record.amount_range_mid or 1.0)
        for record in bullish_company_specific or [item for item in active_records if item.bullish]
    ]
    filer_abnormality = _weighted_average(abnormality_scores) if abnormality_scores else 0.0
    accumulation_score, cluster_type, buyer_count, cluster_buyer_count = _cluster_components(active_records)
    timeliness_score = _weighted_average(
        (_timeliness_score(record.days_to_file), record.amount_range_mid or 1.0)
        for record in active_records
        if record.bullish
    )

    company_classification = classification_provider.classify(ticker)
    audit_bundle.company_classifications.append(asdict(company_classification))
    role_results = []
    role_statuses: list[str] = []
    for record in bullish_company_specific:
        filer = PoliticalFiler(
            filer_id=record.filer_id,
            filer_name=record.filer_name,
            branch=record.branch,
            chamber=record.chamber,
            party=record.party,
            state=record.state,
            agency=record.agency,
            level=record.level,
            office=record.office,
            owner_relationship=record.owner_relationship,
            source_id=record.source_id,
        )
        role_resolution = role_provider.current_roles(filer, as_of=observed_at)
        record.bioguide_id = role_resolution.filer.bioguide_id
        record.identity_resolution_status = role_resolution.filer.identity_resolution_status
        record.role_relevance_status = role_resolution.status
        audit_bundle.identity_resolutions.append(asdict(role_resolution.filer))
        if role_resolution.roles:
            audit_bundle.committee_role_snapshots.extend(asdict(role) for role in role_resolution.roles)
        if role_resolution.executive_role is not None:
            audit_bundle.executive_role_resolutions.append(asdict(role_resolution.executive_role))
        if record.identity_resolution_status in {"EXPLICIT_OVERRIDE", "EXACT_NAME", "NAME_CHAMBER_STATE"}:
            pass
        role_relevance = evaluate_role_relevance(role_resolution, company_classification)
        audit_bundle.role_relevance_calculations.append(
            {
                "ticker": ticker,
                "filer_id": record.filer_id,
                "score": role_relevance.score,
                "status": role_relevance.status,
                "evidence": [item.to_dict() for item in role_relevance.evidences],
            }
        )
        role_results.append(role_relevance)
        role_statuses.append(role_relevance.status)

    resolved_count = sum(status in {"EXPLICIT_OVERRIDE", "EXACT_NAME", "NAME_CHAMBER_STATE"} for status in [record.identity_resolution_status for record in active_records])
    unresolved_count = sum(status in {"UNRESOLVED", "AMBIGUOUS"} for status in [record.identity_resolution_status for record in active_records])
    role_relevance = max(role_results, key=lambda item: item.score, default=None)
    role_score = float(role_relevance.score if role_relevance is not None else 0.0)
    role_status = role_relevance.status if role_relevance is not None else ("NOT_APPLICABLE" if not company_specific else "ROLE_SOURCE_UNAVAILABLE")

    sale_penalty_value, _, _, _, same_full = sale_metrics([record for record in retained if record.asset_class == "stock"])
    strong_distribution = same_full or sale_penalty_value >= 12 or put_penalty_value >= 8
    base = weighted_intentionality + materiality + filer_abnormality + accumulation_score + timeliness_score + role_score
    conviction = max(0.0, min(100.0, base + leverage_bonus - put_penalty_value - sale_penalty_value))
    entry = max(
        0.0,
        min(
            100.0,
            price_score(weighted_return)
            + transaction_freshness_entry
            + trend_score(close)
            + liquidity_score(close, volume),
        ),
    )

    if broad_market_only or not company_specific or weighted_intentionality < 15:
        category = "context"
    elif strong_distribution and (conviction >= RISK_C or put_penalty_value > 0):
        category = "risk"
    elif conviction >= ACTIONABLE_C and entry >= ACTIONABLE_E and weighted_intentionality >= 15 and weighted_return > SEVERE_DRAWDOWN and any(record.is_new_discovery or record.is_materially_amended for record in active_records):
        category = "actionable"
    elif conviction >= WAIT_C and entry < ACTIONABLE_E:
        category = "wait"
    elif 60.0 <= conviction < ACTIONABLE_C and (weighted_intentionality >= 20 or role_score >= 14):
        category = "wait"
    else:
        category = "other"

    historical_context_capital = sum(
        record.amount_range_mid
        for record in retained
        if record.broad_outcome == "RETAINED_CONTEXT" and record.action == "purchase"
    )
    trigger_types = {record.trigger_type for record in active_records if record.trigger_type}
    if trigger_types == {"late_disclosure"}:
        signal_trigger = "late_disclosure"
    elif trigger_types == {"fresh_transaction"}:
        signal_trigger = "fresh_transaction"
    else:
        signal_trigger = "mixed"

    if resolved_count:
        audit_bundle.normalised_transactions.extend(record.to_dict() for record in active_records)
    if unresolved_count:
        audit_bundle.excluded_record_reasons.extend(
            {"trade_key": record.trade_key, "reason": record.identity_resolution_status}
            for record in active_records
            if record.identity_resolution_status in {"UNRESOLVED", "AMBIGUOUS"}
        )

    result = CongressTickerResult(
        ticker=ticker,
        category=category,
        conviction=conviction,
        entry=entry,
        base=base,
        sale_penalty=sale_penalty_value,
        call_bonus=leverage_bonus,
        put_penalty=put_penalty_value,
        low=low,
        mid=mid,
        high=high,
        effective=effective,
        active_bullish_capital=mid,
        historical_context_capital=historical_context_capital,
        call_mid=call_mid,
        put_mid=put_mid,
        buyers=buyer_count,
        cluster_buyers=cluster_buyer_count,
        weighted_age=weighted_age,
        weighted_return=weighted_return,
        flow=_flow_label(cluster_type=cluster_type, asset_intents=primary_intents, strong_distribution=strong_distribution, role_score=role_score),
        names=sorted({surname(record.filer_name) for record in active_records}),
        unclear_sales=unclear_sales,
        matched_sales=matched_sales,
        matched_full_sales=matched_full,
        active_trade_count=len(active_records),
        active_fresh_trade_count=sum(record.reason == "ACTIVE_FRESH" for record in active_records),
        active_late_disclosed_trade_count=sum(record.reason == "ACTIVE_LATE_DISCLOSED" for record in active_records),
        signal_trigger=signal_trigger,
        trigger_types=sorted(trigger_types),
        transaction_dates=[record.transaction_date for record in active_records],
        filing_dates=[record.filing_date for record in active_records if record.filing_date],
        transaction_ages=[int(record.transaction_age or 0) for record in active_records],
        filing_ages=[int(record.filing_age or 0) for record in active_records if record.filing_age is not None],
        alertable=any(record.is_new_discovery or record.is_materially_amended for record in active_records),
        weighted_average_activity_weight=average_activity_weight,
        valid_for_days=max(_remaining_valid_days(record) for record in active_records),
        source_payload_hash=payload_hash,
        intentionality_score=weighted_intentionality,
        materiality_score=materiality,
        filer_abnormality_score=filer_abnormality,
        accumulation_score=accumulation_score,
        timeliness_score=timeliness_score,
        role_relevance_score=role_score,
        role_relevance_status=role_status,
        role_evidence=[item.to_dict() for item in (role_relevance.evidences if role_relevance is not None else ())],
        seniority_classes=list(role_relevance.seniority_classes if role_relevance is not None else ()),
        seniority_multipliers=list(role_relevance.seniority_multipliers if role_relevance is not None else ()),
        committee_ids=list(role_relevance.committee_ids if role_relevance is not None else ()),
        committee_names=list(role_relevance.committee_names if role_relevance is not None else ()),
        subcommittee_ids=list(role_relevance.subcommittee_ids if role_relevance is not None else ()),
        agency_keys=list(role_relevance.agency_keys if role_relevance is not None else ()),
        branches=sorted({record.branch for record in active_records if record.branch}),
        chambers=sorted({record.chamber for record in active_records if record.chamber}),
        filers=sorted({record.filer_name for record in active_records if record.filer_name}),
        bioguide_ids=sorted({record.bioguide_id for record in active_records if record.bioguide_id}),
        company_sector=company_classification.sector,
        company_industry=company_classification.industry,
        company_thematic_exposures=list(company_classification.thematic_exposures),
        company_classification_source=company_classification.source,
        company_classification_confidence=company_classification.confidence,
        asset_intent_classes=sorted(primary_intents),
        cluster_type=cluster_type,
        role_confirmation=_role_confirmation(role_status, role_score),
        high_policy_access_flag=bool(role_relevance.high_policy_access_flag if role_relevance is not None else False),
        active_amount_low=active_capital_low,
        active_amount_mid=active_capital_mid,
        active_amount_high=active_capital_high,
        source_ids=sorted({record.source_id for record in active_records if record.source_id}),
        filing_ids=sorted({record.filing_id for record in active_records if record.filing_id}),
        filing_types=sorted({record.filing_type for record in active_records if record.filing_type}),
        document_urls=sorted({record.doc_url for record in active_records if record.doc_url}),
        risk_flags=sorted(
            {
                *([] if company_classification.confidence != "UNAVAILABLE" else ["company_classification_unavailable"]),
                *([] if role_score > 0 else [role_status.lower()]),
            }
        ),
    )
    return result, None


def run_scan_from_payload(
    payload: list[dict[str, Any]],
    *,
    raw_bytes: bytes | None = None,
    observed_at: str | date | datetime | None = None,
    prior_ledger: dict[str, dict[str, Any]] | None = None,
    branch_scope: str | None = None,
    price_fetcher: PriceFetcher | None = None,
) -> CongressScanResult:
    observed_datetime = _normalise_datetime(observed_at or datetime.now(SINGAPORE_TZ))
    observed_on = observed_datetime.date()
    payload_hash, payload_bytes = _payload_hash(raw_bytes or b"", payload)
    scope = _resolve_scope(branch_scope)
    metadata = PayloadMetadata(
        source_url=RAW_KADOA_URL,
        fetched_at=observed_datetime.isoformat(),
        record_count=len(payload),
        payload_sha256=payload_hash,
        payload_bytes=payload_bytes,
    )

    records, ledger, counts = classify_payload_records(
        payload,
        observed_on=observed_on,
        prior_ledger=prior_ledger,
        branch_scope=scope,
    )
    for entry in ledger.values():
        entry["last_seen_payload_hash"] = payload_hash
        entry["last_seen_at"] = observed_datetime.isoformat()

    active_tickers = sorted({record.ticker for record in records if record.broad_outcome == "RETAINED_ACTIVE" and record.ticker})
    counts["included by scope"] = counts.get("included by scope", 0)
    counts["excluded by scope"] = counts.get("excluded by scope", 0)
    counts["total_raw_records"] = len(payload)
    counts["active_tickers_before_market_checks"] = len(active_tickers)

    grouped: dict[str, list[TransactionRecord]] = defaultdict(list)
    for record in records:
        if record.ticker:
            grouped[record.ticker].append(record)
    history_by_filer: dict[str, list[TransactionRecord]] = defaultdict(list)
    for record in records:
        if record.action == "purchase" and record.bullish and record.discretionary_weight > 0:
            history_by_filer[record.filer_id].append(record)

    ticker_results: list[CongressTickerResult] = []
    audit_bundle = PoliticalAuditBundle()
    role_provider = CongressionalRoleProvider()
    classification_provider = CompanyClassificationProvider()
    if active_tickers:
        active_records = [record for ticker in active_tickers for record in grouped[ticker] if record.broad_outcome == "RETAINED_ACTIVE"]
        earliest = min(pdate(record.transaction_date) or observed_on for record in active_records)
        market_fetcher = price_fetcher or prices
        market = market_fetcher([yf_ticker(ticker) for ticker in active_tickers], earliest)
        for ticker in active_tickers:
            symbol = yf_ticker(ticker)
            if symbol not in market:
                counts["missing prices"] += 1
                continue
            try:
                result, rejection_reason = score_ticker(
                    ticker,
                    grouped[ticker],
                    market[symbol],
                    payload_hash=payload_hash,
                    role_provider=role_provider,
                    classification_provider=classification_provider,
                    observed_at=observed_datetime,
                    history_by_filer=history_by_filer,
                    audit_bundle=audit_bundle,
                )
            except Exception as exc:
                logger.warning("Political disclosure ticker failed for %s: %s", ticker, exc.__class__.__name__)
                counts["errors"] += 1
                continue
            if rejection_reason in {"missing_price"}:
                counts["missing prices"] += 1
                continue
            if result is not None:
                ticker_results.append(result)
                counts[result.category] += 1
                if result.role_relevance_score > 0:
                    counts["role relevance resolved"] += 1
                else:
                    counts["role relevance unavailable"] += 1
                if result.bioguide_ids:
                    counts["identity resolved"] += 1
                elif "executive" in result.branches:
                    counts["agency roles resolved"] += int(bool(result.agency_keys))
                else:
                    counts["identity unresolved"] += 1

    counts["scored_tickers"] = len(ticker_results)
    counts["signals_retained_after_minimum_conviction"] = len(
        [result for result in ticker_results if result.alertable and (result.category in SUPPORTED_CATEGORIES or result.conviction >= 15.0)]
    )
    counts["final_actionable_count"] = sum(result.alertable and result.category == "actionable" for result in ticker_results)
    counts["final_wait_count"] = sum(result.alertable and result.category == "wait" for result in ticker_results)
    counts["final_risk_count"] = sum(result.alertable and result.category == "risk" for result in ticker_results)
    counts["final_near_miss_count"] = sum(result.alertable and result.category == "other" and result.conviction >= 15.0 for result in ticker_results)

    return CongressScanResult(
        metadata=metadata,
        transactions=records,
        ticker_results=sorted(
            ticker_results,
            key=lambda result: (
                {"actionable": 5, "wait": 4, "risk": 3, "other": 2, "context": 1}.get(result.category, 0),
                result.conviction,
                result.ticker,
            ),
            reverse=True,
        ),
        review_audit=_review_rows(records),
        counts=dict(counts),
        ledger=ledger,
        raw_payload=payload,
        audit_bundle=audit_bundle,
        scope_used=scope,
    )


def run_live_scan(
    *,
    prior_ledger: dict[str, dict[str, Any]] | None = None,
    payload_fetcher: FetchPayload | None = None,
    price_fetcher: PriceFetcher | None = None,
    branch_scope: str | None = None,
) -> CongressScanResult:
    fetcher = payload_fetcher or fetch_live_payload
    raw_bytes, payload = fetcher()
    return run_scan_from_payload(
        payload,
        raw_bytes=raw_bytes,
        observed_at=datetime.now(SINGAPORE_TZ),
        prior_ledger=prior_ledger,
        branch_scope=branch_scope,
        price_fetcher=price_fetcher,
    )
