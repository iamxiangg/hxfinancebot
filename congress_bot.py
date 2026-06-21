import os
import re
import time
import math
import logging
import asyncio
from collections import Counter
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from telegram import Bot
from urllib3.util.retry import Retry


# =============================================================================
# ENVIRONMENT
# =============================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RAW_KADOA_URL = (
    "https://raw.githubusercontent.com/"
    "kadoa-org/congress-trading-monitor/"
    "main/public/data/trades.json"
)


# =============================================================================
# STRATEGY SETTINGS
# =============================================================================

LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

# Only retain purchases made within this period.
MAX_DAYS_AGO = 45

# Buyers are treated as a cluster only when purchases occur within this window.
CLUSTER_WINDOW_DAYS = 14

# Price history required for moving averages and liquidity calculations.
TECHNICAL_HISTORY_DAYS = 160

# At least this proportion of disclosed midpoint value must have valid prices.
MIN_PRICE_COVERAGE = 0.75

# A large fall is treated as a risk signal, not automatically as a good entry.
SEVERE_DRAWDOWN_PCT = -15.0

# Price-chase limits.
SINGLE_BUYER_CHASE_LIMIT_PCT = 8.0
CLUSTER_CHASE_LIMIT_PCT = 15.0

# Minimum scores for each output category.
ACTIONABLE_MIN_CONVICTION = 60.0
ACTIONABLE_MIN_ENTRY = 60.0

WAIT_MIN_CONVICTION = 70.0
WAIT_MIN_EFFECTIVE_AMOUNT = 500_000

RISK_MIN_CONVICTION = 40.0
RISK_MIN_EFFECTIVE_AMOUNT = 250_000

# Telegram output limits.
MAX_ACTIONABLE = 8
MAX_WAIT = 6
MAX_RISK = 6
MAX_TOTAL_RESULTS = 20

# Set to True only if industry data is important.
# Keeping this False materially reduces Yahoo requests.
FETCH_INDUSTRY = False
INDUSTRY_REQUEST_DELAY = 0.5


# =============================================================================
# YAHOO FINANCE SETTINGS
# =============================================================================

YF_BATCH_SIZE = 30
YF_BATCH_RETRIES = 3
YF_FALLBACK_RETRIES = 2
YF_TIMEOUT = 30
YF_THREADS = 4
YF_RETRY_BASE_DELAY = 4

YF_CACHE_DIRECTORY = Path(
    os.getenv("YF_CACHE_DIRECTORY", "./yfinance_cache")
)

# Add only explicit mappings that you have verified.
YAHOO_TICKER_OVERRIDES = {
    "BRK.B": "BRK-B",
    "BF.B": "BF-B",
}


# =============================================================================
# TELEGRAM AND OPERATIONAL SETTINGS
# =============================================================================

TELEGRAM_CHAR_LIMIT = 3800
INTER_CHUNK_DELAY = 1.5

LOCK_FILE = Path("congress_bot.lock")
LOCK_STALE_HOURS = 6

LOG_FILE = "congress_bot.log"


# =============================================================================
# LOGGING
# =============================================================================

logger = logging.getLogger("congress_bot")
logger.setLevel(logging.INFO)
logger.handlers.clear()

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)


# =============================================================================
# GENERAL HELPERS
# =============================================================================

def today_local() -> date:
    """Return today's date in Singapore."""
    return datetime.now(LOCAL_TIMEZONE).date()


def parse_date(value):
    """Parse YYYY-MM-DD into a date."""
    try:
        return datetime.strptime(
            str(value),
            "%Y-%m-%d",
        ).date()
    except (TypeError, ValueError):
        return None


