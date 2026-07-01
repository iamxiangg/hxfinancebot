from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from providers.sec.base import SECProvider
from scanners.vpma.alpha_vantage import AlphaVantageClient, AlphaVantageConfirmation
from scanners.vpma.guidance_extraction import extract_confirmation
from scanners.vpma.guidance_models import EarningsFundamentalConfirmation
from scanners.vpma.guidance_scoring import (
    apply_economic_overlay,
    classify_economic_event,
    determine_conflict_type,
    score_economic_event,
)


logger = logging.getLogger(__name__)

MODEL_VERSION = "2026-06-25-vpma-pead-v2-lite"
DEFAULT_UNIVERSE_URL = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/all.csv"
DEFAULT_BENCHMARK = "SPY"

COMMON_STOCK_EXCLUDE_TOKENS = (
    " warrant",
    " rights",
    " unit",
    " note",
    " bond",
    " etf",
    " etn",
    " fund",
    " index",
    " preferred",
)

ADR_ALLOW_TOKENS = ("adr",)


@dataclass(frozen=True)
class UniverseTicker:
    ticker: str
    name: str
    industry: str
    source_price: float | None
    source_market_cap: float | None
    source_volume: float | None


@dataclass(frozen=True)
class EarningsEvent:
    earnings_timestamp: datetime
    release_timing: str
    reaction_session: pd.Timestamp
    reaction_session_confidence: str
    days_since_reaction: int
    eps_surprise_pct: float | None


@dataclass
class VpmaTickerResult:
    ticker: str
    classification: str
    core_score: float
    event_score: float
    drift_score: float
    entry_score: float
    confirmation_score: float | None
    data_confidence: str
    setup_type: str
    reason: str
    valid_for_days: int
    details: dict[str, Any] = field(default_factory=dict)
    economic_classification: str = ""
    economic_confirmation_score: float = 0.0
    conflict_classification: str = ""
    guidance_action: str = ""
    downgrade_reason: str = ""


@dataclass
class VpmaScanResult:
    results: list[VpmaTickerResult]
    observed_at: str
    analysed_tickers: int
    counts: dict[str, int]
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VpmaConfig:
    enable_enrichment: bool = True
    max_enrich: int = 20
    event_lookback_days: int = 90
    valid_days: int = 3
    min_price: float = 3.0
    min_market_cap: float = 300_000_000.0
    min_source_volume: float = 200_000.0
    min_median_dollar_volume: float = 5_000_000.0
    universe_url: str = DEFAULT_UNIVERSE_URL
    actionable_core_min: float = 75.0
    actionable_event_min: float = 26.0
    actionable_drift_min: float = 23.0
    actionable_entry_min: float = 15.0
    wait_core_min: float = 68.0
    near_miss_core_min: float = 58.0
    next_earnings_guard_days: int = 14
    guidance_enable: bool = True
    guidance_max_tickers: int = 10

    @classmethod
    def from_env(cls) -> "VpmaConfig":
        return cls(
            enable_enrichment=_env_bool("VPMA_ENABLE_ENRICHMENT", True),
            max_enrich=_env_int("VPMA_MAX_ENRICH", 20),
            event_lookback_days=_env_int("VPMA_EVENT_LOOKBACK_DAYS", 90),
            valid_days=_env_int("VPMA_VALID_DAYS", 3),
            min_price=_env_float("VPMA_MIN_PRICE", 3.0),
            min_market_cap=_env_float("VPMA_MIN_MARKET_CAP", 300_000_000.0),
            min_source_volume=_env_float("VPMA_MIN_SOURCE_VOLUME", 200_000.0),
            min_median_dollar_volume=_env_float("VPMA_MIN_MEDIAN_DOLLAR_VOLUME", 5_000_000.0),
            universe_url=str(os.getenv("VPMA_UNIVERSE_URL", DEFAULT_UNIVERSE_URL)).strip() or DEFAULT_UNIVERSE_URL,
            guidance_enable=_env_bool("VPMA_GUIDANCE_ENABLE", True),
            guidance_max_tickers=_env_int("VPMA_GUIDANCE_MAX_TICKERS", 10),
        )


