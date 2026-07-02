from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scanners.earnings.engine import (
    EarningsScannerConfig,
    _empty_counts,
    run_earnings_scan,
)
from scanners.earnings.market_data import (
    DEFAULT_DELISTED_TICKERS_PATH,
    YahooEarningsDataSource,
)
from scanners.earnings.models import HistoricalEventMove

TEST_YAHOO_ENV = {
    "EARNINGS_SKIP_WARMUP": "1",
    "YAHOO_MIN_INTERVAL_SECONDS": "0",
    "YAHOO_RETRY_LIMIT": "0",
}


def _build_history_frame() -> pd.DataFrame:
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
    return pd.DataFrame(
        {
            "Open": [100, 101, 103, 104, 103, 102, 104, 105, 107, 109, 108, 110, 111],
            "High": [102, 103, 105, 106, 104, 104, 106, 107, 110, 111, 112, 113, 114],
            "Low": [99, 100, 102, 103, 101, 101, 103, 104, 106, 108, 107, 109, 110],
            "Close": [101, 102, 104, 103, 102, 103, 105, 106, 109, 108, 110, 111, 112],
            "Volume": [3_000_000] * 13,
        },
        index=index,
    )


def _build_earnings_frame() -> pd.DataFrame:
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
    return pd.DataFrame(index=historical_index)


def _build_historical_moves() -> list[HistoricalEventMove]:
    return [
        HistoricalEventMove(date(2026, 8, 5), "AMC", 100.0, 105.0, 0.05, 0.06, 0.07),
        HistoricalEventMove(date(2026, 8, 6), "AMC", 100.0, 104.0, 0.04, 0.05, 0.06),
        HistoricalEventMove(date(2026, 8, 7), "AMC", 100.0, 106.0, 0.06, 0.07, 0.08),
        HistoricalEventMove(date(2026, 8, 10), "AMC", 100.0, 103.5, 0.035, 0.04, 0.05),
        HistoricalEventMove(date(2026, 8, 11), "AMC", 100.0, 104.5, 0.045, 0.05, 0.06),
        HistoricalEventMove(date(2026, 8, 12), "AMC", 100.0, 105.5, 0.055, 0.06, 0.07),
        HistoricalEventMove(date(2026, 8, 13), "AMC", 100.0, 103.0, 0.03, 0.035, 0.045),
        HistoricalEventMove(date(2026, 8, 14), "AMC", 100.0, 104.2, 0.042, 0.05, 0.06),
    ]


def _make_option_chain() -> tuple[pd.DataFrame, pd.DataFrame]:
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


class _FakeDataSource:
    def __init__(
        self,
        tickers: list[str] | None = None,
        *,
        empty_history: set[str] | None = None,
    ) -> None:
        self.history_frame = _build_history_frame()
        self.earnings_frame = _build_earnings_frame()
        self._tickers = list(tickers) if tickers is not None else ["NVDA", "ERR"]
        # Default ``empty_history={"ERR"}`` matches the historical fake
        # contract where ERR simulates a ticker yfinance returns no
        # data for (e.g. delisted). Tests can override explicitly.
        self._empty_history = set(empty_history) if empty_history is not None else {"ERR"}

    def load_universe(self, *, configured_tickers, max_tickers, universe_url=None, cache_path=None, delisted_tickers_path=None):
        return type("Universe", (), {"tickers": list(self._tickers), "source": "configured"})()

    def history(self, ticker: str, *, period: str = "2y"):
        if ticker in self._empty_history:
            return pd.DataFrame()
        return self.history_frame.copy()

    def batch_history(self, tickers, *, period: str = "2y"):
        return {
            ticker: self.history(ticker, period=period)
            for ticker in tickers
            if ticker not in self._empty_history
        }

    def earnings_dates(self, ticker: str, *, limit: int = 40):
        return self.earnings_frame.copy()

    def calendar(self, ticker: str):
        return None

    def info(self, ticker: str):
        return {"sector": "Technology", "industry": "Software"}

    def option_expirations(self, ticker: str):
        return [date(2026, 8, 21)]

    def option_chain(self, ticker: str, expiry: date):
        return _make_option_chain()


