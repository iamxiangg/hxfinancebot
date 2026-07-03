from __future__ import annotations

from datetime import date, datetime
import math
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from scanners.vpma.engine import (
    EarningsEvent,
    UniverseTicker,
    VpmaConfig,
    YfinanceVpmaDataSource,
    _extract_finite_scalar,
    _yahoo_valid_symbol,
    calculate_event_quality,
    classify_core_result,
    clean_universe_rows,
    earnings_anchored_vwap,
    evaluate_ticker,
    extract_recent_earnings_event,
    map_reaction_session,
    reaction_abnormal_return,
    reaction_closing_position,
    reaction_volume_shock,
    run_vpma_scan,
)


def _make_history() -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    dates = pd.bdate_range("2026-01-02", periods=90)
    reaction_index = 55
    closes: list[float] = []
    price = 100.0
    for idx in range(len(dates)):
        if idx < reaction_index:
            price += 0.05
        elif idx == reaction_index:
            price = 115.0
        elif idx < len(dates) - 10:
            price += 0.20
        else:
            price += 0.02 if idx % 2 == 0 else -0.01
        closes.append(round(price, 2))

    history = pd.DataFrame(index=dates)
    history["Close"] = closes
    history["Open"] = history["Close"] * 0.995
    history["High"] = history["Close"] * 1.01
    history["Low"] = history["Close"] * 0.99
    history["Volume"] = 1_000_000
    history.iloc[reaction_index, history.columns.get_loc("Volume")] = 5_000_000
    history.iloc[-10:, history.columns.get_loc("Volume")] = 700_000

    benchmark = pd.DataFrame(index=dates)
    benchmark["Close"] = [100 + idx * 0.08 for idx in range(len(dates))]
    benchmark["Open"] = benchmark["Close"] * 0.998
    benchmark["High"] = benchmark["Close"] * 1.004
    benchmark["Low"] = benchmark["Close"] * 0.996
    benchmark["Volume"] = 10_000_000

    return history, benchmark, dates[reaction_index]


