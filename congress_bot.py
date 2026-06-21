import os
import re
import time
import math
import logging
import asyncio
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


# ── Environment ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RAW_KADOA_URL = (
    "https://raw.githubusercontent.com/kadoa-org/"
    "congress-trading-monitor/main/public/data/trades.json"
)


# ── Strategy ────────────────────────────────────────────────────────────────

LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

MAX_DAYS_AGO = 45
CLUSTER_WINDOW_DAYS = 14

MIN_PRICE_COVERAGE = 0.75

SEVERE_DRAWDOWN_PCT = -15.0

SINGLE_BUYER_CHASE_LIMIT_PCT = 8.0
CLUSTER_CHASE_LIMIT_PCT = 15.0

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


# ── Yahoo Finance ───────────────────────────────────────────────────────────

YF_PERIOD = "6mo"
YF_TIMEOUT = 30

YF_BATCH_SIZE = 10
YF_BATCH_RETRIES = 2
YF_BATCH_RETRY_DELAY = 5

YF_FALLBACK_RETRIES = 1
YF_FALLBACK_DELAY = 0.6

# Prevent failed batch calls from causing a request for every ticker.
MAX_INDIVIDUAL_FALLBACKS = 25

YF_CACHE_DIRECTORY = Path(
    os.getenv(
        "YF_CACHE_DIRECTORY",
        "./yfinance_cache",
    )
)

# Add only mappings that have been verified.
YAHOO_TICKER_OVERRIDES = {
    "BRK.B": "BRK-B",
    "BF.B": "BF-B",
}


# ── Operations ──────────────────────────────────────────────────────────────

TELEGRAM_CHAR_LIMIT = 3800
INTER_CHUNK_DELAY = 1.5

LOCK_FILE = Path("congress_bot.lock")
LOCK_STALE_HOURS = 6

LOG_FILE = "congress_bot.log"


# ── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("congress_bot")
logger.setLevel(logging.INFO)
logger.handlers.clear()

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

_console = logging.StreamHandler()
_console.setFormatter(_formatter)

_file = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file.setFormatter(_formatter)

logger.addHandler(_console)
logger.addHandler(_file)


# ── General helpers ─────────────────────────────────────────────────────────

def today_local() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def parse_date(value):
    try:
        return datetime.strptime(
            str(value),
            "%Y-%m-%d",
        ).date()
    except (TypeError, ValueError):
        return None


def safe_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    return value if math.isfinite(value) else None


def format_amount(value):
    value = safe_float(value) or 0.0

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}b"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}m"

    if value >= 1_000:
        return f"${value / 1_000:.0f}k"

    return f"${value:.0f}"


def compact_name(full_name):
    parts = str(full_name or "").strip().split()

    suffixes = {
        "jr",
        "jr.",
        "sr",
        "sr.",
        "ii",
        "iii",
        "iv",
    }

    while parts and parts[-1].lower() in suffixes:
        parts.pop()

    return parts[-1] if parts else "Unknown"


def normalise_ticker(value):
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

    if not re.fullmatch(
        r"[A-Z0-9.^=\-]+",
        ticker,
    ):
        return None

    return ticker


def is_eligible_equity(
    asset_type,
    asset_name,
):
    asset_type = str(
        asset_type or ""
    ).strip().lower()

    asset_name = str(
        asset_name or ""
    ).strip().lower()

    text = f"{asset_type} {asset_name}"

    excluded_terms = (
        "option",
        "bond",
        "note",
        "debenture",
        "warrant",
        "preferred",
        "mutual fund",
        "exchange traded fund",
        " etf",
    )

    if any(
        term in text
        for term in excluded_terms
    ):
        return False

    if asset_type == "st":
        return True

    included_terms = (
        "common stock",
        "class a common",
        "class b common",
        "ordinary share",
        "depositary receipt",
        "equity",
    )

    return any(
        term in text
        for term in included_terms
    )


def estimate_amounts(item):
    low = safe_float(
        item.get("amount_range_low")
    )

    high = safe_float(
        item.get("amount_range_high")
    )

    if (
        low is None
        or high is None
        or low < 0
        or high < low
    ):
        return 0.0, 0.0, 0.0

    midpoint = (
        low + high
    ) / 2.0

    return low, midpoint, high


def chunked(
    values,
    size,
):
    for index in range(
        0,
        len(values),
        size,
    ):
        yield values[
            index:index + size
        ]


