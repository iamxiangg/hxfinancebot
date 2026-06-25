from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from funnel.congress_adapter import (
    _signal_breakdown,
    _ledger_rows_from_state,
    _ledger_state_from_rows,
    result_to_signal,
    run_congress_adapter,
)
from scanners.congress.engine import CongressTickerResult


class TestCongressAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.observed_at = "2026-06-24T12:00:00+08:00"

    def test_actionable_result_converted(self) -> None:
        result = CongressTickerResult(
            ticker="BWXT",
            category="actionable",
            conviction=75.0,
            entry=68.0,
            base=65.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=100_000.0,
            mid=180_000.0,
            high=250_000.0,
            effective=148_000.0,
            active_bullish_capital=180_000.0,
            historical_context_capital=90_000.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=2,
            cluster_buyers=2,
            weighted_age=12.0,
            weighted_return=-3.0,
            flow="Accumulation",
            names=["Jones", "Smith"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=2,
            active_fresh_trade_count=2,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-20"],
            filing_dates=["2026-06-21"],
            transaction_ages=[4],
            filing_ages=[3],
            alertable=True,
            weighted_average_activity_weight=1.0,
            valid_for_days=20,
            source_payload_hash="abc123",
        )

        signal = result_to_signal(result=result, observed_at=self.observed_at)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.ticker, "BWXT")
        self.assertEqual(signal.classification, "actionable")
        self.assertEqual(signal.score, 75.0)
        self.assertEqual(signal.details["trigger_type"], "fresh_transaction")

    def test_other_above_threshold_becomes_near_miss(self) -> None:
        result = CongressTickerResult(
            ticker="BIIB",
            category="other",
            conviction=28.0,
            entry=42.0,
            base=28.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=80_000.0,
            mid=100_000.0,
            high=120_000.0,
            effective=92_000.0,
            active_bullish_capital=100_000.0,
            historical_context_capital=0.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=1,
            cluster_buyers=1,
            weighted_age=18.0,
            weighted_return=1.0,
            flow="Accumulation",
            names=["Smith"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=1,
            active_fresh_trade_count=1,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-10"],
            filing_dates=["2026-06-11"],
            transaction_ages=[14],
            filing_ages=[13],
            alertable=True,
            weighted_average_activity_weight=1.0,
            valid_for_days=12,
            source_payload_hash="hash",
        )

        signal = result_to_signal(result=result, observed_at=self.observed_at, min_conviction=15)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.classification, "near_miss")

    def test_non_alertable_result_is_suppressed(self) -> None:
        result = CongressTickerResult(
            ticker="MSFT",
            category="actionable",
            conviction=80.0,
            entry=70.0,
            base=70.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=300_000.0,
            mid=400_000.0,
            high=500_000.0,
            effective=360_000.0,
            active_bullish_capital=400_000.0,
            historical_context_capital=0.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=1,
            cluster_buyers=1,
            weighted_age=7.0,
            weighted_return=-2.0,
            flow="Accumulation",
            names=["Doe"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=1,
            active_fresh_trade_count=1,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-22"],
            filing_dates=["2026-06-23"],
            transaction_ages=[2],
            filing_ages=[1],
            alertable=False,
            weighted_average_activity_weight=1.0,
            valid_for_days=40,
            source_payload_hash="hash",
        )

        self.assertIsNone(result_to_signal(result=result, observed_at=self.observed_at))

    def test_ledger_rows_round_trip(self) -> None:
        ledger = {
            "id:123": {
                "fingerprint": "abc",
                "ticker": "MSFT",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "last_seen_at": "2026-06-25T10:00:00+08:00",
                "last_seen_payload_hash": "hash123",
            }
        }

        rows = _ledger_rows_from_state(ledger)
        rebuilt = _ledger_state_from_rows(rows)

        self.assertEqual(rebuilt, ledger)

    @patch("funnel.congress_adapter._save_ledger")
    @patch("funnel.congress_adapter.run_live_scan")
    @patch("funnel.congress_adapter._load_ledger")
    def test_run_congress_adapter_can_skip_persistence(
        self,
        mock_load_ledger: Mock,
        mock_run_live_scan: Mock,
        mock_save_ledger: Mock,
    ) -> None:
        mock_load_ledger.return_value = ({}, None)
        mock_run_live_scan.return_value = Mock(
            metadata=Mock(fetched_at="2026-06-24T12:00:00+08:00", payload_sha256="hash"),
            ticker_results=[],
            ledger={},
            counts={"total_raw_records": 0, "active_tickers_before_market_checks": 0, "scored_tickers": 0},
        )

        signals, analysed = run_congress_adapter(min_conviction=15.0, persist_ledger=False)

        self.assertEqual(signals, [])
        self.assertEqual(analysed, 0)
        mock_save_ledger.assert_not_called()

    def test_signal_breakdown_separates_seen_and_threshold_filtered(self) -> None:
        already_seen = CongressTickerResult(
            ticker="MSFT",
            category="actionable",
            conviction=80.0,
            entry=70.0,
            base=70.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=300_000.0,
            mid=400_000.0,
            high=500_000.0,
            effective=360_000.0,
            active_bullish_capital=400_000.0,
            historical_context_capital=0.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=1,
            cluster_buyers=1,
            weighted_age=7.0,
            weighted_return=-2.0,
            flow="Accumulation",
            names=["Doe"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=1,
            active_fresh_trade_count=1,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-22"],
            filing_dates=["2026-06-23"],
            transaction_ages=[2],
            filing_ages=[1],
            alertable=False,
            weighted_average_activity_weight=1.0,
            valid_for_days=40,
            source_payload_hash="hash",
        )
        below_threshold = CongressTickerResult(
            ticker="XYZ",
            category="other",
            conviction=8.0,
            entry=50.0,
            base=8.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=10_000.0,
            mid=15_000.0,
            high=20_000.0,
            effective=13_000.0,
            active_bullish_capital=15_000.0,
            historical_context_capital=0.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=1,
            cluster_buyers=1,
            weighted_age=12.0,
            weighted_return=1.0,
            flow="Accumulation",
            names=["Smith"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=1,
            active_fresh_trade_count=1,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-10"],
            filing_dates=["2026-06-11"],
            transaction_ages=[14],
            filing_ages=[13],
            alertable=True,
            weighted_average_activity_weight=1.0,
            valid_for_days=12,
            source_payload_hash="hash",
        )
        retained = CongressTickerResult(
            ticker="BWXT",
            category="actionable",
            conviction=75.0,
            entry=68.0,
            base=65.0,
            sale_penalty=0.0,
            call_bonus=0.0,
            put_penalty=0.0,
            low=100_000.0,
            mid=180_000.0,
            high=250_000.0,
            effective=148_000.0,
            active_bullish_capital=180_000.0,
            historical_context_capital=90_000.0,
            call_mid=0.0,
            put_mid=0.0,
            buyers=2,
            cluster_buyers=2,
            weighted_age=12.0,
            weighted_return=-3.0,
            flow="Accumulation",
            names=["Jones", "Smith"],
            unclear_sales=0,
            matched_sales=0,
            matched_full_sales=0,
            active_trade_count=2,
            active_fresh_trade_count=2,
            active_late_disclosed_trade_count=0,
            signal_trigger="fresh_transaction",
            trigger_types=["fresh_transaction"],
            transaction_dates=["2026-06-20"],
            filing_dates=["2026-06-21"],
            transaction_ages=[4],
            filing_ages=[3],
            alertable=True,
            weighted_average_activity_weight=1.0,
            valid_for_days=20,
            source_payload_hash="abc123",
        )

        retained_signal = result_to_signal(retained, observed_at=self.observed_at)
        assert retained_signal is not None

        breakdown = _signal_breakdown(
            [already_seen, below_threshold, retained],
            [retained_signal],
            min_conviction=15.0,
        )

        self.assertEqual(breakdown.alertable, 2)
        self.assertEqual(breakdown.already_seen_suppressed, 1)
        self.assertEqual(breakdown.below_threshold, 1)
        self.assertEqual(breakdown.retained, 1)


if __name__ == "__main__":
    unittest.main()