def safe_float(value):
    """Return a finite float or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return number if math.isfinite(number) else None


def format_amount(value):
    """Format monetary values for Telegram."""
    value = safe_float(value)

    if value is None:
        return "$0"

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}b"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}m"

    if value >= 1_000:
        return f"${value / 1_000:.0f}k"

    return f"${value:.0f}"


def compact_name(full_name):
    """
    Return a display surname while removing suffixes such as Jr and III.

    Filer IDs, rather than this display name, are used for buyer counting.
    """
    suffixes = {
        "jr",
        "jr.",
        "sr",
        "sr.",
        "ii",
        "iii",
        "iv",
    }

    parts = str(full_name or "").strip().split()

    while parts and parts[-1].lower() in suffixes:
        parts.pop()

    return parts[-1] if parts else "Unknown"


def normalise_ticker(value):
    """Return a cleaned ticker or None."""
    ticker = str(value or "").strip().upper()

    if ticker.lower() in {
        "",
        "null",
        "none",
        "--",
        "n/a",
        "nan",
    }:
        return None

    if not re.fullmatch(r"[A-Z0-9.^=\-]+", ticker):
        return None

    return ticker


def yahoo_ticker(ticker):
    """Map a disclosed ticker to a verified Yahoo ticker."""
    return YAHOO_TICKER_OVERRIDES.get(ticker, ticker)


def is_eligible_equity(asset_type):
    """
    Retain ordinary equity securities while excluding bonds, options,
    funds, warrants and preferred shares.
    """
    text = str(asset_type or "").strip().lower()

    if text == "st":
        return True

    eligible_terms = (
        "common stock",
        "ordinary share",
        "depositary receipt",
        "equity",
        "stock",
    )

    excluded_terms = (
        "option",
        "bond",
        "note",
        "fund",
        "etf",
        "warrant",
        "preferred",
    )

    return (
        any(term in text for term in eligible_terms)
        and not any(term in text for term in excluded_terms)
    )


def estimate_amounts(item):
    """
    Return the disclosed lower bound, midpoint estimate and upper bound.
    """
    low = safe_float(item.get("amount_range_low"))
    high = safe_float(item.get("amount_range_high"))

    if (
        low is None
        or high is None
        or low < 0
        or high < low
    ):
        return 0.0, 0.0, 0.0

    midpoint = (low + high) / 2.0

    return low, midpoint, high


def chunked(sequence, size):
    """Yield fixed-size slices from a sequence."""
    for index in range(0, len(sequence), size):
        yield sequence[index:index + size]


# =============================================================================
# LOCKING AND HTTP
# =============================================================================

def acquire_lock():
    """
    Prevent overlapping runs.

    A lock older than LOCK_STALE_HOURS is treated as stale.
    """
    if LOCK_FILE.exists():
        try:
            age_hours = (
                time.time() - LOCK_FILE.stat().st_mtime
            ) / 3600

            if age_hours > LOCK_STALE_HOURS:
                logger.warning(
                    "Removing stale lock file: %s",
                    LOCK_FILE,
                )
                LOCK_FILE.unlink()
            else:
                logger.error(
                    "Another bot run appears active: %s",
                    LOCK_FILE,
                )
                return False

        except OSError as exc:
            logger.error(
                "Could not inspect lock file: %s",
                exc,
            )
            return False

    try:
        descriptor = os.open(
            str(LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )

        os.write(
            descriptor,
            str(os.getpid()).encode("utf-8"),
        )
        os.close(descriptor)

        return True

    except FileExistsError:
        logger.error(
            "Another bot run appears active: %s",
            LOCK_FILE,
        )
        return False


def release_lock():
    """Remove the process lock."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not remove lock file: %s",
            exc,
        )


