from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.market_data import (
    VpAvwapYahooDataSource,
    intraday_retention_days,
    request_exceeds_intraday_retention,
)


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


class MarketDataTests(unittest.TestCase):
    def test_retention_helper_flags_long_30m_request(self) -> None:
        self.assertEqual(intraday_retention_days("30m"), 60)
        self.assertTrue(
            request_exceeds_intraday_retention(
                interval="30m",
                start=datetime(2026, 4, 21),
                end=datetime(2026, 7, 4),
            )
        )
        self.assertFalse(
            request_exceeds_intraday_retention(
                interval="60m",
                start=datetime(2026, 4, 21),
                end=datetime(2026, 7, 4),
            )
        )

    def test_retention_helper_accepts_mixed_naive_and_aware_datetimes(self) -> None:
        self.assertTrue(
            request_exceeds_intraday_retention(
                interval="30m",
                start=datetime(2026, 4, 21),
                end=datetime(2026, 7, 4, tzinfo=UTC),
            )
        )
        self.assertFalse(
            request_exceeds_intraday_retention(
                interval="30m",
                start=datetime(2026, 6, 1),
                end=datetime(2026, 7, 4, tzinfo=UTC),
            )
        )

    def test_datasource_skips_unsupported_intraday_download_before_yahoo_call(self) -> None:
        source = VpAvwapYahooDataSource(config=_config())
        start = datetime(2026, 4, 21)
        end = datetime(2026, 7, 4, tzinfo=UTC)

        with patch("scanners.vp_avwap.market_data.yahoo_download") as mocked_download:
            frame = source.intraday_history("TSLA", interval="30m", start=start, end=end)

        self.assertTrue(frame.empty)
        mocked_download.assert_not_called()
        reason = source.intraday_skip_reason("TSLA", interval="30m", start=start, end=end)
        self.assertIsNotNone(reason)
        self.assertIn("retention window", reason or "")

    def test_datasource_allows_supported_intraday_download(self) -> None:
        source = VpAvwapYahooDataSource(config=_config())
        start = datetime(2026, 6, 1)
        end = datetime(2026, 7, 4)
        payload = pd.DataFrame(
            {
                "Open": [1.0],
                "High": [1.1],
                "Low": [0.9],
                "Close": [1.0],
                "Volume": [100.0],
            },
            index=pd.date_range("2026-06-01 10:00", periods=1, freq="h"),
        )

        with patch("scanners.vp_avwap.market_data.yahoo_download", return_value=payload) as mocked_download:
            frame = source.intraday_history("TSLA", interval="30m", start=start, end=end)

        self.assertFalse(frame.empty)
        mocked_download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
