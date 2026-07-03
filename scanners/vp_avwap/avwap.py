from __future__ import annotations

import math

import numpy as np
import pandas as pd

from scanners.vp_avwap.models import AvwapResult


REQUIRED_COLUMNS = {"High", "Low", "Close", "Volume"}


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    working = bars.copy()
    for column in REQUIRED_COLUMNS:
        working[column] = pd.to_numeric(working[column], errors="coerce")
    return working


def compute_anchored_vwap(
    bars: pd.DataFrame,
    *,
    slope_lookback_sessions: int,
    previous_period_bars: pd.DataFrame | None = None,
) -> AvwapResult:
    if bars.empty or not REQUIRED_COLUMNS.issubset(bars.columns):
        return AvwapResult(None, pd.Series(dtype=float), {}, None, None, status="DATA_UNAVAILABLE", reason="Missing OHLCV bars for anchored VWAP.")
    working = _prepare_bars(bars)
    if working[list(REQUIRED_COLUMNS)].isna().any().any():
        return AvwapResult(None, pd.Series(dtype=float), {}, None, None, status="DATA_UNAVAILABLE", reason="Anchored VWAP bars contain malformed values.")
    if (working["Volume"] < 0).any():
        return AvwapResult(None, pd.Series(dtype=float), {}, None, None, status="DATA_UNAVAILABLE", reason="Anchored VWAP bars contain negative volume.")

    typical = (working["High"] + working["Low"] + working["Close"]) / 3.0
    cumulative_volume = working["Volume"].cumsum()
    if cumulative_volume.iloc[-1] <= 0:
        return AvwapResult(None, pd.Series(dtype=float), {}, None, None, status="DATA_UNAVAILABLE", reason="Anchored VWAP cumulative volume is zero.")
    cumulative_price_volume = (typical * working["Volume"]).cumsum()
    avwap_series = cumulative_price_volume / cumulative_volume
    avwap_series.index = working.index
    current_avwap = float(avwap_series.iloc[-1])

    session_keys = pd.Index([pd.Timestamp(idx).date().isoformat() for idx in working.index], dtype=object)
    snapshots: dict[str, float] = {}
    for key in session_keys.unique():
        session_series = avwap_series[session_keys == key]
        if not session_series.empty:
            snapshots[str(key)] = float(session_series.iloc[-1])

    slope = None
    snapshot_values = list(snapshots.values())
    if len(snapshot_values) > slope_lookback_sessions:
        earlier = snapshot_values[-(slope_lookback_sessions + 1)]
        if earlier and math.isfinite(earlier):
            slope = ((snapshot_values[-1] / earlier) - 1.0) * 100.0

    previous_anchor_vwap_close = None
    if previous_period_bars is not None and not previous_period_bars.empty and REQUIRED_COLUMNS.issubset(previous_period_bars.columns):
        previous = _prepare_bars(previous_period_bars)
        if not previous[list(REQUIRED_COLUMNS)].isna().any().any():
            prev_typical = (previous["High"] + previous["Low"] + previous["Close"]) / 3.0
            prev_cum_volume = previous["Volume"].cumsum()
            if not prev_cum_volume.empty and prev_cum_volume.iloc[-1] > 0:
                prev_cum_pv = (prev_typical * previous["Volume"]).cumsum()
                previous_anchor_vwap_close = float((prev_cum_pv / prev_cum_volume).iloc[-1])

    return AvwapResult(
        current_avwap=current_avwap,
        avwap_series=avwap_series,
        end_of_session_snapshots=snapshots,
        five_session_slope_pct=slope,
        previous_anchor_vwap_close=previous_anchor_vwap_close,
    )