class VpmaEngineTests(unittest.TestCase):
    def test_universe_cleaning_normalises_and_filters(self) -> None:
        rows = [
            {"symbol": " brk/b ", "name": "Berkshire Hathaway Inc", "price": "400", "marketCap": "900000000000", "volume": "500000"},
            {"symbol": "abcw", "name": "ABC Warrant", "price": "10", "marketCap": "900000000", "volume": "500000"},
            {"symbol": "xyz", "name": "XYZ Holdings Depositary Shares ADR", "price": "20", "marketCap": "800000000", "volume": "500000"},
            {"symbol": "brk/b", "name": "Duplicate", "price": "410", "marketCap": "900000000000", "volume": "700000"},
        ]

        cleaned = clean_universe_rows(rows, min_price=3.0, min_market_cap=300_000_000.0, min_source_volume=200_000.0)

        self.assertEqual([ticker.ticker for ticker in cleaned], ["BRK-B", "XYZ"])

    def test_reaction_session_mapping_before_after_and_uncertain(self) -> None:
        trading_index = pd.bdate_range("2026-06-01", periods=5)
        before_market = datetime(2026, 6, 3, 8, 0, 0)
        after_market = datetime(2026, 6, 3, 16, 30, 0)
        uncertain = datetime(2026, 6, 3, 0, 0, 0)

        before_session, before_timing, before_conf = map_reaction_session(before_market, trading_index)
        after_session, after_timing, after_conf = map_reaction_session(after_market, trading_index)
        uncertain_session, uncertain_timing, uncertain_conf = map_reaction_session(uncertain, trading_index)

        self.assertEqual(before_session, pd.Timestamp("2026-06-03"))
        self.assertEqual(before_timing, "before_market")
        self.assertEqual(before_conf, "high")
        self.assertEqual(after_session, pd.Timestamp("2026-06-04"))
        self.assertEqual(after_timing, "after_market")
        self.assertEqual(after_conf, "high")
        self.assertEqual(uncertain_session, pd.Timestamp("2026-06-03"))
        self.assertEqual(uncertain_timing, "uncertain")
        self.assertEqual(uncertain_conf, "low")

    def test_extract_recent_event_uses_latest_eligible_row(self) -> None:
        trading_index = pd.bdate_range("2026-06-01", periods=20)
        frame = pd.DataFrame(
            {"Surprise(%)": [5.0, 12.0]},
            index=[pd.Timestamp("2026-06-05 08:00:00"), pd.Timestamp("2026-06-20 16:30:00")],
        )

        event = extract_recent_earnings_event(frame, trading_index, lookback_days=90, today=date(2026, 6, 25))

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.reaction_session, pd.Timestamp("2026-06-22"))
        self.assertEqual(event.release_timing, "after_market")
        self.assertEqual(event.eps_surprise_pct, 12.0)

    def test_event_quality_is_monotonic(self) -> None:
        weaker = calculate_event_quality(
            eps_surprise_pct=5.0,
            abnormal_return=0.01,
            closing_position=0.55,
            volume_shock=1.2,
        )
        stronger = calculate_event_quality(
            eps_surprise_pct=15.0,
            abnormal_return=0.08,
            closing_position=0.9,
            volume_shock=3.5,
        )

        self.assertGreater(stronger["event_score"], weaker["event_score"])

    def test_earnings_anchored_vwap_uses_daily_bar_approximation(self) -> None:
        history = pd.DataFrame(
            {
                "High": [11.0, 12.0, 13.0],
                "Low": [9.0, 10.0, 11.0],
                "Close": [10.0, 11.0, 12.0],
                "Volume": [100.0, 100.0, 200.0],
            },
            index=pd.bdate_range("2026-06-01", periods=3),
        )

        avwap = earnings_anchored_vwap(history, history.index[0])

        self.assertAlmostEqual(avwap or 0.0, 11.25)

    def test_classification_thresholds(self) -> None:
        config = VpmaConfig()
        self.assertEqual(
            classify_core_result(core_score=78, event_score=28, drift_score=24, entry_score=18, risk_flags=[], config=config),
            "actionable",
        )
        self.assertEqual(
            classify_core_result(core_score=70, event_score=24, drift_score=22, entry_score=10, risk_flags=[], config=config),
            "wait",
        )
        self.assertEqual(
            classify_core_result(core_score=60, event_score=18, drift_score=18, entry_score=10, risk_flags=[], config=config),
            "near_miss",
        )
        self.assertEqual(
            classify_core_result(
                core_score=50,
                event_score=18,
                drift_score=10,
                entry_score=5,
                risk_flags=["reaction_low_broken", "below_earnings_avwap"],
                config=config,
            ),
            "risk",
        )

    def test_missing_data_becomes_flagged_not_neutral(self) -> None:
        history, benchmark, reaction_session = _make_history()
        event = EarningsEvent(
            earnings_timestamp=datetime(2026, 3, 20, 0, 0, 0),
            release_timing="uncertain",
            reaction_session=reaction_session,
            reaction_session_confidence="low",
            days_since_reaction=20,
            eps_surprise_pct=None,
        )
        result = evaluate_ticker(
            UniverseTicker("TEAM", "Atlassian", "Software", 100.0, 10_000_000_000.0, 500_000.0),
            history,
            benchmark,
            event,
            next_earnings_date=date(2026, 6, 30),
            config=VpmaConfig(),
        )

        self.assertIn("missing_eps_surprise", result.details["risk_flags"])
        self.assertIn("event_date_uncertain", result.details["risk_flags"])
        self.assertGreaterEqual(result.event_score, 0.0)


