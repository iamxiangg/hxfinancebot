from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import unittest

import pandas as pd

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.engine import run_vp_avwap_scan


class StubDataSource:
    def __init__(self) -> None:
        self.daily_calls: dict[str, int] = {}
        self.intraday_calls: dict[tuple[str, str], int] = {}

    def latest_completed_daily(self, ticker: str, *, now_utc: datetime | None = None) -> pd.DataFrame:  # noqa: ARG002
        self.daily_calls[ticker] = self.daily_calls.get(ticker, 0) + 1
        if ticker == "FAIL":
            raise RuntimeError("boom")
        index = pd.bdate_range("2026-06-01", periods=8)
        return pd.DataFrame(
            {
                "Open": [100, 101, 102, 103, 104, 105, 106, 107],
                "High": [101, 102, 103, 104, 105, 107, 108, 109],
                "Low": [99, 100, 101, 102, 103, 104, 105, 106],
                "Close": [100, 101, 102, 103, 104, 106, 107, 108],
                "Volume": [1000] * 8,
            },
            index=index,
        )

    def earnings_dates(self, ticker: str) -> pd.DataFrame:
        return pd.DataFrame(
            {"surprise": [1.0, 1.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-06-05 08:00"), pd.Timestamp("2026-05-01 16:05")]),
        )

    def intraday_history(self, ticker: str, *, interval: str, start: datetime, end: datetime) -> pd.DataFrame:  # noqa: ARG002
        key = (ticker, interval)
        self.intraday_calls[key] = self.intraday_calls.get(key, 0) + 1
        if interval == "30m":
            index = pd.date_range("2026-06-05 10:00", periods=6, freq="4h")
        else:
            index = pd.date_range("2026-06-05 10:00", periods=6, freq="D")
        return pd.DataFrame(
            {
                "Open": [100, 101, 102, 103, 104, 105],
                "High": [101, 102, 103, 104, 105, 106],
                "Low": [99, 100, 101, 102, 103, 104],
                "Close": [100, 101, 102, 103, 104, 105],
                "Volume": [100] * 6,
            },
            index=index,
        )

    def intraday_skip_reason(self, ticker: str, *, interval: str, start: datetime, end: datetime) -> str | None:  # noqa: ARG002
        return None


class EmptyIntradayDataSource(StubDataSource):
    def intraday_history(self, ticker: str, *, interval: str, start: datetime, end: datetime) -> pd.DataFrame:  # noqa: ARG002
        key = (ticker, interval)
        self.intraday_calls[key] = self.intraday_calls.get(key, 0) + 1
        return pd.DataFrame()


def _config() -> VpAvwapConfig:
    return VpAvwapConfig(
        test_tickers=[],
        max_tickers=None,
        dry_run=True,
        write_sheets=False,
        send_telegram=False,
        telegram_test_mode=False,
        calibration=False,
        rows=20,
        value_area_pct=70.0,
        primary_interval="30m",
        secondary_interval="60m",
        confluence_pct=1.5,
        zone_buffer_pct=0.5,
        approach_pct=2.0,
        invalidation_buffer_pct=0.5,
        extension_pct=8.0,
        avwap_slope_lookback=3,
        avwap_flat_threshold_pct=0.25,
        falling_override_pct=-0.5,
        breakout_buffer_pct=0.5,
        breakout_retest_window=10,
        output_dir=Path("funnel_output/vp_avwap"),
    )


class EngineTests(unittest.TestCase):
    def test_empty_intraday_frames_fall_back_to_daily_bars(self) -> None:
        source = EmptyIntradayDataSource()
        result = run_vp_avwap_scan(
            [{"ticker": "AAA"}],
            config=_config(),
            data_source=source,
            observed_at="2026-07-03T23:30:00Z",
        )

        item = result.results[0]
        self.assertEqual(item.status, "OK")
        self.assertEqual(item.data_interval_used, "daily")
        self.assertFalse(item.error)
        self.assertEqual(source.intraday_calls, {("AAA", "30m"): 1, ("AAA", "60m"): 1})

    def test_duplicate_tickers_are_processed_once_and_sorted(self) -> None:
        source = StubDataSource()
        result = run_vp_avwap_scan(
            [
                {"ticker": "AAA", "stock_name": "Alpha"},
                {"ticker": "AAA", "stock_name": "Alpha Duplicate"},
                {"ticker": "BBB", "stock_name": "Beta"},
            ],
            config=_config(),
            data_source=source,
            observed_at="2026-07-03T23:30:00Z",
        )

        self.assertEqual(result.tickers_requested, 2)
        self.assertEqual(len(result.results), 2)
        self.assertEqual(source.daily_calls["AAA"], 1)
        self.assertEqual([item.overall_technical_rank for item in result.results], [1, 2])

    def test_failed_ticker_does_not_stop_other_results(self) -> None:
        source = StubDataSource()
        result = run_vp_avwap_scan(
            [{"ticker": "AAA"}, {"ticker": "FAIL"}],
            config=_config(),
            data_source=source,
            observed_at="2026-07-03T23:30:00Z",
        )

        self.assertEqual(len(result.results), 2)
        self.assertTrue(any(item.ticker == "AAA" and item.status in {"OK", "DATA_UNAVAILABLE"} for item in result.results))
        self.assertTrue(any(item.ticker == "FAIL" and item.final_tier == 4 for item in result.results))

    def test_output_contains_no_nan_or_infinity(self) -> None:
        source = StubDataSource()
        result = run_vp_avwap_scan(
            [{"ticker": "AAA"}],
            config=_config(),
            data_source=source,
            observed_at=datetime(2026, 7, 3, 23, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        )

        item = result.results[0]
        for value in (item.current_price, item.technical_score, item.profile_high, item.profile_low):
            if isinstance(value, float):
                self.assertTrue(value == value)


if __name__ == "__main__":
    unittest.main()
