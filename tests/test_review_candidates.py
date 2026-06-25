from __future__ import annotations

import unittest

from funnel.review_candidates import merge_candidate


class ReviewCandidateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
