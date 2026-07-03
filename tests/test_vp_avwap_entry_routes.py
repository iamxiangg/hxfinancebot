from __future__ import annotations

from pathlib import Path
import unittest

import pandas as pd

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.entry_routes import evaluate_routes


def _config() -> VpAvwapConfig:
    return VpAvwapConfig(
        test_tickers=[],
        max_tickers=None,
        dry_run=True,
        write_sheets=False,
        send_telegram=False,
        telegram_test_mode=False,
        calibration=False,
        rows=60,
        value_area_pct=70.0,
        primary_interval="30m",
        secondary_interval="60m",
        confluence_pct=1.5,
        zone_buffer_pct=0.5,
        approach_pct=2.0,
        invalidation_buffer_pct=0.5,
        extension_pct=8.0,
        avwap_slope_lookback=5,
        avwap_flat_threshold_pct=0.25,
        falling_override_pct=-0.5,
        breakout_buffer_pct=0.5,
        breakout_retest_window=10,
        output_dir=Path("funnel_output/vp_avwap"),
    )


class EntryRouteTests(unittest.TestCase):
    def test_vah_route_can_confirm(self) -> None:
        frame = pd.DataFrame(
            {
                "Open": [101, 103, 106],
                "High": [103, 107, 107],
                "Low": [100, 102, 100],
                "Close": [102, 106, 106],
                "Volume": [1, 1, 1],
            },
            index=pd.date_range("2026-06-01", periods=3, freq="D"),
        )

        route = next(
            route for route in evaluate_routes(
                frame,
                latest_close=106.0,
                avwap=103.0,
                poc=102.0,
                vah=101.0,
                val=95.0,
                previous_anchor_vwap_close=99.0,
                avwap_slope_pct=0.5,
                config=_config(),
            )
            if route.route_code == "VAH_DEFENDED_PULLBACK"
        )

        self.assertTrue(route.eligible)
        self.assertEqual(route.status, "CONFIRMED")

    def test_poc_avwap_route_uses_single_level_fallback_without_false_confluence(self) -> None:
        frame = pd.DataFrame(
            {
                "Open": [100, 101, 102],
                "High": [101, 102, 103],
                "Low": [99, 100, 101],
                "Close": [100, 101.5, 102.0],
                "Volume": [1, 1, 1],
            },
            index=pd.date_range("2026-06-01", periods=3, freq="D"),
        )

        route = next(
            route for route in evaluate_routes(
                frame,
                latest_close=102.0,
                avwap=100.0,
                poc=104.0,
                vah=103.0,
                val=96.0,
                previous_anchor_vwap_close=97.0,
                avwap_slope_pct=0.5,
                config=_config(),
            )
            if route.route_code == "POC_AVWAP_RECOVERY"
        )

        self.assertIn("stronger single level", route.reason)
        self.assertEqual(len(route.level_basis), 1)

    def test_breakout_retest_avoids_same_bar_lookahead(self) -> None:
        frame = pd.DataFrame(
            {
                "Open": [10.0, 10.2, 11.1, 10.8],
                "High": [10.3, 10.5, 11.3, 11.1],
                "Low": [9.8, 10.0, 10.9, 10.4],
                "Close": [10.1, 10.4, 11.2, 11.0],
                "Volume": [1, 1, 1, 1],
            },
            index=pd.date_range("2026-06-01", periods=4, freq="D"),
        )

        route = next(
            route for route in evaluate_routes(
                frame,
                latest_close=11.0,
                avwap=10.7,
                poc=10.8,
                vah=10.9,
                val=9.9,
                previous_anchor_vwap_close=10.0,
                avwap_slope_pct=0.5,
                config=_config(),
            )
            if route.route_code == "BREAKOUT_RETEST"
        )

        self.assertEqual(route.status, "CONFIRMED")
        self.assertAlmostEqual(route.metadata["breakout_level"], 10.5)

    def test_val_reclaim_not_confirmed_while_below_zone(self) -> None:
        frame = pd.DataFrame(
            {
                "Open": [10.0, 9.8],
                "High": [10.2, 9.9],
                "Low": [9.7, 9.4],
                "Close": [9.8, 9.45],
                "Volume": [1, 1],
            },
            index=pd.date_range("2026-06-01", periods=2, freq="D"),
        )

        route = next(
            route for route in evaluate_routes(
                frame,
                latest_close=9.45,
                avwap=9.8,
                poc=9.7,
                vah=10.1,
                val=9.5,
                previous_anchor_vwap_close=9.3,
                avwap_slope_pct=-0.1,
                config=_config(),
            )
            if route.route_code == "VAL_RECLAIM"
        )

        self.assertNotEqual(route.status, "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