# ── Locking and source HTTP ─────────────────────────────────────────────────

def acquire_lock():
    if LOCK_FILE.exists():
        try:
            age_hours = (
                time.time()
                - LOCK_FILE.stat().st_mtime
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
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY,
        )

        os.write(
            descriptor,
            str(
                os.getpid()
            ).encode("utf-8"),
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
    try:
        LOCK_FILE.unlink(
            missing_ok=True
        )

    except OSError as exc:
        logger.warning(
            "Could not remove lock file: %s",
            exc,
        )


def build_http_session():
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
        allowed_methods=frozenset(
            {"GET"}
        ),
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
            "User-Agent":
                "CongressPurchaseMonitor/4.0"
        }
    )

    return session


# ── Trade retrieval ─────────────────────────────────────────────────────────

def fetch_trades():
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

    if not isinstance(
        payload,
        list,
    ):
        logger.error(
            "Unexpected trade-data payload type: %s",
            type(payload).__name__,
        )

        return None

    retained = []
    seen = set()

    for item in payload:
        if not isinstance(
            item,
            dict,
        ):
            continue

        transaction_type = str(
            item.get(
                "transaction_type",
                item.get(
                    "type",
                    "",
                ),
            )
        ).lower()

        if (
            "purchase"
            not in transaction_type
            and "buy"
            not in transaction_type
        ):
            continue

        if not is_eligible_equity(
            item.get("asset_type"),
            item.get("asset_name"),
        ):
            continue

        ticker = normalise_ticker(
            item.get("ticker")
        )

        transaction_date = parse_date(
            item.get(
                "transaction_date"
            )
        )

        filing_date = parse_date(
            item.get(
                "filing_date"
            )
        )

        if (
            ticker is None
            or transaction_date is None
        ):
            continue

        age = (
            today_local()
            - transaction_date
        ).days

        if (
            age < 0
            or age > MAX_DAYS_AGO
        ):
            continue

        (
            amount_low,
            amount_midpoint,
            amount_high,
        ) = estimate_amounts(item)

        if amount_midpoint <= 0:
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
                (
                    filing_date.isoformat()
                    if filing_date
                    else ""
                ),
                amount_low,
                amount_high,
                owner,
            )

        if dedup_key in seen:
            continue

        seen.add(dedup_key)

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
                "price_ticker": (
                    YAHOO_TICKER_OVERRIDES.get(
                        ticker,
                        ticker,
                    )
                ),
                "transaction_date":
                    transaction_date,
                "filing_date":
                    filing_date,
                "filer_id":
                    filer_id,
                "display_name":
                    compact_name(
                        filer_name
                    ),
                "owner":
                    owner,
                "amount_low":
                    amount_low,
                "amount_midpoint":
                    amount_midpoint,
                "amount_high":
                    amount_high,
            }
        )

    logger.info(
        "Retained %d unique equity purchases.",
        len(retained),
    )

    return retained


# ── Yfinance initialisation and diagnostics ─────────────────────────────────

def initialise_yfinance():
    try:
        YF_CACHE_DIRECTORY.mkdir(
            parents=True,
            exist_ok=True,
        )

        yf.set_tz_cache_location(
            str(
                YF_CACHE_DIRECTORY
            )
        )

    except Exception as exc:
        logger.warning(
            "Could not configure yfinance cache: %s",
            exc,
        )


def clean_series(
    series,
    positive_only=True,
):
    if series is None:
        return pd.Series(
            dtype="float64"
        )

    series = pd.to_numeric(
        series,
        errors="coerce",
    )

    series = series.replace(
        [
            math.inf,
            -math.inf,
        ],
        pd.NA,
    ).dropna()

    if positive_only:
        series = series[
            series > 0
        ]

    else:
        series = series[
            series >= 0
        ]

    if series.empty:
        return pd.Series(
            dtype="float64"
        )

    try:
        series.index = pd.to_datetime(
            series.index
        )

        if getattr(
            series.index,
            "tz",
            None,
        ) is not None:
            series.index = (
                series.index.tz_localize(
                    None
                )
            )

        series = series.sort_index()

    except Exception:
        pass

    return series.astype(float)