class ExtractFiniteScalarTests(unittest.TestCase):
    def test_python_float(self) -> None:
        self.assertEqual(_extract_finite_scalar(42.0), 42.0)
        self.assertEqual(_extract_finite_scalar(0), 0.0)

    def test_numpy_scalar(self) -> None:
        self.assertEqual(_extract_finite_scalar(np.float64(3.14)), 3.14)
        self.assertEqual(_extract_finite_scalar(np.int64(7)), 7.0)

    def test_single_element_series(self) -> None:
        series = pd.Series([2.5])
        self.assertEqual(_extract_finite_scalar(series), 2.5)

    def test_single_element_dataframe(self) -> None:
        df = pd.DataFrame({"a": [3.0]})
        self.assertEqual(_extract_finite_scalar(df), 3.0)

    def test_none_and_nan_return_none(self) -> None:
        self.assertIsNone(_extract_finite_scalar(None))
        self.assertIsNone(_extract_finite_scalar(float("nan")))
        self.assertIsNone(_extract_finite_scalar(np.nan))

    def test_inf_returns_none(self) -> None:
        self.assertIsNone(_extract_finite_scalar(float("inf")))
        self.assertIsNone(_extract_finite_scalar(float("-inf")))

    def test_multi_element_series_rejected(self) -> None:
        series = pd.Series([1.0, 2.0])
        with self.assertRaises(ValueError):
            _extract_finite_scalar(series)

    def test_multi_element_dataframe_rejected(self) -> None:
        df = pd.DataFrame({"a": [1.0, 2.0]})
        with self.assertRaises(ValueError):
            _extract_finite_scalar(df)

    def test_empty_series_returns_none(self) -> None:
        self.assertIsNone(_extract_finite_scalar(pd.Series([], dtype=float)))

    def test_empty_dataframe_returns_none(self) -> None:
        self.assertIsNone(_extract_finite_scalar(pd.DataFrame()))

    def test_pd_na_returns_none(self) -> None:
        self.assertIsNone(_extract_finite_scalar(pd.NA))

    def test_string_numeric_parsed_as_float(self) -> None:
        self.assertEqual(_extract_finite_scalar("42.5"), 42.5)


class YahooSymbolValidationTests(unittest.TestCase):
    def test_normal_symbol_valid(self) -> None:
        self.assertTrue(_yahoo_valid_symbol("AAPL"))
        self.assertTrue(_yahoo_valid_symbol("BRK-B"))

    def test_caret_symbol_invalid(self) -> None:
        self.assertFalse(_yahoo_valid_symbol("NEE^U"))
        self.assertFalse(_yahoo_valid_symbol("SCE^L"))

    def test_empty_string_invalid(self) -> None:
        self.assertFalse(_yahoo_valid_symbol(""))


