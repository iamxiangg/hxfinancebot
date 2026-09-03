from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from providers.yahoo_throttle import yahoo_download
from tactical.market_structure_capability_runner import (
    INTERVAL,
    _clean_history,
    _session_dates,
    _slice_last_sessions,
    _trim_to_completed_regular_sessions,
    _window_metrics,
)

REACTION_METHODOLOGY_VERSION = "HX_MARKET_REACTION_STATE_v1"
DEFAULT_BACKFILL_SESSIONS = 10
DEFAULT_WARMUP_SESSIONS = 5
MAX_TRACK_AGE_SESSIONS = 5

VP_STATE_MAP = {
    "VAL_HOLD": "POSITIVE",
    "VP_LOW_RECLAIM": "POSITIVE",
    "VALUE_RECLAIM": "POSITIVE",
    "POC_HOLD": "POSITIVE",
    "POC_RECLAIM": "POSITIVE",
    "VAH_BREAKOUT_HOLD": "POSITIVE",
    "VALUE_AREA_HOLD": "NEUTRAL",
    "BOUNCE_ATTEMPT": "NEUTRAL",
    "VAH_REJECTION": "NEGATIVE",
    "POC_REJECTION": "NEGATIVE",
    "VAL_LOSS": "NEGATIVE",
    "FAILED_RECLAIM": "NEGATIVE",
    "BREAKDOWN": "NEGATIVE",
    "CONTINUATION": "NEGATIVE",
    "REACTION_UNAVAILABLE": "UNAVAILABLE",
}

VWAP_STATE_MAP = {
    "VWAP_HOLD": "POSITIVE",
    "VWAP_RECLAIM": "POSITIVE",
    "ABOVE_VWAP_NO_TEST": "NEUTRAL",
    "AT_VWAP_NO_TEST": "NEUTRAL",
    "BELOW_VWAP_NO_TEST": "NEGATIVE",
    "VWAP_REJECTION": "NEGATIVE",
    "VWAP_LOSS": "NEGATIVE",
    "CONTINUATION_BELOW_VWAP": "NEGATIVE",
    "REACTION_UNAVAILABLE": "UNAVAILABLE",
}


@dataclass
class AnchorTracker:
    anchor: str
    tested_level: float
    tested_at: str
    origin_side: str
    phase: str = "TESTED"
    session_index: int = 0
    above_closes: int = 0
    below_closes: int = 0
    max_close_after: float | None = None
    min_close_after: float | None = None
    reclaim_at: str | None = None
    resolved_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def evidence(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor,
            "tested_level": self.tested_level,
            "tested_at": self.tested_at,
            "origin_side": self.origin_side,
            "phase": self.phase,
            "above_closes": self.above_closes,
            "below_closes": self.below_closes,
            "max_close_after": self.max_close_after,
            "min_close_after": self.min_close_after,
            "reclaim_at": self.reclaim_at,
            "resolved_at": self.resolved_at,
            "events": self.events[-8:],
        }


def _download_completed_history(symbol: str, *, checked_at: str) -> pd.DataFrame:
    raw = yahoo_download(
        symbol,
        period="6mo",
        interval=INTERVAL,
        auto_adjust=False,
        actions=True,
        repair=False,
        progress=False,
        threads=False,
        prepost=False,
        timeout=15,
        _yahoo_retries=3,
    )
    frame = _clean_history(raw)
    if frame.empty:
        raise ValueError("Yahoo returned no 60m history for reaction backfill")
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Missing OHLCV columns for reaction backfill: {', '.join(missing)}")
    numeric = frame[list(required)].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("Malformed or non-finite OHLCV values in reaction backfill")
    frame, _, verified = _trim_to_completed_regular_sessions(frame, now_utc=pd.Timestamp(checked_at))
    if frame.empty or not verified:
        raise ValueError("Completed-session guard not verified for reaction backfill")
    return frame


def _session_frame(frame: pd.DataFrame, session_date: date) -> pd.DataFrame:
    return frame[[pd.Timestamp(idx).date() == session_date for idx in frame.index]].copy()


