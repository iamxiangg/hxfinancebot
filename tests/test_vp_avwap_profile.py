from __future__ import annotations

import unittest

import pandas as pd

from scanners.vp_avwap.profile import build_volume_profile


class VolumeProfileTests(unittest.TestCase):
    def test_profile_uses_exact_row_count_and_conserves_volume(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [14.0, 15.0],
                "Low": [10.0, 11.0],
                "Close": [13.0, 14.5],
                "Volume": [100.0, 200.0],
            },
            index=pd.date_range("2026-06-01", periods=2, freq="D"),
        )

        result = build_volume_profile(
            bars,
            rows=60,
            value_area_pct=70.0,
            current_avwap=13.0,
            interval_used="30m",
            data_quality="HIGH",
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(len(result.row_boundaries), 60)
        self.assertEqual(len(result.allocated_row_volumes), 60)
        self.assertAlmostEqual(result.total_source_volume, 300.0)
        self.assertAlmostEqual(result.total_allocated_volume, 300.0)

    def test_zero_range_bar_allocates_to_single_row(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [10.0],
                "Low": [10.0],
                "Close": [10.0],
                "Volume": [120.0],
            },
            index=pd.date_range("2026-06-01", periods=1, freq="D"),
        )

        result = build_volume_profile(
            bars,
            rows=60,
            value_area_pct=70.0,
            current_avwap=10.0,
            interval_used="daily",
            data_quality="LOW",
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.allocated_row_volumes[0], 120.0)
        self.assertAlmostEqual(sum(result.allocated_row_volumes[1:]), 0.0)

    def test_poc_tie_breaks_toward_nearest_avwap(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [11.0, 20.0],
                "Low": [10.0, 19.0],
                "Close": [10.5, 19.5],
                "Volume": [100.0, 100.0],
            },
            index=pd.date_range("2026-06-01", periods=2, freq="D"),
        )

        result = build_volume_profile(
            bars,
            rows=2,
            value_area_pct=70.0,
            current_avwap=19.5,
            interval_used="30m",
            data_quality="HIGH",
        )

        self.assertGreater(result.poc or 0.0, 15.0)

    def test_equal_adjacent_rows_are_added_together_in_value_area(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [2.0, 3.0, 4.0],
                "Low": [1.0, 2.0, 3.0],
                "Close": [1.5, 2.5, 3.5],
                "Volume": [20.0, 30.0, 20.0],
            },
            index=pd.date_range("2026-06-01", periods=3, freq="D"),
        )

        result = build_volume_profile(
            bars,
            rows=3,
            value_area_pct=70.0,
            current_avwap=2.5,
            interval_used="daily",
            data_quality="LOW",
        )

        self.assertEqual(result.status, "OK")
        self.assertAlmostEqual(result.actual_value_area_percentage or 0.0, 100.0)
        self.assertEqual(result.val, 1.0)
        self.assertEqual(result.vah, 4.0)

    def test_zero_volume_returns_unavailable(self) -> None:
        bars = pd.DataFrame(
            {
                "High": [11.0],
                "Low": [10.0],
                "Close": [10.5],
                "Volume": [0.0],
            },
            index=pd.date_range("2026-06-01", periods=1, freq="D"),
        )

        result = build_volume_profile(
            bars,
            rows=60,
            value_area_pct=70.0,
            current_avwap=10.5,
            interval_used="daily",
            data_quality="LOW",
        )

        self.assertEqual(result.status, "DATA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