class ReactionCalculationTests(unittest.TestCase):
    def test_abnormal_return_with_normal_series(self) -> None:
        dates = pd.bdate_range("2026-06-01", periods=5)
        history = pd.DataFrame({"Close": [100.0, 101.0, 105.0, 103.0, 107.0]}, index=dates)
        benchmark = pd.DataFrame({"Close": [200.0, 200.5, 201.0, 201.5, 202.0]}, index=dates)
        abnormal = reaction_abnormal_return(history, benchmark, dates[2])
        self.assertIsNotNone(abnormal)
        self.assertTrue(math.isfinite(abnormal))

    def test_abnormal_return_with_duplicate_index(self) -> None:
        dates = pd.DatetimeIndex([
            pd.Timestamp("2026-06-01"),
            pd.Timestamp("2026-06-02"),
            pd.Timestamp("2026-06-03"),
            pd.Timestamp("2026-06-03"),
            pd.Timestamp("2026-06-04"),
        ])
        history = pd.DataFrame({"Close": [100.0, 101.0, 105.0, 106.0, 107.0]}, index=dates)
        benchmark = pd.DataFrame({"Close": [200.0, 200.5, 201.0, 201.5, 202.0]}, index=dates)
        abnormal = reaction_abnormal_return(history, benchmark, pd.Timestamp("2026-06-03"))
        self.assertIsNone(abnormal)

    def test_closing_position_with_duplicate_index(self) -> None:
        dates = pd.DatetimeIndex([
            pd.Timestamp("2026-06-01"),
            pd.Timestamp("2026-06-02"),
            pd.Timestamp("2026-06-03"),
            pd.Timestamp("2026-06-03"),
        ])
        history = pd.DataFrame({
            "Close": [100.0, 101.0, 105.0, 106.0],
            "High": [101.0, 102.0, 106.0, 107.0],
            "Low": [99.0, 100.0, 103.0, 104.0],
        }, index=dates)
        result = reaction_closing_position(history, pd.Timestamp("2026-06-03"))
        self.assertIsNone(result)

    def test_volume_shock_with_duplicate_index(self) -> None:
        dates = pd.DatetimeIndex([
            pd.Timestamp(f"2026-06-{d:02d}") for d in range(1, 25)
        ] + [pd.Timestamp("2026-06-24")])
        volume = [1_000_000.0] * 24 + [5_000_000.0]
        history = pd.DataFrame({"Volume": volume}, index=dates)
        result = reaction_volume_shock(history, pd.Timestamp("2026-06-24"))
        self.assertIsNone(result)

    def test_missing_benchmark_session_returns_none(self) -> None:
        dates = pd.bdate_range("2026-06-01", periods=5)
        history = pd.DataFrame({"Close": [100.0, 101.0, 105.0, 103.0, 107.0]}, index=dates)
        benchmark = pd.DataFrame({"Close": [200.0]}, index=pd.DatetimeIndex([pd.Timestamp("2026-05-01")]))
        abnormal = reaction_abnormal_return(history, benchmark, dates[2])
        self.assertIsNone(abnormal)


