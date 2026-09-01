from __future__ import annotations

import pandas as pd

from tactical.market_structure_capability_runner import _split_adjustment_audit


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