def _prefix_before(frame: pd.DataFrame, session_date: date) -> pd.DataFrame:
    return frame[[pd.Timestamp(idx).date() < session_date for idx in frame.index]].copy()


def _prefix_through(frame: pd.DataFrame, session_date: date) -> pd.DataFrame:
    return frame[[pd.Timestamp(idx).date() <= session_date for idx in frame.index]].copy()


def _tolerance_pct(prefix: pd.DataFrame) -> float:
    recent = _slice_last_sessions(prefix, min(20, len(_session_dates(prefix))))
    if recent.empty:
        return 0.005
    close = pd.to_numeric(recent["Close"], errors="coerce").replace(0, np.nan)
    ranges = (pd.to_numeric(recent["High"], errors="coerce") - pd.to_numeric(recent["Low"], errors="coerce")) / close
    values = ranges.replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return 0.005
    median_hourly_range = float(values.median())
    return max(0.0035, min(0.0125, median_hourly_range * 0.35))


def _side(value: float, level: float, tol: float) -> str:
    if value > level + tol:
        return "ABOVE"
    if value < level - tol:
        return "BELOW"
    return "AT"


def _bar_interacts(bar: pd.Series, level: float, tol: float) -> bool:
    return float(bar["Low"]) <= level + tol and float(bar["High"]) >= level - tol


def _new_tracker(
    anchor: str,
    level: float,
    bar_at: object,
    previous_close: float,
    session_index: int,
    tol: float,
) -> AnchorTracker:
    origin = _side(previous_close, level, tol)
    tracker = AnchorTracker(
        anchor=anchor,
        tested_level=float(level),
        tested_at=pd.Timestamp(bar_at).isoformat(),
        origin_side=origin,
        session_index=session_index,
    )
    tracker.events.append({"at": tracker.tested_at, "event": "INTERACTION", "level": float(level), "origin_side": origin})
    return tracker


def _advance_existing(tracker: AnchorTracker, bars: pd.DataFrame, *, tol_pct: float, session_index: int) -> AnchorTracker:
    level = tracker.tested_level
    tol = level * tol_pct
    for bar_at, bar in bars.iterrows():
        close = float(bar["Close"])
        tracker.max_close_after = close if tracker.max_close_after is None else max(tracker.max_close_after, close)
        tracker.min_close_after = close if tracker.min_close_after is None else min(tracker.min_close_after, close)
        side = _side(close, level, tol)

        if tracker.origin_side == "ABOVE":
            if side == "ABOVE":
                tracker.above_closes += 1
                tracker.below_closes = 0
                if tracker.above_closes >= 2 and (tracker.max_close_after or close) >= level + 2 * tol:
                    if tracker.phase != "HOLD_CONFIRMED":
                        tracker.events.append({"at": pd.Timestamp(bar_at).isoformat(), "event": "HOLD_CONFIRMED", "close": close})
                    tracker.phase = "HOLD_CONFIRMED"
                    tracker.resolved_at = pd.Timestamp(bar_at).isoformat()
                elif tracker.phase not in ("HOLD_CONFIRMED", "LOSS_CONFIRMED"):
                    tracker.phase = "HOLD_CANDIDATE"
            elif side == "BELOW":
                tracker.below_closes += 1
                tracker.above_closes = 0
                if tracker.below_closes >= 2 or close <= level - 2 * tol:
                    if tracker.phase != "LOSS_CONFIRMED":
                        tracker.events.append({"at": pd.Timestamp(bar_at).isoformat(), "event": "LOSS_CONFIRMED", "close": close})
                    tracker.phase = "LOSS_CONFIRMED"
                    tracker.resolved_at = pd.Timestamp(bar_at).isoformat()
                elif tracker.phase != "LOSS_CONFIRMED":
                    tracker.phase = "LOSS_CANDIDATE"

        elif tracker.origin_side == "BELOW":
            if side == "ABOVE":
                tracker.above_closes += 1
                tracker.below_closes = 0
                if tracker.reclaim_at is None:
                    tracker.reclaim_at = pd.Timestamp(bar_at).isoformat()
                    tracker.events.append({"at": tracker.reclaim_at, "event": "RECLAIM_CANDIDATE", "close": close})
                if tracker.above_closes >= 2 and (tracker.max_close_after or close) >= level + 2 * tol:
                    if tracker.phase != "RECLAIM_CONFIRMED":
                        tracker.events.append({"at": pd.Timestamp(bar_at).isoformat(), "event": "RECLAIM_CONFIRMED", "close": close})
                    tracker.phase = "RECLAIM_CONFIRMED"
                    tracker.resolved_at = pd.Timestamp(bar_at).isoformat()
                else:
                    tracker.phase = "RECLAIM_CANDIDATE"
            elif side == "BELOW":
                tracker.below_closes += 1
                if tracker.phase in ("RECLAIM_CANDIDATE", "RECLAIM_CONFIRMED"):
                    tracker.events.append({"at": pd.Timestamp(bar_at).isoformat(), "event": "FAILED_RECLAIM", "close": close})
                    tracker.phase = "FAILED_RECLAIM"
                    tracker.resolved_at = pd.Timestamp(bar_at).isoformat()
                elif tracker.below_closes >= 2 or close <= level - 2 * tol:
                    tracker.phase = "REJECTION_CONFIRMED"
                    tracker.resolved_at = pd.Timestamp(bar_at).isoformat()
                else:
                    tracker.phase = "REJECTION_CANDIDATE"

        else:  # origin AT: first decisive move establishes direction conservatively.
            if side == "ABOVE":
                tracker.origin_side = "BELOW"
                tracker.above_closes = 1
                tracker.reclaim_at = pd.Timestamp(bar_at).isoformat()
                tracker.phase = "RECLAIM_CANDIDATE"
            elif side == "BELOW":
                tracker.origin_side = "ABOVE"
                tracker.below_closes = 1
                tracker.phase = "LOSS_CANDIDATE"

    tracker.session_index = session_index
    return tracker