class YfinanceVpmaDataSource:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session

    def download_histories(
        self,
        tickers: list[str],
        *,
        period: str = "1y",
        batch_size: int = 40,
    ) -> dict[str, pd.DataFrame]:
        histories: dict[str, pd.DataFrame] = {}
        for index in range(0, len(tickers), batch_size):
            batch = tickers[index : index + batch_size]
            if not batch:
                continue
            data = yf.download(
                batch,
                period=period,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=False,
                session=self.session,
            )
            if data is None or data.empty:
                continue
            if isinstance(data.columns, pd.MultiIndex):
                for ticker in batch:
                    try:
                        frame = data[ticker].dropna(how="all").copy()
                    except KeyError:
                        continue
                    if not frame.empty:
                        histories[ticker] = frame
            else:
                histories[batch[0]] = data.dropna(how="all").copy()
        return histories

    def benchmark_history(self, ticker: str = DEFAULT_BENCHMARK) -> pd.DataFrame:
        history = yf.download(
            ticker,
            period="1y",
            auto_adjust=True,
            progress=False,
            threads=False,
            session=self.session,
        )
        if history is None:
            return pd.DataFrame()
        return history.dropna(how="all").copy()

    def earnings_dates(self, ticker: str) -> pd.DataFrame:
        yf_ticker = yf.Ticker(ticker, session=self.session)
        try:
            frame = yf_ticker.get_earnings_dates(limit=8)
        except Exception:
            frame = getattr(yf_ticker, "earnings_dates", pd.DataFrame())
        if frame is None or isinstance(frame, list):
            return pd.DataFrame()
        return frame.copy()

    def next_earnings_date(self, ticker: str) -> date | None:
        yf_ticker = yf.Ticker(ticker, session=self.session)
        try:
            calendar = yf_ticker.calendar
        except Exception:
            calendar = None
        if calendar is None:
            return None
        if isinstance(calendar, pd.DataFrame) and "Earnings Date" in calendar.index:
            value = calendar.loc["Earnings Date"].iloc[0]
        elif isinstance(calendar, pd.Series) and "Earnings Date" in calendar:
            value = calendar["Earnings Date"]
        elif isinstance(calendar, dict):
            value = calendar.get("Earnings Date")
        else:
            return None
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return None


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


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


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalise_universe_ticker(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.replace("/", "-")


def _extract_finite_scalar(value: Any) -> float | None:
    """Extract a finite float from a Python numeric, NumPy scalar, or single-element pandas object.

    Returns None for None, NaN, inf, -inf, and pd.NA.
    Raises ValueError for multi-element Series or DataFrames.
    """
    if value is None:
        return None
    if isinstance(value, (float, int)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, (np.floating, np.integer)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, pd.Series):
        if value.empty:
            return None
        if len(value) != 1:
            raise ValueError(f"Ambiguous scalar extraction: Series has {len(value)} elements")
        return _extract_finite_scalar(value.iloc[0])
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return None
        if value.size != 1:
            raise ValueError(f"Ambiguous scalar extraction: DataFrame has {value.size} elements")
        return _extract_finite_scalar(value.iloc[0, 0])
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _numeric_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _yahoo_valid_symbol(ticker: str) -> bool:
    """Return False for ticker representations that Yahoo Finance cannot resolve.

    Filtered patterns:
    - Symbols containing '^' (e.g. NEE^U, SCE^L) represent preferred or
      special securities in a format Yahoo does not recognise.
    """
    clean = str(ticker or "").strip().upper()
    if not clean:
        return False
    if "^" in clean:
        return False
    return True


def is_probably_common_stock(name: str, ticker: str) -> bool:
    clean_name = f" {str(name or '').strip().lower()} "
    clean_ticker = str(ticker or "").upper()
    if not clean_ticker:
        return False
    if clean_ticker.endswith("W") and "-" in clean_ticker:
        return False
    if any(token in clean_name for token in ADR_ALLOW_TOKENS):
        filtered_name = clean_name.replace(" depositary shares ", " ")
    else:
        filtered_name = clean_name
    return not any(token in filtered_name for token in COMMON_STOCK_EXCLUDE_TOKENS)


def clean_universe_rows(
    rows: Iterable[dict[str, Any]],
    *,
    min_price: float,
    min_market_cap: float,
    min_source_volume: float,
) -> list[UniverseTicker]:
    deduped: dict[str, UniverseTicker] = {}
    for row in rows:
        ticker = normalise_universe_ticker(row.get("symbol") or row.get("Symbol") or row.get("ticker"))
        if not ticker:
            continue
        name = str(row.get("name") or row.get("Name") or "").strip()
        if not is_probably_common_stock(name, ticker):
            continue
        source_price = _numeric_or_none(row.get("price") or row.get("Price"))
        market_cap = _numeric_or_none(row.get("marketCap") or row.get("MarketCap") or row.get("market_cap"))
        source_volume = _numeric_or_none(row.get("volume") or row.get("Volume"))
        if source_price is not None and source_price < min_price:
            continue
        if market_cap is not None and market_cap < min_market_cap:
            continue
        if source_volume is not None and source_volume < min_source_volume:
            continue
        deduped[ticker] = UniverseTicker(
            ticker=ticker,
            name=name,
            industry=str(row.get("industry") or row.get("Industry") or "").strip(),
            source_price=source_price,
            source_market_cap=market_cap,
            source_volume=source_volume,
        )
    return [deduped[ticker] for ticker in sorted(deduped)]


def fetch_universe_rows(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    active_session = session or requests.Session()
    response = active_session.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text))
    return frame.to_dict(orient="records")


def median_dollar_volume(history: pd.DataFrame, *, window: int = 50) -> float | None:
    required = {"Close", "Volume"}
    if history.empty or not required.issubset(history.columns):
        return None
    sample = history.tail(window)
    if sample.empty:
        return None
    series = sample["Close"] * sample["Volume"]
    if series.empty:
        return None
    try:
        return _extract_finite_scalar(series.median())
    except ValueError:
        return None


def classify_release_timing(timestamp: datetime) -> tuple[str, str]:
    if timestamp.hour == 0 and timestamp.minute == 0 and timestamp.second == 0:
        return "uncertain", "low"
    if timestamp.hour < 12:
        return "before_market", "high"
    if timestamp.hour >= 16:
        return "after_market", "high"
    return "during_market", "high"


def map_reaction_session(
    earnings_timestamp: datetime,
    trading_index: pd.DatetimeIndex,
) -> tuple[pd.Timestamp | None, str, str]:
    if trading_index.empty:
        return None, "uncertain", "low"
    normalized = trading_index.tz_localize(None) if trading_index.tz is not None else trading_index
    event_day = pd.Timestamp(earnings_timestamp.date())
    release_timing, confidence = classify_release_timing(earnings_timestamp)
    if release_timing == "after_market":
        candidates = normalized[normalized > event_day]
    else:
        candidates = normalized[normalized >= event_day]
    if len(candidates) == 0:
        return None, release_timing, confidence
    return candidates[0], release_timing, confidence