class VpmaScanIsolationTests(unittest.TestCase):
    """Verify that one ticker failure does not prevent other tickers from producing signals."""

    def setUp(self) -> None:
        dates = pd.bdate_range("2026-01-02", periods=90)
        self.history_aapl = pd.DataFrame({
            "Open": [150.0] * 90, "High": [152.0] * 90, "Low": [149.0] * 90,
            "Close": [151.0] * 90, "Volume": [1_000_000] * 90,
        }, index=dates)
        self.history_msft = pd.DataFrame({
            "Open": [300.0] * 90, "High": [303.0] * 90, "Low": [298.0] * 90,
            "Close": [301.0] * 90, "Volume": [2_000_000] * 90,
        }, index=dates)
        self.benchmark = pd.DataFrame({
            "Open": [500.0] * 90, "High": [502.0] * 90, "Low": [499.0] * 90,
            "Close": [501.0] * 90, "Volume": [10_000_000] * 90,
        }, index=dates)

    def test_invalid_symbol_isolated_other_tickers_proceed(self) -> None:
        config = VpmaConfig(enable_enrichment=False, guidance_enable=False, valid_days=3)
        with patch.dict("os.environ", {"VPMA_TEST_TICKERS": "AAPL,NEE^U"}, clear=True):
            with patch.object(YfinanceVpmaDataSource, "download_histories", return_value={
                "AAPL": self.history_aapl,
            }), patch.object(YfinanceVpmaDataSource, "benchmark_history", return_value=self.benchmark), patch.object(
                YfinanceVpmaDataSource, "earnings_dates", return_value=pd.DataFrame()
            ), patch.object(
                YfinanceVpmaDataSource, "next_earnings_date", return_value=None
            ):
                scan = run_vpma_scan(config=config)
                self.assertIn("NEE^U", [r.ticker for r in scan.results if r.classification == "excluded"])
                counts = scan.counts
                self.assertGreaterEqual(counts.get("invalid_symbol", 0), 1)

    def test_calculation_failure_on_one_ticker_does_not_block_others(self) -> None:
        """Verifies both tickers appear in results when only one has market data."""
        config = VpmaConfig(enable_enrichment=False, guidance_enable=False, valid_days=3)
        with patch.dict("os.environ", {"VPMA_TEST_TICKERS": "AAPL,MSFT"}, clear=True):
            with patch.object(
                YfinanceVpmaDataSource, "download_histories", return_value={
                    "AAPL": self.history_aapl,
                }
            ), patch.object(
                YfinanceVpmaDataSource, "benchmark_history", return_value=self.benchmark
            ), patch.object(
                YfinanceVpmaDataSource, "earnings_dates", return_value=pd.DataFrame()
            ), patch.object(
                YfinanceVpmaDataSource, "next_earnings_date", return_value=None
            ):
                scan = run_vpma_scan(config=config)
                tickers = {r.ticker for r in scan.results}
                self.assertIn("AAPL", tickers)
                self.assertIn("MSFT", tickers)
                self.assertGreaterEqual(scan.counts.get("missing_market_data", 0), 1)

    def test_evaluate_ticker_pipeline_completes_without_error(self) -> None:
        """Verify the full evaluation pipeline completes without raising."""
        dates = pd.bdate_range("2026-01-02", periods=90)
        mid = len(dates) // 2
        history = pd.DataFrame(index=dates)
        history["Open"] = [150.0 + i * 0.1 for i in range(len(dates))]
        history["High"] = history["Open"] * 1.01
        history["Low"] = history["Open"] * 0.99
        history["Close"] = history["Open"] * 1.005
        history["Volume"] = 1_000_000
        history.iloc[mid, history.columns.get_loc("Volume")] = 5_000_000

        benchmark = pd.DataFrame(index=dates)
        benchmark["Close"] = [500.0 + i * 0.05 for i in range(len(dates))]
        benchmark["Open"] = benchmark["Close"] * 0.998
        benchmark["High"] = benchmark["Close"] * 1.004
        benchmark["Low"] = benchmark["Close"] * 0.996
        benchmark["Volume"] = 10_000_000

        event = EarningsEvent(
            earnings_timestamp=datetime(2026, 3, 20, 8, 0, 0),
            release_timing="before_market",
            reaction_session=dates[mid],
            reaction_session_confidence="high",
            days_since_reaction=10,
            eps_surprise_pct=8.0,
        )

        ticker = UniverseTicker("TEST", "Test Corp", "Tech", 150.0, 10_000_000_000.0, 500_000.0)
        result = evaluate_ticker(
            ticker, history, benchmark, event,
            next_earnings_date=None,
            config=VpmaConfig(),
        )
        self.assertIsNotNone(result)
        self.assertIn(result.classification, {"actionable", "wait", "near_miss", "risk", "excluded"})


