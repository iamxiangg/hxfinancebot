from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from funnel.telegram_review import (
    answer_callback,
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


if __name__ == "__main__":
    unittest.main()
