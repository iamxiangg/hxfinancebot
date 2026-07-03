from __future__ import annotations

from datetime import datetime
from typing import Literal

import pandas as pd

from scanners.vp_avwap.models import EarningsAnchor, EarningsAnchorSelection


DuringMarketPolicy = Literal["same_session", "next_session"]


def classify_release_timing(timestamp: datetime) -> tuple[str, str]:
    if timestamp.hour == 0 and timestamp.minute == 0 and timestamp.second == 0:
        return "uncertain", "low"
    if timestamp.hour < 12:
        return "before_market", "high"
    if timestamp.hour >= 16:
        return "after_market", "high"
    return "during_market", "high"


def _normalise_index(trading_index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if trading_index.tz is not None:
        return trading_index.tz_localize(None)
    return trading_index


def map_reaction_session(
    earnings_timestamp: datetime,
    trading_index: pd.DatetimeIndex,
    *,
    during_market_policy: DuringMarketPolicy = "same_session",
) -> tuple[pd.Timestamp | None, str, str]:
    if trading_index.empty:
        return None, "uncertain", "low"
    normalized = _normalise_index(trading_index)
    event_day = pd.Timestamp(earnings_timestamp.date())
    release_timing, confidence = classify_release_timing(earnings_timestamp)
    if release_timing == "after_market":
        candidates = normalized[normalized > event_day]
    elif release_timing == "during_market" and during_market_policy == "next_session":
        candidates = normalized[normalized > event_day]
    else:
        candidates = normalized[normalized >= event_day]
    if len(candidates) == 0:
        return None, release_timing, confidence
    return candidates[0], release_timing, confidence


def _coerce_earnings_index(earnings_frame: pd.DataFrame) -> pd.DatetimeIndex:
    if not isinstance(earnings_frame.index, pd.DatetimeIndex):
        return pd.to_datetime(earnings_frame.index, errors="coerce")
    index = earnings_frame.index
    if index.tz is not None:
        return index.tz_localize(None)
    return index


def select_latest_confirmed_earnings_anchor(
    earnings_frame: pd.DataFrame,
    trading_index: pd.DatetimeIndex,
    *,
    latest_completed_session: pd.Timestamp,
    during_market_policy: DuringMarketPolicy = "next_session",
) -> EarningsAnchorSelection:
    if earnings_frame is None or earnings_frame.empty:
        return EarningsAnchorSelection(None, None, "Missing confirmed earnings history.")
    if trading_index.empty:
        return EarningsAnchorSelection(None, None, "Missing trading-session history.")

    working = earnings_frame.copy()
    working.index = _coerce_earnings_index(working)
    working = working[working.index.notna()]
    if working.empty:
        return EarningsAnchorSelection(None, None, "Earnings history does not contain usable timestamps.")

    anchors: list[EarningsAnchor] = []
    normalized_index = _normalise_index(trading_index)
    latest_session = pd.Timestamp(latest_completed_session).tz_localize(None) if getattr(latest_completed_session, "tzinfo", None) is not None else pd.Timestamp(latest_completed_session)
    for timestamp in sorted(working.index.tolist(), reverse=True):
        if timestamp > latest_session:
            continue
        reaction_session, release_timing, confidence = map_reaction_session(
            timestamp.to_pydatetime(),
            normalized_index,
            during_market_policy=during_market_policy,
        )
        if reaction_session is None or reaction_session > latest_session:
            continue
        anchors.append(
            EarningsAnchor(
                earnings_timestamp=timestamp.to_pydatetime(),
                release_timing=release_timing,
                reaction_session=reaction_session,
                reaction_session_confidence=confidence,
            )
        )
    if not anchors:
        return EarningsAnchorSelection(None, None, "No usable confirmed earnings anchor before the latest completed session.")
    current = anchors[0]
    previous = anchors[1] if len(anchors) > 1 else None
    return EarningsAnchorSelection(current=current, previous=previous, reason=None)
