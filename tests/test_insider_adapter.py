from __future__ import annotations

import unittest
from unittest.mock import patch

from scanners.insider.engine import InsiderTickerResult
from funnel.insider_adapter import result_to_signal, run_insider_adapter


class InsiderAdapterTests(unittest.TestCase):
    def test_result_to_signal_preserves_key_fields(self) -> None:
        result = InsiderTickerResult(
            ticker="TEAM",
            classification="actionable",
            total_score=82,
            conviction_score=43,
            commitment_score=24,
            market_context_score=15,
            unique_insiders=3,
            operating_insiders=2,
            director_count=1,
            purchase_event_count=3,
            transaction_row_count=4,
            aggregate_purchase_value=1_400_000,
            largest_individual_purchase=700_000,
            weighted_purchase_price=42.8,
            cluster_span_days=12,
            insider_names=["A", "B", "C"],
            insider_roles=["CEO", "CFO", "Director"],
            direct_purchase_count=3,
            indirect_purchase_count=0,
            plan_10b5_1_count=0,
            entry_state="trend_confirmed",
            data_confidence="high",
            reason="Strong insider cluster",
            risk_flags=[],
            valid_for_days=14,
            source_accessions=["a1", "a2"],
            details={},
        )

        signal = result_to_signal(result, "2026-06-26T00:00:00+00:00")

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.scanner, "insider")
        self.assertEqual(signal.details["total_score"], 82)

    @patch("funnel.insider_adapter.persist_ledger_rows")
    @patch("funnel.insider_adapter.save_processed_accessions")
    @patch("funnel.insider_adapter.load_qualified_purchases", return_value=[])
    @patch("funnel.insider_adapter.load_processed_accessions", return_value={"old"})
    @patch("funnel.insider_adapter.run_insider_scan")
    def test_run_adapter_persists_accessions_and_ledger(
        self,
        mock_scan,
        mock_load,
        mock_load_history,
        mock_save,
        mock_persist_rows,
    ) -> None:
        mock_scan.return_value = (
            [],
            {
                "processed_accessions": ["new"],
                "ledger_rows": [{"accession": "new", "ticker": "TEAM", "decision": "QUALIFIED"}],
            },
        )

        signals, analysed = run_insider_adapter(observed_at="2026-06-26T00:00:00+00:00")

        self.assertEqual(signals, [])
        self.assertEqual(analysed, 0)
        mock_save.assert_called_once_with({"old", "new"})
        mock_persist_rows.assert_called_once()
        mock_load.assert_called_once()
        mock_load_history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