def _update_anchor(
    tracker: AnchorTracker | None,
    *,
    anchor: str,
    reference_level: float,
    session_bars: pd.DataFrame,
    previous_close: float,
    tol_pct: float,
    session_index: int,
) -> AnchorTracker | None:
    if tracker is not None and session_index - tracker.session_index > MAX_TRACK_AGE_SESSIONS:
        tracker = None

    if tracker is not None:
        tracker = _advance_existing(tracker, session_bars, tol_pct=tol_pct, session_index=session_index)

    level = float(reference_level)
    tol = level * tol_pct
    interaction_rows = [(idx, bar) for idx, bar in session_bars.iterrows() if _bar_interacts(bar, level, tol)]
    if not interaction_rows:
        return tracker

    should_start_new = tracker is None
    if tracker is not None:
        material_level_change = abs(level - tracker.tested_level) > max(tol * 2, abs(tracker.tested_level) * 0.0075)
        resolved_old_enough = tracker.phase in {
            "HOLD_CONFIRMED", "LOSS_CONFIRMED", "RECLAIM_CONFIRMED", "REJECTION_CONFIRMED", "FAILED_RECLAIM"
        } and session_index - tracker.session_index >= 1
        should_start_new = material_level_change or resolved_old_enough

    if not should_start_new:
        return tracker

    first_idx, _ = interaction_rows[0]
    prior_rows = session_bars.loc[:first_idx].iloc[:-1]
    origin_close = float(prior_rows.iloc[-1]["Close"]) if not prior_rows.empty else previous_close
    tracker = _new_tracker(anchor, level, first_idx, origin_close, session_index, tol)
    post = session_bars.loc[first_idx:]
    return _advance_existing(tracker, post, tol_pct=tol_pct, session_index=session_index)


