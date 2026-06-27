from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scanners.earnings.engine import EarningsScannerConfig, run_earnings_scan
from scanners.earnings.models import HistoricalEventMove


class _FakeDataSource:
    def __init__(self) -> None:
        index = pd.to_datetime(
            [
                "2026-08-05",
                "2026-08-06",
                "2026-08-07",
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
                "2026-08-13",
                "2026-08-14",
                "2026-08-17",
                "2026-08-18",
                "2026-08-19",
                "2026-08-20",
                "2026-08-21",
            ]
        )
        self.history_frame = pd.DataFrame(
            {
                "Open": [100, 101, 103, 104, 103, 102, 104, 105, 107, 109, 108, 110, 111],
                "High": [102, 103, 105, 106, 104, 104, 106, 107, 110, 111, 112, 113, 114],
                "Low": [99, 100, 102, 103, 101, 101, 103, 104, 106, 108, 107, 109, 110],
                "Close": [101, 102, 104, 103, 102, 103, 105, 106, 109, 108, 110, 111, 112],
                "Volume": [3_000_000] * 13,
            },
            index=index,
        )
        historical_index = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-08-05 16:05"),
                pd.Timestamp("2026-08-06 16:05"),
                pd.Timestamp("2026-08-07 16:05"),
                pd.Timestamp("2026-08-10 16:05"),
                pd.Timestamp("2026-08-11 16:05"),
                pd.Timestamp("2026-08-12 16:05"),
                pd.Timestamp("2026-08-13 16:05"),
                pd.Timestamp("2026-08-14 16:05"),
                pd.Timestamp("2026-08-19 16:05"),
            ]
        )
        self.earnings_frame = pd.DataFrame(index=historical_index)

    def load_universe(self, *, configured_tickers, max_tickers, universe_url=None, cache_path=None):
        return type("Universe", (), {"tickers": ["NVDA", "ERR"], "source": "configured"})()

    def history(self, ticker: str, *, period: str = "2y"):
        if ticker == "ERR":
            raise RuntimeError("boom")
        return self.history_frame.copy()

    def earnings_dates(self, ticker: str, *, limit: int = 40):
        return self.earnings_frame.copy()

    def calendar(self, ticker: str):
        return None

    def info(self, ticker: str):
        return {"sector": "Technology", "industry": "Software"}

    def option_expirations(self, ticker: str):
        return [date(2026, 8, 21)]

    def option_chain(self, ticker: str, expiry: date):
        calls = pd.DataFrame(
            [
                {"strike": 100, "bid": 7.8, "ask": 8.2, "volume": 20, "openInterest": 200},
                {"strike": 118, "bid": 1.3, "ask": 1.5, "volume": 15, "openInterest": 180},
            ]
        )
        puts = pd.DataFrame(
            [
                {"strike": 100, "bid": 7.6, "ask": 8.0, "volume": 24, "openInterest": 220},
                {"strike": 84, "bid": 1.15, "ask": 1.25, "volume": 12, "openInterest": 140},
            ]
        )
        return calls, puts


class EarningsEngineTests(unittest.TestCase):
    def test_scan_produces_actionable_and_isolates_ticker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
            )
            config.overrides_path.write_text('{"NVDA|2026-08-19":"AMC"}', encoding="utf-8")
            historical_moves = [
                HistoricalEventMove(date(2026, 8, 5), "AMC", 100.0, 105.0, 0.05, 0.06, 0.07),
                HistoricalEventMove(date(2026, 8, 6), "AMC", 100.0, 104.0, 0.04, 0.05, 0.06),
                HistoricalEventMove(date(2026, 8, 7), "AMC", 100.0, 106.0, 0.06, 0.07, 0.08),
                HistoricalEventMove(date(2026, 8, 10), "AMC", 100.0, 103.5, 0.035, 0.04, 0.05),
                HistoricalEventMove(date(2026, 8, 11), "AMC", 100.0, 104.5, 0.045, 0.05, 0.06),
                HistoricalEventMove(date(2026, 8, 12), "AMC", 100.0, 105.5, 0.055, 0.06, 0.07),
                HistoricalEventMove(date(2026, 8, 13), "AMC", 100.0, 103.0, 0.03, 0.035, 0.045),
                HistoricalEventMove(date(2026, 8, 14), "AMC", 100.0, 104.2, 0.042, 0.05, 0.06),
            ]
            with patch("scanners.earnings.engine.build_historical_event_moves", return_value=historical_moves):
                result = run_earnings_scan(
                    now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
                    config=config,
                    data_source=_FakeDataSource(),
                )

        self.assertEqual(result.counts["universe_size"], 2)
        self.assertEqual(result.counts["errors"], 1)
        self.assertTrue(result.opportunities)
        self.assertIn(result.opportunities[0].classification, {"ACTIONABLE", "STRONG_ACTIONABLE"})

    def test_engine_does_not_import_long_term_funnel_or_openai(self) -> None:
        source = Path("scanners/earnings/engine.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("from funnel", source)
        self.assertNotIn("openai", source)


if __name__ == "__main__":
    unittest.main()
