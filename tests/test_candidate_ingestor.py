from __future__ import annotations

import unittest

from funnel.candidate_ingestor import classify_signals
from funnel.signal_schema import Signal


class CandidateIngestorTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "ticker": "MSFT",
                "google_ticker": "NASDAQ:MSFT",
                "stock_name": "Microsoft",
                "sheet_row": 2,
            }
        ]

    def make_signal(self, ticker, classification, score):
        return Signal(
            ticker=ticker,
            scanner="congress",
            classification=classification,
            score=score,
            observed_at="2026-06-22T20:00:00+08:00",
            valid_until="2026-07-01T20:00:00+08:00",
            details={"conviction": score, "entry_quality": 65},
        )

    def test_existing_and_new_tickers_are_separated(self):
        output = classify_signals(
            [
                self.make_signal("MSFT", "actionable", 75),
                self.make_signal("BWXT", "wait", 55),
            ],
            self.records,
        )
        by_ticker = {row["ticker"]: row for row in output}
        self.assertTrue(by_ticker["MSFT"]["already_in_stock_summary"])
        self.assertFalse(by_ticker["BWXT"]["already_in_stock_summary"])
        self.assertEqual(
            by_ticker["BWXT"]["candidate_status"], "NEW_CANDIDATE"
        )

    def test_stronger_signal_is_primary(self):
        output = classify_signals(
            [
                self.make_signal("MSFT", "near_miss", 30),
                self.make_signal("MSFT", "actionable", 70),
            ],
            self.records,
        )
        self.assertEqual(output[0]["primary_classification"], "actionable")
        self.assertEqual(output[0]["signal_count"], 2)


if __name__ == "__main__":
    unittest.main()
