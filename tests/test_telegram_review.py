from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from funnel.telegram_review import (
    answer_callback,
    build_review_message,
    build_callback_data,
    candidate_id_for_ticker,
    parse_callback_data,
    send_telegram_text,
)


class TelegramReviewTests(unittest.TestCase):
    def test_callback_round_trip(self) -> None:
        candidate_id = candidate_id_for_ticker(" msft ")
        data = build_callback_data("approve", candidate_id)
        parsed = parse_callback_data(data)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.candidate_id, candidate_id)

    def test_ignores_unrelated_callback(self) -> None:
        self.assertIsNone(parse_callback_data("other:approve:abc"))
        self.assertIsNone(parse_callback_data("hxv2:delete:abc"))

    def test_candidate_id_is_stable_and_normalised(self) -> None:
        self.assertEqual(
            candidate_id_for_ticker("msft"),
            candidate_id_for_ticker(" MSFT "),
        )

    @patch("funnel.telegram_review.requests.post")
    def test_answer_callback_failure_is_non_fatal(self, mock_post: Mock) -> None:
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("expired")
        mock_post.return_value = response

        self.assertFalse(
            answer_callback(
                "callback-id",
                "Processed",
                token="token",
            )
        )

    @patch("funnel.telegram_review.requests.post")
    def test_send_telegram_text_success(self, mock_post: Mock) -> None:
        response = Mock()
        response.json.return_value = {"ok": True}
        mock_post.return_value = response

        self.assertTrue(send_telegram_text("Done", token="token", chat_id="chat"))

    @patch("funnel.telegram_review.requests.post")
    def test_send_telegram_text_failure_is_non_fatal(self, mock_post: Mock) -> None:
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("bad")
        mock_post.return_value = response

        self.assertFalse(send_telegram_text("Done", token="token", chat_id="chat"))

    def test_review_message_includes_congress_breadth(self) -> None:
        message = build_review_message(
            {
                "Ticker": "NVDA",
                "Status": "ENRICHED",
                "Funnel Score": 78,
                "BTD Score": 0.4,
                "BTD Ratio": 0.4,
                "BTD Gate": "PASS",
                "Gross Margin": 0.72,
                "Revenue Growth": 0.24,
                "Discovery Reason": "Political Disclosures: 4 unique members",
                "Decision Lane": "WAITING_CONFIRMATION",
                "Attention Family": "OWNERSHIP",
                "Technical Confirmation": "NO",
                "Ownership Confirmation": "POLITICAL",
                "Forward Confirmation": "NO",
                "Risk Flags": "Feroldi first cut pending",
                "Congress Unique Members": 4,
                "Congress Recent Cluster Members": 3,
                "Congress Active Purchases": 6,
                "Congress Member Names": "Pelosi, Gottheimer, Tuberville, Moore",
            }
        )

        self.assertIn("JUDGMENT LAYER", message)
        self.assertIn("Suggested lane: WAITING_CONFIRMATION", message)
        self.assertIn("BTD BASIC GATE", message)
        self.assertIn("BTD ratio: 0.40", message)
        self.assertIn("Gross margin: 72.0%", message)
        self.assertIn("Political disclosure breadth:", message)
        self.assertIn("Unique members represented: 4", message)
        self.assertIn("Recent cluster members: 3", message)
        self.assertIn("Active purchases: 6", message)
        self.assertIn("Members represented: Pelosi, Gottheimer, Tuberville, Moore", message)

    def test_review_message_includes_insider_block(self) -> None:
        message = build_review_message(
            {
                "Ticker": "TEAM",
                "Source": "insider, vpma",
                "Corroboration Level": "STRONG",
                "BTD Gate": "PASS",
                "BTD Ratio": 0.68,
                "Gross Margin": 0.72,
                "Revenue Growth": 0.24,
                "Insider Total Score": 82,
                "Insider Conviction": 43,
                "Insider Economic Commitment": 24,
                "Insider Market Context": 15,
                "Insider Unique Insiders": 3,
                "Insider Roles": "CEO, CFO, Director",
                "Insider Aggregate Purchase": "$1.4m",
                "Insider Cluster Span Days": 12,
                "Insider Weighted Purchase Price": 42.8,
                "Insider Entry State": "trend_confirmed",
                "Decision Lane": "RESEARCH_NOW",
                "Attention Family": "TECHNICAL + OWNERSHIP",
                "Technical Confirmation": "YES",
                "Ownership Confirmation": "INSIDER",
                "Forward Confirmation": "NO",
            }
        )

        self.assertIn("Sources: Corporate Insider, VPMA / PEAD", message)
        self.assertIn("Corroboration: STRONG", message)
        self.assertIn("Suggested lane: RESEARCH_NOW", message)
        self.assertIn("Attention family: TECHNICAL + OWNERSHIP", message)
        self.assertIn("Corporate insider:", message)
        self.assertIn("Total score: 82", message)


if __name__ == "__main__":
    unittest.main()