def _vp_reaction(trackers: dict[str, AnchorTracker | None], current: dict[str, float], close: float) -> tuple[str, AnchorTracker | None]:
    low = trackers.get("profile_low")
    val = trackers.get("val")
    poc = trackers.get("poc")
    vah = trackers.get("vah")

    if low and low.phase == "LOSS_CONFIRMED":
        return "BREAKDOWN", low
    if val and val.phase == "FAILED_RECLAIM":
        return "FAILED_RECLAIM", val
    if val and val.phase == "LOSS_CONFIRMED":
        return "VAL_LOSS", val
    if poc and poc.phase == "REJECTION_CONFIRMED":
        return "POC_REJECTION", poc
    if vah and vah.phase == "REJECTION_CONFIRMED":
        return "VAH_REJECTION", vah

    if low and low.phase == "RECLAIM_CONFIRMED":
        return "VP_LOW_RECLAIM", low
    if val and val.phase == "RECLAIM_CONFIRMED":
        return "VALUE_RECLAIM", val
    if val and val.phase == "HOLD_CONFIRMED":
        return "VAL_HOLD", val
    if poc and poc.phase == "RECLAIM_CONFIRMED":
        return "POC_RECLAIM", poc
    if poc and poc.phase == "HOLD_CONFIRMED":
        return "POC_HOLD", poc
    if vah and vah.phase in ("RECLAIM_CONFIRMED", "HOLD_CONFIRMED") and close >= current["vah"]:
        return "VAH_BREAKOUT_HOLD", vah

    candidates = [t for t in (low, val, poc, vah) if t and t.phase in {
        "TESTED", "HOLD_CANDIDATE", "RECLAIM_CANDIDATE", "REJECTION_CANDIDATE", "LOSS_CANDIDATE"
    }]
    if candidates:
        return "BOUNCE_ATTEMPT", sorted(candidates, key=lambda t: t.tested_at)[-1]

    if close < current["profile_low"]:
        return "CONTINUATION", low
    if current["val"] <= close <= current["vah"]:
        return "VALUE_AREA_HOLD", None
    return "REACTION_UNAVAILABLE", None


def _vwap_reaction(tracker: AnchorTracker | None, *, current_vwap: float, close: float, tol_pct: float) -> tuple[str, AnchorTracker | None]:
    if tracker:
        if tracker.phase == "HOLD_CONFIRMED":
            return "VWAP_HOLD", tracker
        if tracker.phase == "RECLAIM_CONFIRMED":
            return "VWAP_RECLAIM", tracker
        if tracker.phase in ("REJECTION_CONFIRMED", "FAILED_RECLAIM"):
            return "VWAP_REJECTION", tracker
        if tracker.phase == "LOSS_CONFIRMED":
            return "VWAP_LOSS", tracker
    tol = current_vwap * tol_pct
    if close > current_vwap + tol:
        return "ABOVE_VWAP_NO_TEST", tracker
    if close < current_vwap - tol:
        if tracker and tracker.phase == "LOSS_CONFIRMED":
            return "CONTINUATION_BELOW_VWAP", tracker
        return "BELOW_VWAP_NO_TEST", tracker
    return "AT_VWAP_NO_TEST", tracker


def _composite(vp_state: str, vwap_state: str) -> str:
    if vp_state == "NEGATIVE" or vwap_state == "NEGATIVE":
        return "RED"
    if vp_state == "POSITIVE" and vwap_state == "POSITIVE":
        return "GREEN"
    if (vp_state == "POSITIVE") ^ (vwap_state == "POSITIVE"):
        return "ORANGE"
    return "GREY"


