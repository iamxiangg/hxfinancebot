from __future__ import annotations

from datetime import date, datetime
import unittest

import pandas as pd

from scanners.vpma.engine import (
    EarningsEvent,
    UniverseTicker,
    VpmaConfig,
    calculate_event_quality,
    classify_core_result,
    clean_universe_rows,
    earnings_anchored_vwap,
    evaluate_ticker,
    extract_recent_earnings_event,
    map_reaction_session,
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


if __name__ == "__main__":
    unittest.main()
