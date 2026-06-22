# VERSION: 2026-06-22-CANDIDATE-ROUTING-TESTS-1

from __future__ import annotations

import unittest

from funnel.candidate_ingestor import (
    classify_signals,
    get_pending_new_ticker_records,
)
from funnel.signal_schema import Signal


class CandidateIngestorTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.records = [
            {
                "ticker": "MSFT",
                "google_ticker": "MSFT",
                "stock_name": "Microsoft Corp",
                "sheet_row": 91,
            }
        ]

    def make_signal(
        self,
        ticker: str,
        classification: str,
        score: float,
    ) -> Signal:
        return Signal(
            ticker=ticker,
            scanner="congress",
            classification=classification,
            score=score,
            observed_at=(
                "2026-06-22T20:00:00+08:00"
            ),
            valid_until=(
                "2026-07-01T20:00:00+08:00"
            ),
            details={
                "conviction": score,
                "entry_quality": 65,
                "estimated_capital_mid": 100000,
                "buyers": 1,
                "cluster_buyers": 0,
                "flow": "Accumulation",
                "names": [
                    "Example Member"
                ],
            },
        )

    def test_existing_and_new_tickers_are_separated(
        self,
    ) -> None:
        output = classify_signals(
            [
                self.make_signal(
                    "MSFT",
                    "actionable",
                    75,
                ),
                self.make_signal(
                    "BWXT",
                    "wait",
                    55,
                ),
            ],
            self.records,
        )

        by_ticker = {
            row["ticker"]: row
            for row in output
        }

        self.assertEqual(
            by_ticker["MSFT"][
                "already_in_stock_summary"
            ],
            "YES",
        )

        self.assertEqual(
            by_ticker["MSFT"][
                "candidate_status"
            ],
            "EXISTING_MONITORED_TICKER",
        )

        self.assertEqual(
            by_ticker["MSFT"][
                "pending_new_ticker"
            ],
            "NO",
        )

        self.assertEqual(
            by_ticker["BWXT"][
                "already_in_stock_summary"
            ],
            "NO",
        )

        self.assertEqual(
            by_ticker["BWXT"][
                "candidate_status"
            ],
            "NEW_SIGNAL_TICKER",
        )

        self.assertEqual(
            by_ticker["BWXT"][
                "pending_new_ticker"
            ],
            "YES",
        )

        self.assertEqual(
            by_ticker["BWXT"][
                "review_route"
            ],
            "PENDING_NEW_TICKERS",
        )

    def test_stronger_signal_is_primary(
        self,
    ) -> None:
        output = classify_signals(
            [
                self.make_signal(
                    "MSFT",
                    "near_miss",
                    30,
                ),
                self.make_signal(
                    "MSFT",
                    "actionable",
                    70,
                ),
            ],
            self.records,
        )

        self.assertEqual(
            output[0]["classification"],
            "actionable",
        )

        self.assertEqual(
            output[0]["signal_count"],
            2,
        )

    def test_absent_risk_ticker_is_log_only(
        self,
    ) -> None:
        output = classify_signals(
            [
                self.make_signal(
                    "T",
                    "risk",
                    39,
                )
            ],
            self.records,
        )

        self.assertEqual(
            len(output),
            1,
        )

        row = output[0]

        self.assertEqual(
            row["candidate_status"],
            "NEW_SIGNAL_TICKER",
        )

        self.assertEqual(
            row["pending_new_ticker"],
            "NO",
        )

        self.assertEqual(
            row["review_route"],
            "SIGNAL_LOG_ONLY",
        )

        pending = (
            get_pending_new_ticker_records(
                output
            )
        )

        self.assertEqual(
            pending,
            [],
        )

    def test_absent_near_miss_enters_pending_review(
        self,
    ) -> None:
        output = classify_signals(
            [
                self.make_signal(
                    "BIIB",
                    "near_miss",
                    27.7,
                )
            ],
            self.records,
        )

        row = output[0]

        self.assertEqual(
            row["pending_new_ticker"],
            "YES",
        )

        self.assertEqual(
            row["review_route"],
            "PENDING_NEW_TICKERS",
        )

        self.assertEqual(
            row["review_priority"],
            "OPTIONAL",
        )

        pending = (
            get_pending_new_ticker_records(
                output
            )
        )

        self.assertEqual(
            len(pending),
            1,
        )

        self.assertEqual(
            pending[0]["ticker"],
            "BIIB",
        )


if __name__ == "__main__":
    unittest.main()