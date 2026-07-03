from __future__ import annotations

import math

import numpy as np
import pandas as pd

from scanners.vp_avwap.models import VolumeProfileResult


REQUIRED_COLUMNS = {"High", "Low", "Close", "Volume"}


def _finite_series(frame: pd.DataFrame, columns: set[str]) -> bool:
    for column in columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            return False
    return True


def _row_index(price: float, *, profile_low: float, profile_high: float, row_width: float, rows: int) -> int:
    if rows < 1:
        return 0
    if row_width <= 0 or profile_high <= profile_low:
        return 0
    if price >= profile_high:
        return rows - 1
    if price <= profile_low:
        return 0
    return max(0, min(rows - 1, int((price - profile_low) / row_width)))


def build_volume_profile(
    bars: pd.DataFrame,
    *,
    rows: int,
    value_area_pct: float,
    current_avwap: float | None,
    interval_used: str,
    data_quality: str,
) -> VolumeProfileResult:
    if bars.empty:
        return VolumeProfileResult(None, None, None, [], [], None, None, None, 0.0, 0.0, 0.0, None, 0, interval_used, data_quality, status="DATA_UNAVAILABLE", reason="No source bars were available for the earnings profile.")
    if not REQUIRED_COLUMNS.issubset(bars.columns):
        return VolumeProfileResult(None, None, None, [], [], None, None, None, 0.0, 0.0, 0.0, None, len(bars), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile bars are missing one or more required OHLCV columns.")
    if not _finite_series(bars, REQUIRED_COLUMNS):
        return VolumeProfileResult(None, None, None, [], [], None, None, None, 0.0, 0.0, 0.0, None, len(bars), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile bars contain non-finite or malformed OHLCV values.")

    working = bars.copy()
    working["High"] = pd.to_numeric(working["High"], errors="coerce")
    working["Low"] = pd.to_numeric(working["Low"], errors="coerce")
    working["Close"] = pd.to_numeric(working["Close"], errors="coerce")
    working["Volume"] = pd.to_numeric(working["Volume"], errors="coerce")
    if (working["Volume"] < 0).any():
        return VolumeProfileResult(None, None, None, [], [], None, None, None, 0.0, 0.0, 0.0, None, len(working), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile bars contain negative volume.")

    total_source_volume = float(working["Volume"].sum())
    if total_source_volume <= 0:
        return VolumeProfileResult(None, None, None, [], [], None, None, None, total_source_volume, 0.0, 0.0, None, len(working), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile bars contain no usable volume.")
    profile_low = float(working["Low"].min())
    profile_high = float(working["High"].max())
    row_width = (profile_high - profile_low) / rows if rows > 0 else 0.0

    if rows <= 0:
        return VolumeProfileResult(None, None, None, [], [], None, None, None, total_source_volume, 0.0, 0.0, None, len(working), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile row count must be positive.")

    boundaries: list[tuple[float, float]] = []
    if profile_high == profile_low:
        boundaries = [(profile_low, profile_high) for _ in range(rows)]
    else:
        for index in range(rows):
            low = profile_low + (row_width * index)
            high = profile_high if index == rows - 1 else profile_low + (row_width * (index + 1))
            boundaries.append((low, high))

    allocated = np.zeros(rows, dtype=float)
    for row in working.itertuples():
        low = float(row.Low)
        high = float(row.High)
        close = float(row.Close)
        volume = float(row.Volume)
        if volume <= 0:
            continue
        if high < low:
            return VolumeProfileResult(profile_low, profile_high, row_width, boundaries, allocated.tolist(), None, None, None, total_source_volume, float(allocated.sum()), 0.0, None, len(working), interval_used, data_quality, status="DATA_UNAVAILABLE", reason="Profile bars contain High values below Low.")
        if math.isclose(high, low):
            target_price = (high + low + close) / 3.0
            allocated[_row_index(target_price, profile_low=profile_low, profile_high=profile_high, row_width=row_width, rows=rows)] += volume
            continue
        overlap_total = 0.0
        per_row_overlap: list[tuple[int, float]] = []
        start_idx = _row_index(low, profile_low=profile_low, profile_high=profile_high, row_width=row_width, rows=rows)
        end_idx = _row_index(high, profile_low=profile_low, profile_high=profile_high, row_width=row_width, rows=rows)
        for idx in range(start_idx, end_idx + 1):
            row_low, row_high = boundaries[idx]
            overlap = max(0.0, min(high, row_high) - max(low, row_low))
            if idx == end_idx and math.isclose(high, row_high):
                overlap = max(overlap, min(high, row_high) - max(low, row_low))
            if overlap > 0:
                per_row_overlap.append((idx, overlap))
                overlap_total += overlap
        if overlap_total <= 0:
            allocated[start_idx] += volume
            continue
        for idx, overlap in per_row_overlap:
            allocated[idx] += volume * (overlap / overlap_total)

    total_allocated_volume = float(allocated.sum())
    if not math.isclose(total_allocated_volume, total_source_volume, rel_tol=1e-6, abs_tol=1e-6):
        difference = total_source_volume - total_allocated_volume
        allocated[0] += difference
        total_allocated_volume = float(allocated.sum())

    max_volume = float(allocated.max())
    tied_indices = [idx for idx, value in enumerate(allocated.tolist()) if math.isclose(value, max_volume, rel_tol=1e-9, abs_tol=1e-9)]
    midpoints = [((low + high) / 2.0) for low, high in boundaries]
    if current_avwap is not None and math.isfinite(current_avwap):
        poc_index = min(tied_indices, key=lambda idx: (abs(midpoints[idx] - current_avwap), midpoints[idx]))
    else:
        poc_index = min(tied_indices)

    included = {poc_index}
    included_volume = float(allocated[poc_index])
    target_volume = total_allocated_volume * (value_area_pct / 100.0)
    lower = poc_index - 1
    upper = poc_index + 1
    while included_volume < target_volume and (lower >= 0 or upper < rows):
        lower_volume = allocated[lower] if lower >= 0 else None
        upper_volume = allocated[upper] if upper < rows else None
        if lower_volume is not None and upper_volume is not None and math.isclose(float(lower_volume), float(upper_volume), rel_tol=1e-9, abs_tol=1e-9):
            included.add(lower)
            included.add(upper)
            included_volume += float(lower_volume) + float(upper_volume)
            lower -= 1
            upper += 1
            continue
        choose_upper = lower_volume is None or (upper_volume is not None and float(upper_volume) > float(lower_volume))
        if choose_upper and upper_volume is not None:
            included.add(upper)
            included_volume += float(upper_volume)
            upper += 1
        elif lower_volume is not None:
            included.add(lower)
            included_volume += float(lower_volume)
            lower -= 1
        else:
            break

    min_idx = min(included)
    max_idx = max(included)
    vah = boundaries[max_idx][1]
    val = boundaries[min_idx][0]
    poc = midpoints[poc_index]
    actual_pct = (included_volume / total_allocated_volume) * 100.0 if total_allocated_volume > 0 else None

    return VolumeProfileResult(
        profile_low=profile_low,
        profile_high=profile_high,
        row_width=row_width,
        row_boundaries=boundaries,
        allocated_row_volumes=allocated.tolist(),
        poc=poc,
        vah=vah,
        val=val,
        total_source_volume=total_source_volume,
        total_allocated_volume=total_allocated_volume,
        included_value_area_volume=included_volume,
        actual_value_area_percentage=actual_pct,
        bar_count=len(working),
        interval_used=interval_used,
        data_quality=data_quality,
    )
