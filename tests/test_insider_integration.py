from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from funnel.signal_schema import Signal


class InsiderIntegrationTests(unittest.TestCase):
    def _signal(self, ticker: str, score: float) -> Signal:
        return Signal(
            ticker=ticker,
            scanner="insider",
            classification="actionable",
            score=score,
            observed_at="2026-06-25T12:00:00+00:00",
            valid_until="2026-07-09T12:00:00+00:00",
            details={
                "total_score": score,
                "insider_conviction": 43,
                "economic_commitment": 24,
                "market_context": 15,
                "unique_insiders": 3,
                "insider_roles": ["CEO", "CFO", "Director"],
                "aggregate_purchase_value": 1_400_000,
                "entry_state": "trend_confirmed",
            },
        )

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
    def test_insider_only_candidate_with_passing_btd_reaches_telegram(
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
        mock_insider.return_value = ([self._signal("TEAM", 82)], 1)
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 0.68,
            "BTD Ratio": 0.68,
            "BTD Applicability": "APPLICABLE",
        }

        from funnel import review_candidates

        review_candidates.run()

        mock_send_candidate_review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