def history_health_check():
    version = getattr(
        yf,
        "__version__",
        "unknown",
    )

    logger.info(
        "Testing Yahoo history route "
        "with yfinance %s.",
        version,
    )

    try:
        frame = yf.Ticker(
            "MSFT"
        ).history(
            period="5d",
            interval="1d",
            auto_adjust=True,
            repair=False,
            keepna=False,
            timeout=YF_TIMEOUT,
            raise_errors=True,
        )

        if (
            frame is None
            or frame.empty
            or "Close"
            not in frame.columns
        ):
            raise RuntimeError(
                "MSFT history check "
                "returned no usable frame."
            )

        close = clean_series(
            frame["Close"]
        )

        if close.empty:
            raise RuntimeError(
                "MSFT history check "
                "returned no valid closes."
            )

        logger.info(
            "Yahoo history health "
            "check passed."
        )

        return True

    except Exception as exc:
        logger.exception(
            "Yahoo history health "
            "check failed: %s",
            exc,
        )

        return False


def batch_health_check():
    logger.info(
        "Testing Yahoo "
        "batch-download route."
    )

    try:
        frame = yf.download(
            tickers=[
                "MSFT",
                "AAPL",
            ],
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            repair=False,
            keepna=False,
            actions=False,
            threads=False,
            progress=False,
            timeout=YF_TIMEOUT,
            multi_level_index=True,
        )

        if (
            frame is None
            or frame.empty
        ):
            logger.warning(
                "Yahoo batch health "
                "check returned no data."
            )

            return False

        logger.info(
            "Yahoo batch health "
            "check passed."
        )

        return True

    except Exception as exc:
        logger.warning(
            "Yahoo batch health "
            "check failed: %s",
            exc,
        )

        return False


# ── Yahoo data retrieval ────────────────────────────────────────────────────

