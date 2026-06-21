import os
import re
import json
import time
import math
import hashlib
import logging
import asyncio
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
from logging.handlers import RotatingFileHandler

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Bot

# ── Logging ────────────────────────────────────────────────────────────────
LOG_FILE = "congress_bot.log"

logger = logging.getLogger("congress_bot")
logger.setLevel(logging.INFO)
logger.handlers.clear()

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)

# ── Environment ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RAW_KADOA_URL = (
    "https://raw.githubusercontent.com/"
    "kadoa-org/congress-trading-monitor/"
    "main/public/data/trades.json"
)

# ── Strategy Configuration ─────────────────────────────────────────────────
LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

# Recent purchases drive the opportunity signal.
PURCHASE_LOOKBACK_DAYS = 45

# Sales provide wider context but decay with age.
SALE_CONTEXT_DAYS = 90

# Older option purchases are retained only to match later option sales.
OPTION_MATCH_LOOKBACK_DAYS = 365

CLUSTER_WINDOW_DAYS = 14
TECHNICAL_HISTORY_BUFFER_DAYS = 100
MIN_PRICE_COVERAGE = 0.75

# Do not automatically present extreme falls as attractive entries.
SEVERE_DRAWDOWN_PCT = -15.0
SINGLE_BUYER_CHASE_LIMIT_PCT = 8.0
CLUSTER_CHASE_LIMIT_PCT = 15.0

# Section thresholds.
ACTIONABLE_MIN_CONVICTION = 60.0
ACTIONABLE_MIN_ENTRY = 60.0
WAIT_MIN_CONVICTION = 70.0
WAIT_MIN_EFFECTIVE_AMOUNT = 500_000
RISK_MIN_CONVICTION = 40.0
RISK_MIN_EFFECTIVE_AMOUNT = 250_000

MAX_ACTIONABLE = 8
MAX_WAIT = 6
MAX_RISK = 6
MAX_TOTAL_RESULTS = 20

# Always report that the scheduled run completed. When no ticker meets the
# strict actionable/wait/risk thresholds, show a small, clearly labelled list
# of the nearest signals instead of silently exiting.
SEND_STATUS_WHEN_NO_QUALIFYING = True
MAX_NEAREST_SIGNALS = 5
NEAREST_SIGNAL_MIN_CONVICTION = 15.0

# Sales and options affect one final conviction score.
MAX_STOCK_SALE_PENALTY = 20.0
MAX_OPTION_ADJUSTMENT = 15.0

# Optional current-option quality check. This never blocks the bot.
ENABLE_CURRENT_OPTION_ENRICHMENT = True
OPTION_ENRICHMENT_MIN_BASE_CONVICTION = 40.0
OPTION_ENRICHMENT_MIN_CALL_PREMIUM = 100_000.0
MAX_OPTION_ENRICHMENT_TICKERS = 10
MAX_OPTION_CHAIN_CALLS = 15

# Optional repeat-alert suppression.
SEND_ONLY_NEW_OR_CHANGED = False
MIN_SCORE_CHANGE_TO_NOTIFY = 5.0
STATE_FILE = Path("congress_state.json")
LOCK_FILE = Path("congress_bot.lock")
YF_CACHE_DIRECTORY = Path(
    os.getenv("YF_CACHE_DIRECTORY", "./yfinance_cache")
)

# Yahoo settings: moderate batches, conservative concurrency, capped fallback.
YF_BATCH_SIZE = 20
YF_BATCH_ATTEMPTS = 2
YF_FALLBACK_ATTEMPTS = 1
MAX_INDIVIDUAL_FALLBACKS = 10
YF_TIMEOUT = 30
YF_RETRY_BASE_DELAY = 4

# Only use explicit, validated symbol overrides.
YAHOO_TICKER_OVERRIDES = {
    "BRK.B": "BRK-B",
    "BF.B": "BF-B",
}

# Telegram settings.
TELEGRAM_CHAR_LIMIT = 3800
INTER_CHUNK_DELAY = 1.5

# In-memory caches last only for the current run.
TICKER_OBJECT_CACHE = {}
OPTION_CHAIN_CACHE = {}
OPTION_CHAIN_CALL_COUNT = 0

# ── General Helpers ────────────────────────────────────────────────────────

def today_local() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def parse_date(value):
    if value in {None, ""}:
        return None

    text = str(value).strip()
    formats = (
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
    )

    for date_format in formats:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def safe_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_amount(value):
    value = safe_float(value)
    if value is None:
        return "$0"
    absolute = abs(value)
    sign = "-" if value < 0 else ""
    if absolute >= 1_000_000_000:
        return f"{sign}${absolute / 1_000_000_000:.1f}b"
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.1f}m"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.0f}k"
    return f"{sign}${absolute:.0f}"


def compact_name(full_name):
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
    parts = str(full_name or "").strip().split()
    while parts and parts[-1].lower() in suffixes:
        parts.pop()
    return parts[-1] if parts else "Unknown"


def normalise_ticker(value):
    ticker = str(value or "").strip().upper()
    if ticker.lower() in {"", "null", "none", "--", "n/a", "nan"}:
        return None
    if not re.fullmatch(r"[A-Z0-9.^=\-]+", ticker):
        return None
    return ticker


def yahoo_ticker(ticker):
    return YAHOO_TICKER_OVERRIDES.get(ticker, ticker)


def estimate_amounts(item):
    low = safe_float(item.get("amount_range_low"))
    high = safe_float(item.get("amount_range_high"))
    if low is None or high is None or low < 0 or high < low:
        return 0.0, 0.0, 0.0
    return low, (low + high) / 2.0, high


