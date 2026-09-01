from __future__ import annotations

import pandas as pd

from tactical.market_structure_capability_runner import (
    _split_adjustment_audit,
    _trim_to_completed_regular_sessions,
)


def _frame(previous_price: float, post_price: float, ratio: float) -> pd.DataFrame:
    index = pd.to_datetime([
        "2026-07-01 15:30:00-04:00",
        "2026-07-02 09:30:00-04:00",
    ])
    return pd.DataFrame(
        {
            "Open": [previous_price, post_price],
            "High": [previous_price, post_price],
            "Low": [previous_price, post_price],
            "Close": [previous_price, post_price],
            "Volume": [1000, 4000],
            "Stock Splits": [0.0, ratio],
        },
        index=index,
    )


def _session_frame(*, include_current_bars: int = 7, current_date: str = "2026-09-01") -> pd.DataFrame:
    rows: list[tuple[pd.Timestamp, float]] = []
    base_dates = ["2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"]
    bar_times = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    price = 100.0
    for day in base_dates:
        for clock in bar_times:
            rows.append((pd.Timestamp(f"{day} {clock}", tz="America/New_York"), price))
            price += 0.1
    for clock in bar_times[:include_current_bars]:
        rows.append((pd.Timestamp(f"{current_date} {clock}", tz="America/New_York"), price))
        price += 0.1
    index = pd.DatetimeIndex([row[0] for row in rows])
    values = [row[1] for row in rows]
    return pd.DataFrame(
        {
            "Open": values,
            "High": [value + 0.2 for value in values],
            "Low": [value - 0.2 for value in values],
            "Close": values,
            "Volume": [1000.0] * len(values),
            "Stock Splits": [0.0] * len(values),
        },
        index=index,
    )


def test_forward_split_adjusted_history_is_verified() -> None:
    split_in_window, checks, verified = _split_adjustment_audit(_frame(100.0, 101.0, 4.0))
    assert split_in_window is True
    assert verified is True
    assert checks[0]["state"] == "ADJUSTED"


def test_forward_split_unadjusted_history_fails_closed() -> None:
    split_in_window, checks, verified = _split_adjustment_audit(_frame(400.0, 101.0, 4.0))
    assert split_in_window is True
    assert verified is False
    assert checks[0]["state"] == "UNADJUSTED"


def test_reverse_split_adjusted_history_is_verified() -> None:
    split_in_window, checks, verified = _split_adjustment_audit(_frame(100.0, 98.0, 0.1))
    assert split_in_window is True
    assert verified is True
    assert checks[0]["state"] == "ADJUSTED"


def test_reverse_split_unadjusted_history_fails_closed() -> None:
    split_in_window, checks, verified = _split_adjustment_audit(_frame(10.0, 100.0, 0.1))
    assert split_in_window is True
    assert verified is False
    assert checks[0]["state"] == "UNADJUSTED"


def test_completed_session_guard_drops_live_current_session() -> None:
    frame = _session_frame(include_current_bars=1)
    trimmed, flags, verified = _trim_to_completed_regular_sessions(
        frame,
        now_utc=pd.Timestamp("2026-09-01 13:35:00+00:00"),
    )
    assert verified is True
    assert flags["state"] == "DROPPED_INCOMPLETE_CURRENT_SESSION"
    assert flags["reason"] == "CURRENT_LOCAL_SESSION_BEFORE_INFERRED_END"
    assert flags["dropped_session_date"] == "2026-09-01"
    assert pd.Timestamp(trimmed.index[-1]).date().isoformat() == "2026-08-31"


def test_completed_session_guard_keeps_full_session_after_conservative_end() -> None:
    frame = _session_frame(include_current_bars=7)
    trimmed, flags, verified = _trim_to_completed_regular_sessions(
        frame,
        now_utc=pd.Timestamp("2026-09-01 20:35:00+00:00"),
    )
    assert verified is True
    assert flags["state"] == "VERIFIED_COMPLETED"
    assert flags["reason"] == "CURRENT_LOCAL_SESSION_PAST_INFERRED_END"
    assert len(trimmed) == len(frame)
    assert pd.Timestamp(trimmed.index[-1]).date().isoformat() == "2026-09-01"


def test_completed_session_guard_drops_current_session_missing_final_bars_even_after_end() -> None:
    frame = _session_frame(include_current_bars=4)
    trimmed, flags, verified = _trim_to_completed_regular_sessions(
        frame,
        now_utc=pd.Timestamp("2026-09-01 21:00:00+00:00"),
    )
    assert verified is True
    assert flags["state"] == "DROPPED_INCOMPLETE_CURRENT_SESSION"
    assert flags["reason"] == "CURRENT_LOCAL_SESSION_MISSING_EXPECTED_FINAL_BARS"
    assert pd.Timestamp(trimmed.index[-1]).date().isoformat() == "2026-08-31"


def test_completed_session_guard_accepts_history_ending_before_local_today() -> None:
    frame = _session_frame(include_current_bars=0)
    trimmed, flags, verified = _trim_to_completed_regular_sessions(
        frame,
        now_utc=pd.Timestamp("2026-09-01 13:35:00+00:00"),
    )
    assert verified is True
    assert flags["state"] == "VERIFIED_COMPLETED"
    assert flags["reason"] == "LATEST_SESSION_PRECEDES_LOCAL_TODAY"
    assert len(trimmed) == len(frame)
