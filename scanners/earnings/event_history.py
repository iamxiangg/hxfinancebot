from __future__ import annotations

import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scanners.earnings.market_data import load_json_file, normalise_ticker
from scanners.earnings.models import EarningsEventInfo, HistoricalEventMove, HistoricalMoveSummary


logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")


def load_timing_overrides(path: Path) -> dict[str, str]:
    payload = load_json_file(path, default={})
    if not isinstance(payload, dict):
        return {}
    overrides: dict[str, str] = {}
    for key, value in payload.items():
        timing = str(value or "").strip().upper()
        if timing in {"AMC", "BMO", "UNKNOWN"}:
            overrides[str(key).strip().upper()] = timing
    return overrides


def to_ny_datetime(value: Any) -> datetime | None:
    if value in (None, "", pd.NaT):
        return None
    if isinstance(value, pd.Timestamp):
        timestamp = value.to_pydatetime()
    elif isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, date):
        timestamp = datetime(value.year, value.month, value.day)
    else:
        try:
            timestamp = pd.Timestamp(value).to_pydatetime()
        except Exception:
            return None

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=NY_TZ)
    return timestamp.astimezone(NY_TZ)


def infer_timing_from_timestamp(timestamp: datetime | None) -> tuple[str, str]:
    if timestamp is None:
        return "UNKNOWN", "no_timestamp"
    if (timestamp.hour, timestamp.minute, timestamp.second, timestamp.microsecond) == (0, 0, 0, 0):
        return "UNKNOWN", "date_without_time"
    if timestamp.hour >= 16:
        return "AMC", "timestamp_at_or_after_close"
    if timestamp.hour < 9 or (timestamp.hour == 9 and timestamp.minute <= 30):
        return "BMO", "timestamp_at_or_before_open"
    return "UNKNOWN", "timestamp_inside_regular_session"


def trading_sessions_from_history(history: pd.DataFrame) -> list[date]:
    if history.empty:
        return []
    index = history.index
    if isinstance(index, pd.DatetimeIndex):
        if index.tz is not None:
            index = index.tz_convert(NY_TZ).tz_localize(None)
        return sorted({timestamp.date() for timestamp in index})
    return []


def previous_trading_session(sessions: list[date], current: date) -> date | None:
    prior = [session for session in sessions if session < current]
    return prior[-1] if prior else None


def next_trading_session(sessions: list[date], current: date) -> date | None:
    future = [session for session in sessions if session > current]
    return future[0] if future else None


def _calendar_timestamp(calendar: Any) -> datetime | None:
    if calendar is None:
        return None
    if isinstance(calendar, pd.DataFrame):
        if "Earnings Date" in calendar.columns and not calendar.empty:
            value = calendar["Earnings Date"].iloc[0]
        elif "Earnings Date" in calendar.index:
            row = calendar.loc["Earnings Date"]
            value = row.iloc[0] if hasattr(row, "iloc") else row
        else:
            return None
    elif isinstance(calendar, pd.Series):
        value = calendar.get("Earnings Date")
    elif isinstance(calendar, dict):
        value = calendar.get("Earnings Date")
    else:
        return None
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    return to_ny_datetime(value)


def _future_earnings_candidates(earnings_frame: pd.DataFrame, *, now_ny: datetime) -> list[datetime]:
    if earnings_frame is None or len(getattr(earnings_frame, "index", [])) == 0:
        return []
    working = earnings_frame.copy()
    if not isinstance(working.index, pd.DatetimeIndex):
        try:
            working.index = pd.to_datetime(working.index)
        except Exception:
            return []
    candidates: list[datetime] = []
    for timestamp in working.index.tolist():
        dt = to_ny_datetime(timestamp)
        if dt is None or dt < now_ny:
            continue
        candidates.append(dt)
    return sorted(candidates)


def get_upcoming_earnings_event(
    ticker: str,
    *,
    earnings_frame: pd.DataFrame,
    calendar: Any,
    sessions: list[date],
    now_ny: datetime,
    lookahead_days: int,
    overrides: dict[str, str],
) -> EarningsEventInfo | None:
    ticker_key = normalise_ticker(ticker)
    candidates = _future_earnings_candidates(earnings_frame, now_ny=now_ny)
    calendar_timestamp = _calendar_timestamp(calendar)
    if calendar_timestamp is not None and calendar_timestamp >= now_ny:
        candidates.append(calendar_timestamp)
    if not candidates:
        return None

    earnings_at = min(candidates)
    if (earnings_at.date() - now_ny.date()).days > lookahead_days:
        return None

    override_key = f"{ticker_key}|{earnings_at.date().isoformat()}".upper()
    override_timing = overrides.get(override_key)
    if override_timing:
        earnings_timing = override_timing
        timing_source = "override"
        timing_reason = "manual_override"
    else:
        earnings_timing, timing_reason = infer_timing_from_timestamp(earnings_at)
        if calendar_timestamp is not None and calendar_timestamp == earnings_at:
            timing_source = "calendar"
        else:
            timing_source = "earnings_dates"

    entry_session_date = None
    exit_session_date = None
    if earnings_timing == "AMC":
        entry_session_date = earnings_at.date() if earnings_at.date() in sessions else None
        exit_session_date = next_trading_session(sessions, earnings_at.date())
    elif earnings_timing == "BMO":
        entry_session_date = previous_trading_session(sessions, earnings_at.date())
        exit_session_date = earnings_at.date() if earnings_at.date() in sessions else None

    return EarningsEventInfo(
        ticker=ticker_key,
        earnings_at=earnings_at,
        earnings_timing=earnings_timing,
        timing_source=timing_source,
        timing_reason=timing_reason,
        entry_session_date=entry_session_date,
        event_session_date=earnings_at.date(),
        exit_session_date=exit_session_date,
        event_date_key=earnings_at.date().isoformat(),
    )


