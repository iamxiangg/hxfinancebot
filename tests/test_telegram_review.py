from __future__ import annotations

import unittest

from funnel.telegram_review import (
    build_callback_data,
    candidate_id_for_ticker,
    parse_callback_data,
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


if __name__ == "__main__":
    unittest.main()