def load_json(path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
    return default


def save_json(path, payload):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(path)


def acquire_lock():
    try:
        descriptor = os.open(
            str(LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        os.write(descriptor, str(os.getpid()).encode("utf-8"))
        os.close(descriptor)
        return True
    except FileExistsError:
        logger.error("Another bot run appears active: %s", LOCK_FILE)
        return False


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not remove lock file: %s", exc)


def build_http_session():
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
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "CongressTradeMonitor/3.0"})
    return session


def context_weight(trade):
    """Reduce clearly non-discretionary transaction signals."""
    text = " ".join(
        str(trade.get(field) or "")
        for field in ("asset_name", "comment")
    ).lower()

    low_signal_terms = (
        "inherited",
        "inheritance",
        "estate",
        "mandatory divestment",
        "required divestment",
        "issuer called",
        "called by issuer",
    )
    return 0.25 if any(term in text for term in low_signal_terms) else 1.0


# ── Transaction and Asset Classification ─────────────────────────────────

def classify_transaction_action(value):
    text = str(value or "").strip().lower()

    if "purchase" in text or re.search(r"\bbuy\b", text):
        return "purchase"
    if "sale" in text and "partial" in text:
        return "sale_partial"
    if "sale" in text and "full" in text:
        return "sale_full"
    if "sale" in text or re.search(r"\bsell\b", text):
        return "sale_unknown"
    return "other"


def combined_asset_text(item):
    fields = (
        "asset_type",
        "asset_name",
        "asset_description",
        "description",
        "comment",
    )
    return " ".join(str(item.get(field) or "") for field in fields).lower()


def is_option_transaction(item):
    asset_type = str(item.get("asset_type") or "").strip().lower()
    text = combined_asset_text(item)

    if "option" in asset_type or "option" in text:
        return True

    has_side = re.search(r"\b(call|put)\b", text) is not None
    has_contract_term = re.search(
        r"\b(strike|expiry|expiration|expires|maturity)\b",
        text,
    ) is not None
    return has_side and has_contract_term


def is_eligible_stock(item):
    """
    Accept plain Kadoa/House/Senate stock labels, including asset_type='Stock',
    after first excluding non-common-equity instruments from all text fields.
    """
    asset_type = str(item.get("asset_type") or "").strip().lower()
    text = combined_asset_text(item)

    excluded_terms = (
        "option",
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
        "fund ",
        " fund",
        "warrant",
        "preferred stock",
        "preferred share",
        "annuity",
        "certificate of deposit",
        "cryptocurrency",
        "crypto asset",
    )
    padded_text = f" {text} "
    if any(term in padded_text for term in excluded_terms):
        return False

    accepted_types = {
        "st",
        "stock",
        "common stock",
        "equity",
        "ordinary shares",
        "ordinary share",
    }
    if asset_type in accepted_types:
        return True

    accepted_name_terms = (
        "common stock",
        "class a common",
        "class b common",
        "ordinary share",
        "american depositary share",
        "american depositary receipt",
        "depositary receipt",
        "adr",
    )
    return any(term in text for term in accepted_name_terms)


def extract_option_side(item):
    explicit = str(
        item.get("option_type")
        or item.get("put_call")
        or item.get("call_put")
        or ""
    ).strip().lower()
    if explicit in {"call", "put"}:
        return explicit

    text = combined_asset_text(item)
    match = re.search(r"\b(call|put)\b", text)
    return match.group(1).lower() if match else None


def extract_option_strike(item):
    for key in ("strike", "strike_price", "option_strike"):
        value = safe_float(item.get(key))
        if value is not None and value > 0:
            return value

    text = combined_asset_text(item)
    patterns = (
        r"(?:strike(?:\s+price)?|strk)\s*[:=@\-]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:strike|call|put)\b",
        r"\b(?:call|put)\s*(?:at|@)?\s*\$\s*([0-9]+(?:\.[0-9]+)?)",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = safe_float(match.group(1))
            if value is not None and value > 0:
                return value
    return None


def extract_option_expiry(item):
    for key in (
        "expiration_date",
        "expiry_date",
        "option_expiry",
        "maturity_date",
    ):
        parsed = parse_date(item.get(key))
        if parsed is not None:
            return parsed

    text = combined_asset_text(item)
    labelled_patterns = (
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*"
        r"(\d{4}-\d{1,2}-\d{1,2})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*"
        r"(\d{1,2}/\d{1,2}/\d{2,4})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*"
        r"(\d{1,2}-\d{1,2}-\d{2,4})",
        r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    )

    for pattern in labelled_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            parsed = parse_date(match.group(1).replace(",", ", "))
            if parsed is not None:
                return parsed

    # Conservative fallback: only accept an unlabelled date when the record
    # is already known to be an option.
    generic_patterns = (
        r"\b(20\d{2}-\d{1,2}-\d{1,2})\b",
        r"\b(\d{1,2}/\d{1,2}/20\d{2})\b",
    )
    for pattern in generic_patterns:
        match = re.search(pattern, text)
        if match:
            parsed = parse_date(match.group(1))
            if parsed is not None:
                return parsed
    return None


def parse_option_contract(item):
    return {
        "option_side": extract_option_side(item),
        "option_strike": extract_option_strike(item),
        "option_expiry": extract_option_expiry(item),
    }


def option_contract_key(trade):
    side = trade.get("option_side")
    strike = trade.get("option_strike")
    expiry = trade.get("option_expiry")

    if side not in {"call", "put"} or strike is None or expiry is None:
        return None

    return (
        trade.get("filer_id"),
        trade.get("ticker"),
        trade.get("owner"),
        side,
        round(float(strike), 4),
        expiry,
    )


# ── Trade Retrieval and Deduplication ─────────────────────────────────────

def fetch_trades():
    session = build_http_session()
    try:
        response = session.get(RAW_KADOA_URL, timeout=30)
        response.raise_for_status()
        raw_bytes = response.content
        payload = response.json()
    except Exception as exc:
        logger.error("Trade-data retrieval failed: %s", exc)
        return None

    if not isinstance(payload, list):
        logger.error(
            "Unexpected trade-data payload type: %s",
            type(payload).__name__,
        )
        return None

    payload_hash = hashlib.sha256(raw_bytes).hexdigest()
    logger.info(
        "Kadoa payload: records=%d | bytes=%d | SHA256=%s",
        len(payload),
        len(raw_bytes),
        payload_hash,
    )

    retained = []
    seen_keys = set()
    rejection_counts = Counter()
    today = today_local()

    for item in payload:
        if not isinstance(item, dict):
            rejection_counts["not_dict"] += 1
            continue

        action = classify_transaction_action(
            item.get("transaction_type", item.get("type", ""))
        )
        if action not in {
            "purchase",
            "sale_partial",
            "sale_full",
            "sale_unknown",
        }:
            rejection_counts["unsupported_action"] += 1
            continue

        option_record = is_option_transaction(item)
        if option_record:
            asset_class = "option"
            contract = parse_option_contract(item)
        elif is_eligible_stock(item):
            asset_class = "stock"
            contract = {
                "option_side": None,
                "option_strike": None,
                "option_expiry": None,
            }
        else:
            rejection_counts["ineligible_asset"] += 1
            continue

        ticker = normalise_ticker(item.get("ticker"))
        transaction_date = parse_date(item.get("transaction_date"))
        filing_date = parse_date(item.get("filing_date"))
        if ticker is None:
            rejection_counts["invalid_ticker"] += 1
            continue
        if transaction_date is None:
            rejection_counts["invalid_transaction_date"] += 1
            continue

        age = (today - transaction_date).days
        if age < 0:
            rejection_counts["future_transaction"] += 1
            continue

        if asset_class == "option":
            maximum_age = OPTION_MATCH_LOOKBACK_DAYS
        else:
            maximum_age = max(PURCHASE_LOOKBACK_DAYS, SALE_CONTEXT_DAYS)

        if age > maximum_age:
            rejection_counts["outside_context_window"] += 1
            continue

        low, midpoint, high = estimate_amounts(item)
        filer_name = str(
            item.get("filer_name", item.get("representative", "Unknown"))
        ).strip() or "Unknown"
        filer_id = str(item.get("filer_id") or filer_name).strip()
        owner = str(item.get("owner") or "Unknown").strip()

        trade_id = str(item.get("id") or "").strip()
        if trade_id:
            dedup_key = ("id", trade_id)
        else:
            dedup_key = (
                "composite",
                filer_id,
                ticker,
                transaction_date.isoformat(),
                filing_date.isoformat() if filing_date else "",
                action,
                asset_class,
                low,
                high,
                owner,
                contract.get("option_side"),
                contract.get("option_strike"),
                contract.get("option_expiry").isoformat()
                if contract.get("option_expiry")
                else "",
            )

        if dedup_key in seen_keys:
            rejection_counts["duplicate"] += 1
            continue
        seen_keys.add(dedup_key)

        retained.append(
            {
                "trade_id": trade_id or "|".join(map(str, dedup_key[1:])),
                "ticker": ticker,
                "price_ticker": yahoo_ticker(ticker),
                "transaction_date": transaction_date,
                "filing_date": filing_date,
                "age": age,
                "transaction_action": action,
                "asset_class": asset_class,
                "filer_id": filer_id,
                "filer_name": filer_name,
                "display_name": compact_name(filer_name),
                "owner": owner,
                "comment": item.get("comment"),
                "asset_name": item.get("asset_name"),
                "asset_type": item.get("asset_type"),
                "doc_url": item.get("doc_url"),
                "amount_low": low,
                "amount_midpoint": midpoint,
                "amount_high": high,
                "option_side": contract.get("option_side"),
                "option_strike": contract.get("option_strike"),
                "option_expiry": contract.get("option_expiry"),
            }
        )

    retained_counts = Counter(
        (trade["asset_class"], trade["transaction_action"])
        for trade in retained
    )
    logger.info(
        "Retained %d unique relevant transactions: %s",
        len(retained),
        dict(retained_counts),
    )
    logger.info("Trade rejection counts: %s", dict(rejection_counts))
    return retained


# ── Yahoo Historical Price Retrieval ──────────────────────────────────────

def initialise_yfinance():
    try:
        YF_CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(YF_CACHE_DIRECTORY))
    except Exception as exc:
        logger.warning("Could not configure yfinance cache: %s", exc)


def chunked(sequence, size):
    for index in range(0, len(sequence), size):
        yield sequence[index:index + size]


def clean_series(series, positive_only=False):
    if series is None:
        return pd.Series(dtype="float64")

    cleaned = pd.to_numeric(series, errors="coerce")
    cleaned = cleaned.replace([math.inf, -math.inf], pd.NA).dropna()
    if positive_only:
        cleaned = cleaned[cleaned > 0]
    else:
        cleaned = cleaned[cleaned >= 0]

    try:
        cleaned.index = pd.to_datetime(cleaned.index)
        if getattr(cleaned.index, "tz", None) is not None:
            cleaned.index = cleaned.index.tz_localize(None)
        cleaned = cleaned.sort_index()
    except Exception:
        pass
    return cleaned


def extract_ticker_frame(data, ticker, batch_size):
    if data is None or data.empty:
        return None

    try:
        if isinstance(data.columns, pd.MultiIndex):
            level0 = set(map(str, data.columns.get_level_values(0)))
            level1 = set(map(str, data.columns.get_level_values(1)))

            if ticker in level0:
                return data[ticker].copy()
            if ticker in level1:
                return data.xs(ticker, axis=1, level=1).copy()

        elif batch_size == 1:
            return data.copy()
    except Exception as exc:
        logger.warning("Could not extract %s from batch data: %s", ticker, exc)
    return None


def download_price_bundle(price_tickers, start_date, end_date):
    result = {}
    unique_tickers = sorted(set(price_tickers))

    for batch in chunked(unique_tickers, YF_BATCH_SIZE):
        batch_data = None

        for attempt in range(1, YF_BATCH_ATTEMPTS + 1):
            try:
                logger.info(
                    "Yahoo history batch: %d tickers | attempt %d/%d",
                    len(batch),
                    attempt,
                    YF_BATCH_ATTEMPTS,
                )
                batch_data = yf.download(
                    tickers=batch,
                    start=start_date,
                    end=end_date,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    repair=False,
                    keepna=False,
                    actions=False,
                    threads=False,
                    progress=False,
                    timeout=YF_TIMEOUT,
                )
                if batch_data is not None and not batch_data.empty:
                    break
            except Exception as exc:
                logger.warning("Yahoo batch attempt failed: %s", exc)

            if attempt < YF_BATCH_ATTEMPTS:
                time.sleep(YF_RETRY_BASE_DELAY * attempt)

        if batch_data is None or batch_data.empty:
            logger.warning(
                "Yahoo batch returned no usable data for %d tickers.",
                len(batch),
            )
            continue

        for ticker in batch:
            frame = extract_ticker_frame(batch_data, ticker, len(batch))
            if frame is None or frame.empty or "Close" not in frame.columns:
                continue

            close = clean_series(frame["Close"], positive_only=True)
            volume = (
                clean_series(frame["Volume"], positive_only=False)
                if "Volume" in frame.columns
                else pd.Series(dtype="float64")
            )
            if not close.empty:
                result[ticker] = {
                    "close": close,
                    "volume": volume,
                    "source": "batch",
                }

    return result


def download_single_ticker(ticker, start_date, end_date):
    for attempt in range(1, YF_FALLBACK_ATTEMPTS + 1):
        try:
            data = yf.download(
                tickers=ticker,
                start=start_date,
                end=end_date,
                interval="1d",
                auto_adjust=True,
                repair=False,
                keepna=False,
                actions=False,
                threads=False,
                progress=False,
                timeout=YF_TIMEOUT,
            )
            frame = extract_ticker_frame(data, ticker, 1)
            if frame is not None and not frame.empty and "Close" in frame.columns:
                close = clean_series(frame["Close"], positive_only=True)
                volume = (
                    clean_series(frame["Volume"], positive_only=False)
                    if "Volume" in frame.columns
                    else pd.Series(dtype="float64")
                )
                if not close.empty:
                    return {
                        "close": close,
                        "volume": volume,
                        "source": "fallback",
                    }
        except Exception as exc:
            logger.warning(
                "Fallback price request failed for %s: %s",
                ticker,
                exc,
            )

        if attempt < YF_FALLBACK_ATTEMPTS:
            time.sleep(YF_RETRY_BASE_DELAY * attempt)
    return None


def get_trade_date_close(close, transaction_date):
    if close.empty:
        return None
    timestamp = pd.Timestamp(transaction_date)
    eligible = close[close.index >= timestamp]
    if eligible.empty:
        return None
    return safe_float(eligible.iloc[0])


# ── Current Option-Chain Enrichment ───────────────────────────────────────

def get_ticker_object(symbol):
    if symbol not in TICKER_OBJECT_CACHE:
        TICKER_OBJECT_CACHE[symbol] = yf.Ticker(symbol)
    return TICKER_OBJECT_CACHE[symbol]


def get_option_chain_cached(symbol, expiry):
    global OPTION_CHAIN_CALL_COUNT

    cache_key = (symbol, expiry)
    if cache_key in OPTION_CHAIN_CACHE:
        return OPTION_CHAIN_CACHE[cache_key]

    if OPTION_CHAIN_CALL_COUNT >= MAX_OPTION_CHAIN_CALLS:
        logger.warning(
            "Option-chain call cap reached: %d",
            MAX_OPTION_CHAIN_CALLS,
        )
        OPTION_CHAIN_CACHE[cache_key] = None
        return None

    if expiry < today_local():
        OPTION_CHAIN_CACHE[cache_key] = None
        return None

    try:
        OPTION_CHAIN_CALL_COUNT += 1
        chain = get_ticker_object(symbol).option_chain(expiry.isoformat())
        OPTION_CHAIN_CACHE[cache_key] = chain
        return chain
    except Exception as exc:
        logger.warning(
            "Current option-chain lookup failed for %s %s: %s",
            symbol,
            expiry,
            exc,
        )
        OPTION_CHAIN_CACHE[cache_key] = None
        return None


def find_current_option_contract(trade):
    expiry = trade.get("option_expiry")
    strike = trade.get("option_strike")
    side = trade.get("option_side")

    if (
        expiry is None
        or expiry < today_local()
        or strike is None
        or side not in {"call", "put"}
    ):
        return None

    chain = get_option_chain_cached(trade["price_ticker"], expiry)
    if chain is None:
        return None

    frame = chain.calls if side == "call" else chain.puts
    if frame is None or frame.empty or "strike" not in frame.columns:
        return None

    strikes = pd.to_numeric(frame["strike"], errors="coerce")
    matching = frame[(strikes - float(strike)).abs() <= 0.01]
    if matching.empty:
        return None
    return matching.iloc[0].to_dict()


def current_option_quality_multiplier(contract):
    """
    Current data can only reduce confidence. It never increases the historical
    signal and is not treated as transaction-date evidence.
    """
    if not contract:
        return 1.0

    multiplier = 1.0
    volume = safe_float(contract.get("volume")) or 0.0
    open_interest = safe_float(contract.get("openInterest")) or 0.0

    if volume == 0 and open_interest == 0:
        multiplier *= 0.85

    bid = safe_float(contract.get("bid"))
    ask = safe_float(contract.get("ask"))
    if bid is not None and ask is not None and bid >= 0 and ask >= bid:
        midpoint = (bid + ask) / 2.0
        if midpoint > 0:
            spread_pct = (ask - bid) / midpoint
            if spread_pct > 0.50:
                multiplier *= 0.85

    return max(0.70, min(multiplier, 1.0))


# ── Scoring ────────────────────────────────────────────────────────────────

def amount_score(effective_amount):
    thresholds = (
        (15_000, 2),
        (50_000, 8),
        (100_000, 15),
        (250_000, 25),
        (500_000, 38),
        (1_000_000, 48),
        (2_000_000, 52),
    )
    for threshold, points in thresholds:
        if effective_amount < threshold:
            return float(points)
    return 55.0


def floor_score(total_low):
    if total_low >= 1_000_000:
        return 10.0
    if total_low >= 500_000:
        return 8.0
    if total_low >= 250_000:
        return 6.0
    if total_low >= 100_000:
        return 4.0
    if total_low >= 50_000:
        return 2.0
    return 0.0


def transaction_size_points(midpoint):
    midpoint = safe_float(midpoint) or 0.0
    if midpoint < 15_000:
        return 2.0
    if midpoint < 50_000:
        return 4.0
    if midpoint < 100_000:
        return 6.0
    if midpoint < 250_000:
        return 8.0
    if midpoint < 500_000:
        return 10.0
    if midpoint < 1_000_000:
        return 12.0
    return 15.0


def freshness_points(weighted_age, maximum, lookback_days=PURCHASE_LOOKBACK_DAYS):
    if weighted_age is None:
        return 0.0
    return round(
        max(0.0, 1.0 - weighted_age / lookback_days) * maximum,
        1,
    )


def sale_recency_weight(age):
    if age < 0 or age > SALE_CONTEXT_DAYS:
        return 0.0
    return max(0.25, 1.0 - age / SALE_CONTEXT_DAYS)


def entry_price_points(weighted_return):
    if weighted_return is None:
        return 0.0
    if -5.0 <= weighted_return <= 3.0:
        return 45.0
    if -10.0 <= weighted_return < -5.0:
        return 35.0
    if -15.0 <= weighted_return < -10.0:
        return 15.0
    if weighted_return < -15.0:
        return 0.0
    if 3.0 < weighted_return <= 8.0:
        return 25.0
    if 8.0 < weighted_return <= 15.0:
        return 10.0
    return 0.0


def trend_points(current_price, ma20, ma50):
    if current_price is None:
        return 0.0
    if ma20 is not None and ma50 is not None:
        if current_price >= ma20 >= ma50:
            return 20.0
        if current_price >= ma20:
            return 15.0
        if current_price >= ma50:
            return 10.0
        return 4.0
    if ma20 is not None:
        return 15.0 if current_price >= ma20 else 5.0
    return 0.0


def liquidity_points(avg_dollar_volume):
    if avg_dollar_volume is None:
        return 0.0
    if avg_dollar_volume >= 50_000_000:
        return 15.0
    if avg_dollar_volume >= 10_000_000:
        return 12.0
    if avg_dollar_volume >= 2_000_000:
        return 8.0
    if avg_dollar_volume >= 500_000:
        return 4.0
    return 0.0


def best_cluster_window(trades):
    ordered = sorted(trades, key=lambda trade: trade["transaction_date"])
    best = {"buyers": 0, "amount": 0.0, "start": None, "end": None}

    for left, start_trade in enumerate(ordered):
        start_date = start_trade["transaction_date"]
        end_date = start_date + timedelta(days=CLUSTER_WINDOW_DAYS - 1)
        window = [
            trade
            for trade in ordered[left:]
            if trade["transaction_date"] <= end_date
        ]
        buyers = len({trade["filer_id"] for trade in window})
        amount = sum(trade["amount_midpoint"] for trade in window)
        if (buyers, amount) > (best["buyers"], best["amount"]):
            best = {
                "buyers": buyers,
                "amount": amount,
                "start": start_date,
                "end": end_date,
            }
    return best


def aggregate_for_size_points(trades, include_contract=False):
    """Prevent split disclosures from earning repeated tier points."""
    grouped = defaultdict(float)

    for trade in trades:
        key = (
            trade["filer_id"],
            trade["transaction_date"],
            trade["transaction_action"],
            trade.get("option_side"),
        )
        if include_contract:
            key += (
                trade.get("option_strike"),
                trade.get("option_expiry"),
            )
        grouped[key] += trade["amount_midpoint"]
    return grouped


def calculate_stock_sale_penalty(stock_trades):
    recent_sales = [
        trade
        for trade in stock_trades
        if trade["transaction_action"] in {
            "sale_partial",
            "sale_full",
            "sale_unknown",
        }
        and trade["age"] <= SALE_CONTEXT_DAYS
    ]

    purchase_history = defaultdict(list)
    for trade in stock_trades:
        if trade["transaction_action"] == "purchase":
            purchase_history[trade["filer_id"]].append(trade)

    grouped_sales = defaultdict(list)
    for sale in recent_sales:
        grouped_sales[
            (
                sale["filer_id"],
                sale["transaction_date"],
                sale["transaction_action"],
            )
        ].append(sale)

    total_penalty = 0.0
    same_filer_full_sale = False
    sale_midpoint_total = 0.0
    partial_sale_midpoint = 0.0
    full_sale_midpoint = 0.0

    for (_, sale_date, action), sales in grouped_sales.items():
        midpoint = sum(sale["amount_midpoint"] for sale in sales)
        sale_midpoint_total += midpoint

        if action == "sale_full":
            type_weight = 1.0
            full_sale_midpoint += midpoint
        elif action == "sale_partial":
            type_weight = 0.50
            partial_sale_midpoint += midpoint
        else:
            type_weight = 0.75

        filer_id = sales[0]["filer_id"]
        same_filer_prior_purchase = any(
            purchase["transaction_date"] < sale_date
            for purchase in purchase_history.get(filer_id, [])
        )
        relationship_weight = 1.0 if same_filer_prior_purchase else 0.50

        age = (today_local() - sale_date).days
        recency = sale_recency_weight(age)
        non_discretionary_weight = min(context_weight(sale) for sale in sales)

        group_penalty = (
            transaction_size_points(midpoint)
            * type_weight
            * relationship_weight
            * recency
            * non_discretionary_weight
        )
        total_penalty += group_penalty

        if action == "sale_full" and same_filer_prior_purchase:
            later_purchase = any(
                purchase["transaction_date"] > sale_date
                for purchase in purchase_history.get(filer_id, [])
            )
            if not later_purchase:
                same_filer_full_sale = True

    return {
        "penalty": round(min(total_penalty, MAX_STOCK_SALE_PENALTY), 1),
        "sale_midpoint_total": sale_midpoint_total,
        "partial_sale_midpoint": partial_sale_midpoint,
        "full_sale_midpoint": full_sale_midpoint,
        "same_filer_full_sale": same_filer_full_sale,
        "sale_count": len(recent_sales),
    }


def has_same_day_multi_leg(option_trades, trade):
    same_day = [
        other
        for other in option_trades
        if other["filer_id"] == trade["filer_id"]
        and other["transaction_date"] == trade["transaction_date"]
    ]
    has_purchase = any(
        other["transaction_action"] == "purchase" for other in same_day
    )
    has_sale = any(
        other["transaction_action"] in {
            "sale_partial",
            "sale_full",
            "sale_unknown",
        }
        for other in same_day
    )
    return has_purchase and has_sale


def calculate_option_adjustment(option_trades, quality_by_trade_id=None):
    quality_by_trade_id = quality_by_trade_id or {}

    prior_contract_purchases = defaultdict(list)
    for trade in option_trades:
        if trade["transaction_action"] != "purchase":
            continue
        key = option_contract_key(trade)
        if key is not None:
            prior_contract_purchases[key].append(trade)

    recent_purchases = [
        trade
        for trade in option_trades
        if trade["transaction_action"] == "purchase"
        and trade["age"] <= PURCHASE_LOOKBACK_DAYS
    ]
    recent_sales = [
        trade
        for trade in option_trades
        if trade["transaction_action"] in {
            "sale_partial",
            "sale_full",
            "sale_unknown",
        }
        and trade["age"] <= SALE_CONTEXT_DAYS
    ]

    raw_adjustment = 0.0
    call_purchase_midpoint = 0.0
    put_purchase_midpoint = 0.0
    option_sale_midpoint = sum(
        trade["amount_midpoint"] for trade in recent_sales
    )
    matched_sales = 0
    unclear_sales = 0
    multi_leg_dates = set()

    # Aggregate recent option purchases by filer/date/side so split records do
    # not earn repeated size tiers. Contract-specific quality is averaged by
    # premium weight within each group.
    purchase_groups = defaultdict(list)
    for trade in recent_purchases:
        purchase_groups[
            (
                trade["filer_id"],
                trade["transaction_date"],
                trade.get("option_side"),
            )
        ].append(trade)

    for (filer_id, trade_date, side), trades in purchase_groups.items():
        midpoint = sum(trade["amount_midpoint"] for trade in trades)
        if midpoint <= 0:
            continue

        weighted_quality_numerator = 0.0
        for trade in trades:
            quality = quality_by_trade_id.get(trade["trade_id"], 1.0)
            weighted_quality_numerator += trade["amount_midpoint"] * quality
        quality = weighted_quality_numerator / midpoint

        group_context = min(context_weight(trade) for trade in trades)
        multi_leg = any(
            has_same_day_multi_leg(option_trades, trade) for trade in trades
        )
        multi_leg_weight = 0.50 if multi_leg else 1.0
        if multi_leg:
            multi_leg_dates.add((filer_id, trade_date))

        points = (
            transaction_size_points(midpoint)
            * quality
            * group_context
            * multi_leg_weight
        )

        if side == "call":
            raw_adjustment += points
            call_purchase_midpoint += midpoint
        elif side == "put":
            raw_adjustment -= 0.50 * points
            put_purchase_midpoint += midpoint
        else:
            # Unknown option side is retained and audited, but not scored.
            continue

    # Option sales only become directional when an exact earlier contract
    # purchase by the same filer/owner can be identified.
    sale_groups = defaultdict(list)
    for trade in recent_sales:
        sale_groups[
            (
                trade["filer_id"],
                trade["transaction_date"],
                trade["transaction_action"],
                trade.get("option_side"),
                trade.get("option_strike"),
                trade.get("option_expiry"),
                trade.get("owner"),
            )
        ].append(trade)

    for (_, sale_date, action, side, _, _, _), trades in sale_groups.items():
        representative = trades[0]
        key = option_contract_key(representative)
        prior_matches = []
        if key is not None:
            prior_matches = [
                purchase
                for purchase in prior_contract_purchases.get(key, [])
                if purchase["transaction_date"] < sale_date
            ]

        if not prior_matches:
            unclear_sales += len(trades)
            continue

        matched_sales += len(trades)
        midpoint = sum(trade["amount_midpoint"] for trade in trades)
        if action == "sale_full":
            sale_type_weight = 1.0
        elif action == "sale_partial":
            sale_type_weight = 0.50
        else:
            sale_type_weight = 0.75

        age = (today_local() - sale_date).days
        recency = sale_recency_weight(age)
        group_context = min(context_weight(trade) for trade in trades)
        points = (
            transaction_size_points(midpoint)
            * sale_type_weight
            * recency
            * group_context
        )

        if side == "call":
            raw_adjustment -= points
        elif side == "put":
            raw_adjustment += 0.50 * points

    adjustment = max(
        -MAX_OPTION_ADJUSTMENT,
        min(raw_adjustment, MAX_OPTION_ADJUSTMENT),
    )

    return {
        "adjustment": round(adjustment, 1),
        "call_purchase_midpoint": call_purchase_midpoint,
        "put_purchase_midpoint": put_purchase_midpoint,
        "option_sale_midpoint": option_sale_midpoint,
        "matched_option_sales": matched_sales,
        "unclear_option_sales": unclear_sales,
        "multi_leg_count": len(multi_leg_dates),
    }


def classify_result(
    conviction,
    base_conviction,
    entry,
    weighted_return,
    effective_amount,
    sale_penalty,
    option_adjustment,
    same_filer_full_sale,
):
    severe_drawdown = (
        weighted_return is not None
        and weighted_return < SEVERE_DRAWDOWN_PCT
    )

    strong_distribution = (
        same_filer_full_sale
        or sale_penalty >= 12.0
        or option_adjustment <= -8.0
    )

    if strong_distribution and (
        base_conviction >= RISK_MIN_CONVICTION
        or effective_amount >= RISK_MIN_EFFECTIVE_AMOUNT
    ):
        return "risk"

    if (
        conviction >= ACTIONABLE_MIN_CONVICTION
        and entry >= ACTIONABLE_MIN_ENTRY
        and not severe_drawdown
    ):
        return "actionable"

    if (
        weighted_return is not None
        and weighted_return <= -10.0
        and (
            conviction >= RISK_MIN_CONVICTION
            or effective_amount >= RISK_MIN_EFFECTIVE_AMOUNT
        )
    ):
        return "risk"

    if (
        conviction >= WAIT_MIN_CONVICTION
        or effective_amount >= WAIT_MIN_EFFECTIVE_AMOUNT
    ):
        return "wait"

    return "other"


def determine_flow_label(
    sale_penalty,
    option_adjustment,
    same_filer_full_sale,
    call_purchase_midpoint,
    stock_purchase_midpoint,
):
    if same_filer_full_sale:
        return "🔴 Full sale disclosed"
    if sale_penalty >= 12.0 or option_adjustment <= -8.0:
        return "🔴 Distribution"
    if sale_penalty > 3.0 or option_adjustment < 0:
        return "🟡 Mixed / trimming"
    if (
        call_purchase_midpoint >= 250_000
        and stock_purchase_midpoint < 50_000
    ):
        return "🎯 Options-led"
    return "🟢 Accumulation"


# ── Analytics ─────────────────────────────────────────────────────────────

def active_ticker_groups(trades):
    groups = defaultdict(list)
    for trade in trades:
        groups[trade["ticker"]].append(trade)

    active = {}
    for ticker, ticker_trades in groups.items():
        has_recent_stock_purchase = any(
            trade["asset_class"] == "stock"
            and trade["transaction_action"] == "purchase"
            and trade["age"] <= PURCHASE_LOOKBACK_DAYS
            for trade in ticker_trades
        )
        has_recent_call_purchase = any(
            trade["asset_class"] == "option"
            and trade["transaction_action"] == "purchase"
            and trade.get("option_side") == "call"
            and trade["age"] <= PURCHASE_LOOKBACK_DAYS
            for trade in ticker_trades
        )

        # Preserve the bot's purpose: investigate tickers with a recent bullish
        # purchase signal, while using sales and puts as context.
        if has_recent_stock_purchase or has_recent_call_purchase:
            active[ticker] = ticker_trades
    return active


def preliminary_group_strength(ticker_trades):
    stock_buys = sum(
        trade["amount_midpoint"]
        for trade in ticker_trades
        if trade["asset_class"] == "stock"
        and trade["transaction_action"] == "purchase"
        and trade["age"] <= PURCHASE_LOOKBACK_DAYS
    )
    call_buys = sum(
        trade["amount_midpoint"]
        for trade in ticker_trades
        if trade["asset_class"] == "option"
        and trade["transaction_action"] == "purchase"
        and trade.get("option_side") == "call"
        and trade["age"] <= PURCHASE_LOOKBACK_DAYS
    )
    return stock_buys + 0.50 * call_buys


def analyse_ticker(ticker, trades, price_data, quality_by_trade_id=None):
    close = price_data["close"]
    volume = price_data.get("volume", pd.Series(dtype="float64"))
    current_price = safe_float(close.iloc[-1]) if not close.empty else None
    if current_price is None:
        return None

    recent_stock_purchases = [
        trade
        for trade in trades
        if trade["asset_class"] == "stock"
        and trade["transaction_action"] == "purchase"
        and trade["age"] <= PURCHASE_LOOKBACK_DAYS
    ]
    option_trades = [
        trade for trade in trades if trade["asset_class"] == "option"
    ]
    stock_trades = [
        trade for trade in trades if trade["asset_class"] == "stock"
    ]

    total_low = sum(trade["amount_low"] for trade in recent_stock_purchases)
    total_mid = sum(
        trade["amount_midpoint"] for trade in recent_stock_purchases
    )
    total_high = sum(trade["amount_high"] for trade in recent_stock_purchases)

    # Entry and price-versus-buy analytics use stock purchases when present.
    # For options-led tickers, call purchase dates are a fallback reference.
    reference_trades = recent_stock_purchases
    reference_kind = "stock"
    if not reference_trades:
        reference_trades = [
            trade
            for trade in option_trades
            if trade["transaction_action"] == "purchase"
            and trade.get("option_side") == "call"
            and trade["age"] <= PURCHASE_LOOKBACK_DAYS
        ]
        reference_kind = "call"

    reference_midpoint_total = sum(
        trade["amount_midpoint"] for trade in reference_trades
    )
    if reference_midpoint_total <= 0:
        return None

    priced_trades = []
    priced_midpoint = 0.0

    for trade in reference_trades:
        reference_close = get_trade_date_close(
            close,
            trade["transaction_date"],
        )
        if reference_close is None or reference_close <= 0:
            continue

        trade_return = (
            (current_price - reference_close) / reference_close * 100.0
        )
        if not math.isfinite(trade_return):
            continue

        weight = (
            trade["amount_midpoint"]
            if trade["amount_midpoint"] > 0
            else 1.0
        )
        priced_midpoint += trade["amount_midpoint"]
        priced_trades.append(
            {
                **trade,
                "reference_close": reference_close,
                "return_pct": trade_return,
                "weight": weight,
            }
        )

    price_coverage = (
        priced_midpoint / reference_midpoint_total
        if reference_midpoint_total
        else 0.0
    )
    if not priced_trades or price_coverage < MIN_PRICE_COVERAGE:
        logger.warning(
            "Skipping %s: price coverage %.1f%% below %.1f%%.",
            ticker,
            price_coverage * 100,
            MIN_PRICE_COVERAGE * 100,
        )
        return None

    total_weight = sum(trade["weight"] for trade in priced_trades)
    weighted_return = sum(
        trade["return_pct"] * trade["weight"]
        for trade in priced_trades
    ) / total_weight
    weighted_age = sum(
        trade["age"] * trade["weight"]
        for trade in priced_trades
    ) / total_weight

    latest_trade_date = max(
        trade["transaction_date"] for trade in reference_trades
    )
    latest_age = (today_local() - latest_trade_date).days

    unique_buyers = {
        trade["filer_id"] for trade in recent_stock_purchases
    }
    display_names = sorted(
        {trade["display_name"] for trade in recent_stock_purchases}
    )
    transaction_count = len(recent_stock_purchases)
    repeat_purchase_count = max(0, transaction_count - len(unique_buyers))
    cluster = (
        best_cluster_window(recent_stock_purchases)
        if recent_stock_purchases
        else {"buyers": 0, "amount": 0.0, "start": None, "end": None}
    )

    effective_amount = 0.60 * total_mid + 0.40 * total_low
    if recent_stock_purchases:
        base_conviction = (
            amount_score(effective_amount)
            + floor_score(total_low)
            + min(max(cluster["buyers"] - 1, 0) * 5.0, 15.0)
            + min(repeat_purchase_count * 5.0, 10.0)
            + freshness_points(weighted_age, 10.0)
        )
    else:
        base_conviction = 0.0
    base_conviction = round(min(base_conviction, 100.0), 1)

    sale_metrics = calculate_stock_sale_penalty(stock_trades)
    option_metrics = calculate_option_adjustment(
        option_trades,
        quality_by_trade_id=quality_by_trade_id,
    )

    final_conviction = max(
        0.0,
        min(
            100.0,
            base_conviction
            - sale_metrics["penalty"]
            + option_metrics["adjustment"],
        ),
    )
    final_conviction = round(final_conviction, 1)

    ma20 = safe_float(close.tail(20).mean()) if len(close) >= 20 else None
    ma50 = safe_float(close.tail(50).mean()) if len(close) >= 50 else None

    avg_dollar_volume = None
    if not volume.empty:
        aligned = pd.concat(
            [close.rename("close"), volume.rename("volume")],
            axis=1,
        ).dropna()
        if not aligned.empty:
            avg_dollar_volume = safe_float(
                (aligned["close"] * aligned["volume"]).tail(20).mean()
            )

    entry = (
        entry_price_points(weighted_return)
        + freshness_points(weighted_age, 20.0)
        + trend_points(current_price, ma20, ma50)
        + liquidity_points(avg_dollar_volume)
    )
    entry = round(min(entry, 100.0), 1)

    if len(unique_buyers) < 2 and weighted_return > SINGLE_BUYER_CHASE_LIMIT_PCT:
        chase_flag = True
    elif len(unique_buyers) >= 2 and weighted_return > CLUSTER_CHASE_LIMIT_PCT:
        chase_flag = True
    else:
        chase_flag = False

    category = classify_result(
        conviction=final_conviction,
        base_conviction=base_conviction,
        entry=entry,
        weighted_return=weighted_return,
        effective_amount=effective_amount,
        sale_penalty=sale_metrics["penalty"],
        option_adjustment=option_metrics["adjustment"],
        same_filer_full_sale=sale_metrics["same_filer_full_sale"],
    )
    if chase_flag and category == "actionable":
        category = "wait"

    flow_label = determine_flow_label(
        sale_penalty=sale_metrics["penalty"],
        option_adjustment=option_metrics["adjustment"],
        same_filer_full_sale=sale_metrics["same_filer_full_sale"],
        call_purchase_midpoint=option_metrics["call_purchase_midpoint"],
        stock_purchase_midpoint=total_mid,
    )

    owners = Counter(trade["owner"] for trade in recent_stock_purchases)
    trade_ids = sorted(
        trade["trade_id"]
        for trade in trades
        if (
            trade["age"] <= SALE_CONTEXT_DAYS
            or (
                trade["asset_class"] == "option"
                and trade["age"] <= OPTION_MATCH_LOOKBACK_DAYS
            )
        )
    )

    buyer_names = display_names
    if not buyer_names and option_metrics["call_purchase_midpoint"] > 0:
        buyer_names = sorted(
            {
                trade["display_name"]
                for trade in option_trades
                if trade["transaction_action"] == "purchase"
                and trade.get("option_side") == "call"
                and trade["age"] <= PURCHASE_LOOKBACK_DAYS
            }
        )

    result = {
        "ticker": ticker,
        "price_ticker": trades[0]["price_ticker"],
        "trade_ids": trade_ids,
        "category": category,
        "base_conviction": base_conviction,
        "conviction_score": final_conviction,
        "entry_score": entry,
        "priority_score": round(0.65 * final_conviction + 0.35 * entry, 1),
        "total_low": total_low,
        "total_mid": total_mid,
        "total_high": total_high,
        "effective_amount": effective_amount,
        "unique_buyers": len(unique_buyers),
        "cluster_buyers": cluster["buyers"],
        "cluster_amount": cluster["amount"],
        "transaction_count": transaction_count,
        "repeat_purchase_count": repeat_purchase_count,
        "buyer_names": buyer_names,
        "owner_counts": dict(owners),
        "latest_age": latest_age,
        "weighted_age": round(weighted_age, 1),
        "weighted_return": round(weighted_return, 1),
        "reference_kind": reference_kind,
        "current_price": current_price,
        "ma20": ma20,
        "ma50": ma50,
        "avg_dollar_volume": avg_dollar_volume,
        "price_coverage": round(price_coverage, 3),
        "price_source": price_data.get("source", "unknown"),
        "severe_drawdown": weighted_return < SEVERE_DRAWDOWN_PCT,
        "chase_flag": chase_flag,
        "stock_sale_penalty": sale_metrics["penalty"],
        "stock_sale_midpoint": sale_metrics["sale_midpoint_total"],
        "partial_sale_midpoint": sale_metrics["partial_sale_midpoint"],
        "full_sale_midpoint": sale_metrics["full_sale_midpoint"],
        "same_filer_full_sale": sale_metrics["same_filer_full_sale"],
        "option_adjustment": option_metrics["adjustment"],
        "call_purchase_midpoint": option_metrics["call_purchase_midpoint"],
        "put_purchase_midpoint": option_metrics["put_purchase_midpoint"],
        "option_sale_midpoint": option_metrics["option_sale_midpoint"],
        "matched_option_sales": option_metrics["matched_option_sales"],
        "unclear_option_sales": option_metrics["unclear_option_sales"],
        "multi_leg_count": option_metrics["multi_leg_count"],
        "flow_label": flow_label,
        "industry": "N/A",
        "trades": trades,
    }

    logger.info(
        "%s score audit | base=%.1f | sale=-%.1f | options=%+.1f | "
        "final=%.1f | entry=%.1f | category=%s | flow=%s",
        ticker,
        result["base_conviction"],
        result["stock_sale_penalty"],
        result["option_adjustment"],
        result["conviction_score"],
        result["entry_score"],
        result["category"],
        result["flow_label"],
    )
    return result


def option_enrichment_shortlist(results):
    eligible = [
        result
        for result in results
        if result["call_purchase_midpoint"] > 0
        and (
            result["base_conviction"] >= OPTION_ENRICHMENT_MIN_BASE_CONVICTION
            or result["call_purchase_midpoint"]
            >= OPTION_ENRICHMENT_MIN_CALL_PREMIUM
        )
    ]
    return sorted(
        eligible,
        key=lambda result: (
            result["conviction_score"],
            result["call_purchase_midpoint"],
        ),
        reverse=True,
    )[:MAX_OPTION_ENRICHMENT_TICKERS]


def enrich_shortlisted_options(results, price_bundle):
    if not ENABLE_CURRENT_OPTION_ENRICHMENT:
        return results

    quality_by_ticker = defaultdict(dict)

    for result in option_enrichment_shortlist(results):
        for trade in result["trades"]:
            if not (
                trade["asset_class"] == "option"
                and trade["transaction_action"] == "purchase"
                and trade["age"] <= PURCHASE_LOOKBACK_DAYS
                and trade.get("option_side") in {"call", "put"}
            ):
                continue

            # Skip expired, unparsed or unavailable contracts without penalty.
            if (
                trade.get("option_expiry") is None
                or trade["option_expiry"] < today_local()
                or trade.get("option_strike") is None
            ):
                continue

            contract = find_current_option_contract(trade)
            quality = current_option_quality_multiplier(contract)
            quality_by_ticker[result["ticker"]][trade["trade_id"]] = quality

    if not quality_by_ticker:
        return results

    refreshed = []
    for result in results:
        ticker = result["ticker"]
        if ticker not in quality_by_ticker:
            refreshed.append(result)
            continue

        price_data = price_bundle.get(result["price_ticker"])
        if price_data is None:
            refreshed.append(result)
            continue

        recalculated = analyse_ticker(
            ticker,
            result["trades"],
            price_data,
            quality_by_trade_id=quality_by_ticker[ticker],
        )
        refreshed.append(recalculated or result)

    return refreshed


def process_all_trades(trades):
    groups = active_ticker_groups(trades)
    if not groups:
        return []

    reference_dates = [
        trade["transaction_date"]
        for ticker_trades in groups.values()
        for trade in ticker_trades
        if trade["age"] <= PURCHASE_LOOKBACK_DAYS
    ]
    if not reference_dates:
        return []

    earliest = min(reference_dates)
    history_start = min(
        earliest - timedelta(days=TECHNICAL_HISTORY_BUFFER_DAYS),
        today_local() - timedelta(days=TECHNICAL_HISTORY_BUFFER_DAYS),
    )
    history_end = today_local() + timedelta(days=1)

    price_tickers = [
        ticker_trades[0]["price_ticker"]
        for ticker_trades in groups.values()
    ]
    bundle = download_price_bundle(
        price_tickers,
        history_start.isoformat(),
        history_end.isoformat(),
    )

    missing = [
        (ticker, ticker_trades)
        for ticker, ticker_trades in groups.items()
        if ticker_trades[0]["price_ticker"] not in bundle
    ]
    missing.sort(
        key=lambda item: preliminary_group_strength(item[1]),
        reverse=True,
    )

    for ticker, ticker_trades in missing[:MAX_INDIVIDUAL_FALLBACKS]:
        price_symbol = ticker_trades[0]["price_ticker"]
        price_data = download_single_ticker(
            price_symbol,
            history_start.isoformat(),
            history_end.isoformat(),
        )
        if price_data is not None:
            bundle[price_symbol] = price_data

    results = []
    for ticker, ticker_trades in groups.items():
        price_symbol = ticker_trades[0]["price_ticker"]
        price_data = bundle.get(price_symbol)
        if price_data is None:
            logger.warning("Skipping %s: no usable Yahoo price data.", ticker)
            continue

        analysed = analyse_ticker(ticker, ticker_trades, price_data)
        if analysed is not None:
            results.append(analysed)

    results = enrich_shortlisted_options(results, bundle)
    return results


# ── Selection and Repeat-Alert Control ────────────────────────────────────

def select_results(results):
    actionable = sorted(
        [result for result in results if result["category"] == "actionable"],
        key=lambda result: (
            result["priority_score"],
            result["effective_amount"],
        ),
        reverse=True,
    )[:MAX_ACTIONABLE]

    wait = sorted(
        [result for result in results if result["category"] == "wait"],
        key=lambda result: (
            result["conviction_score"],
            result["effective_amount"],
        ),
        reverse=True,
    )[:MAX_WAIT]

    risk = sorted(
        [result for result in results if result["category"] == "risk"],
        key=lambda result: (
            result["conviction_score"],
            result["stock_sale_penalty"],
            -result["weighted_return"],
        ),
        reverse=True,
    )[:MAX_RISK]

    selected = (actionable + wait + risk)[:MAX_TOTAL_RESULTS]
    return actionable, wait, risk, selected


def select_nearest_signals(results):
    """
    Select informative near-misses for a status-only Telegram message.

    These are not promoted into actionable/wait/risk categories. Conviction
    remains the primary sort key so a high entry score cannot make a weak
    congressional signal look attractive.
    """
    candidates = [
        result
        for result in results
        if result["category"] == "other"
        and (
            result["conviction_score"] >= NEAREST_SIGNAL_MIN_CONVICTION
            or result["call_purchase_midpoint"]
            >= OPTION_ENRICHMENT_MIN_CALL_PREMIUM
        )
    ]

    candidates.sort(
        key=lambda result: (
            result["conviction_score"],
            result["entry_score"],
            result["effective_amount"],
            result["call_purchase_midpoint"],
        ),
        reverse=True,
    )
    return candidates[:MAX_NEAREST_SIGNALS]


def change_signature(result):
    return {
        "trade_ids": result["trade_ids"],
        "category": result["category"],
        "conviction_score": result["conviction_score"],
        "entry_score": result["entry_score"],
        "cluster_buyers": result["cluster_buyers"],
        "flow_label": result["flow_label"],
    }


def filter_changed_results(results):
    if not SEND_ONLY_NEW_OR_CHANGED:
        return results

    previous_state = load_json(STATE_FILE, {})
    changed = []

    for result in results:
        old = previous_state.get(result["ticker"])
        new = change_signature(result)
        if old is None:
            changed.append(result)
            continue
        if old.get("trade_ids") != new["trade_ids"]:
            changed.append(result)
            continue
        if old.get("category") != new["category"]:
            changed.append(result)
            continue
        if old.get("flow_label") != new["flow_label"]:
            changed.append(result)
            continue
        if abs(
            float(old.get("conviction_score", 0))
            - new["conviction_score"]
        ) >= MIN_SCORE_CHANGE_TO_NOTIFY:
            changed.append(result)
            continue
        if abs(
            float(old.get("entry_score", 0))
            - new["entry_score"]
        ) >= MIN_SCORE_CHANGE_TO_NOTIFY:
            changed.append(result)
            continue
        if int(old.get("cluster_buyers", 0)) != new["cluster_buyers"]:
            changed.append(result)

    return changed


def save_notification_state(results):
    state = {
        result["ticker"]: change_signature(result)
        for result in results
    }
    save_json(STATE_FILE, state)


# ── Audit Logging ─────────────────────────────────────────────────────────

def log_selected_transaction_audit(results):
    for result in results:
        logger.info(
            "%s selected | base=%.1f sale=-%.1f options=%+.1f final=%.1f "
            "entry=%.1f flow=%s",
            result["ticker"],
            result["base_conviction"],
            result["stock_sale_penalty"],
            result["option_adjustment"],
            result["conviction_score"],
            result["entry_score"],
            result["flow_label"],
        )

        for trade in sorted(
            result["trades"],
            key=lambda item: (
                item["transaction_date"],
                item["trade_id"],
            ),
        ):
            if trade["age"] > OPTION_MATCH_LOOKBACK_DAYS:
                continue

            option_details = ""
            if trade["asset_class"] == "option":
                option_details = (
                    f" side={trade.get('option_side')}"
                    f" strike={trade.get('option_strike')}"
                    f" expiry={trade.get('option_expiry')}"
                )

            logger.info(
                "%s tx | id=%s filer=%s date=%s asset=%s action=%s "
                "range=%s-%s%s",
                result["ticker"],
                trade["trade_id"],
                trade["filer_name"],
                trade["transaction_date"],
                trade["asset_class"],
                trade["transaction_action"],
                format_amount(trade["amount_low"]),
                format_amount(trade["amount_high"]),
                option_details,
            )


# ── Telegram Output ────────────────────────────────────────────────────────

def buyer_label(result):
    names = result["buyer_names"]
    if not names:
        return "Unknown"
    if len(names) <= 3:
        return ", ".join(names)
    return f"{names[0]}, ... +{len(names) - 1}"


def option_summary(result):
    parts = []
    if result["call_purchase_midpoint"] > 0:
        parts.append(
            f"Calls {format_amount(result['call_purchase_midpoint'])}"
        )
    if result["put_purchase_midpoint"] > 0:
        parts.append(
            f"Puts {format_amount(result['put_purchase_midpoint'])}"
        )
    if result["matched_option_sales"] > 0:
        parts.append(f"{result['matched_option_sales']} matched opt sale")
    if result["unclear_option_sales"] > 0:
        parts.append(f"{result['unclear_option_sales']} unclear opt sale")
    if result["multi_leg_count"] > 0:
        parts.append("possible spread")
    return ", ".join(parts)


def result_line(result):
    flags = []
    if result["cluster_buyers"] >= 2:
        flags.append("👥")
    if result["severe_drawdown"]:
        flags.append("⚠️")
    if result["chase_flag"]:
        flags.append("🏃")
    flag_text = "".join(flags)

    if result["total_mid"] > 0:
        amount_text = (
            f"Buy est {format_amount(result['total_mid'])} "
            f"[{format_amount(result['total_low'])}-"
            f"{format_amount(result['total_high'])}]"
        )
        buyer_text = (
            f"{result['unique_buyers']} buyers "
            f"({result['cluster_buyers']}/{CLUSTER_WINDOW_DAYS}d)"
        )
    else:
        amount_text = (
            f"Call premium est "
            f"{format_amount(result['call_purchase_midpoint'])}"
        )
        buyer_text = f"Signals: {buyer_label(result)}"

    versus_label = (
        "Vs buys" if result["reference_kind"] == "stock" else "Vs calls"
    )
    option_text = option_summary(result)
    option_suffix = f" | {option_text}" if option_text else ""

    return (
        f"{flag_text}${result['ticker']} | "
        f"C{result['conviction_score']:.0f}/E{result['entry_score']:.0f} | "
        f"{amount_text} | {buyer_text} | "
        f"Wtd age {result['weighted_age']:.0f}d | "
        f"{versus_label} {result['weighted_return']:+.1f}% | "
        f"{result['flow_label']}{option_suffix} | "
        f"{buyer_label(result)}"
    )


def build_messages(actionable, wait, risk, total_analysed):
    sections = []
    if actionable:
        sections.append(("🔥 BEST ACTIONABLE", actionable))
    if wait:
        sections.append(("👀 HIGH CONVICTION — WAIT FOR ENTRY", wait))
    if risk:
        sections.append(("⚠️ CONFLICTING / HIGHER RISK", risk))

    if not sections:
        return []

    header = (
        "📊 CONGRESS TRADE OPPORTUNITIES\n"
        f"Analysed: {total_analysed} tickers | "
        f"Shown: {sum(len(items) for _, items in sections)}\n"
        "C = final conviction after sales/options | E = entry quality\n\n"
    )

    footer = (
        "\nBuy est = stock-purchase disclosure midpoints only\n"
        "Calls/Puts = disclosed option-premium midpoints; not stock exposure\n"
        "Sales affect C as a capped penalty; they are not netted against dollars\n"
        f"👥 = cluster within {CLUSTER_WINDOW_DAYS} days | "
        "⚠️ = severe drawdown | 🏃 = price-chase risk\n"
        "Screening signal only; verify filings and current news before buying."
    )

    messages = []
    current = header

    for title, items in sections:
        section_header = f"{title}\n"
        if (
            len(current) + len(section_header) + len(footer)
            > TELEGRAM_CHAR_LIMIT
        ):
            current += footer
            messages.append(current)
            current = "📊 CONGRESS TRADE OPPORTUNITIES [CONTINUED]\n\n"
        current += section_header

        for result in items:
            line = result_line(result) + "\n"
            if len(current) + len(line) + len(footer) > TELEGRAM_CHAR_LIMIT:
                current += footer
                messages.append(current)
                current = (
                    "📊 CONGRESS TRADE OPPORTUNITIES [CONTINUED]\n\n"
                    + section_header
                )
            current += line
        current += "\n"

    current += footer
    messages.append(current)
    return messages


def build_no_qualifying_message(nearest, total_analysed):
    """Build a completion/status report when strict thresholds select none."""
    lines = [
        "📊 CONGRESS TRADE MONITOR",
        "No ticker met the strict actionable, wait or risk thresholds.",
        f"Analysed: {total_analysed} tickers | Qualified: 0",
        "",
    ]

    if nearest:
        lines.append("🔎 NEAREST SIGNALS — NOT QUALIFIED")
        for result in nearest:
            lines.append(result_line(result))
        lines.append("")
    else:
        lines.append("No near-miss reached the minimum reporting level.")
        lines.append("")

    lines.extend(
        [
            "Current thresholds:",
            (
                f"Actionable: C≥{ACTIONABLE_MIN_CONVICTION:.0f} and "
                f"E≥{ACTIONABLE_MIN_ENTRY:.0f}"
            ),
            (
                f"Wait: C≥{WAIT_MIN_CONVICTION:.0f} or effective stock buys "
                f"≥{format_amount(WAIT_MIN_EFFECTIVE_AMOUNT)}"
            ),
            (
                "Risk: meaningful bullish activity plus severe drawdown or "
                "strong distribution"
            ),
            "",
            "C = final conviction after sales/options | E = entry quality",
            "Near-miss entries are monitoring context, not purchase signals.",
        ]
    )

    return ["\n".join(lines)]


def build_no_recent_activity_message():
    return [
        (
            "📊 CONGRESS TRADE MONITOR\n"
            "Feed checked successfully, but no relevant recent bullish "
            "purchase signals were retained."
        )
    ]


async def send_messages(messages):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    for index, message in enumerate(messages):
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
        )
        if index < len(messages) - 1:
            await asyncio.sleep(INTER_CHUNK_DELAY)