class YfinanceVpmaDataSourceErrorPathsTests(unittest.TestCase):
    """Verify yfinance-internal failures don't escalate to ``unexpected_errors``.

    Both ``Ticker.get_earnings_dates`` and the lazy ``Ticker.earnings_dates``
    property can raise ``KeyError`` (or other exceptions) when Yahoo lacks a
    usable calendar entry for a given ticker. The data source must absorb
    these so the scan step can downgrade such tickers via the standard
    missing-market-data or no-event paths, instead of emitting a per-ticker
    ``WARNING VPMA evaluation failed`` and inflating the unexpected_errors
    bucket at the scan step.
    """

    def test_earnings_dates_returns_empty_when_both_methods_raise_keyerror(self) -> None:
        class _BothFailTicker:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                raise KeyError("chart")

            @property
            def earnings_dates(self) -> pd.DataFrame:
                raise KeyError("chart")

        ds = YfinanceVpmaDataSource()
        with patch("scanners.vpma.engine.yf.Ticker", return_value=_BothFailTicker()):
            result = ds.earnings_dates("AAPL")

        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_earnings_dates_returns_empty_when_first_method_raises_and_property_raises(self) -> None:
        """Migration-safe path: when ``get_earnings_dates`` is removed and the
        property raises, we still want a clean empty DataFrame rather than a
        leaked exception."""
        class _FirstRaisesPropertyRaises:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                raise RuntimeError("removed in yfinance")

            @property
            def earnings_dates(self) -> pd.DataFrame:
                raise RuntimeError("chart endpoint unavailable")

        ds = YfinanceVpmaDataSource()
        with patch(
            "scanners.vpma.engine.yf.Ticker",
            return_value=_FirstRaisesPropertyRaises(),
        ):
            result = ds.earnings_dates("MSFT")

        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_earnings_dates_falls_back_to_property_when_first_method_raises(self) -> None:
        """Happy path: first method raises, property returns a valid DataFrame."""
        frame = pd.DataFrame(
            {"EPS Estimate": [1.5], "Reported EPS": [1.6]},
            index=[pd.Timestamp("2026-06-15 16:30:00")],
        )

        class _FirstRaisesPropertyOK:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                raise KeyError("transient")

            @property
            def earnings_dates(self) -> pd.DataFrame:
                return frame

        ds = YfinanceVpmaDataSource()
        with patch(
            "scanners.vpma.engine.yf.Ticker",
            return_value=_FirstRaisesPropertyOK(),
        ):
            result = ds.earnings_dates("NVDA")

        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(len(result), 1)

    def test_next_earnings_date_reuses_cached_earnings_frame_before_calendar(self) -> None:
        frame = pd.DataFrame(
            {"EPS Estimate": [1.5, 1.7]},
            index=[
                pd.Timestamp("2026-06-15 16:30:00"),
                pd.Timestamp("2099-06-30 16:30:00"),
            ],
        )

        class _TickerWithFailingCalendar:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                return frame

            @property
            def calendar(self) -> pd.DataFrame:
                raise AssertionError("calendar should not be called when a future earnings date exists")

        ds = YfinanceVpmaDataSource()
        with patch("scanners.vpma.engine.yf.Ticker", return_value=_TickerWithFailingCalendar()):
            next_date = ds.next_earnings_date("TEAM")

        self.assertEqual(next_date, date(2099, 6, 30))

    def test_next_earnings_date_skips_calendar_fallback_by_default(self) -> None:
        frame = pd.DataFrame(
            {"EPS Estimate": [1.5]},
            index=[pd.Timestamp("2026-06-15 16:30:00")],
        )

        class _TickerWithNoisyCalendar:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                return frame

            @property
            def calendar(self) -> pd.DataFrame:
                raise AssertionError("calendar fallback should stay disabled by default")

        ds = YfinanceVpmaDataSource()
        with patch("scanners.vpma.engine.yf.Ticker", return_value=_TickerWithNoisyCalendar()):
            next_date = ds.next_earnings_date("TEAM")

        self.assertIsNone(next_date)

    def test_next_earnings_date_uses_calendar_only_when_explicitly_allowed(self) -> None:
        frame = pd.DataFrame(
            {"EPS Estimate": [1.5]},
            index=[pd.Timestamp("2026-06-15 16:30:00")],
        )

        class _TickerWithCalendar:
            def get_earnings_dates(self, limit: int = 8) -> pd.DataFrame:  # noqa: ARG002
                return frame

            @property
            def calendar(self) -> dict[str, pd.Timestamp]:
                return {"Earnings Date": pd.Timestamp("2099-07-15 16:30:00")}

        ds = YfinanceVpmaDataSource()
        with patch("scanners.vpma.engine.yf.Ticker", return_value=_TickerWithCalendar()):
            next_date = ds.next_earnings_date("TEAM", allow_calendar_fallback=True)

        self.assertEqual(next_date, date(2099, 7, 15))