def _session_row(history: pd.DataFrame, session: date) -> pd.Series | None:
    if history.empty:
        return None
    working = history.copy()
    index = working.index
    if isinstance(index, pd.DatetimeIndex) and index.tz is not None:
        working.index = index.tz_convert(NY_TZ).tz_localize(None)
    stamp = pd.Timestamp(session)
    if stamp not in working.index:
        return None
    return working.loc[stamp]


def _event_timing_for_past_row(
    ticker: str,
    timestamp: pd.Timestamp,
    overrides: dict[str, str],
) -> tuple[str, str]:
    override_key = f"{normalise_ticker(ticker)}|{timestamp.date().isoformat()}".upper()
    if override_key in overrides:
        return overrides[override_key], "override"
    return infer_timing_from_timestamp(to_ny_datetime(timestamp))


def build_historical_event_moves(
    ticker: str,
    *,
    earnings_frame: pd.DataFrame,
    history: pd.DataFrame,
    now_ny: datetime,
    overrides: dict[str, str],
    max_events: int = 20,
) -> list[HistoricalEventMove]:
    if earnings_frame is None or len(getattr(earnings_frame, "index", [])) == 0 or history.empty:
        return []
    working = earnings_frame.copy()
    if not isinstance(working.index, pd.DatetimeIndex):
        try:
            working.index = pd.to_datetime(working.index)
        except Exception:
            return []
    if working.index.tz is not None:
        working.index = working.index.tz_convert(NY_TZ).tz_localize(None)

    sessions = trading_sessions_from_history(history)
    results: list[HistoricalEventMove] = []
    for timestamp in sorted(working.index.tolist(), reverse=True):
        if len(results) >= max_events:
            break
        if timestamp.date() >= now_ny.date():
            continue
        timing, _reason = _event_timing_for_past_row(ticker, timestamp, overrides)
        if timing not in {"AMC", "BMO"}:
            continue
        event_day = timestamp.date()
        event_row = _session_row(history, event_day)
        if event_row is None:
            continue
        if timing == "AMC":
            pre_row = event_row
            post_day = next_trading_session(sessions, event_day)
            if post_day is None:
                continue
            post_row = _session_row(history, post_day)
        else:
            pre_day = previous_trading_session(sessions, event_day)
            if pre_day is None:
                continue
            pre_row = _session_row(history, pre_day)
            post_row = event_row
        if pre_row is None or post_row is None:
            continue

        pre_close = float(pre_row["Close"])
        post_open = float(post_row["Open"])
        session_close = float(post_row["Close"])
        session_high = float(post_row["High"])
        session_low = float(post_row["Low"])
        if min(pre_close, post_open, session_close, session_high, session_low) <= 0:
            continue
        absolute_event_move = abs(post_open / pre_close - 1.0)
        close_to_close_move = abs(session_close / pre_close - 1.0)
        max_excursion = max(abs(session_high / pre_close - 1.0), abs(session_low / pre_close - 1.0))
        if not all(math.isfinite(value) for value in (absolute_event_move, close_to_close_move, max_excursion)):
            continue
        if max(pre_close, post_open) / min(pre_close, post_open) > 3.0:
            continue

        results.append(
            HistoricalEventMove(
                event_date=event_day,
                timing=timing,
                pre_event_close=pre_close,
                post_event_open=post_open,
                absolute_event_move=absolute_event_move,
                close_to_close_move=close_to_close_move,
                maximum_first_session_excursion=max_excursion,
            )
        )
    return results


def summarise_historical_moves(moves: list[HistoricalEventMove]) -> HistoricalMoveSummary:
    if not moves:
        return HistoricalMoveSummary(0, None, None, None, None, None, None, None)
    values = np.array([move.absolute_event_move for move in moves], dtype=float)
    recent = values[:8]
    return HistoricalMoveSummary(
        usable_event_count=len(moves),
        median_absolute_move=float(np.median(values)),
        mean_absolute_move=float(np.mean(values)),
        recent_eight_event_mean=float(np.mean(recent)),
        p75_move=float(np.percentile(values, 75)),
        p90_move=float(np.percentile(values, 90)),
        maximum_move=float(np.max(values)),
        standard_deviation=float(np.std(values)),
    )


def realised_move_percentile(implied_move_pct: float, moves: list[HistoricalEventMove]) -> float | None:
    if not moves:
        return None
    values = [move.absolute_event_move for move in moves]
    count = sum(value <= implied_move_pct for value in values)
    return round((count / len(values)) * 100.0, 2)
