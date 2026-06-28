from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from funnel.review_candidates import apply_btd_gate, comparison_to_candidate, merge_candidate
from funnel.signal_schema import Signal


class ReviewCandidateTests(unittest.TestCase):
    def test_apply_btd_gate_manual_bypass_only_for_manual(self) -> None:
        candidate = apply_btd_gate(
            {
                "Ticker": "ABC",
                "Source": "manual",
                "BTD Applicability": "UNAVAILABLE",
            },
            manual_bypass=True,
            threshold=1.0,
        )

        self.assertEqual(candidate["BTD Gate"], "BYPASSED_MANUAL")
        self.assertEqual(candidate["Telegram Eligible"], "YES")

    def test_apply_btd_gate_multiple_sources_cannot_bypass_failure(self) -> None:
        candidate = apply_btd_gate(
            {
                "Ticker": "ABC",
                "Source": "insider, manual",
                "BTD Applicability": "APPLICABLE",
                "BTD Ratio": 1.4,
            },
            manual_bypass=True,
            threshold=1.0,
        )

        self.assertEqual(candidate["BTD Gate"], "FAIL")
        self.assertEqual(candidate["Telegram Eligible"], "NO")

    def test_merge_updates_active_candidate(self) -> None:
        existing = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "ENRICHED",
            "First Seen": "2026-06-01T00:00:00+00:00",
            "Funnel Score": "50",
        }
        incoming = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "NEW",
            "First Seen": "2026-06-24T00:00:00+00:00",
            "Last Seen": "2026-06-24T01:00:00+00:00",
            "Funnel Score": "70",
            "Discovery Reason": "New signal",
        }

        merged = merge_candidate(existing, incoming, "2026-06-24T02:00:00+00:00")

        self.assertEqual(merged["First Seen"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(merged["Funnel Score"], "70")
        self.assertEqual(merged["Discovery Reason"], "New signal")
        self.assertEqual(merged["Active?"], "YES")

    def test_final_candidate_is_not_reopened(self) -> None:
        existing = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "REJECTED",
            "Funnel Score": "50",
        }
        incoming = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "NEW",
            "Funnel Score": "90",
        }

        merged = merge_candidate(existing, incoming, "2026-06-24T02:00:00+00:00")

        self.assertEqual(merged["Status"], "REJECTED")
        self.assertEqual(merged["Funnel Score"], "50")

    def test_candidate_uses_combined_sources(self) -> None:
        candidate = comparison_to_candidate(
            {
                "ticker": "TEAM",
                "scanner": "congress",
                "all_sources": ["congress", "vpma"],
                "classification": "actionable",
                "score": 82,
                "signal_count": 2,
                "discovery_reason": "Political Disclosures: cluster purchase | VPMA: pead consolidation, core 82.0",
                "observed_at": "2026-06-25T01:00:00+00:00",
            },
            "2026-06-25T02:00:00+00:00",
        )

        self.assertEqual(candidate["Source"], "congress, vpma")
        self.assertEqual(candidate["Congress Unique Members"], "")

    def test_candidate_preserves_congress_breadth_fields(self) -> None:
        candidate = comparison_to_candidate(
            {
                "ticker": "NVDA",
                "scanner": "congress",
                "all_sources": ["congress"],
                "classification": "actionable",
                "score": 78,
                "signal_count": 1,
                "discovery_reason": "Political Disclosures: 4 unique members",
                "congress_unique_members": 4,
                "congress_recent_cluster_members": 3,
                "congress_active_purchases": 6,
                "congress_member_names": "Pelosi, Gottheimer, Tuberville, Moore",
                "observed_at": "2026-06-25T01:00:00+00:00",
            },
            "2026-06-25T02:00:00+00:00",
        )

        self.assertEqual(candidate["Congress Unique Members"], 4)
        self.assertEqual(candidate["Congress Recent Cluster Members"], 3)
        self.assertEqual(candidate["Congress Active Purchases"], 6)
        self.assertEqual(candidate["Congress Member Names"], "Pelosi, Gottheimer, Tuberville, Moore")