class TzAwareTradingIndexInExtractEventTests(unittest.TestCase):
    """``yf.download`` returns a tz-aware ``DatetimeIndex`` for US session
    hours. ``extract_recent_earnings_event`` must localize the trading index
    before comparing against the tz-naive ``reaction_session`` returned by
    ``map_reaction_session`` - otherwise pandas raises on the cross-tz
    comparison and the failure bubbles up to ``unexpected_errors``."""

    def test_extract_recent_event_with_tz_aware_trading_index(self) -> None:
        tz_aware_index = pd.bdate_range("2026-06-01", periods=20).tz_localize("America/New_York")
        frame = pd.DataFrame(
            {"Surprise(%)": [5.0, 12.0]},
            index=[pd.Timestamp("2026-06-05 08:00:00"), pd.Timestamp("2026-06-20 16:30:00")],
        )

        event = extract_recent_earnings_event(
            frame,
            tz_aware_index,
            lookback_days=90,
            today=date(2026, 6, 25),
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.reaction_session, pd.Timestamp("2026-06-22"))
        self.assertEqual(event.release_timing, "after_market")
        self.assertEqual(event.eps_surprise_pct, 12.0)


class VpmaUnexpectedErrorDiagnosticTests(unittest.TestCase):
    """Verify that warnings now include the actual exception message and
    traceback so future funnel runs surface the root cause - exactly what
    was missing from the prior ``WARNING VPMA evaluation failed for <T>: KeyError``
    entries that hid the actual exception text."""

    def test_unexpected_error_warning_includes_exception_message_and_details(self) -> None:
        """A valid OHLCV history is required - the prior version used an empty
        DataFrame which triggered ``missing_market_data`` early return before
        ``earnings_dates`` was called, so the warning path was never reached."""
        dates = pd.bdate_range("2026-01-02", periods=90)
        history_aapl = pd.DataFrame({
            "Open": [150.0] * 90,
            "High": [152.0] * 90,
            "Low": [149.0] * 90,
            "Close": [151.0] * 90,
            "Volume": [10_000_000] * 90,
        }, index=dates)
        benchmark = pd.DataFrame({
            "Open": [500.0] * 90,
            "High": [502.0] * 90,
            "Low": [499.0] * 90,
            "Close": [501.0] * 90,
            "Volume": [10_000_000] * 90,
        }, index=dates)

        config = VpmaConfig(enable_enrichment=False, guidance_enable=False, valid_days=3)
        with patch.dict("os.environ", {"VPMA_TEST_TICKERS": "AAPL"}, clear=True):
            with patch.object(
                YfinanceVpmaDataSource,
                "download_histories",
                return_value={"AAPL": history_aapl},
            ), patch.object(
                YfinanceVpmaDataSource,
                "benchmark_history",
                return_value=benchmark,
            ), patch.object(
                YfinanceVpmaDataSource,
                "next_earnings_date",
                return_value=None,
            ), patch.object(
                YfinanceVpmaDataSource,
                "earnings_dates",
                side_effect=KeyError("calendar_payload_missing"),
            ):
                with self.assertLogs("scanners.vpma.engine", level="WARNING") as captured:
                    scan = run_vpma_scan(config=config)

        joined = "\n".join(captured.output)
        self.assertIn("AAPL", joined)
        self.assertIn("KeyError", joined)
        self.assertIn("calendar_payload_missing", joined)
        # ``exc_info=True`` triggers Python's logging to print the full traceback.
        self.assertIn("Traceback", joined)

        result = next(r for r in scan.results if r.ticker == "AAPL")
        self.assertIn("KeyError", result.reason)
        self.assertIn("calendar_payload_missing", result.reason)
        self.assertEqual(result.details.get("error_class"), "KeyError")
        self.assertIn(
            "calendar_payload_missing",
            result.details.get("error_message", ""),
        )
        self.assertEqual(scan.counts.get("unexpected_errors", 0), 1)
        # The ``errors`` list is now colon-parseable (no embedded message).
        self.assertIn("evaluate:AAPL:KeyError", scan.errors)


if __name__ == "__main__":
    unittest.main()
