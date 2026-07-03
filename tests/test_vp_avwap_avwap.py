from __future__ import annotations

import unittest

import pandas as pd

from scanners.vp_avwap.avwap import compute_anchored_vwap


class AnchoredVwapTests(unittest.TestCase):
    def test_hlc3_cumulative_vwap_and_previous_period_close(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [11.0, 12.0, 13.0],
                "Low": [9.0, 10.0, 11.0],
                "Close": [10.0, 11.0, 12.0],
                "Volume": [100.0, 100.0, 200.0],
            },
            index=pd.date_range("2026-06-01", periods=3, freq="D"),
        )
        previous = pd.DataFrame(
            {
                "High": [10.0, 11.0],
                "Low": [8.0, 9.0],
                "Close": [9.0, 10.0],
                "Volume": [100.0, 100.0],
            },
            index=pd.date_range("2026-05-20", periods=2, freq="D"),
        )

        result = compute_anchored_vwap(
            bars,
            slope_lookback_sessions=2,
            previous_period_bars=previous,
        )

        self.assertEqual(result.status, "OK")
        self.assertAlmostEqual(result.current_avwap or 0.0, 11.25)
        self.assertAlmostEqual(result.previous_anchor_vwap_close or 0.0, 9.5)
        self.assertEqual(len(result.end_of_session_snapshots), 3)

    def test_five_session_slope_uses_session_snapshots(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [11, 12, 13, 14, 15, 16],
                "Low": [9, 10, 11, 12, 13, 14],
                "Close": [10, 11, 12, 13, 14, 15],
                "Volume": [100, 100, 100, 100, 100, 100],
            },
            index=pd.date_range("2026-06-01", periods=6, freq="D"),
        )

        result = compute_anchored_vwap(bars, slope_lookback_sessions=5)

        self.assertIsNotNone(result.five_session_slope_pct)
        self.assertGreater(result.five_session_slope_pct or 0.0, 0.0)

    def test_zero_cumulative_volume_returns_unavailable(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.0],
                "Volume": [0.0],
            },
            index=pd.date_range("2026-06-01", periods=1, freq="D"),
        )

        result = compute_anchored_vwap(bars, slope_lookback_sessions=5)

        self.assertEqual(result.status, "DATA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
