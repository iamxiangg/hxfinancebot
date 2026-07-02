from __future__ import annotations

import unittest
from unittest.mock import patch

from funnel.review_bot import apply_action


class ReviewBotTests(unittest.TestCase):
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_watch_action_keeps_candidate_active(self, mock_promote) -> None:
        updated, log_row, result = apply_action(
            service=object(),
            spreadsheet_id="sheet-id",
            candidate={"Candidate ID": "cand-ABC", "Ticker": "ABC", "Status": "WAITING_CONFIRMATION"},
            action="watch",
            actor="neo",
            update_id="1",
        )

        mock_promote.assert_not_called()
        self.assertEqual(updated["Status"], "WATCH")
        self.assertEqual(updated["Active?"], "YES")
        self.assertEqual(updated["Decision"], "WATCH")
        self.assertEqual(log_row["Action"], "WATCH")
        self.assertEqual(result, "Candidate kept on watch")


if __name__ == "__main__":
    unittest.main()