async def send_failure_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Congress monitor failure\n{message}",
        )
    except Exception:
        logger.exception("Could not send failure alert.")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID.")
        return

    if not acquire_lock():
        return

    try:
        initialise_yfinance()

        trades = fetch_trades()
        if trades is None:
            asyncio.run(
                send_failure_alert(
                    "The congressional trade feed could not be retrieved."
                )
            )
            return
        if not trades:
            logger.info(
                "Feed retrieved, but no relevant recent transactions found."
            )
            if SEND_STATUS_WHEN_NO_QUALIFYING:
                asyncio.run(
                    send_messages(build_no_recent_activity_message())
                )
                logger.info("Sent no-recent-activity Telegram status.")
            return

        analysed = process_all_trades(trades)
        if not analysed:
            asyncio.run(
                send_failure_alert(
                    "No ticker produced usable price analytics."
                )
            )
            return

        actionable, wait, risk, qualified = select_results(analysed)

        if not qualified:
            nearest = select_nearest_signals(analysed)
            logger.info(
                "No ticker met the strict reporting thresholds; "
                "sending completion status with %d near-miss signal(s).",
                len(nearest),
            )
            if SEND_STATUS_WHEN_NO_QUALIFYING:
                messages = build_no_qualifying_message(
                    nearest=nearest,
                    total_analysed=len(analysed),
                )
                asyncio.run(send_messages(messages))
                logger.info(
                    "Sent %d no-qualifying-status Telegram message(s).",
                    len(messages),
                )
            return

        selected = filter_changed_results(qualified)

        if not selected:
            logger.info(
                "Qualified opportunities exist, but none are new or "
                "materially changed under repeat-alert suppression."
            )
            if SEND_STATUS_WHEN_NO_QUALIFYING:
                nearest = qualified[:MAX_NEAREST_SIGNALS]
                messages = build_no_qualifying_message(
                    nearest=nearest,
                    total_analysed=len(analysed),
                )
                # Make the status accurate when suppression, rather than the
                # investment thresholds, caused the empty notification set.
                messages[0] = messages[0].replace(
                    "No ticker met the strict actionable, wait or risk thresholds.",
                    "Qualified signals were found, but none changed materially.",
                ).replace(
                    "Qualified: 0",
                    f"Qualified: {len(qualified)} | New/changed: 0",
                ).replace(
                    "🔎 NEAREST SIGNALS — NOT QUALIFIED",
                    "📌 QUALIFIED SIGNALS — UNCHANGED",
                )
                asyncio.run(send_messages(messages))
                logger.info("Sent unchanged-signals Telegram status.")
            return

        selected_by_ticker = {
            result["ticker"]: result for result in selected
        }
        actionable = [
            result
            for result in actionable
            if result["ticker"] in selected_by_ticker
        ]
        wait = [
            result
            for result in wait
            if result["ticker"] in selected_by_ticker
        ]
        risk = [
            result
            for result in risk
            if result["ticker"] in selected_by_ticker
        ]

        log_selected_transaction_audit(selected)

        messages = build_messages(
            actionable=actionable,
            wait=wait,
            risk=risk,
            total_analysed=len(analysed),
        )
        if not messages:
            logger.info("No messages were generated.")
            return

        asyncio.run(send_messages(messages))
        save_notification_state(selected)
        logger.info(
            "Sent %d Telegram message(s). Yahoo option-chain calls=%d.",
            len(messages),
            OPTION_CHAIN_CALL_COUNT,
        )

    except Exception as exc:
        logger.exception("Unhandled monitor failure: %s", exc)
        asyncio.run(send_failure_alert(str(exc)[:500]))
    finally:
        release_lock()


if __name__ == "__main__":
    main()
