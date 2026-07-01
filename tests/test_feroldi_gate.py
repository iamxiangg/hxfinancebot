from __future__ import annotations

import unittest

from funnel.feroldi_gate import apply_feroldi_gate, format_feroldi_score


class FeroldiGateTests(unittest.TestCase):
    def test_complete_score_display(self) -> None:
        self.assertEqual(format_feroldi_score(32, 38, 38), "32/38 (84.2%)")

    def test_partial_score_display_is_not_misleading(self) -> None:
        self.assertEqual(
            format_feroldi_score(31, 35, 38),
            "31/35 available (88.6%; 33.7/38 equivalent)",
        )

    def test_observe_mode_preserves_btd_eligibility(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Ticker": "ABC",
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": 18,
                "Feroldi Available Points": 38,
            },
            mode="observe",
            pass_threshold=27.5,
            review_threshold=23.0,
        )

        self.assertEqual(candidate["Feroldi Gate"], "FAIL")
        self.assertEqual(candidate["Telegram Eligible"], "YES")
        self.assertEqual(candidate["Status"], "BTD_PASSED")
        self.assertIn("Observe-only", candidate["Feroldi Gate Reason"])

    def test_partial_score_is_normalised_to_38(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Ticker": "ABC",
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": 28,
                "Feroldi Available Points": 35,
            },
            mode="enforce",
            pass_threshold=27.5,
            review_threshold=23.0,
            min_coverage=0.75,
        )

        self.assertEqual(candidate["Feroldi Gate"], "PASS")
        self.assertAlmostEqual(candidate["Feroldi Equivalent Score"], 30.4, places=1)
        self.assertEqual(candidate["Telegram Eligible"], "YES")
        self.assertEqual(candidate["Status"], "FEROLDI_PASSED")

    def test_review_band_can_continue_to_human_review(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": 24,
                "Feroldi Available Points": 38,
            },
            mode="enforce",
            pass_threshold=27.5,
            review_threshold=23.0,
            allow_review=True,
        )

        self.assertEqual(candidate["Feroldi Gate"], "REVIEW")
        self.assertEqual(candidate["Telegram Eligible"], "YES")
        self.assertEqual(candidate["Status"], "FEROLDI_REVIEW")

    def test_fail_blocks_when_enforced(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": 20,
                "Feroldi Available Points": 38,
            },
            mode="enforce",
            pass_threshold=27.5,
            review_threshold=23.0,
        )

        self.assertEqual(candidate["Feroldi Gate"], "FAIL")
        self.assertEqual(candidate["Telegram Eligible"], "NO")
        self.assertEqual(candidate["Status"], "FEROLDI_FAILED")

    def test_low_coverage_blocks_when_enforced(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": 20,
                "Feroldi Available Points": 25,
            },
            mode="enforce",
            min_coverage=0.75,
        )

        self.assertEqual(candidate["Feroldi Gate"], "LOW_COVERAGE")
        self.assertEqual(candidate["Telegram Eligible"], "NO")
        self.assertEqual(candidate["Status"], "FEROLDI_UNAVAILABLE")

    def test_missing_score_is_pending_without_blocking_observe_mode(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
            },
            mode="observe",
        )

        self.assertEqual(candidate["Feroldi Gate"], "PENDING")
        self.assertEqual(candidate["Telegram Eligible"], "YES")

    def test_btd_failure_skips_feroldi_gate(self) -> None:
        candidate = apply_feroldi_gate(
            {
                "Status": "BTD_FAILED",
                "Telegram Eligible": "NO",
                "Feroldi First Cut Score": 35,
                "Feroldi Available Points": 38,
            },
            mode="enforce",
        )

        self.assertEqual(candidate["Feroldi Gate"], "SKIPPED_BTD")
        self.assertEqual(candidate["Telegram Eligible"], "NO")


if __name__ == "__main__":
    unittest.main()