def build_http_session():
    """Create a requests session with retry and back-off behaviour."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(
            429,
            500,
            502,
            503,
            504,
        ),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )

    session = requests.Session()

    adapter = HTTPAdapter(
        max_retries=retry
    )

    session.mount(
        "https://",
        adapter,
    )

    session.mount(
        "http://",
        adapter,
    )

    session.headers.update(
        {
            "User-Agent": "CongressPurchaseMonitor/3.0"
        }
    )

    return session


# =============================================================================
# TRADE RETRIEVAL AND DEDUPLICATION
# =============================================================================

def fetch_trades():
    """
    Retrieve and retain recent congressional equity purchases.

    The function deduplicates primarily by the upstream transaction ID.
    """
    session = build_http_session()

    try:
        response = session.get(
            RAW_KADOA_URL,
            timeout=30,
        )

        response.raise_for_status()
        payload = response.json()

    except Exception as exc:
        logger.error(
            "Trade-data retrieval failed: %s",
            exc,
        )
        return None

    finally:
        session.close()

    if not isinstance(payload, list):
        logger.error(
            "Unexpected trade-data payload type: %s",
            type(payload).__name__,
        )
        return None

    retained = []
    seen_keys = set()

    for item in payload:
        if not isinstance(item, dict):
            continue

        transaction_type = str(
            item.get(
                "transaction_type",
                item.get("type", ""),
            )
        ).lower()

        if (
            "purchase" not in transaction_type
            and "buy" not in transaction_type
        ):
            continue

        if not is_eligible_equity(
            item.get("asset_type")
        ):
            continue

        ticker = normalise_ticker(
            item.get("ticker")
        )

        transaction_date = parse_date(
            item.get("transaction_date")
        )

        filing_date = parse_date(
            item.get("filing_date")
        )

        if ticker is None or transaction_date is None:
            continue

        age = (
            today_local() - transaction_date
        ).days

        if age < 0 or age > MAX_DAYS_AGO:
            continue

        low, midpoint, high = estimate_amounts(item)

        if midpoint <= 0:
            continue

        filer_name = str(
            item.get(
                "filer_name",
                item.get(
                    "representative",
                    "Unknown",
                ),
            )
        ).strip() or "Unknown"

        filer_id = str(
            item.get("filer_id")
            or filer_name
        ).strip()

        owner = str(
            item.get("owner")
            or "Unknown"
        ).strip()

        trade_id = str(
            item.get("id")
            or ""
        ).strip()

        if trade_id:
            dedup_key = (
                "id",
                trade_id,
            )
        else:
            dedup_key = (
                "composite",
                filer_id,
                ticker,
                transaction_date.isoformat(),
                filing_date.isoformat()
                if filing_date
                else "",
                low,
                high,
                owner,
            )

        if dedup_key in seen_keys:
            continue

        seen_keys.add(dedup_key)

        retained.append(
            {
                "trade_id": (
                    trade_id
                    or "|".join(
                        map(
                            str,
                            dedup_key[1:],
                        )
                    )
                ),
                "ticker": ticker,
                "price_ticker": yahoo_ticker(ticker),
                "transaction_date": transaction_date,
                "filing_date": filing_date,
                "filer_id": filer_id,
                "filer_name": filer_name,
                "display_name": compact_name(filer_name),
                "owner": owner,
                "comment": item.get("comment"),
                "asset_name": item.get("asset_name"),
                "doc_url": item.get("doc_url"),
                "amount_low": low,
                "amount_midpoint": midpoint,
                "amount_high": high,
            }
        )

    logger.info(
        "Retained %d unique equity purchases.",
        len(retained),
    )

    return retained


# =============================================================================
# YAHOO PRICE RETRIEVAL
# =============================================================================

def initialise_yfinance():
    """Configure the persistent yfinance cache."""
    try:
        YF_CACHE_DIRECTORY.mkdir(
            parents=True,
            exist_ok=True,
        )

        yf.set_tz_cache_location(
            str(YF_CACHE_DIRECTORY)
        )

    except Exception as exc:
        logger.warning(
            "Could not configure yfinance cache: %s",
            exc,
        )


def clean_price_series(
    series,
    positive_only=True,
):
    """
    Convert a Yahoo series to clean finite numeric values.
    """
    if series is None:
        return pd.Series(
            dtype="float64"
        )

    cleaned = pd.to_numeric(
        series,
        errors="coerce",
    )

    cleaned = cleaned.replace(
        [
            math.inf,
            -math.inf,
        ],
        pd.NA,
    ).dropna()

    if positive_only:
        cleaned = cleaned[
            cleaned > 0
        ]
    else:
        cleaned = cleaned[
            cleaned >= 0
        ]

    if cleaned.empty:
        return pd.Series(
            dtype="float64"
        )

    try:
        cleaned.index = pd.to_datetime(
            cleaned.index
        )

        if getattr(
            cleaned.index,
            "tz",
            None,
        ) is not None:
            cleaned.index = (
                cleaned.index.tz_localize(None)
            )

        cleaned = cleaned.sort_index()

    except Exception:
        pass

    return cleaned.astype(float)


def extract_symbol_data(
    frame,
    symbol,
    symbol_count,
):
    """
    Extract Close and Volume from either ticker-first or field-first
    yfinance MultiIndex output.
    """
    if frame is None or frame.empty:
        return None

    close = None
    volume = None

    try:
        if isinstance(
            frame.columns,
            pd.MultiIndex,
        ):
            level_0 = set(
                map(
                    str,
                    frame.columns.get_level_values(0),
                )
            )

            level_1 = set(
                map(
                    str,
                    frame.columns.get_level_values(1),
                )
            )

            # group_by="ticker" normally places tickers on level 0.
            if symbol in level_0:
                symbol_frame = frame[symbol]

                if "Close" in symbol_frame.columns:
                    close = symbol_frame["Close"]

                if "Volume" in symbol_frame.columns:
                    volume = symbol_frame["Volume"]

            # Defensive support for field-first output.
            elif (
                "Close" in level_0
                and symbol in level_1
            ):
                close = frame["Close"][symbol]

                if "Volume" in level_0:
                    volume = frame["Volume"][symbol]

        elif (
            symbol_count == 1
            and "Close" in frame.columns
        ):
            close = frame["Close"]

            if "Volume" in frame.columns:
                volume = frame["Volume"]

    except (
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning(
            "Could not extract %s from Yahoo batch: %s",
            symbol,
            exc,
        )
        return None

    close = clean_price_series(
        close,
        positive_only=True,
    )

    volume = clean_price_series(
        volume,
        positive_only=False,
    )

    if close.empty:
        return None

    return {
        "close": close,
        "volume": volume,
        "source": "batch",
    }


def download_batch(
    symbols,
    start_date,
    end_date,
):
    """Download a group of ticker histories in one Yahoo request."""
    symbols = sorted(
        set(symbols)
    )

    if not symbols:
        return {}

    for attempt in range(
        1,
        YF_BATCH_RETRIES + 1,
    ):
        try:
            logger.info(
                "Yahoo batch attempt %d/%d for %d symbols.",
                attempt,
                YF_BATCH_RETRIES,
                len(symbols),
            )

            frame = yf.download(
                tickers=symbols,
                start=start_date,
                end=end_date,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                repair=True,
                keepna=False,
                actions=False,
                threads=YF_THREADS,
                progress=False,
                timeout=YF_TIMEOUT,
            )

            if (
                frame is not None
                and not frame.empty
            ):
                output = {}

                for symbol in symbols:
                    extracted = extract_symbol_data(
                        frame,
                        symbol,
                        len(symbols),
                    )

                    if extracted is not None:
                        output[symbol] = extracted

                return output

            logger.warning(
                "Yahoo batch returned no data."
            )

        except Exception as exc:
            logger.warning(
                "Yahoo batch attempt %d failed: %s",
                attempt,
                exc,
            )

        if attempt < YF_BATCH_RETRIES:
            time.sleep(
                YF_RETRY_BASE_DELAY
                * attempt
            )

    return {}


def download_price_bundle(
    symbols,
    start_date,
    end_date,
):
    """Download all symbols in controlled batches."""
    bundle = {}

    unique_symbols = sorted(
        set(symbols)
    )

    for batch_symbols in chunked(
        unique_symbols,
        YF_BATCH_SIZE,
    ):
        bundle.update(
            download_batch(
                batch_symbols,
                start_date,
                end_date,
            )
        )

        time.sleep(0.5)

    return bundle


def download_single_ticker(
    symbol,
    start_date,
    end_date,
):
    """Fallback download for a ticker missing from the batch result."""
    for attempt in range(
        1,
        YF_FALLBACK_RETRIES + 1,
    ):
        try:
            logger.info(
                "Yahoo fallback attempt %d/%d for %s.",
                attempt,
                YF_FALLBACK_RETRIES,
                symbol,
            )

            frame = yf.download(
                tickers=symbol,
                start=start_date,
                end=end_date,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                repair=True,
                keepna=False,
                actions=False,
                threads=False,
                progress=False,
                timeout=YF_TIMEOUT,
            )

            extracted = extract_symbol_data(
                frame,
                symbol,
                1,
            )

            if extracted is not None:
                extracted["source"] = "fallback"
                return extracted

        except Exception as exc:
            logger.warning(
                "Yahoo fallback failed for %s: %s",
                symbol,
                exc,
            )

        if attempt < YF_FALLBACK_RETRIES:
            time.sleep(
                YF_RETRY_BASE_DELAY
                * attempt
            )

    return None


def get_trade_date_close(
    close,
    transaction_date,
):
    """
    Return the first valid market close on or after the disclosed date.
    """
    if close.empty:
        return None

    target = pd.Timestamp(
        transaction_date
    )

    try:
        eligible = close[
            close.index >= target
        ]
    except TypeError:
        return None

    if eligible.empty:
        return None

    return safe_float(
        eligible.iloc[0]
    )


# =============================================================================
# CONVICTION AND ENTRY SCORING
# =============================================================================

def amount_score(amount):
    """Maximum 55 conviction points for effective dollar size."""
    if amount < 15_000:
        return 2.0

    if amount < 50_000:
        return 8.0

    if amount < 100_000:
        return 15.0

    if amount < 250_000:
        return 25.0

    if amount < 500_000:
        return 38.0

    if amount < 1_000_000:
        return 48.0

    if amount < 2_000_000:
        return 52.0

    return 55.0


def floor_score(total_low):
    """Maximum 10 points for the aggregate disclosed minimum."""
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


def freshness_points(
    weighted_age,
    maximum,
):
    """Linearly reduce freshness points across the 45-day window."""
    if weighted_age is None:
        return 0.0

    return (
        max(
            0.0,
            1.0
            - weighted_age
            / MAX_DAYS_AGO,
        )
        * maximum
    )


def entry_price_points(
    weighted_return,
):
    """
    Reward prices close to disclosed trade-date closing prices.

    Large declines are treated as potential adverse signals rather than
    automatically receiving higher entry scores.
    """
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


def trend_points(
    current_price,
    ma20,
    ma50,
):
    """Maximum 20 points for basic price-trend condition."""
    if current_price is None:
        return 0.0

    if ma20 is None and ma50 is None:
        return 0.0

    if ma20 is not None and ma50 is not None:
        if current_price >= ma20 >= ma50:
            return 20.0

        if (
            current_price >= ma20
            and current_price >= ma50
        ):
            return 16.0

        if current_price >= ma20:
            return 12.0

        if current_price >= ma50:
            return 8.0

        return 3.0

    if ma20 is not None:
        return (
            10.0
            if current_price >= ma20
            else 3.0
        )

    return (
        8.0
        if current_price >= ma50
        else 3.0
    )


def liquidity_points(
    avg_dollar_volume,
):
    """Maximum 15 points for average daily dollar liquidity."""
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
    """
    Find the strongest rolling cluster based on unique buyers.

    Aggregate midpoint amount is used as the tie-breaker.
    """
    ordered = sorted(
        trades,
        key=lambda trade: trade["transaction_date"],
    )

    best = {
        "buyers": 0,
        "amount": 0.0,
    }

    for start_trade in ordered:
        end_date = (
            start_trade["transaction_date"]
            + timedelta(
                days=CLUSTER_WINDOW_DAYS - 1
            )
        )

        window = [
            trade
            for trade in ordered
            if (
                start_trade["transaction_date"]
                <= trade["transaction_date"]
                <= end_date
            )
        ]

        buyers = len(
            {
                trade["filer_id"]
                for trade in window
            }
        )

        amount = sum(
            trade["amount_midpoint"]
            for trade in window
        )

        if (
            buyers,
            amount,
        ) > (
            best["buyers"],
            best["amount"],
        ):
            best = {
                "buyers": buyers,
                "amount": amount,
            }

    return best


def classify_result(
    conviction,
    entry,
    weighted_return,
    effective_amount,
):
    """Assign a ticker to one output category."""
    if (
        weighted_return <= -10.0
        and (
            conviction
            >= RISK_MIN_CONVICTION
            or effective_amount
            >= RISK_MIN_EFFECTIVE_AMOUNT
        )
    ):
        return "risk"

    if (
        conviction
        >= ACTIONABLE_MIN_CONVICTION
        and entry
        >= ACTIONABLE_MIN_ENTRY
        and weighted_return
        >= SEVERE_DRAWDOWN_PCT
    ):
        return "actionable"

    if (
        conviction
        >= WAIT_MIN_CONVICTION
        or effective_amount
        >= WAIT_MIN_EFFECTIVE_AMOUNT
    ):
        return "wait"

    return None


# =============================================================================
# TICKER ANALYSIS
# =============================================================================

def analyse_ticker(
    ticker,
    trades,
    price_data,
):
    """
    Analyse all recent purchases for one ticker.

    Each transaction receives its own trade-date reference price.
    Returns and ages are then weighted by disclosed midpoint estimates.
    """
    close = price_data["close"]

    volume = price_data.get(
        "volume",
        pd.Series(dtype="float64"),
    )

    current_price = (
        safe_float(close.iloc[-1])
        if not close.empty
        else None
    )

    if current_price is None:
        return None

    total_low = sum(
        trade["amount_low"]
        for trade in trades
    )

    total_mid = sum(
        trade["amount_midpoint"]
        for trade in trades
    )

    total_high = sum(
        trade["amount_high"]
        for trade in trades
    )

    if total_mid <= 0:
        return None

    priced_trades = []
    priced_midpoint = 0.0

    for trade in trades:
        reference_close = get_trade_date_close(
            close,
            trade["transaction_date"],
        )

        if (
            reference_close is None
            or reference_close <= 0
        ):
            continue

        trade_return = (
            current_price
            - reference_close
        ) / reference_close * 100.0

        if not math.isfinite(trade_return):
            continue

        age = (
            today_local()
            - trade["transaction_date"]
        ).days

        weight = trade["amount_midpoint"]

        priced_midpoint += weight

        priced_trades.append(
            {
                **trade,
                "return_pct": trade_return,
                "age": age,
                "weight": weight,
            }
        )

    price_coverage = (
        priced_midpoint / total_mid
        if total_mid
        else 0.0
    )

    if (
        not priced_trades
        or price_coverage
        < MIN_PRICE_COVERAGE
    ):
        logger.warning(
            "Skipping %s: price coverage %.1f%% below %.1f%%.",
            ticker,
            price_coverage * 100,
            MIN_PRICE_COVERAGE * 100,
        )
        return None

    total_weight = sum(
        trade["weight"]
        for trade in priced_trades
    )

    weighted_return = (
        sum(
            trade["return_pct"]
            * trade["weight"]
            for trade in priced_trades
        )
        / total_weight
    )

    weighted_age = (
        sum(
            trade["age"]
            * trade["weight"]
            for trade in priced_trades
        )
        / total_weight
    )

    latest_trade_date = max(
        trade["transaction_date"]
        for trade in trades
    )

    latest_age = (
        today_local()
        - latest_trade_date
    ).days

    unique_buyers = {
        trade["filer_id"]
        for trade in trades
    }

    buyer_names = sorted(
        {
            trade["display_name"]
            for trade in trades
        }
    )

    transaction_count = len(trades)

    repeat_purchase_count = max(
        0,
        transaction_count
        - len(unique_buyers),
    )

    cluster = best_cluster_window(
        trades
    )

    # Blend midpoint with the disclosed lower bound.
    effective_amount = (
        0.60 * total_mid
        + 0.40 * total_low
    )

    cluster_score = min(
        max(
            cluster["buyers"] - 1,
            0,
        )
        * 5.0,
        15.0,
    )

    repeat_score = min(
        repeat_purchase_count
        * 5.0,
        10.0,
    )

    conviction = (
        amount_score(effective_amount)
        + floor_score(total_low)
        + cluster_score
        + repeat_score
        + freshness_points(
            weighted_age,
            10.0,
        )
    )

    conviction = round(
        min(
            conviction,
            100.0,
        ),
        1,
    )

    ma20 = (
        safe_float(
            close.tail(20).mean()
        )
        if len(close) >= 20
        else None
    )

    ma50 = (
        safe_float(
            close.tail(50).mean()
        )
        if len(close) >= 50
        else None
    )

    avg_dollar_volume = None

    if not volume.empty:
        aligned = pd.concat(
            [
                close.rename("close"),
                volume.rename("volume"),
            ],
            axis=1,
        ).dropna()

        if not aligned.empty:
            avg_dollar_volume = safe_float(
                (
                    aligned["close"]
                    * aligned["volume"]
                ).tail(20).mean()
            )

    entry = (
        entry_price_points(
            weighted_return
        )
        + freshness_points(
            weighted_age,
            20.0,
        )
        + trend_points(
            current_price,
            ma20,
            ma50,
        )
        + liquidity_points(
            avg_dollar_volume
        )
    )

    entry = round(
        min(
            entry,
            100.0,
        ),
        1,
    )

    if len(unique_buyers) < 2:
        chase_flag = (
            weighted_return
            > SINGLE_BUYER_CHASE_LIMIT_PCT
        )
    else:
        chase_flag = (
            weighted_return
            > CLUSTER_CHASE_LIMIT_PCT
        )

    category = classify_result(
        conviction,
        entry,
        weighted_return,
        effective_amount,
    )

    if (
        chase_flag
        and category == "actionable"
    ):
        category = "wait"

    if category is None:
        return None

    return {
        "ticker": ticker,
        "price_ticker": trades[0]["price_ticker"],
        "category": category,
        "conviction_score": conviction,
        "entry_score": entry,
        "priority_score": round(
            0.65 * conviction
            + 0.35 * entry,
            1,
        ),
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
        "owner_counts": dict(
            Counter(
                trade["owner"]
                for trade in trades
            )
        ),
        "latest_age": latest_age,
        "weighted_age": round(
            weighted_age,
            1,
        ),
        "weighted_return": round(
            weighted_return,
            1,
        ),
        "current_price": current_price,
        "ma20": ma20,
        "ma50": ma50,
        "avg_dollar_volume": avg_dollar_volume,
        "price_coverage": round(
            price_coverage,
            3,
        ),
        "price_source": price_data.get(
            "source",
            "unknown",
        ),
        "severe_drawdown": (
            weighted_return
            < SEVERE_DRAWDOWN_PCT
        ),
        "chase_flag": chase_flag,
        "industry": "N/A",
    }


def process_all_trades(trades):
    """Group purchases by ticker and calculate all ticker analytics."""
    groups = {}

    for trade in trades:
        groups.setdefault(
            trade["ticker"],
            [],
        ).append(trade)

    history_start = (
        today_local()
        - timedelta(
            days=TECHNICAL_HISTORY_DAYS
        )
    )

    history_end = (
        today_local()
        + timedelta(days=1)
    )

    symbols = [
        ticker_trades[0]["price_ticker"]
        for ticker_trades
        in groups.values()
    ]

    bundle = download_price_bundle(
        symbols,
        history_start.isoformat(),
        history_end.isoformat(),
    )

    results = []

    for ticker, ticker_trades in groups.items():
        symbol = ticker_trades[0]["price_ticker"]

        price_data = bundle.get(symbol)

        if price_data is None:
            price_data = download_single_ticker(
                symbol,
                history_start.isoformat(),
                history_end.isoformat(),
            )

        if price_data is None:
            logger.warning(
                "Skipping %s: no usable Yahoo price data.",
                ticker,
            )
            continue

        analysed = analyse_ticker(
            ticker,
            ticker_trades,
            price_data,
        )

        if analysed is not None:
            results.append(analysed)

    return results


# =============================================================================
# OPTIONAL INDUSTRY ENRICHMENT
# =============================================================================

def enrich_industries(results):
    """
    Fetch industry data only for the final displayed tickers.

    This is skipped entirely when FETCH_INDUSTRY is False.
    """
    if not FETCH_INDUSTRY:
        return results

    cache = {}

    for result in results:
        symbol = result["price_ticker"]

        if symbol in cache:
            result["industry"] = cache[symbol]
            continue

        try:
            info = (
                yf.Ticker(symbol).get_info()
                or {}
            )

            industry = str(
                info.get("industry")
                or info.get("sector")
                or "N/A"
            ).strip()

        except Exception as exc:
            logger.warning(
                "Industry lookup failed for %s: %s",
                symbol,
                exc,
            )
            industry = "N/A"

        cache[symbol] = industry
        result["industry"] = industry

        time.sleep(
            INDUSTRY_REQUEST_DELAY
        )

    return results


# =============================================================================
# RESULT SELECTION
# =============================================================================

def select_results(results):
    """Select and limit the three Telegram sections."""
    actionable = sorted(
        [
            result
            for result in results
            if result["category"]
            == "actionable"
        ],
        key=lambda result: (
            result["priority_score"],
            result["effective_amount"],
        ),
        reverse=True,
    )[:MAX_ACTIONABLE]

    wait = sorted(
        [
            result
            for result in results
            if result["category"]
            == "wait"
        ],
        key=lambda result: (
            result["conviction_score"],
            result["effective_amount"],
        ),
        reverse=True,
    )[:MAX_WAIT]

    risk = sorted(
        [
            result
            for result in results
            if result["category"]
            == "risk"
        ],
        key=lambda result: (
            result["conviction_score"],
            -result["weighted_return"],
        ),
        reverse=True,
    )[:MAX_RISK]

    selected = (
        actionable
        + wait
        + risk
    )[:MAX_TOTAL_RESULTS]

    return (
        actionable,
        wait,
        risk,
        selected,
    )


# =============================================================================
# TELEGRAM OUTPUT
# =============================================================================

def buyer_label(result):
    """Create a compact buyer display."""
    names = result["buyer_names"]

    if len(names) <= 3:
        return ", ".join(names)

    return (
        f"{names[0]}, "
        f"... +{len(names) - 1}"
    )


def result_line(result):
    """Build one Telegram result line."""
    flags = []

    if result["cluster_buyers"] >= 2:
        flags.append("👥")

    if result["severe_drawdown"]:
        flags.append("⚠️")

    if result["chase_flag"]:
        flags.append("🏃")

    industry = ""

    if (
        FETCH_INDUSTRY
        and result.get("industry")
        not in {
            None,
            "",
            "N/A",
        }
    ):
        industry = (
            f" | "
            f"{result['industry'][:12]}"
        )

    return (
        f"{''.join(flags)}"
        f"${result['ticker']} | "
        f"C{result['conviction_score']:.0f}/"
        f"E{result['entry_score']:.0f} | "
        f"Est "
        f"{format_amount(result['total_mid'])} "
        f"["
        f"{format_amount(result['total_low'])}"
        f"-"
        f"{format_amount(result['total_high'])}"
        f"] | "
        f"{result['unique_buyers']} buyers "
        f"("
        f"{result['cluster_buyers']}"
        f"/"
        f"{CLUSTER_WINDOW_DAYS}d"
        f") | "
        f"Wtd age "
        f"{result['weighted_age']:.0f}d | "
        f"Vs buys "
        f"{result['weighted_return']:+.1f}% | "
        f"{buyer_label(result)}"
        f"{industry}"
    )


def build_messages(
    actionable,
    wait,
    risk,
    total_analysed,
):
    """Build Telegram-safe message chunks."""
    sections = []

    if actionable:
        sections.append(
            (
                "🔥 BEST ACTIONABLE",
                actionable,
            )
        )

    if wait:
        sections.append(
            (
                "👀 HIGH CONVICTION — WAIT FOR ENTRY",
                wait,
            )
        )

    if risk:
        sections.append(
            (
                "⚠️ DISCOUNTED / HIGHER RISK",
                risk,
            )
        )

    if not sections:
        return []

    shown_count = sum(
        len(items)
        for _, items in sections
    )

    header = (
        "📊 CONGRESS PURCHASE OPPORTUNITIES\n"
        f"Analysed: "
        f"{total_analysed} qualifying tickers | "
        f"Shown: {shown_count}\n"
        "C = conviction | E = entry quality\n\n"
    )

    footer = (
        "\n"
        "Est = sum of disclosure-range midpoints\n"
        "Vs buys = amount-weighted return "
        "versus trade-date closes\n"
        f"👥 = cluster within "
        f"{CLUSTER_WINDOW_DAYS} days | "
        "⚠️ = severe drawdown | "
        "🏃 = price-chase risk\n"
        "Screening signal only; review filings "
        "and current company news before buying."
    )

    messages = []
    current = header

    for title, items in sections:
        section_header = (
            f"{title}\n"
        )

        if (
            len(current)
            + len(section_header)
            + len(footer)
            > TELEGRAM_CHAR_LIMIT
        ):
            current += footer
            messages.append(current)

            current = (
                "📊 CONGRESS PURCHASE OPPORTUNITIES "
                "[CONTINUED]\n\n"
            )

        current += section_header

        for result in items:
            line = (
                result_line(result)
                + "\n"
            )

            if (
                len(current)
                + len(line)
                + len(footer)
                > TELEGRAM_CHAR_LIMIT
            ):
                current += footer
                messages.append(current)

                current = (
                    "📊 CONGRESS PURCHASE OPPORTUNITIES "
                    "[CONTINUED]\n\n"
                    + section_header
                )

            current += line

        current += "\n"

    current += footer
    messages.append(current)

    return messages


async def send_messages(messages):
    """Send all Telegram message chunks using one Bot instance."""
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN
    )

    for index, message in enumerate(messages):
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
        )

        if index < len(messages) - 1:
            await asyncio.sleep(
                INTER_CHUNK_DELAY
            )


async def send_failure_alert(message):
    """Send an operational failure alert."""
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return

    try:
        bot = Bot(
            token=TELEGRAM_BOT_TOKEN
        )

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "⚠️ Congress monitor failure\n"
                f"{message}"
            ),
        )

    except Exception:
        logger.exception(
            "Could not send failure alert."
        )


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run the full monitoring cycle."""
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        logger.error(
            "Missing TELEGRAM_BOT_TOKEN "
            "and/or TELEGRAM_CHAT_ID."
        )
        return

    if not acquire_lock():
        return

    try:
        initialise_yfinance()

        trades = fetch_trades()

        if trades is None:
            asyncio.run(
                send_failure_alert(
                    "The congressional trade feed "
                    "could not be retrieved."
                )
            )
            return

        if not trades:
            logger.info(
                "No recent qualifying equity "
                "purchases were found."
            )
            return

        analysed = process_all_trades(
            trades
        )

        if not analysed:
            asyncio.run(
                send_failure_alert(
                    "No ticker produced usable "
                    "price analytics."
                )
            )
            return

        (
            actionable,
            wait,
            risk,
            selected,
        ) = select_results(
            analysed
        )

        if not selected:
            logger.info(
                "No opportunities passed "
                "the selection thresholds."
            )
            return

        enrich_industries(
            selected
        )

        messages = build_messages(
            actionable=actionable,
            wait=wait,
            risk=risk,
            total_analysed=len(analysed),
        )

        if not messages:
            logger.info(
                "No Telegram messages "
                "were generated."
            )
            return

        asyncio.run(
            send_messages(messages)
        )

        logger.info(
            "Sent %d Telegram message(s).",
            len(messages),
        )

    except Exception as exc:
        logger.exception(
            "Unhandled monitor failure: %s",
            exc,
        )

        asyncio.run(
            send_failure_alert(
                str(exc)[:500]
            )
        )

    finally:
        release_lock()


if __name__ == "__main__":
    main()