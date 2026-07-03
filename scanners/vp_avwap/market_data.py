from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
import math

import pandas as pd
import yfinance as yf

from providers.yahoo_throttle import yahoo_call, yahoo_download
from scanners.vp_avwap.config import VpAvwapConfig

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]


logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")

# Yahoo intraday history is not available uniformly across every interval.
# These caps are intentionally conservative and are used only to avoid
# issuing requests that Yahoo will reject outright for the selected interval.
_INTRADAY_RETENTION_DAYS: dict[str, int] = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "90m": 60,
    "1h": 730,
}


class VpAvwapYahooDataSource:
    def __init__(self, *, config: VpAvwapConfig) -> None:
        self.config = config
        self._daily_cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._intraday_cache: dict[tuple[str, str, str, str], pd.DataFrame] = {}
        self._earnings_cache: dict[str, pd.DataFrame] = {}
        self._intraday_skip_reasons: dict[tuple[str, str, str, str], str] = {}

    def daily_history(self, ticker: str, *, period: str | None = None) -> pd.DataFrame:
        key = (ticker, period or self.config.daily_period)
        if key not in self._daily_cache:
            frame = yahoo_download(
                ticker,
                period=key[1],
                auto_adjust=self.config.auto_adjust,
                progress=False,
                threads=False,
                prepost=False,
            )
            self._daily_cache[key] = self._clean_history(frame)
        return self._daily_cache[key].copy()

    def intraday_history(self, ticker: str, *, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        key = (ticker, interval, start.date().isoformat(), end.date().isoformat())
        if key not in self._intraday_cache:
            retention_days = intraday_retention_days(interval)
            if retention_days is not None and request_exceeds_intraday_retention(
                interval=interval,
                start=start,
                end=end,
                retention_days=retention_days,
            ):
                self._intraday_skip_reasons[key] = (
                    f"{interval} history requested from {start.date().isoformat()} exceeds Yahoo's "
                    f"conservative retention window of {retention_days} days."
                )
                self._intraday_cache[key] = pd.DataFrame()
                return self._intraday_cache[key].copy()
            frame = yahoo_download(
                ticker,
                start=start.date().isoformat(),
                end=end.date().isoformat(),
                interval=interval,
                auto_adjust=self.config.auto_adjust,
                progress=False,
                threads=False,
                prepost=False,
            )
            self._intraday_cache[key] = self._clean_history(frame)
        return self._intraday_cache[key].copy()

    def intraday_skip_reason(self, ticker: str, *, interval: str, start: datetime, end: datetime) -> str | None:
        key = (ticker, interval, start.date().isoformat(), end.date().isoformat())
        return self._intraday_skip_reasons.get(key)

    def earnings_dates(self, ticker: str, *, limit: int | None = None) -> pd.DataFrame:
        if ticker not in self._earnings_cache:
            yf_ticker = yf.Ticker(ticker)
            try:
                frame = yahoo_call(lambda: yf_ticker.get_earnings_dates(limit=limit or self.config.earnings_limit), label=f"vp-avwap-earnings:{ticker}")
            except Exception:
                try:
                    frame = yahoo_call(lambda: getattr(yf_ticker, "earnings_dates", pd.DataFrame()), label=f"vp-avwap-earnings-fallback:{ticker}")
                except Exception:
                    frame = pd.DataFrame()
            self._earnings_cache[ticker] = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        return self._earnings_cache[ticker].copy()

    def latest_completed_daily(self, ticker: str, *, now_utc: datetime | None = None) -> pd.DataFrame:
        return trim_to_completed_daily(self.daily_history(ticker), now_utc=now_utc)

    @staticmethod
    def _clean_history(frame: pd.DataFrame | None) -> pd.DataFrame:
        if frame is None:
            return pd.DataFrame()
        cleaned = frame.dropna(how="all").copy()
        if cleaned.empty:
            return cleaned
        if isinstance(cleaned.columns, pd.MultiIndex):
            cleaned.columns = cleaned.columns.get_level_values(0)
        return cleaned


def trim_to_completed_daily(frame: pd.DataFrame, *, now_utc: datetime | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    now = now_utc or datetime.now(UTC)
    now_ny = now.astimezone(NY_TZ)
    working = frame.copy()
    index = pd.DatetimeIndex(working.index)
    if index.tz is not None:
        index = index.tz_convert(NY_TZ).tz_localize(None)
    working.index = index.normalize()
    latest_allowed = now_ny.date()
    if now_ny.time() < time(16, 0):
        latest_allowed = (now_ny - timedelta(days=1)).date()
    working = working[working.index.date <= latest_allowed]
    return working[~working.index.duplicated(keep="last")].copy()


def trim_intraday_to_completed_sessions(frame: pd.DataFrame, *, now_utc: datetime | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    now = now_utc or datetime.now(UTC)
    now_ny = now.astimezone(NY_TZ)
    working = frame.copy()
    index = pd.DatetimeIndex(working.index)
    if index.tz is None:
        index = index.tz_localize(NY_TZ)
    else:
        index = index.tz_convert(NY_TZ)
    working.index = index
    if now_ny.time() < time(16, 0):
        working = working[working.index.date < now_ny.date()]
    return working.tz_localize(None)


def has_full_session_coverage(frame: pd.DataFrame, *, reaction_session: pd.Timestamp, latest_completed_session: pd.Timestamp) -> bool:
    if frame.empty:
        return False
    dates = {pd.Timestamp(index).date() for index in frame.index}
    return reaction_session.date() in dates and latest_completed_session.date() in dates


def intraday_retention_days(interval: str) -> int | None:
    return _INTRADAY_RETENTION_DAYS.get(str(interval).strip().lower())


def request_exceeds_intraday_retention(
    *,
    interval: str,
    start: datetime,
    end: datetime,
    retention_days: int | None = None,
) -> bool:
    max_days = retention_days if retention_days is not None else intraday_retention_days(interval)
    if max_days is None:
        return False
    requested_days = (end - start).total_seconds() / 86_400.0
    if not math.isfinite(requested_days):
        return True
    return requested_days > max_days