class EarningsEngineTests(unittest.TestCase):
    def test_scan_produces_actionable_and_isolates_ticker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
                max_workers=1,  # deterministic for the existing assertion
            )
            config.overrides_path.write_text('{"NVDA|2026-08-19":"AMC"}', encoding="utf-8")
            with patch("scanners.earnings.engine.build_historical_event_moves", return_value=_build_historical_moves()):
                result = run_earnings_scan(
                    now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
                    config=config,
                    data_source=_FakeDataSource(),
                )

        self.assertEqual(result.counts["universe_size"], 2)
        self.assertEqual(result.counts["errors"], 1)
        self.assertTrue(result.opportunities)
        self.assertIn(result.opportunities[0].classification, {"ACTIONABLE", "STRONG_ACTIONABLE"})
        # Each opportunity should carry the universe_source the engine resolved.
        for opportunity in result.opportunities:
            self.assertEqual(opportunity.details.get("universe_source"), "configured")

    def test_engine_does_not_import_long_term_funnel_or_openai(self) -> None:
        source = Path("scanners/earnings/engine.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("from funnel", source)
        self.assertNotIn("openai", source)


class BatchHistoryTests(unittest.TestCase):
    def test_batch_history_returns_empty_dict_for_empty_input(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(warmup_session=False)
        self.assertEqual(source.batch_history([]), {})

    def test_batch_history_single_ticker_returns_flat_dataframe(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(warmup_session=False)
        history = _build_history_frame()

        with patch("scanners.earnings.market_data.yf.download", return_value=history) as mock_download:
            result = source.batch_history(["AAPL"], period="2y")

        mock_download.assert_called_once()
        kwargs = mock_download.call_args.kwargs
        self.assertEqual(kwargs["tickers"], ["AAPL"])
        self.assertTrue(kwargs["group_by"] == "ticker")
        # threads=False: the outer ThreadPoolExecutor owns concurrency;
        # stacking yfinance's internal thread pool on top would risk
        # Yahoo's ~100 req/min rate limit.
        self.assertFalse(kwargs["threads"])
        self.assertEqual(list(result.keys()), ["AAPL"])
        self.assertFalse(result["AAPL"].empty)
        self.assertIn("Close", result["AAPL"].columns)

    def test_batch_history_retries_on_empty_then_succeeds(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,  # disable to keep the test fast
                request_delay_seconds=0,  # isolate the backoff sleeps
            )
        history = _build_history_frame()
        empty = pd.DataFrame()

        with patch("scanners.earnings.market_data.yf.download", side_effect=[empty, empty, history]) as mock_download, \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            result = source.batch_history(["AAPL"], period="2y", max_retries=2)

        self.assertEqual(mock_download.call_count, 3)
        self.assertEqual(list(result.keys()), ["AAPL"])
        self.assertFalse(result["AAPL"].empty)
        # Two backoff sleeps between three attempts.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_batch_history_gives_up_after_max_retries(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,  # isolate the backoff sleeps
            )

        with patch("scanners.earnings.market_data.yf.download", return_value=pd.DataFrame()) as mock_download, \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            result = source.batch_history(["AAPL"], period="2y", max_retries=2)

        self.assertEqual(result, {})
        # 3 attempts total (initial + 2 retries).
        self.assertEqual(mock_download.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_batch_history_multi_ticker_extracts_per_ticker_columns(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(warmup_session=False)
        history_a = _build_history_frame()
        history_b = _build_history_frame().assign(Close=lambda df: df["Close"] + 50)
        raw = pd.concat({"AAPL": history_a, "MSFT": history_b}, axis=1)
        raw.columns = pd.MultiIndex.from_tuples(raw.columns)

        with patch("scanners.earnings.market_data.yf.download", return_value=raw):
            result = source.batch_history(["AAPL", "MSFT"], period="2y")

        self.assertEqual(set(result.keys()), {"AAPL", "MSFT"})
        self.assertFalse(result["AAPL"].empty)
        self.assertFalse(result["MSFT"].empty)
        self.assertEqual(result["MSFT"]["Close"].iloc[-1], history_b["Close"].iloc[-1])

    def test_batch_history_drops_tickers_with_no_rows(self) -> None:
        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(warmup_session=False)
        history_a = _build_history_frame()
        raw = pd.concat({"AAPL": history_a, "DELISTED": history_a.iloc[0:0]}, axis=1)
        raw.columns = pd.MultiIndex.from_tuples(raw.columns)

        with patch("scanners.earnings.market_data.yf.download", return_value=raw):
            result = source.batch_history(["AAPL", "DELISTED"], period="2y")

        self.assertEqual(list(result.keys()), ["AAPL"])


class DelistedFilterTests(unittest.TestCase):
    def test_load_universe_filters_delisted_from_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            denylist_path = Path(temp_dir) / "denylist.json"
            denylist_path.write_text(json.dumps(["ALOY", "BCAT", "BSTZ", "BTX", "CEPT"]), encoding="utf-8")
            with patch.dict("os.environ", TEST_YAHOO_ENV):
                source = YahooEarningsDataSource(warmup_session=False)
            result = source.load_universe(
                configured_tickers=["AAPL", "aloy", "MSFT", "BCAT", "NVDA"],
                max_tickers=10,
                delisted_tickers_path=denylist_path,
            )
        self.assertEqual(result.source, "configured")
        self.assertNotIn("ALOY", result.tickers)
        self.assertNotIn("BCAT", result.tickers)
        self.assertEqual(result.tickers, ["AAPL", "MSFT", "NVDA"])

    def test_load_universe_with_no_denylist_returns_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", TEST_YAHOO_ENV):
                source = YahooEarningsDataSource(warmup_session=False)
            result = source.load_universe(
                configured_tickers=["AAPL", "MSFT"],
                max_tickers=10,
                delisted_tickers_path=Path(temp_dir) / "missing.json",
            )
        self.assertEqual(result.tickers, ["AAPL", "MSFT"])

    def test_load_universe_empty_when_all_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            denylist_path = Path(temp_dir) / "denylist.json"
            denylist_path.write_text(json.dumps(["AAPL", "MSFT"]), encoding="utf-8")
            with patch.dict("os.environ", TEST_YAHOO_ENV):
                source = YahooEarningsDataSource(warmup_session=False)
            result = source.load_universe(
                configured_tickers=["AAPL", "MSFT"],
                max_tickers=10,
                delisted_tickers_path=denylist_path,
            )
        self.assertEqual(result.tickers, [])

    def test_load_universe_handles_malformed_denylist(self) -> None:
        # Corrupt JSON and non-list payloads should silently degrade to
        # an empty denylist rather than crash the scan.
        with tempfile.TemporaryDirectory() as temp_dir:
            for raw_payload, label in (
                ("not json at all", "corrupt"),
                ('{"AAPL": true}', "non-list"),
            ):
                denylist_path = Path(temp_dir) / f"denylist_{label}.json"
                denylist_path.write_text(raw_payload, encoding="utf-8")
                with patch.dict("os.environ", TEST_YAHOO_ENV):
                    source = YahooEarningsDataSource(warmup_session=False)
                with self.subTest(payload=raw_payload):
                    result = source.load_universe(
                        configured_tickers=["AAPL", "MSFT"],
                        max_tickers=10,
                        delisted_tickers_path=denylist_path,
                    )
                    self.assertEqual(result.tickers, ["AAPL", "MSFT"])

    def test_load_universe_skips_non_string_denylist_entries(self) -> None:
        # Non-string entries (numbers, null) are ignored so a
        # partially-malformed denylist doesn't accidentally filter
        # real tickers. An explicit empty list is the "denylist
        # disabled" sentinel — must NOT filter everything.
        with tempfile.TemporaryDirectory() as temp_dir:
            for raw_payload, label in (
                ("[1, 2, null]", "non-string"),
                ("[]", "empty-list"),
            ):
                denylist_path = Path(temp_dir) / f"denylist_{label}.json"
                denylist_path.write_text(raw_payload, encoding="utf-8")
                with patch.dict("os.environ", TEST_YAHOO_ENV):
                    source = YahooEarningsDataSource(warmup_session=False)
                with self.subTest(payload=raw_payload):
                    result = source.load_universe(
                        configured_tickers=["AAPL", "MSFT"],
                        max_tickers=10,
                        delisted_tickers_path=denylist_path,
                    )
                    self.assertEqual(result.tickers, ["AAPL", "MSFT"])


class EngineIntegrationTests(unittest.TestCase):
    def _run(self, *, tickers, config, data_source, now_ny):
        return run_earnings_scan(
            now_ny=now_ny,
            config=config,
            data_source=data_source,
        )

    def test_engine_emits_startup_and_completion_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
                max_workers=1,
            )
            config.overrides_path.write_text(
                '{"NVDA|2026-08-19":"AMC"}',
                encoding="utf-8",
            )
            with patch("scanners.earnings.engine.build_historical_event_moves", return_value=_build_historical_moves()):
                with self.assertLogs("scanners.earnings.engine", level="INFO") as captured:
                    self._run(
                        tickers=["NVDA"],
                        config=config,
                        data_source=_FakeDataSource(tickers=["NVDA"]),
                        now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
                    )
        joined = "\n".join(captured.output)
        self.assertIn("Starting earnings scan over 1 tickers", joined)
        self.assertIn("source=configured", joined)
        self.assertIn("Batch history returned data for 1 / 1 tickers", joined)
        self.assertIn("Earnings scan complete", joined)

    def test_engine_returns_empty_for_empty_universe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
            )
            result = self._run(
                tickers=[],
                config=config,
                data_source=_FakeDataSource(tickers=[]),
                now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
            )
        self.assertEqual(result.opportunities, [])
        self.assertEqual(result.counts, _empty_counts())
        self.assertEqual(result.errors, [])

    def test_engine_aggregates_counts_across_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
                max_workers=4,  # force concurrent execution
            )
            overrides = {f"TICK{i}|2026-08-19": "AMC" for i in range(3)}
            config.overrides_path.write_text(json.dumps(overrides), encoding="utf-8")
            with patch("scanners.earnings.engine.build_historical_event_moves", return_value=_build_historical_moves()):
                result = self._run(
                    tickers=["TICK0", "TICK1", "TICK2"],
                    config=config,
                    data_source=_FakeDataSource(tickers=["TICK0", "TICK1", "TICK2"]),
                    now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
                )
        self.assertEqual(result.counts["universe_size"], 3)
        self.assertEqual(result.counts["errors"], 0)
        # All three should be counted as earnings_candidates + timing_confirmed.
        self.assertEqual(result.counts["earnings_candidates"], 3)
        self.assertEqual(result.counts["timing_confirmed"], 3)
        # Each ticker with a valid option chain should have a chain retrieved.
        self.assertEqual(result.counts["option_chains_retrieved"], 3)

    def test_engine_handles_worker_crashes_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = EarningsScannerConfig(
                overrides_path=Path(temp_dir) / "overrides.json",
                max_candidates=10,
                max_workers=2,
            )
            config.overrides_path.write_text("{}", encoding="utf-8")
            with patch("scanners.earnings.engine.build_historical_event_moves", return_value=_build_historical_moves()):
                result = self._run(
                    tickers=["NVDA", "EMPTY"],
                    config=config,
                    data_source=_FakeDataSource(tickers=["NVDA", "EMPTY"], empty_history={"EMPTY"}),
                    now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
                )
        self.assertEqual(result.counts["universe_size"], 2)
        self.assertEqual(result.counts["errors"], 1)
        self.assertTrue(result.opportunities, "Expected the non-empty ticker to still produce opportunities.")

    def test_engine_uses_default_universe_when_configured_tickers_unset(self) -> None:
        # Smoke test that the default denylist path is wired up (the file
        # exists from the seed commit); no delisted tickers should be
        # loaded from the configured_tickers path.
        self.assertTrue(DEFAULT_DELISTED_TICKERS_PATH.exists())

    def test_default_config_has_conservative_concurrency(self) -> None:
        # max_workers=1 + request_delay=0.5 + rate_limit_per_minute=50
        # is the yfinance-safe default. Bumping any of these raises the
        # risk of hitting Yahoo's ~100 req/min rate limit on a 500-ticker
        # scan; an operator changing them should know why.
        config = EarningsScannerConfig()
        self.assertEqual(config.max_workers, 1)
        self.assertEqual(config.request_delay_seconds, 0.5)
        self.assertEqual(config.rate_limit_per_minute, 50)

    def test_from_env_reads_rate_limit_per_minute_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"EARNINGS_RATE_LIMIT_PER_MINUTE": "120", "EARNINGS_MAX_WORKERS": "2"},
        ):
            config = EarningsScannerConfig.from_env()
        self.assertEqual(config.rate_limit_per_minute, 120)
        self.assertEqual(config.max_workers, 2)