def extract_symbol_data(
    frame,
    symbol,
    symbol_count,
):
    if (
        frame is None
        or frame.empty
    ):
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
                    frame.columns.get_level_values(
                        0
                    ),
                )
            )

            level_1 = set(
                map(
                    str,
                    frame.columns.get_level_values(
                        1
                    ),
                )
            )

            if symbol in level_0:
                ticker_frame = frame[
                    symbol
                ]

                if (
                    "Close"
                    in ticker_frame.columns
                ):
                    close = ticker_frame[
                        "Close"
                    ]

                if (
                    "Volume"
                    in ticker_frame.columns
                ):
                    volume = ticker_frame[
                        "Volume"
                    ]

            elif (
                "Close"
                in level_0
                and symbol
                in level_1
            ):
                close = frame[
                    "Close"
                ][symbol]

                if "Volume" in level_0:
                    volume = frame[
                        "Volume"
                    ][symbol]

        elif (
            symbol_count == 1
            and "Close"
            in frame.columns
        ):
            close = frame["Close"]

            if "Volume" in frame.columns:
                volume = frame[
                    "Volume"
                ]

    except (
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning(
            "Could not extract %s "
            "from Yahoo frame: %s",
            symbol,
            exc,
        )

        return None

    close = clean_series(
        close,
        positive_only=True,
    )

    volume = clean_series(
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


def download_batch(symbols):
    symbols = sorted(
        set(symbols)
    )

    for attempt in range(
        1,
        YF_BATCH_RETRIES + 1,
    ):
        try:
            logger.info(
                "Yahoo batch attempt "
                "%d/%d for %d symbols.",
                attempt,
                YF_BATCH_RETRIES,
                len(symbols),
            )

            frame = yf.download(
                tickers=symbols,
                period=YF_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                repair=False,
                keepna=False,
                actions=False,
                threads=False,
                progress=False,
                timeout=YF_TIMEOUT,
                multi_level_index=True,
            )

            if (
                frame is not None
                and not frame.empty
            ):
                output = {}

                for symbol in symbols:
                    item = extract_symbol_data(
                        frame,
                        symbol,
                        len(symbols),
                    )

                    if item is not None:
                        output[
                            symbol
                        ] = item

                if output:
                    logger.info(
                        "Yahoo batch returned "
                        "%d/%d symbols.",
                        len(output),
                        len(symbols),
                    )

                    return output

            logger.warning(
                "Yahoo batch returned "
                "no usable data."
            )

        except Exception as exc:
            logger.warning(
                "Yahoo batch attempt "
                "%d failed: %s",
                attempt,
                exc,
            )

        if attempt < YF_BATCH_RETRIES:
            time.sleep(
                YF_BATCH_RETRY_DELAY
                * attempt
            )

    return {}


def download_single(symbol):
    for attempt in range(
        1,
        YF_FALLBACK_RETRIES + 1,
    ):
        try:
            logger.info(
                "Yahoo explicit history "
                "attempt %d/%d for %s.",
                attempt,
                YF_FALLBACK_RETRIES,
                symbol,
            )

            frame = yf.Ticker(
                symbol
            ).history(
                period=YF_PERIOD,
                interval="1d",
                auto_adjust=True,
                repair=False,
                keepna=False,
                timeout=YF_TIMEOUT,
                raise_errors=True,
            )

            if (
                frame is None
                or frame.empty
                or "Close"
                not in frame.columns
            ):
                raise RuntimeError(
                    f"{symbol} returned "
                    "no usable frame."
                )

            close = clean_series(
                frame["Close"],
                positive_only=True,
            )

            volume = clean_series(
                (
                    frame["Volume"]
                    if "Volume"
                    in frame.columns
                    else None
                ),
                positive_only=False,
            )

            if close.empty:
                raise RuntimeError(
                    f"{symbol} returned "
                    "no valid closes."
                )

            return {
                "close": close,
                "volume": volume,
                "source": "history",
            }

        except Exception as exc:
            logger.warning(
                "Yahoo explicit history "
                "failed for %s: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )

        if (
            attempt
            < YF_FALLBACK_RETRIES
        ):
            time.sleep(
                YF_BATCH_RETRY_DELAY
                * attempt
            )

    return None


def group_trades(trades):
    groups = {}

    for trade in trades:
        groups.setdefault(
            trade["ticker"],
            [],
        ).append(trade)

    return groups


def raw_priority(trades):
    total_low = sum(
        trade["amount_low"]
        for trade in trades
    )

    total_midpoint = sum(
        trade["amount_midpoint"]
        for trade in trades
    )

    effective_amount = (
        0.60 * total_midpoint
        + 0.40 * total_low
    )

    unique_buyers = len(
        {
            trade["filer_id"]
            for trade in trades
        }
    )

    latest_date = max(
        trade["transaction_date"]
        for trade in trades
    )

    latest_age = (
        today_local()
        - latest_date
    ).days

    return (
        effective_amount,
        unique_buyers,
        -latest_age,
    )


def build_price_bundle(
    groups,
    batch_available,
):
    symbols = sorted(
        {
            trades[0][
                "price_ticker"
            ]
            for trades
            in groups.values()
        }
    )

    bundle = {}

    if batch_available:
        for batch in chunked(
            symbols,
            YF_BATCH_SIZE,
        ):
            bundle.update(
                download_batch(
                    batch
                )
            )

            time.sleep(0.5)

    missing = [
        ticker
        for ticker, trades
        in groups.items()
        if trades[0][
            "price_ticker"
        ] not in bundle
    ]

    missing.sort(
        key=lambda ticker:
            raw_priority(
                groups[ticker]
            ),
        reverse=True,
    )

    fallbacks = missing[
        :MAX_INDIVIDUAL_FALLBACKS
    ]

    if missing:
        logger.warning(
            "%d symbols missing; "
            "explicit fallback capped at %d.",
            len(missing),
            len(fallbacks),
        )

    for ticker in fallbacks:
        symbol = groups[
            ticker
        ][0]["price_ticker"]

        item = download_single(
            symbol
        )

        if item is not None:
            bundle[
                symbol
            ] = item

        time.sleep(
            YF_FALLBACK_DELAY
        )

    logger.info(
        "Usable prices obtained "
        "for %d/%d symbols.",
        len(bundle),
        len(symbols),
    )

    return bundle, len(symbols)


def trade_date_close(
    close,
    transaction_date,
):
    eligible = close[
        close.index
        >= pd.Timestamp(
            transaction_date
        )
    ]

    if eligible.empty:
        return None

    return safe_float(
        eligible.iloc[0]
    )


# ── Scoring ─────────────────────────────────────────────────────────────────

def amount_score(amount):
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
    return (
        max(
            0.0,
            1.0
            - weighted_age
            / MAX_DAYS_AGO,
        )
        * maximum
    )


def entry_price_score(
    weighted_return,
):
    if (
        -5.0
        <= weighted_return
        <= 3.0
    ):
        return 45.0

    if (
        -10.0
        <= weighted_return
        < -5.0
    ):
        return 35.0

    if (
        -15.0
        <= weighted_return
        < -10.0
    ):
        return 15.0

    if weighted_return < -15.0:
        return 0.0

    if (
        3.0
        < weighted_return
        <= 8.0
    ):
        return 25.0

    if (
        8.0
        < weighted_return
        <= 15.0
    ):
        return 10.0

    return 0.0


def trend_score(
    price,
    ma20,
    ma50,
):
    if (
        ma20 is not None
        and ma50 is not None
    ):
        if price >= ma20 >= ma50:
            return 20.0

        if (
            price >= ma20
            and price >= ma50
        ):
            return 16.0

        if price >= ma20:
            return 12.0

        if price >= ma50:
            return 8.0

        return 3.0

    if ma20 is not None:
        return (
            10.0
            if price >= ma20
            else 3.0
        )

    if ma50 is not None:
        return (
            8.0
            if price >= ma50
            else 3.0
        )

    return 0.0


def liquidity_score(
    average_dollar_volume,
):
    if average_dollar_volume is None:
        return 0.0

    if average_dollar_volume >= 50_000_000:
        return 15.0

    if average_dollar_volume >= 10_000_000:
        return 12.0

    if average_dollar_volume >= 2_000_000:
        return 8.0

    if average_dollar_volume >= 500_000:
        return 4.0

    return 0.0


def best_cluster(trades):
    ordered = sorted(
        trades,
        key=lambda trade:
            trade["transaction_date"],
    )

    best = {
        "buyers": 0,
        "amount": 0.0,
    }

    for start_trade in ordered:
        window_end = (
            start_trade[
                "transaction_date"
            ]
            + timedelta(
                days=(
                    CLUSTER_WINDOW_DAYS
                    - 1
                )
            )
        )

        window = [
            trade
            for trade in ordered
            if (
                start_trade[
                    "transaction_date"
                ]
                <= trade[
                    "transaction_date"
                ]
                <= window_end
            )
        ]

        buyers = len(
            {
                trade["filer_id"]
                for trade in window
            }
        )

        amount = sum(
            trade[
                "amount_midpoint"
            ]
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


# ── Analysis ────────────────────────────────────────────────────────────────

def analyse_ticker(
    ticker,
    trades,
    prices,
):
    close = prices["close"]

    volume = prices.get(
        "volume",
        pd.Series(
            dtype="float64"
        ),
    )

    current_price = (
        safe_float(
            close.iloc[-1]
        )
        if not close.empty
        else None
    )

    if current_price is None:
        return None

    total_low = sum(
        trade["amount_low"]
        for trade in trades
    )

    total_midpoint = sum(
        trade["amount_midpoint"]
        for trade in trades
    )

    total_high = sum(
        trade["amount_high"]
        for trade in trades
    )

    priced_trades = []
    priced_midpoint = 0.0

    for trade in trades:
        reference_close = trade_date_close(
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

        if not math.isfinite(
            trade_return
        ):
            continue

        weight = trade[
            "amount_midpoint"
        ]

        priced_midpoint += weight

        priced_trades.append(
            {
                **trade,
                "return":
                    trade_return,
                "age": (
                    today_local()
                    - trade[
                        "transaction_date"
                    ]
                ).days,
                "weight":
                    weight,
            }
        )

    price_coverage = (
        priced_midpoint
        / total_midpoint
        if total_midpoint
        else 0.0
    )

    if (
        not priced_trades
        or price_coverage
        < MIN_PRICE_COVERAGE
    ):
        logger.warning(
            "Skipping %s: "
            "price coverage %.1f%%.",
            ticker,
            price_coverage * 100,
        )

        return None

    total_weight = sum(
        trade["weight"]
        for trade in priced_trades
    )

    weighted_return = (
        sum(
            trade["return"]
            * trade["weight"]
            for trade
            in priced_trades
        )
        / total_weight
    )

    weighted_age = (
        sum(
            trade["age"]
            * trade["weight"]
            for trade
            in priced_trades
        )
        / total_weight
    )

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

    repeat_purchases = max(
        0,
        len(trades)
        - len(unique_buyers),
    )

    cluster = best_cluster(
        trades
    )

    effective_amount = (
        0.60 * total_midpoint
        + 0.40 * total_low
    )

    cluster_score = min(
        max(
            cluster["buyers"] - 1,
            0,
        ) * 5.0,
        15.0,
    )

    repeat_score = min(
        repeat_purchases * 5.0,
        10.0,
    )

    conviction = min(
        (
            amount_score(
                effective_amount
            )
            + floor_score(
                total_low
            )
            + cluster_score
            + repeat_score
            + freshness_points(
                weighted_age,
                10.0,
            )
        ),
        100.0,
    )

    ma20 = (
        safe_float(
            close.tail(
                20
            ).mean()
        )
        if len(close) >= 20
        else None
    )

    ma50 = (
        safe_float(
            close.tail(
                50
            ).mean()
        )
        if len(close) >= 50
        else None
    )

    average_dollar_volume = None

    if not volume.empty:
        aligned = pd.concat(
            [
                close.rename(
                    "close"
                ),
                volume.rename(
                    "volume"
                ),
            ],
            axis=1,
        ).dropna()

        if not aligned.empty:
            average_dollar_volume = (
                safe_float(
                    (
                        aligned["close"]
                        * aligned["volume"]
                    ).tail(
                        20
                    ).mean()
                )
            )

    entry = min(
        (
            entry_price_score(
                weighted_return
            )
            + freshness_points(
                weighted_age,
                20.0,
            )
            + trend_score(
                current_price,
                ma20,
                ma50,
            )
            + liquidity_score(
                average_dollar_volume
            )
        ),
        100.0,
    )

    chase_limit = (
        SINGLE_BUYER_CHASE_LIMIT_PCT
        if len(unique_buyers) < 2
        else CLUSTER_CHASE_LIMIT_PCT
    )

    chase_flag = (
        weighted_return
        > chase_limit
    )

    category = classify_result(
        conviction,
        entry,
        weighted_return,
        effective_amount,
    )

    if (
        chase_flag
        and category
        == "actionable"
    ):
        category = "wait"

    if category is None:
        return None

    return {
        "ticker":
            ticker,
        "category":
            category,
        "conviction":
            round(
                conviction,
                1,
            ),
        "entry":
            round(
                entry,
                1,
            ),
        "priority":
            round(
                0.65 * conviction
                + 0.35 * entry,
                1,
            ),
        "low":
            total_low,
        "mid":
            total_midpoint,
        "high":
            total_high,
        "effective":
            effective_amount,
        "buyers":
            len(unique_buyers),
        "cluster_buyers":
            cluster["buyers"],
        "buyer_names":
            buyer_names,
        "weighted_age":
            round(
                weighted_age,
                1,
            ),
        "weighted_return":
            round(
                weighted_return,
                1,
            ),
        "severe":
            (
                weighted_return
                < SEVERE_DRAWDOWN_PCT
            ),
        "chase":
            chase_flag,
    }


def process_all(
    trades,
    batch_available,
):
    groups = group_trades(
        trades
    )

    (
        price_bundle,
        total_symbols,
    ) = build_price_bundle(
        groups,
        batch_available,
    )

    results = []
    priced_count = 0

    for ticker, ticker_trades in groups.items():
        symbol = ticker_trades[
            0
        ]["price_ticker"]

        prices = price_bundle.get(
            symbol
        )

        if prices is None:
            continue

        priced_count += 1

        result = analyse_ticker(
            ticker,
            ticker_trades,
            prices,
        )

        if result is not None:
            results.append(
                result
            )

    stats = {
        "groups":
            len(groups),
        "symbols":
            total_symbols,
        "priced":
            priced_count,
        "qualified":
            len(results),
        "batch":
            batch_available,
    }

    return results, stats


# ── Selection and Telegram ──────────────────────────────────────────────────

def select_results(results):
    actionable = sorted(
        [
            result
            for result in results
            if result["category"]
            == "actionable"
        ],
        key=lambda result: (
            result["priority"],
            result["effective"],
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
            result["conviction"],
            result["effective"],
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
            result["conviction"],
            -result[
                "weighted_return"
            ],
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


def buyer_label(result):
    names = result[
        "buyer_names"
    ]

    if len(names) <= 3:
        return ", ".join(
            names
        )

    return (
        f"{names[0]}, "
        f"... +{len(names) - 1}"
    )


def result_line(result):
    flags = ""

    if (
        result["cluster_buyers"]
        >= 2
    ):
        flags += "👥"

    if result["severe"]:
        flags += "⚠️"

    if result["chase"]:
        flags += "🏃"

    return (
        f"{flags}"
        f"${result['ticker']} | "
        f"C{result['conviction']:.0f}/"
        f"E{result['entry']:.0f} | "
        f"Est "
        f"{format_amount(result['mid'])} "
        f"["
        f"{format_amount(result['low'])}"
        f"-"
        f"{format_amount(result['high'])}"
        f"] | "
        f"{result['buyers']} buyers "
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
    )


def build_messages(
    actionable,
    wait,
    risk,
    stats,
):
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
                "👀 HIGH CONVICTION — "
                "WAIT FOR ENTRY",
                wait,
            )
        )

    if risk:
        sections.append(
            (
                "⚠️ DISCOUNTED / "
                "HIGHER RISK",
                risk,
            )
        )

    if not sections:
        return []

    shown = sum(
        len(items)
        for _, items
        in sections
    )

    mode = (
        "batch + fallback"
        if stats["batch"]
        else "explicit history fallback"
    )

    header = (
        "📊 CONGRESS PURCHASE "
        "OPPORTUNITIES\n"
        f"Price mode: {mode}\n"
        f"Priced: "
        f"{stats['priced']}/"
        f"{stats['groups']} tickers | "
        f"Qualified: "
        f"{stats['qualified']} | "
        f"Shown: {shown}\n"
        "C = conviction | "
        "E = entry quality\n\n"
    )

    footer = (
        "\n"
        "Est = sum of disclosure-range "
        "midpoints\n"
        "Vs buys = amount-weighted return "
        "versus trade-date closes\n"
        f"👥 = cluster within "
        f"{CLUSTER_WINDOW_DAYS} days | "
        "⚠️ = severe drawdown | "
        "🏃 = chase risk\n"
        "Screen only; review the filing, "
        "current news and valuation "
        "before buying."
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
            messages.append(
                current + footer
            )

            current = (
                "📊 CONGRESS PURCHASE "
                "OPPORTUNITIES "
                "[CONTINUED]\n\n"
            )

        current += section_header

        for result in items:
            line = (
                result_line(
                    result
                )
                + "\n"
            )

            if (
                len(current)
                + len(line)
                + len(footer)
                > TELEGRAM_CHAR_LIMIT
            ):
                messages.append(
                    current + footer
                )

                current = (
                    "📊 CONGRESS PURCHASE "
                    "OPPORTUNITIES "
                    "[CONTINUED]\n\n"
                    + section_header
                )

            current += line

        current += "\n"

    messages.append(
        current + footer
    )

    return messages


async def send_messages(messages):
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN
    )

    for index, message in enumerate(
        messages
    ):
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
        )

        if (
            index
            < len(messages) - 1
        ):
            await asyncio.sleep(
                INTER_CHUNK_DELAY
            )


async def send_failure(message):
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
                "⚠️ Congress monitor "
                "failure\n"
                f"{message}"
            ),
        )

    except Exception:
        logger.exception(
            "Could not send "
            "failure alert."
        )


# ── Main ────────────────────────────────────────────────────────────────────

def main():
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

        if not history_health_check():
            asyncio.run(
                send_failure(
                    "Yahoo Finance history "
                    "access failed. The monitor "
                    "stopped before bulk requests. "
                    "Upgrade yfinance and review "
                    "congress_bot.log."
                )
            )

            return

        batch_available = (
            batch_health_check()
        )

        if not batch_available:
            logger.warning(
                "Batch mode unavailable; "
                "using explicit history for "
                "top raw candidates only."
            )

        trades = fetch_trades()

        if trades is None:
            asyncio.run(
                send_failure(
                    "The congressional trade "
                    "feed could not be retrieved."
                )
            )

            return

        if not trades:
            logger.info(
                "No recent qualifying equity "
                "purchases were found."
            )

            return

        (
            analysed,
            stats,
        ) = process_all(
            trades,
            batch_available,
        )

        if stats["priced"] == 0:
            asyncio.run(
                send_failure(
                    "Yahoo passed the initial "
                    "health check but no "
                    "congressional ticker produced "
                    "usable price data."
                )
            )

            return

        if not analysed:
            logger.info(
                "Prices were retrieved, but "
                "no opportunity passed the "
                "scoring rules."
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
                "the final display thresholds."
            )

            return

        messages = build_messages(
            actionable,
            wait,
            risk,
            stats,
        )

        asyncio.run(
            send_messages(
                messages
            )
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
            send_failure(
                str(exc)[:500]
            )
        )

    finally:
        release_lock()


if __name__ == "__main__":
    main()