def extract_recent_earnings_event(
    earnings_frame: pd.DataFrame,
    trading_index: pd.DatetimeIndex,
    *,
    lookback_days: int,
    today: date | None = None,
) -> EarningsEvent | None:
    if earnings_frame is None or earnings_frame.empty:
        return None

    working = earnings_frame.copy()
    if isinstance(working.index, pd.DatetimeIndex) and working.index.tz is not None:
        working.index = working.index.tz_localize(None)

    today_value = today or datetime.now(UTC).date()
    candidates = []
    for timestamp, row in working.sort_index(ascending=False).iterrows():
        if not isinstance(timestamp, pd.Timestamp):
            continue
        days_ago = (today_value - timestamp.date()).days
        if days_ago < 0 or days_ago > lookback_days:
            continue
        reaction_session, release_timing, confidence = map_reaction_session(timestamp.to_pydatetime(), trading_index)
        if reaction_session is None:
            continue
        trading_days = len(trading_index[trading_index >= reaction_session])
        days_since_reaction = max(0, trading_days - 1)
        surprise = None
        for key in ("Surprise(%)", "surprise", "EPS Surprise %", "epsSurprisePercent"):
            if key in row:
                surprise = _numeric_or_none(row.get(key))
                break
        candidates.append(
            EarningsEvent(
                earnings_timestamp=timestamp.to_pydatetime(),
                release_timing=release_timing,
                reaction_session=reaction_session,
                reaction_session_confidence=confidence,
                days_since_reaction=days_since_reaction,
                eps_surprise_pct=surprise,
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda event: event.days_since_reaction)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _scale(value: float | None, *, low: float, high: float, max_score: float) -> float:
    if value is None:
        return 0.0
    if high <= low:
        raise ValueError("high must be greater than low")
    normalized = _clamp((value - low) / (high - low), 0.0, 1.0)
    return round(normalized * max_score, 2)


def _inverse_scale(value: float | None, *, good: float, bad: float, max_score: float) -> float:
    if value is None:
        return 0.0
    if bad <= good:
        raise ValueError("bad must be greater than good")
    normalized = _clamp((bad - value) / (bad - good), 0.0, 1.0)
    return round(normalized * max_score, 2)


def earnings_anchored_vwap(history: pd.DataFrame, reaction_session: pd.Timestamp) -> float | None:
    if history.empty or not {"High", "Low", "Close", "Volume"}.issubset(history.columns):
        return None
    sample = history.loc[history.index >= reaction_session]
    if sample.empty:
        return None
    volume = sample["Volume"].astype(float)
    try:
        volume_sum = _extract_finite_scalar(volume.sum())
    except ValueError:
        return None
    if volume_sum is None or volume_sum <= 0:
        return None
    typical = (sample["High"] + sample["Low"] + sample["Close"]) / 3.0
    try:
        return _extract_finite_scalar((typical * volume).sum() / volume.sum())
    except ValueError:
        return None


def reaction_abnormal_return(
    history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    reaction_session: pd.Timestamp,
) -> float | None:
    if history.empty or benchmark_history.empty:
        return None
    if reaction_session not in history.index or reaction_session not in benchmark_history.index:
        return None
    reaction_position = history.index.get_loc(reaction_session)
    benchmark_position = benchmark_history.index.get_loc(reaction_session)
    if isinstance(reaction_position, (slice, np.ndarray)) or isinstance(benchmark_position, (slice, np.ndarray)):
        return None
    if reaction_position == 0 or benchmark_position == 0:
        return None
    try:
        stock_return = _extract_finite_scalar(
            history["Close"].iloc[reaction_position] / history["Close"].iloc[reaction_position - 1] - 1.0
        )
        benchmark_return = _extract_finite_scalar(
            benchmark_history["Close"].iloc[benchmark_position]
            / benchmark_history["Close"].iloc[benchmark_position - 1]
            - 1.0
        )
    except ValueError:
        return None
    if stock_return is None or benchmark_return is None:
        return None
    return stock_return - benchmark_return


def reaction_closing_position(history: pd.DataFrame, reaction_session: pd.Timestamp) -> float | None:
    if reaction_session not in history.index:
        return None
    row = history.loc[reaction_session]
    if isinstance(row, pd.DataFrame):
        return None
    try:
        day_range = _extract_finite_scalar(row["High"] - row["Low"])
    except ValueError:
        return None
    if day_range is None or day_range <= 0:
        return 0.5
    try:
        return _extract_finite_scalar((row["Close"] - row["Low"]) / day_range)
    except ValueError:
        return None


def reaction_volume_shock(history: pd.DataFrame, reaction_session: pd.Timestamp) -> float | None:
    if reaction_session not in history.index:
        return None
    position = history.index.get_loc(reaction_session)
    if isinstance(position, (slice, np.ndarray)):
        return None
    baseline = history["Volume"].iloc[max(0, position - 20) : position]
    if baseline.empty:
        return None
    try:
        median_volume = _extract_finite_scalar(baseline.median())
    except ValueError:
        return None
    if median_volume is None or median_volume <= 0:
        return None
    try:
        return _extract_finite_scalar(history["Volume"].iloc[position] / median_volume)
    except ValueError:
        return None


def calculate_event_quality(
    *,
    eps_surprise_pct: float | None,
    abnormal_return: float | None,
    closing_position: float | None,
    volume_shock: float | None,
) -> dict[str, float]:
    eps_score = _scale(eps_surprise_pct, low=0.0, high=25.0, max_score=10.0)
    if eps_surprise_pct is not None and eps_surprise_pct > 0 and (abnormal_return or 0.0) <= 0:
        eps_score = round(eps_score * 0.4, 2)
    abnormal_score = _scale(abnormal_return, low=0.0, high=0.12, max_score=15.0)
    closing_score = _scale(closing_position, low=0.4, high=0.95, max_score=5.0)
    volume_score = _scale(volume_shock, low=1.0, high=4.0, max_score=10.0)
    total = round(eps_score + abnormal_score + closing_score + volume_score, 2)
    return {
        "event_score": total,
        "eps_score": eps_score,
        "abnormal_score": abnormal_score,
        "closing_score": closing_score,
        "volume_score": volume_score,
    }


def _empty_drift_metrics() -> dict[str, float | None]:
    return {
        "move_retention": None,
        "drawdown_from_post_high": None,
        "relative_strength_post_event": None,
        "earnings_avwap": None,
        "extension_from_avwap": None,
    }


def calculate_drift_metrics(
    history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    reaction_session: pd.Timestamp,
) -> dict[str, float | None]:
    if reaction_session not in history.index or reaction_session not in benchmark_history.index:
        return _empty_drift_metrics()
    position = history.index.get_loc(reaction_session)
    benchmark_position = benchmark_history.index.get_loc(reaction_session)
    if isinstance(position, (slice, np.ndarray)) or isinstance(benchmark_position, (slice, np.ndarray)):
        return _empty_drift_metrics()
    if position == 0 or benchmark_position == 0:
        return _empty_drift_metrics()

    try:
        pre_event_close = _extract_finite_scalar(history["Close"].iloc[position - 1])
        reaction_close = _extract_finite_scalar(history["Close"].iloc[position])
        current_close = _extract_finite_scalar(history["Close"].iloc[-1])
    except ValueError:
        return _empty_drift_metrics()
    if pre_event_close is None or reaction_close is None or current_close is None:
        return _empty_drift_metrics()
    reaction_move = reaction_close - pre_event_close
    move_retention = None
    if reaction_move > 0:
        move_retention = (current_close - pre_event_close) / reaction_move

    post_event_sample = history.iloc[position:]
    try:
        post_event_high = _extract_finite_scalar(post_event_sample["High"].max()) if not post_event_sample.empty else current_close
    except ValueError:
        post_event_high = current_close
    if post_event_high is None:
        post_event_high = current_close
    drawdown = None
    if post_event_high > 0:
        drawdown = (post_event_high - current_close) / post_event_high

    try:
        benchmark_pre = _extract_finite_scalar(benchmark_history["Close"].iloc[benchmark_position - 1])
        benchmark_current = _extract_finite_scalar(benchmark_history["Close"].iloc[-1])
    except ValueError:
        benchmark_pre = None
        benchmark_current = None
    stock_return = (current_close / pre_event_close) - 1.0 if pre_event_close > 0 else None
    benchmark_return = (benchmark_current / benchmark_pre) - 1.0 if benchmark_pre is not None and benchmark_pre > 0 else None
    relative_strength = None
    if stock_return is not None and benchmark_return is not None:
        relative_strength = stock_return - benchmark_return

    avwap = earnings_anchored_vwap(history, reaction_session)
    extension = None
    if avwap not in (None, 0.0):
        extension = (current_close - avwap) / avwap

    return {
        "move_retention": move_retention,
        "drawdown_from_post_high": drawdown,
        "relative_strength_post_event": relative_strength,
        "earnings_avwap": avwap,
        "extension_from_avwap": extension,
    }


def calculate_drift_integrity(metrics: dict[str, float | None], current_close: float) -> dict[str, float]:
    retention_score = _scale(metrics.get("move_retention"), low=0.25, high=1.0, max_score=10.0)
    avwap = metrics.get("earnings_avwap")
    avwap_score = 0.0
    if avwap not in (None, 0.0):
        avwap_score = _scale((current_close / float(avwap)) - 1.0, low=0.0, high=0.12, max_score=10.0)
    drawdown_score = _inverse_scale(metrics.get("drawdown_from_post_high"), good=0.03, bad=0.25, max_score=5.0)
    rel_strength_score = _scale(metrics.get("relative_strength_post_event"), low=-0.02, high=0.10, max_score=10.0)
    total = round(retention_score + avwap_score + drawdown_score + rel_strength_score, 2)
    return {
        "drift_score": total,
        "retention_score": retention_score,
        "avwap_score": avwap_score,
        "drawdown_score": drawdown_score,
        "relative_strength_score": rel_strength_score,
    }


def _empty_entry_quality() -> dict[str, float | None]:
    return {
        "entry_score": 0.0,
        "consolidation_score": 0.0,
        "volume_contraction_score": 0.0,
        "support_score": 0.0,
        "extension_score": 0.0,
        "recent_range_pct": None,
        "base_range_pct": None,
        "volume_contraction_ratio": None,
        "breakout_level": None,
    }


def calculate_entry_quality(history: pd.DataFrame, reaction_session: pd.Timestamp, avwap: float | None) -> dict[str, float | None]:
    post_event = history.loc[history.index >= reaction_session]
    if len(post_event) < 8:
        return _empty_entry_quality()

    try:
        current_close = _extract_finite_scalar(post_event["Close"].iloc[-1])
    except ValueError:
        return _empty_entry_quality()
    if current_close is None:
        return _empty_entry_quality()
    recent = post_event.tail(min(10, len(post_event)))
    base = post_event.tail(min(25, len(post_event)))

    try:
        recent_low = _extract_finite_scalar(recent["Low"].min())
        base_low = _extract_finite_scalar(base["Low"].min())
        recent_high = _extract_finite_scalar(recent["High"].max())
        base_high = _extract_finite_scalar(base["High"].max())
    except ValueError:
        return _empty_entry_quality()

    recent_range_pct = (recent_high - recent_low) / recent_low if recent_low is not None and recent_low > 0 and recent_high is not None else None
    base_range_pct = (base_high - base_low) / base_low if base_low is not None and base_low > 0 and base_high is not None else None

    compression = None
    if recent_range_pct is not None and base_range_pct not in (None, 0.0):
        compression = recent_range_pct / base_range_pct

    prior_volumes = post_event["Volume"].tail(min(30, len(post_event)))
    try:
        recent_volume = _extract_finite_scalar(prior_volumes.tail(min(10, len(prior_volumes))).median()) if not prior_volumes.empty else None
        base_volume = (
            _extract_finite_scalar(prior_volumes.head(max(1, len(prior_volumes) - min(10, len(prior_volumes)))).median())
            if len(prior_volumes) > 10
            else None
        )
    except ValueError:
        recent_volume = None
        base_volume = None
    volume_ratio = None
    if recent_volume is not None and base_volume not in (None, 0.0):
        volume_ratio = recent_volume / base_volume

    try:
        breakout_level = _extract_finite_scalar(base["High"].iloc[:-1].max()) if len(base) > 1 else current_close
    except ValueError:
        breakout_level = current_close
    if breakout_level is None:
        breakout_level = current_close
    extension_from_avwap = None
    if avwap not in (None, 0.0):
        extension_from_avwap = (current_close - float(avwap)) / float(avwap)

    consolidation_score = 0.0
    if compression is not None:
        consolidation_score = _inverse_scale(compression, good=0.45, bad=1.0, max_score=10.0)

    volume_contraction_score = 0.0
    if volume_ratio is not None:
        volume_contraction_score = _inverse_scale(volume_ratio, good=0.55, bad=1.1, max_score=5.0)

    support_score = 0.0
    if breakout_level > 0:
        distance_to_breakout = abs(current_close - breakout_level) / breakout_level
        support_score = _inverse_scale(distance_to_breakout, good=0.0, bad=0.08, max_score=5.0)

    extension_score = 0.0
    if extension_from_avwap is not None:
        extension_score = _inverse_scale(abs(extension_from_avwap), good=0.02, bad=0.18, max_score=5.0)

    total = round(consolidation_score + volume_contraction_score + support_score + extension_score, 2)
    return {
        "entry_score": total,
        "consolidation_score": consolidation_score,
        "volume_contraction_score": volume_contraction_score,
        "support_score": support_score,
        "extension_score": extension_score,
        "recent_range_pct": recent_range_pct,
        "base_range_pct": base_range_pct,
        "volume_contraction_ratio": volume_ratio,
        "breakout_level": breakout_level,
    }


def classify_setup_type(
    *,
    current_close: float,
    breakout_level: float | None,
    recent_range_pct: float | None,
    drawdown_from_post_high: float | None,
    extension_from_avwap: float | None,
) -> str:
    if breakout_level not in (None, 0.0) and current_close >= breakout_level * 0.995:
        return "pead_breakout"
    if recent_range_pct is not None and recent_range_pct <= 0.08:
        return "pead_consolidation"
    if drawdown_from_post_high is not None and drawdown_from_post_high <= 0.15:
        return "pead_pullback"
    if extension_from_avwap is not None and extension_from_avwap < -0.03:
        return "pead_deteriorating"
    return "pead_pullback"


def build_risk_flags(
    *,
    event: EarningsEvent,
    abnormal_return: float | None,
    drift_metrics: dict[str, float | None],
    current_close: float,
    reaction_low: float,
    median_dollar_volume_value: float | None,
    next_earnings_date: date | None,
    config: VpmaConfig,
) -> list[str]:
    flags: list[str] = []
    if abnormal_return is None or abnormal_return <= 0:
        flags.append("negative_reaction")
    if current_close < reaction_low:
        flags.append("reaction_low_broken")
    move_retention = drift_metrics.get("move_retention")
    if move_retention is not None and move_retention < 0.25:
        flags.append("gap_failed")
    avwap = drift_metrics.get("earnings_avwap")
    if avwap not in (None, 0.0) and current_close < avwap:
        flags.append("below_earnings_avwap")
    drawdown = drift_metrics.get("drawdown_from_post_high")
    if drawdown is not None and drawdown > 0.25:
        flags.append("excessive_drawdown")
    extension = drift_metrics.get("extension_from_avwap")
    if extension is not None and extension > 0.18:
        flags.append("overextended")
    if median_dollar_volume_value is not None and median_dollar_volume_value < config.min_median_dollar_volume:
        flags.append("low_liquidity")
    if event.reaction_session_confidence != "high":
        flags.append("event_date_uncertain")
    if next_earnings_date is not None:
        days_to_next = (next_earnings_date - event.reaction_session.date()).days
        if days_to_next <= config.next_earnings_guard_days:
            flags.append("next_earnings_too_close")
    return flags


def classify_core_result(
    *,
    core_score: float,
    event_score: float,
    drift_score: float,
    entry_score: float,
    risk_flags: list[str],
    config: VpmaConfig,
) -> str:
    major_breakdown = any(flag in risk_flags for flag in {"reaction_low_broken", "gap_failed", "below_earnings_avwap"})
    if event_score <= 12.0 or "negative_reaction" in risk_flags:
        return "excluded"
    if major_breakdown and drift_score < 15.0:
        return "risk"
    if (
        core_score >= config.actionable_core_min
        and event_score >= config.actionable_event_min
        and drift_score >= config.actionable_drift_min
        and entry_score >= config.actionable_entry_min
        and "overextended" not in risk_flags
        and "next_earnings_too_close" not in risk_flags
        and not major_breakdown
    ):
        return "actionable"
    if core_score >= config.wait_core_min and event_score >= 22.0 and drift_score >= 20.0:
        return "wait"
    if core_score >= config.near_miss_core_min and event_score >= 16.0:
        return "near_miss"
    if major_breakdown:
        return "risk"
    return "excluded"


def _data_confidence(event: EarningsEvent, risk_flags: list[str], has_eps_surprise: bool) -> str:
    if event.reaction_session_confidence == "high" and has_eps_surprise and not {
        "event_date_uncertain",
        "insufficient_history",
    }.intersection(risk_flags):
        return "high"
    if "event_date_uncertain" in risk_flags or not has_eps_surprise:
        return "medium"
    return "medium"


def _align_reaction_session_tz(
    history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    event: EarningsEvent,
) -> tuple[pd.DataFrame, pd.DataFrame, EarningsEvent]:
    """Normalize history / benchmark indexes and ``event.reaction_session`` to one tz.

    ``yf.download`` returns a tz-aware ``DatetimeIndex`` (America/New_York in
    US session hours; tz-naive elsewhere) while ``map_reaction_session``
    returns the matching trading day as a tz-naive ``pd.Timestamp`` (the
    earnings_dates frame from yfinance is tz-naive and the function strips
    tz before comparing). The earlier tz mismatch caused
    ``history.loc[event.reaction_session]`` in ``evaluate_ticker`` to raise
    ``KeyError`` for many tickers, surfacing in the production logs as
    ``WARNING VPMA evaluation failed for <TICKER>: KeyError``.

    Aligning once up front lets every downstream ``.loc[...]`` and
    ``reaction_session in history.index`` check work without an inline
    try/except chain. Frame contents (rows, columns, dtypes, values) are
    unchanged - only the index / tz is rewritten.
    """
    history_tz = history.index.tz if isinstance(history.index, pd.DatetimeIndex) else None
    benchmark_tz = benchmark_history.index.tz if isinstance(benchmark_history.index, pd.DatetimeIndex) else None
    # ``pd.Timestamp`` exposes ``tz``; plain ``datetime`` exposes ``tzinfo``.
    # Cover both so a caller passing ``datetime`` doesn't silently skip the
    # tz-alignment and re-introduce the ``KeyError`` we're guarding against.
    session_tz = getattr(event.reaction_session, "tz", None) or getattr(
        event.reaction_session, "tzinfo", None
    )
    target_tz = history_tz or benchmark_tz or session_tz

    if history_tz != target_tz and isinstance(history.index, pd.DatetimeIndex):
        history = history.copy()
        if target_tz is None and history_tz is not None:
            history.index = history.index.tz_localize(None)
        elif target_tz is not None and history_tz is None:
            history.index = history.index.tz_localize(target_tz)
    if (
        benchmark_tz != target_tz
        and isinstance(benchmark_history.index, pd.DatetimeIndex)
        and not benchmark_history.empty
    ):
        benchmark_history = benchmark_history.copy()
        if target_tz is None and benchmark_tz is not None:
            benchmark_history.index = benchmark_history.index.tz_localize(None)
        elif target_tz is not None and benchmark_tz is None:
            benchmark_history.index = benchmark_history.index.tz_localize(target_tz)

    if session_tz != target_tz and hasattr(event.reaction_session, "tz_localize"):
        new_session = (
            event.reaction_session.tz_localize(None)
            if target_tz is None
            else event.reaction_session.tz_localize(target_tz)
        )
        event = EarningsEvent(
            **{**event.__dict__, "reaction_session": new_session},
        )
    return history, benchmark_history, event


def evaluate_ticker(
    ticker: UniverseTicker,
    history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
    event: EarningsEvent,
    *,
    next_earnings_date: date | None,
    config: VpmaConfig,
) -> VpmaTickerResult:
    history, benchmark_history, event = _align_reaction_session_tz(history, benchmark_history, event)
    try:
        current_close = _extract_finite_scalar(history["Close"].iloc[-1])
    except ValueError:
        current_close = None
    if current_close is None:
        current_close = 0.0
    row = history.loc[event.reaction_session]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    try:
        reaction_low = _extract_finite_scalar(row["Low"])
    except ValueError:
        reaction_low = current_close
    if reaction_low is None:
        reaction_low = current_close
    abnormal = reaction_abnormal_return(history, benchmark_history, event.reaction_session)
    closing_position = reaction_closing_position(history, event.reaction_session)
    volume_shock = reaction_volume_shock(history, event.reaction_session)
    event_scores = calculate_event_quality(
        eps_surprise_pct=event.eps_surprise_pct,
        abnormal_return=abnormal,
        closing_position=closing_position,
        volume_shock=volume_shock,
    )
    drift_metrics = calculate_drift_metrics(history, benchmark_history, event.reaction_session)
    drift_scores = calculate_drift_integrity(drift_metrics, current_close)
    entry_scores = calculate_entry_quality(history, event.reaction_session, drift_metrics.get("earnings_avwap"))
    liquidity = median_dollar_volume(history)
    risk_flags = build_risk_flags(
        event=event,
        abnormal_return=abnormal,
        drift_metrics=drift_metrics,
        current_close=current_close,
        reaction_low=reaction_low,
        median_dollar_volume_value=liquidity,
        next_earnings_date=next_earnings_date,
        config=config,
    )
    if len(history) < 80:
        risk_flags.append("insufficient_history")
    if event.eps_surprise_pct is None:
        risk_flags.append("missing_eps_surprise")

    core_score = round(
        event_scores["event_score"] + drift_scores["drift_score"] + float(entry_scores["entry_score"]),
        2,
    )
    setup_type = classify_setup_type(
        current_close=current_close,
        breakout_level=entry_scores.get("breakout_level"),
        recent_range_pct=entry_scores.get("recent_range_pct"),
        drawdown_from_post_high=drift_metrics.get("drawdown_from_post_high"),
        extension_from_avwap=drift_metrics.get("extension_from_avwap"),
    )
    classification = classify_core_result(
        core_score=core_score,
        event_score=event_scores["event_score"],
        drift_score=drift_scores["drift_score"],
        entry_score=float(entry_scores["entry_score"]),
        risk_flags=risk_flags,
        config=config,
    )

    reason = (
        f"{setup_type.replace('_', ' ')} | core {core_score:.1f} "
        f"(event {event_scores['event_score']:.1f}, drift {drift_scores['drift_score']:.1f}, "
        f"entry {float(entry_scores['entry_score']):.1f})"
    )
    details = {
        "model_version": MODEL_VERSION,
        "earnings_timestamp": event.earnings_timestamp.isoformat(),
        "release_timing": event.release_timing,
        "reaction_session": event.reaction_session.isoformat(),
        "reaction_session_confidence": event.reaction_session_confidence,
        "days_since_reaction": event.days_since_reaction,
        "eps_surprise_pct": event.eps_surprise_pct,
        "abnormal_return": abnormal,
        "closing_position": closing_position,
        "volume_shock": volume_shock,
        "benchmark": DEFAULT_BENCHMARK,
        "median_dollar_volume": liquidity,
        "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date is not None else "",
        "risk_flags": sorted(set(risk_flags)),
        "universe_name": ticker.name,
        "universe_industry": ticker.industry,
        "source_market_cap": ticker.source_market_cap,
        "source_volume": ticker.source_volume,
        "source_price": ticker.source_price,
        **event_scores,
        **drift_metrics,
        **drift_scores,
        **entry_scores,
    }

    return VpmaTickerResult(
        ticker=ticker.ticker,
        classification=classification,
        core_score=core_score,
        event_score=event_scores["event_score"],
        drift_score=drift_scores["drift_score"],
        entry_score=float(entry_scores["entry_score"]),
        confirmation_score=None,
        data_confidence=_data_confidence(event, risk_flags, event.eps_surprise_pct is not None),
        setup_type=setup_type,
        reason=reason,
        valid_for_days=config.valid_days,
        details=details,
    )


def apply_confirmation(
    result: VpmaTickerResult,
    confirmation: AlphaVantageConfirmation,
) -> VpmaTickerResult:
    updated = VpmaTickerResult(**{**result.__dict__})
    updated.confirmation_score = confirmation.confirmation_score
    updated.details = dict(result.details)
    updated.details.update(
        {
            "enrichment_status": confirmation.status,
            "fundamentally_confirmed": confirmation.fundamentally_confirmed,
            "alpha_vantage_raw_payload_hash": confirmation.raw_payload_hash,
            **confirmation.details,
        }
    )
    updated.data_confidence = confirmation.data_confidence if confirmation.data_confidence else result.data_confidence

    if confirmation.fundamentally_confirmed is True:
        updated.data_confidence = "high"
        return updated

    if confirmation.confirmation_score is None:
        return updated

    if confirmation.confirmation_score <= 35.0:
        if updated.classification == "actionable":
            updated.classification = "wait"
        elif updated.classification == "wait":
            updated.classification = "near_miss"
        updated.reason = f"{result.reason} | forward revisions weak ({confirmation.confirmation_score:.1f})"
    elif confirmation.fundamentally_confirmed is None:
        updated.reason = f"{result.reason} | mixed forward revisions ({confirmation.confirmation_score:.1f})"

    return updated


def apply_guidance_confirmation(
    result: VpmaTickerResult,
    confirmation: EarningsFundamentalConfirmation,
    *,
    industry: str = "",
) -> VpmaTickerResult:
    score = score_economic_event(confirmation)
    economic_class = classify_economic_event(score, confirmation)
    conflict = determine_conflict_type(result.classification, economic_class)

    confirmation.score = score
    confirmation.economic_classification = economic_class

    new_classification, new_reason, downgrade_flags = apply_economic_overlay(
        classification=result.classification,
        conflict_type=conflict,
        economic_classification=economic_class,
        rev_guidance_action=confirmation.revenue_guidance_action,
        reason=result.reason,
    )

    updated = VpmaTickerResult(**{**result.__dict__})
    updated.classification = new_classification
    updated.reason = new_reason
    updated.economic_classification = economic_class
    updated.economic_confirmation_score = score
    updated.conflict_classification = conflict
    updated.guidance_action = confirmation.revenue_guidance_action
    updated.downgrade_reason = "; ".join(downgrade_flags) if downgrade_flags else ""

    if economic_class != "ECONOMIC_UNAVAILABLE" and result.data_confidence == "low":
        updated.data_confidence = "medium"

    updated.details = dict(result.details)
    updated.details.update(
        {
            "economic_classification": economic_class,
            "economic_confirmation_score": score,
            "conflict_classification": conflict,
            "guidance_action": confirmation.revenue_guidance_action,
            "margin_guidance_action": confirmation.margin_guidance_action,
            "revenue_guidance_midpoint": confirmation.revenue_guidance_midpoint,
            "revenue_guidance_change_pct": confirmation.revenue_guidance_change_pct,
            "reported_revenue": confirmation.reported_revenue,
            "revenue_growth_yoy": confirmation.revenue_growth_yoy,
            "gross_margin_pct": confirmation.gross_margin_pct,
            "gross_margin_change_bps": confirmation.gross_margin_change_bps,
            "operating_margin_pct": confirmation.operating_margin_pct,
            "free_cash_flow": confirmation.free_cash_flow,
            "business_kpis": confirmation.business_kpis,
            "source_accession": confirmation.source_accession,
            "downgrade_reason": updated.downgrade_reason,
            "downgrade_flags": downgrade_flags,
            "evidence_references": [
                {
                    "field": item.field,
                    "accession": item.accession,
                    "document": item.document,
                    "confidence": item.confidence,
                }
                for item in confirmation.evidence
            ],
        }
    )
    return updated


def _rank_for_enrichment(result: VpmaTickerResult) -> tuple[int, float, float, float, float]:
    classification_rank = {
        "actionable": 4,
        "wait": 3,
        "near_miss": 2,
        "risk": 1,
        "excluded": 0,
    }
    return (
        classification_rank.get(result.classification, 0),
        result.event_score,
        result.drift_score,
        result.core_score,
        result.entry_score,
    )


def _apply_guidance_pass(
    results: list[VpmaTickerResult],
    eligible: list[UniverseTicker],
    config: VpmaConfig,
    sec_provider: SECProvider,
    errors: list[str],
) -> None:
    guidance_candidates = [
        result
        for result in results
        if result.classification in {"actionable", "wait", "near_miss"}
    ]
    guidance_candidates.sort(key=_rank_for_enrichment, reverse=True)
    selected = guidance_candidates[: config.guidance_max_tickers]

    ticker_to_industry: dict[str, str] = {
        ticker.ticker: ticker.industry for ticker in eligible
    }

    for idx, result in enumerate(selected):
        try:
            event_ts_str = result.details.get("earnings_timestamp", "")
            if not event_ts_str:
                continue
            earnings_ts = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
            industry = ticker_to_industry.get(result.ticker, "")
            confirmation = extract_confirmation(
                sec_provider,
                result.ticker,
                industry,
                earnings_ts,
            )
            confirmed = apply_guidance_confirmation(result, confirmation, industry=industry)
            results[results.index(result)] = confirmed
        except Exception as exc:
            logger.warning("Guidance extraction failed for %s: %s", result.ticker, exc.__class__.__name__)
            errors.append(f"guidance:{result.ticker}:{exc.__class__.__name__}")
            continue


def run_vpma_scan(
    *,
    config: VpmaConfig | None = None,
    observed_at: str | None = None,
    universe_rows: list[dict[str, Any]] | None = None,
    data_source: YfinanceVpmaDataSource | None = None,
    alpha_client: AlphaVantageClient | None = None,
    http_session: requests.Session | None = None,
    sec_provider: SECProvider | None = None,
) -> VpmaScanResult:
    config = config or VpmaConfig.from_env()
    observed_at = observed_at or _now_iso()
    data_source = data_source or YfinanceVpmaDataSource(session=http_session)
    alpha_client = alpha_client or AlphaVantageClient(max_calls=config.max_enrich)

    errors: list[str] = []
    counts = {
        "raw_universe_rows": 0,
        "eligible_universe_tickers": 0,
        "liquid_histories": 0,
        "recent_event_tickers": 0,
        "enriched_tickers": 0,
        "invalid_symbol": 0,
        "missing_market_data": 0,
        "calculation_rejected": 0,
        "unexpected_errors": 0,
    }

    requested_test_tickers = [
        normalise_universe_ticker(part)
        for part in str(os.getenv("VPMA_TEST_TICKERS", "")).split(",")
        if normalise_universe_ticker(part)
    ]

    try:
        if requested_test_tickers:
            eligible = [
                UniverseTicker(
                    ticker=ticker,
                    name=ticker,
                    industry="",
                    source_price=None,
                    source_market_cap=None,
                    source_volume=None,
                )
                for ticker in requested_test_tickers
            ]
            counts["raw_universe_rows"] = len(eligible)
        else:
            source_rows = universe_rows if universe_rows is not None else fetch_universe_rows(
                config.universe_url,
                session=http_session,
            )
            counts["raw_universe_rows"] = len(source_rows)
            eligible = clean_universe_rows(
                source_rows,
                min_price=config.min_price,
                min_market_cap=config.min_market_cap,
                min_source_volume=config.min_source_volume,
            )
    except Exception as exc:
        raise RuntimeError(f"VPMA universe load failed: {exc.__class__.__name__}") from exc

    counts["eligible_universe_tickers"] = len(eligible)
    if not eligible:
        return VpmaScanResult(results=[], observed_at=observed_at, analysed_tickers=0, counts=counts, errors=errors)

    results: list[VpmaTickerResult] = []
    invalid_tickers: set[str] = set()
    for ticker in eligible:
        if not _yahoo_valid_symbol(ticker.ticker):
            invalid_tickers.add(ticker.ticker)
            counts["invalid_symbol"] += 1
            results.append(
                VpmaTickerResult(
                    ticker=ticker.ticker,
                    classification="excluded",
                    core_score=0.0,
                    event_score=0.0,
                    drift_score=0.0,
                    entry_score=0.0,
                    confirmation_score=None,
                    data_confidence="low",
                    setup_type="pead_deteriorating",
                    reason="Excluded: unsupported Yahoo symbol.",
                    valid_for_days=config.valid_days,
                    details={"risk_flags": ["invalid_symbol"], "model_version": MODEL_VERSION},
                )
            )

    valid_eligible = [t for t in eligible if t.ticker not in invalid_tickers]

    benchmark_history = data_source.benchmark_history(DEFAULT_BENCHMARK)
    histories = data_source.download_histories([ticker.ticker for ticker in valid_eligible])

    for ticker in valid_eligible:
        try:
            _evaluate_single_ticker(
                ticker,
                histories,
                benchmark_history,
                data_source,
                config,
                results,
                counts,
            )
        except Exception as exc:
            counts["unexpected_errors"] += 1
            logger.warning("VPMA evaluation failed for %s: %s", ticker.ticker, exc.__class__.__name__)
            errors.append(f"evaluate:{ticker.ticker}:{exc.__class__.__name__}")
            results.append(
                VpmaTickerResult(
                    ticker=ticker.ticker,
                    classification="excluded",
                    core_score=0.0,
                    event_score=0.0,
                    drift_score=0.0,
                    entry_score=0.0,
                    confirmation_score=None,
                    data_confidence="low",
                    setup_type="pead_deteriorating",
                    reason=f"Excluded: unexpected error ({exc.__class__.__name__}).",
                    valid_for_days=config.valid_days,
                    details={"risk_flags": ["unexpected_error"], "model_version": MODEL_VERSION},
                )
            )

    _apply_enrichment_pass(results, config, alpha_client, counts)

    if config.guidance_enable and sec_provider is not None:
        _apply_guidance_pass(results, eligible, config, sec_provider, errors)

    retained = [r for r in results if r.classification != "excluded"]
    logger.info(
        "VPMA scan summary: universe=%d evaluated=%d retained=%d "
        "invalid_symbol=%d missing_market_data=%d calculation_rejected=%d unexpected_errors=%d",
        counts["eligible_universe_tickers"],
        counts.get("recent_event_tickers", 0),
        len(retained),
        counts.get("invalid_symbol", 0),
        counts.get("missing_market_data", 0),
        counts.get("calculation_rejected", 0),
        counts.get("unexpected_errors", 0),
    )

    return VpmaScanResult(
        results=results,
        observed_at=observed_at,
        analysed_tickers=counts["recent_event_tickers"],
        counts=counts,
        errors=errors,
    )


def _evaluate_single_ticker(
    ticker: UniverseTicker,
    histories: dict[str, pd.DataFrame],
    benchmark_history: pd.DataFrame,
    data_source: YfinanceVpmaDataSource,
    config: VpmaConfig,
    results: list[VpmaTickerResult],
    counts: dict[str, int],
) -> None:
    history = histories.get(ticker.ticker)
    if history is None or history.empty or not {"Open", "High", "Low", "Close", "Volume"}.issubset(history.columns):
        counts["missing_market_data"] += 1
        results.append(
            VpmaTickerResult(
                ticker=ticker.ticker,
                classification="excluded",
                core_score=0.0,
                event_score=0.0,
                drift_score=0.0,
                entry_score=0.0,
                confirmation_score=None,
                data_confidence="low",
                setup_type="pead_deteriorating",
                reason="Excluded: market data unavailable.",
                valid_for_days=config.valid_days,
                details={"risk_flags": ["missing_market_data"], "model_version": MODEL_VERSION},
            )
        )
        return
    history = history.dropna(subset=["Close", "Volume"]).copy()
    if history.empty:
        counts["missing_market_data"] += 1
        results.append(
            VpmaTickerResult(
                ticker=ticker.ticker,
                classification="excluded",
                core_score=0.0,
                event_score=0.0,
                drift_score=0.0,
                entry_score=0.0,
                confirmation_score=None,
                data_confidence="low",
                setup_type="pead_deteriorating",
                reason="Excluded: market data unavailable after cleaning.",
                valid_for_days=config.valid_days,
                details={"risk_flags": ["missing_market_data"], "model_version": MODEL_VERSION},
            )
        )
        return
    try:
        mdv = median_dollar_volume(history)
    except ValueError:
        mdv = None
    if mdv is None or mdv < config.min_median_dollar_volume:
        results.append(
            VpmaTickerResult(
                ticker=ticker.ticker,
                classification="excluded",
                core_score=0.0,
                event_score=0.0,
                drift_score=0.0,
                entry_score=0.0,
                confirmation_score=None,
                data_confidence="low",
                setup_type="pead_deteriorating",
                reason="Excluded: insufficient liquidity.",
                valid_for_days=config.valid_days,
                details={"risk_flags": ["low_liquidity"], "median_dollar_volume": mdv, "model_version": MODEL_VERSION},
            )
        )
        return
    counts["liquid_histories"] += 1
    event_frame = data_source.earnings_dates(ticker.ticker)
    event = extract_recent_earnings_event(
        event_frame,
        history.index,
        lookback_days=config.event_lookback_days,
    )
    if event is None or event.days_since_reaction > 60:
        results.append(
            VpmaTickerResult(
                ticker=ticker.ticker,
                classification="excluded",
                core_score=0.0,
                event_score=0.0,
                drift_score=0.0,
                entry_score=0.0,
                confirmation_score=None,
                data_confidence="low",
                setup_type="pead_deteriorating",
                reason="Excluded: stale or missing recent earnings event.",
                valid_for_days=config.valid_days,
                details={"risk_flags": ["insufficient_history"], "model_version": MODEL_VERSION},
            )
        )
        return
    counts["recent_event_tickers"] += 1
    next_earnings = data_source.next_earnings_date(ticker.ticker)
    try:
        result = evaluate_ticker(
            ticker,
            history,
            benchmark_history,
            event,
            next_earnings_date=next_earnings,
            config=config,
        )
    except Exception:
        counts["calculation_rejected"] += 1
        results.append(
            VpmaTickerResult(
                ticker=ticker.ticker,
                classification="excluded",
                core_score=0.0,
                event_score=0.0,
                drift_score=0.0,
                entry_score=0.0,
                confirmation_score=None,
                data_confidence="low",
                setup_type="pead_deteriorating",
                reason="Excluded: calculation error.",
                valid_for_days=config.valid_days,
                details={"risk_flags": ["calculation_error"], "model_version": MODEL_VERSION},
            )
        )
        return
    results.append(result)


def _apply_enrichment_pass(
    results: list[VpmaTickerResult],
    config: VpmaConfig,
    alpha_client: AlphaVantageClient,
    counts: dict[str, int],
) -> None:
    if config.enable_enrichment:
        enrichable = [
            result
            for result in results
            if result.classification in {"actionable", "wait", "near_miss"}
        ]
        enrichable.sort(key=_rank_for_enrichment, reverse=True)
        selected = enrichable[: config.max_enrich]
        enriched_by_ticker: dict[str, AlphaVantageConfirmation] = {}
        for result in selected:
            confirmation = alpha_client.fetch_earnings_estimates(result.ticker)
            enriched_by_ticker[result.ticker] = confirmation
            if confirmation.status == "ENRICHED":
                counts["enriched_tickers"] += 1
        results[:] = [
            apply_confirmation(result, enriched_by_ticker[result.ticker])
            if result.ticker in enriched_by_ticker
            else VpmaTickerResult(
                **{
                    **result.__dict__,
                    "details": {**result.details, "enrichment_status": "NOT_SELECTED"},
                }
            )
            for result in results
        ]
    else:
        results[:] = [
            VpmaTickerResult(
                **{
                    **result.__dict__,
                    "details": {**result.details, "enrichment_status": "DISABLED"},
                }
            )
            for result in results
        ]