class RateLimiterTests(unittest.TestCase):
    def test_rate_limiter_enforces_minimum_spacing(self) -> None:
        # max_per_minute=300 → interval=0.2s. Two back-to-back waits
        # must be spaced at least 0.2s apart. Using a longer interval
        # avoids Windows timer resolution eating the slack.
        import time
        from scanners.earnings.market_data import RateLimiter

        limiter = RateLimiter(max_per_minute=300)
        start = time.monotonic()
        limiter.wait()
        limiter.wait()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.18)  # 0.2s with small slack for CI

    def test_rate_limiter_disabled_when_max_per_minute_is_zero(self) -> None:
        import time
        from scanners.earnings.market_data import RateLimiter

        limiter = RateLimiter(max_per_minute=0)
        start = time.monotonic()
        for _ in range(10):
            limiter.wait()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05)

    def test_rate_limiter_is_thread_safe(self) -> None:
        import threading
        import time
        from scanners.earnings.market_data import RateLimiter

        # max_per_minute=600 → interval=0.1s. Three concurrent waits
        # should each take ~0s, ~0.1s, ~0.2s; total >= 0.2s and
        # no individual wait exceeds 0.3s.
        limiter = RateLimiter(max_per_minute=600)
        results: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            start = time.monotonic()
            limiter.wait()
            elapsed = time.monotonic() - start
            with lock:
                results.append(elapsed)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 3)
        self.assertGreaterEqual(sum(results), 0.15)
        self.assertLess(max(results), 0.4)


