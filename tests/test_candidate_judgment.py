from __future__ import annotations

import unittest

from funnel.candidate_judgment import apply_candidate_judgment


class CandidateJudgmentTests(unittest.TestCase):
    def test_research_now_when_quality_passes_and_sources_are_independent(self) -> None:
        judged = apply_candidate_judgment(
            {
                "Source": "vpma, insider",
                "BTD Gate": "PASS",
                "Feroldi Gate": "PASS",
                "Corroboration Level": "STRONG",
                "Conflict Status": "CLEAR",
            }
        )

        self.assertEqual(judged["Attention Family"], "TECHNICAL + OWNERSHIP")
        self.assertEqual(judged["Ownership Confirmation"], "INSIDER")
        self.assertEqual(judged["Decision Lane"], "RESEARCH_NOW")
        self.assertTrue(float(judged["Research Rank"]) > 0)

    def test_watch_when_btd_passes_but_feroldi_is_thin(self) -> None:
        judged = apply_candidate_judgment(
            {
                "Source": "vpma",
                "BTD Gate": "PASS",
                "Feroldi Gate": "LOW_COVERAGE",
                "Feroldi Missing Inputs": "S03",
            }
        )

        self.assertEqual(judged["Decision Lane"], "WATCH")
        self.assertIn("Feroldi coverage still thin", judged["Risk Flags"])
        self.assertEqual(judged["Thesis Breaker Severity"], "MEDIUM")

    def test_reject_when_btd_fails(self) -> None:
        judged = apply_candidate_judgment(
            {
                "Source": "congress, insider",
                "BTD Gate": "FAIL",
                "Feroldi Gate": "PASS",
            }
        )

        self.assertEqual(judged["Decision Lane"], "REJECT")

    def test_forward_confirmation_elevates_borderline_candidate(self) -> None:
        judged = apply_candidate_judgment(
            {
                "Source": "vpma, fundamental_inflection",
                "BTD Gate": "PASS",
                "Feroldi Gate": "REVIEW",
                "Corroboration Level": "STRONG",
                "Conflict Status": "CLEAR",
                "Fundamental Inflection Classification": "VALIDATED_INFLECTION",
                "Fundamental Inflection Score": 78,
                "Fundamental Inflection Pillars": "growth_acceleration, operating_leverage",
                "Fundamental Inflection Revenue Growth": 0.31,
                "Fundamental Inflection Operating Margin Change Bps": 420,
            }
        )

        self.assertEqual(judged["Forward Confirmation"], "VALIDATED")
        self.assertEqual(judged["Decision Lane"], "RESEARCH_NOW")
        self.assertIn("operating margin +420 bps", judged["Forward Confirmation Detail"])


if __name__ == "__main__":
    unittest.main()
