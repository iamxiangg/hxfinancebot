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

    def test_multi_scanner_ticker_keeps_both_sources_and_reasons(self) -> None:
        congress_signal = self.make_signal("TEAM", "actionable", 74)
        vpma_signal = Signal(
            ticker="TEAM",
            scanner="vpma",
            classification="wait",
            score=82,
            observed_at="2026-06-22T20:00:00+08:00",
            valid_until="2026-06-25T20:00:00+08:00",
            details={
                "setup_type": "pead_consolidation",
                "confirmation_score": 76,
            },
        )

        output = classify_signals([congress_signal, vpma_signal], self.records)
        team = next(row for row in output if row["ticker"] == "TEAM")

        self.assertEqual(team["all_sources"], ["congress", "vpma"])
        self.assertEqual(team["signal_count"], 2)
        self.assertIn("Political Disclosures:", team["discovery_reason"])
        self.assertIn("VPMA:", team["discovery_reason"])

    def test_congress_reason_shows_breadth_with_singular_and_plural(self) -> None:
        signal = Signal(
            ticker="NVDA",
            scanner="congress",
            classification="actionable",
            score=78,
            observed_at="2026-06-22T20:00:00+08:00",
            valid_until="2026-07-01T20:00:00+08:00",
            details={
                "conviction": 78,
                "entry_quality": 71,
                "buyers": 4,
                "cluster_buyers": 3,
                "active_trade_count": 6,
                "flow": "Accumulation",
                "names": ["Pelosi", "Gottheimer", "Tuberville", "Moore"],
            },
        )

        row = classify_signals([signal], self.records)[0]

        self.assertIn("4 unique members", row["discovery_reason"])
        self.assertIn("3 recent cluster members", row["discovery_reason"])
        self.assertIn("6 active purchases", row["discovery_reason"])
        self.assertIn("Members: Pelosi, Gottheimer, Tuberville, Moore", row["discovery_reason"])
        self.assertEqual(row["congress_unique_members"], 4)
        self.assertEqual(row["congress_recent_cluster_members"], 3)
        self.assertEqual(row["congress_active_purchases"], 6)
        self.assertEqual(row["congress_member_names"], "Pelosi, Gottheimer, Tuberville, Moore")

    def test_congress_reason_handles_single_member_multiple_purchases(self) -> None:
        signal = Signal(
            ticker="AMD",
            scanner="congress",
            classification="actionable",
            score=62,
            observed_at="2026-06-22T20:00:00+08:00",
            valid_until="2026-07-01T20:00:00+08:00",
            details={
                "conviction": 62,
                "buyers": 1,
                "cluster_buyers": 1,
                "active_trade_count": 4,
                "names": ["Tuberville"],
            },
        )

        row = classify_signals([signal], self.records)[0]

        self.assertIn("1 unique member", row["discovery_reason"])
        self.assertIn("1 recent cluster member", row["discovery_reason"])
        self.assertIn("4 active purchases", row["discovery_reason"])

    def test_congress_reason_omits_missing_names_and_buyer_fields(self) -> None:
        signal = Signal(
            ticker="META",
            scanner="congress",
            classification="wait",
            score=54,
            observed_at="2026-06-22T20:00:00+08:00",
            valid_until="2026-07-01T20:00:00+08:00",
            details={
                "conviction": 54,
                "flow": "Accumulation",
            },
        )

        row = classify_signals([signal], self.records)[0]

        self.assertNotIn("Members:", row["discovery_reason"])
        self.assertEqual(row["congress_unique_members"], "")
        self.assertEqual(row["congress_member_names"], "")


if __name__ == "__main__":
    unittest.main()