class HistoryRetryTests(unittest.TestCase):
    def test_history_retries_on_empty_then_succeeds(self) -> None:
        from unittest.mock import MagicMock
        from scanners.earnings.market_data import YahooEarningsDataSource

        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,  # isolate the backoff sleeps
            )

        history = _build_history_frame()
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [pd.DataFrame(), pd.DataFrame(), history]

        with patch("scanners.earnings.market_data.yf.Ticker", return_value=mock_ticker), \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            result = source.history("AAPL", period="2y", max_retries=3)

        self.assertFalse(result.empty)
        self.assertEqual(mock_ticker.history.call_count, 3)
        # 2 backoff sleeps between 3 attempts.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_history_retries_on_rate_limit_exception(self) -> None:
        from unittest.mock import MagicMock
        from scanners.earnings.market_data import YahooEarningsDataSource

        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,  # isolate the backoff sleeps
            )

        history = _build_history_frame()
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [
            Exception("429 Too Many Requests"),
            Exception("503 Service Unavailable"),
            history,
        ]

        with patch("scanners.earnings.market_data.yf.Ticker", return_value=mock_ticker), \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            result = source.history("AAPL", period="2y", max_retries=3)

        self.assertFalse(result.empty)
        self.assertEqual(mock_ticker.history.call_count, 3)
        # 2 backoff sleeps between 3 attempts.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_history_gives_up_after_max_retries(self) -> None:
        from unittest.mock import MagicMock
        from scanners.earnings.market_data import YahooEarningsDataSource

        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,  # isolate the backoff sleeps
            )

        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        ]

        with patch("scanners.earnings.market_data.yf.Ticker", return_value=mock_ticker), \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            result = source.history("AAPL", period="2y", max_retries=3)

        self.assertTrue(result.empty)
        self.assertEqual(mock_ticker.history.call_count, 3)
        # 2 backoff sleeps between 3 attempts.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_history_does_not_retry_non_rate_limit_exceptions(self) -> None:
        from unittest.mock import MagicMock
        from scanners.earnings.market_data import YahooEarningsDataSource

        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,  # isolate the backoff sleeps
            )

        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = RuntimeError("boom")

        with patch("scanners.earnings.market_data.yf.Ticker", return_value=mock_ticker), \
             patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError):
                source.history("AAPL", period="2y", max_retries=3)

        self.assertEqual(mock_ticker.history.call_count, 1)
        self.assertEqual(mock_sleep.call_count, 0)

    def test_history_does_not_retry_unrelated_rate_substring(self) -> None:
        # Regression guard for the round-2 ``_RATE_LIMIT_SIGNALS`` fix:
        # a bare ``"rate"`` substring (e.g. ``KeyError("interest_rate")``)
        # must NOT trigger a retry loop. The tighter signal set
        # ``("429", "503", "too many", "rate limit")`` only matches
        # actual rate-limit-style errors from yfinance/Yahoo.
        from unittest.mock import MagicMock
        from scanners.earnings.market_data import YahooEarningsDataSource

        with patch.dict("os.environ", TEST_YAHOO_ENV):
            source = YahooEarningsDataSource(
                warmup_session=False,
                rate_limit_per_minute=0,
                request_delay_seconds=0,
            )

        for exc in (KeyError("interest_rate"), ValueError("rate of return missing")):
            mock_ticker = MagicMock()
            mock_ticker.history.side_effect = exc

            with patch("scanners.earnings.market_data.yf.Ticker", return_value=mock_ticker), \
                 patch("scanners.earnings.market_data.time.sleep") as mock_sleep:
                with self.subTest(exception=repr(exc)):
                    with self.assertRaises(type(exc)):
                        source.history("AAPL", period="2y", max_retries=3)
                    self.assertEqual(mock_ticker.history.call_count, 1)
                    self.assertEqual(mock_sleep.call_count, 0)


if __name__ == "__main__":
    unittest.main()
