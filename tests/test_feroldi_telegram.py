from __future__ import annotations

import unittest

from funnel.feroldi_telegram import build_feroldi_block, build_review_message_with_feroldi


class FeroldiTelegramTests(unittest.TestCase):
    def test_block_shows_requested_section_breakdown(self) -> None:
        block = build_feroldi_block(
            {
                "Feroldi Gate": "PASS",
                "Feroldi Gate Mode": "OBSERVE",
                "Feroldi Financial Score": 14,
                "Feroldi Financial Available": 17,
                "Feroldi Management Score": 10,
                "Feroldi Management Available": 10,
                "Feroldi Stock Score": 9,
                "Feroldi Stock Available": 11,
                "Feroldi First Cut Score": 33,
                "Feroldi Available Points": 38,
                "Feroldi Equivalent Score": 36.47,
                "Feroldi Missing Inputs": "Glassdoor",
            }
        )

        self.assertIn("Status: PASS (observe)", block)
        self.assertIn("Financials: 14/17", block)
        self.assertIn("Management & culture: 10/10 available", block)
        self.assertIn("Stock: 9/11", block)
        self.assertIn("Overall: 33/38 available", block)
        self.assertIn("Equivalent: 36.5/42", block)
        self.assertIn("Missing inputs: Glassdoor", block)

    def test_incomplete_financial_or_stock_section_discloses_available_points(self) -> None:
        block = build_feroldi_block(
            {
                "Feroldi Financial Score": 10,
                "Feroldi Financial Available": 14,
                "Feroldi Management Score": 8,
                "Feroldi Management Available": 10,
                "Feroldi Stock Score": 6,
                "Feroldi Stock Available": 8,
                "Feroldi First Cut Score": 24,
                "Feroldi Available Points": 32,
                "Feroldi Equivalent Score": 31.5,
            }
        )

        self.assertIn("Financials: 10/14 available (max 17)", block)
        self.assertIn("Stock: 6/8 available (max 11)", block)

    def test_block_is_inserted_before_other_review_details(self) -> None:
        message = build_review_message_with_feroldi(
            {
                "Candidate ID": "cand-ABC-test",
                "Ticker": "ABC",
                "BTD Gate": "PASS",
                "BTD Ratio": 0.4,
                "Telegram Eligible": "YES",
                "Feroldi Gate": "PENDING",
                "Feroldi Gate Mode": "OBSERVE",
                "Feroldi Gate Reason": "First-cut score has not been populated.",
            }
        )

        self.assertIn("FEROLDI FIRST-CUT", message)
        self.assertLess(message.index("FEROLDI FIRST-CUT"), message.index("BTD basic economic gate passed"))


if __name__ == "__main__":
    unittest.main()