def backfill_reaction_states(
    symbol: str,
    *,
    checked_at: str,
    backfill_sessions: int = DEFAULT_BACKFILL_SESSIONS,
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
) -> list[dict[str, Any]]:
    backfill_sessions = max(3, min(30, int(backfill_sessions)))
    warmup_sessions = max(2, min(10, int(warmup_sessions)))
    frame = _download_completed_history(symbol, checked_at=checked_at)
    dates = _session_dates(frame)
    if len(dates) < 62:
        raise ValueError(f"Only {len(dates)} completed sessions available for reaction backfill")

    process_count = min(len(dates) - 60, backfill_sessions + warmup_sessions)
    process_dates = dates[-process_count:]
    output_dates = set(dates[-backfill_sessions:])
    trackers_by_window: dict[int, dict[str, AnchorTracker | None]] = {
        20: {"profile_low": None, "val": None, "poc": None, "vah": None, "vwap": None},
        60: {"profile_low": None, "val": None, "poc": None, "vah": None, "vwap": None},
    }
    items: list[dict[str, Any]] = []

    for session_index, session_date in enumerate(process_dates):
        before = _prefix_before(frame, session_date)
        through = _prefix_through(frame, session_date)
        session = _session_frame(frame, session_date)
        if session.empty or len(_session_dates(before)) < 60:
            continue
        previous_close = float(before.iloc[-1]["Close"])
        session_close = float(session.iloc[-1]["Close"])
        as_of_bar_at = pd.Timestamp(session.index[-1]).isoformat()
        tol_pct = _tolerance_pct(before)

        for window in (20, 60):
            reference, _ = _window_metrics(before, window)
            current, _ = _window_metrics(through, window)
            trackers = trackers_by_window[window]
            for anchor in ("profile_low", "val", "poc", "vah", "vwap"):
                trackers[anchor] = _update_anchor(
                    trackers.get(anchor),
                    anchor=anchor,
                    reference_level=float(reference[anchor]),
                    session_bars=session,
                    previous_close=previous_close,
                    tol_pct=tol_pct,
                    session_index=session_index,
                )

            vp_reaction, vp_tracker = _vp_reaction(trackers, current, session_close)
            vwap_reaction, vwap_tracker = _vwap_reaction(
                trackers.get("vwap"), current_vwap=float(current["vwap"]), close=session_close, tol_pct=tol_pct
            )
            vp_state = VP_STATE_MAP[vp_reaction]
            vwap_state = VWAP_STATE_MAP[vwap_reaction]
            horizon = _composite(vp_state, vwap_state)
            primary = vp_tracker if vp_tracker is not None else vwap_tracker

            if session_date not in output_dates:
                continue

            items.append(
                {
                    "provider_symbol": symbol,
                    "as_of_session_date": session_date.isoformat(),
                    "as_of_bar_at": as_of_bar_at,
                    "window_sessions": window,
                    "backfill_sessions": backfill_sessions,
                    "close_price": session_close,
                    "vwap": float(current["vwap"]),
                    "poc": float(current["poc"]),
                    "vah": float(current["vah"]),
                    "val": float(current["val"]),
                    "profile_high": float(current["profile_high"]),
                    "profile_low": float(current["profile_low"]),
                    "vp_reaction": vp_reaction,
                    "vp_component_state": vp_state,
                    "vwap_reaction": vwap_reaction,
                    "vwap_component_state": vwap_state,
                    "horizon_composite": horizon,
                    "tested_anchor": primary.anchor if primary else None,
                    "tested_level": primary.tested_level if primary else None,
                    "tested_at": primary.tested_at if primary else None,
                    "interaction_phase": primary.phase if primary else None,
                    "methodology_version": REACTION_METHODOLOGY_VERSION,
                    "evidence": {
                        "reference_level_basis": "PRIOR_COMPLETED_SESSION_ROLLING_LEVELS",
                        "tested_level_frozen_at_interaction": True,
                        "completed_regular_session_bars_only": True,
                        "source_interval": INTERVAL,
                        "tolerance_pct": tol_pct,
                        "session_open": float(session.iloc[0]["Open"]),
                        "session_high": float(session["High"].max()),
                        "session_low": float(session["Low"].min()),
                        "session_close": session_close,
                        "reference_levels": {k: float(reference[k]) for k in ("vwap", "poc", "vah", "val", "profile_high", "profile_low")},
                        "current_levels": {k: float(current[k]) for k in ("vwap", "poc", "vah", "val", "profile_high", "profile_low")},
                        "anchor_states": {k: (v.evidence() if v else None) for k, v in trackers.items()},
                    },
                }
            )

    if not items:
        raise ValueError("Reaction backfill produced no rows")
    return items