class ReviewCandidateRunTests(unittest.TestCase):
    def _signal(self, ticker: str, scanner: str, classification: str, score: float, details: dict | None = None) -> Signal:
        return Signal(
            ticker=ticker,
            scanner=scanner,
            classification=classification,
            score=score,
            observed_at="2026-06-25T12:00:00+00:00",
            valid_until="2026-06-28T12:00:00+00:00",
            details=details or {},
        )

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "true"}, clear=False)
    @patch("funnel.review_candidates.send_candidate_review")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    def test_run_merges_scanners_and_enriches_once(
        self,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_send_candidate_review,
    ) -> None:
        congress_signal = self._signal(
            "TEAM",
            "congress",
            "actionable",
            74,
            {
                "conviction": 74,
                "flow": "cluster purchase",
                "buyers": 4,
                "cluster_buyers": 3,
                "active_trade_count": 6,
                "names": ["Pelosi", "Gottheimer", "Tuberville", "Moore"],
            },
        )
        vpma_signal = self._signal("TEAM", "vpma", "wait", 82, {"setup_type": "pead_consolidation", "confirmation_score": 76})
        mock_congress.return_value = ([congress_signal], 1)
        mock_vpma.return_value = ([vpma_signal], 1)
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 0.42,
            "BTD Ratio": 0.42,
            "BTD Applicability": "APPLICABLE",
            "BTD Summary": "BTD 0.42",
        }
        seen_candidates: list[dict] = []
        mock_send_candidate_review.side_effect = lambda candidate: seen_candidates.append(dict(candidate)) or "123"

        from funnel import review_candidates

        review_candidates.run()

        self.assertEqual(mock_fetch_metrics.call_count, 1)
        signal_log_rows = mock_append_records.call_args_list[0].args[4]
        self.assertEqual(len(signal_log_rows), 2)
        upsert_rows = mock_upsert_records.call_args.args[5]
        self.assertEqual(len(upsert_rows), 1)
        self.assertEqual(upsert_rows[0]["Source"], "congress, vpma")
        self.assertIn("Political Disclosures:", upsert_rows[0]["Discovery Reason"])
        self.assertIn("VPMA:", upsert_rows[0]["Discovery Reason"])
        self.assertEqual(upsert_rows[0]["Congress Unique Members"], 4)
        self.assertEqual(upsert_rows[0]["Congress Recent Cluster Members"], 3)
        self.assertEqual(upsert_rows[0]["Congress Active Purchases"], 6)
        self.assertEqual(upsert_rows[0]["Congress Member Names"], "Pelosi, Gottheimer, Tuberville, Moore")
        self.assertEqual(len(seen_candidates), 1)
        self.assertEqual(seen_candidates[0]["BTD Score"], 0.42)
        self.assertEqual(seen_candidates[0]["BTD Gate"], "PASS")
        self.assertEqual(seen_candidates[0]["Telegram Eligible"], "YES")

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "false"}, clear=False)
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    def test_partial_scanner_failure_continues(
        self,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
    ) -> None:
        mock_congress.side_effect = RuntimeError("bad")
        mock_vpma.return_value = ([self._signal("NVDA", "vpma", "actionable", 81, {"setup_type": "pead_breakout"})], 1)
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 0.2,
            "BTD Ratio": 0.2,
            "BTD Applicability": "APPLICABLE",
        }

        from funnel import review_candidates

        review_candidates.run()

        self.assertTrue(mock_append_records.called)
        self.assertTrue(mock_upsert_records.called)

    @patch.dict(os.environ, {"REVIEW_SOURCES": "insider", "SEND_TELEGRAM_REVIEWS": "true"}, clear=False)
    @patch("funnel.review_candidates.send_candidate_review")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_insider_adapter")
    def test_insider_only_candidate_with_failed_btd_does_not_notify(
        self,
        mock_insider,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_send_candidate_review,
    ) -> None:
        mock_insider.return_value = (
            [
                self._signal(
                    "TEAM",
                    "insider",
                    "actionable",
                    82,
                    {
                        "total_score": 82,
                        "unique_insiders": 2,
                        "insider_roles": ["CEO", "CFO"],
                        "aggregate_purchase_value": 1_400_000,
                        "entry_state": "trend_confirmed",
                    },
                )
            ],
            1,
        )
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 1.4,
            "BTD Ratio": 1.4,
            "BTD Applicability": "APPLICABLE",
        }

        from funnel import review_candidates

        review_candidates.run()

        self.assertFalse(mock_send_candidate_review.called)
        upsert_rows = mock_upsert_records.call_args.args[5]
        self.assertEqual(upsert_rows[0]["BTD Gate"], "FAIL")
        self.assertEqual(upsert_rows[0]["Telegram Eligible"], "NO")

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "false"}, clear=False)
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    def test_all_scanner_failure_aborts_before_writes(
        self,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_upsert_records,
    ) -> None:
        mock_congress.side_effect = RuntimeError("bad congress")
        mock_vpma.side_effect = RuntimeError("bad vpma")
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []

        from funnel import review_candidates

        with self.assertRaises(RuntimeError):
            review_candidates.run()

        self.assertFalse(mock_append_records.called)
        self.assertFalse(mock_upsert_records.called)


if __name__ == "__main__":
    unittest.main()
