from __future__ import annotations

"""Comprehensive tests for M01–M03 management scoring and M03 mission analysis."""

import unittest

from funnel.feroldi_management import score_m01, score_m02, score_m03
from funnel.feroldi_mission import analyse_mission


# ===================================================================
# M01 — Soul in the game (max 4)
# ===================================================================


class M01SoulInGameTests(unittest.TestCase):
    def test_founder_ceo_scores_4(self) -> None:
        r = score_m01(
            evidence_text="John Smith founded the company in 2010 and has served as Chief Executive Officer since 2015.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 4.0)
        self.assertTrue(r.founder_flag)

    def test_cofounder_ceo_scores_4(self) -> None:
        r = score_m01(
            evidence_text="Jane Doe co-founded the company and serves as Chief Executive Officer.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 4.0)
        self.assertTrue(r.cofounder_flag)

    def test_founding_family_ceo_scores_4(self) -> None:
        r = score_m01(
            evidence_text="A member of the founding family, has served as Chief Executive Officer since 2018.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 4.0)
        self.assertTrue(r.founding_family_flag)

    def test_non_founder_tenure_12_years_scores_3(self) -> None:
        r = score_m01(
            evidence_text="has served as Chief Executive Officer since 2014",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 3.0)

    def test_tenure_7_years_scores_2(self) -> None:
        r = score_m01(
            evidence_text="was appointed Chief Executive Officer in 2019",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 2.0)

    def test_tenure_3_years_scores_1(self) -> None:
        r = score_m01(
            evidence_text="was named Chief Executive Officer in 2023",
            extraction_confidence="MEDIUM",
        )
        self.assertEqual(r.score, 1.0)

    def test_tenure_1_year_scores_0(self) -> None:
        r = score_m01(
            evidence_text="was appointed CEO in 2025",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 0.0)

    def test_interim_ceo_scores_0(self) -> None:
        r = score_m01(
            evidence_text="was appointed interim Chief Executive Officer in 2024",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 0.0)
        self.assertTrue(r.interim_ceo_flag)

    def test_low_confidence_unavailable(self) -> None:
        r = score_m01(
            evidence_text="Some text about the CEO",
            extraction_confidence="LOW",
        )
        self.assertEqual(r.available, 0.0)

    def test_no_evidence_unavailable(self) -> None:
        r = score_m01(extraction_confidence="HIGH")
        self.assertEqual(r.score, 0.0)
        # No tenure extractable from empty text


# ===================================================================
# M02 — Insider ownership alignment (max 3)
# ===================================================================


class M02OwnershipTests(unittest.TestCase):
    def test_ceo_owns_10_pct_scores_3(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=10_000_000,
            basic_shares_outstanding=100_000_000,
            current_share_price=50,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 3.0)
        self.assertAlmostEqual(r.ceo_ownership_pct, 0.10)

    def test_ceo_stake_100m_scores_3(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=2_000_000,
            basic_shares_outstanding=200_000_000,
            current_share_price=100,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 3.0)
        self.assertAlmostEqual(r.ceo_stake_value_usd, 200_000_000)

    def test_group_owns_15_pct_scores_3(self) -> None:
        r = score_m02(
            directors_officers_group_pct=0.15,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 3.0)

    def test_ceo_owns_2_pct_scores_2(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=2_000_000,
            basic_shares_outstanding=100_000_000,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 2.0)

    def test_ceo_stake_25m_scores_2(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=500_000,
            current_share_price=50,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 2.0)

    def test_ceo_owns_0_5_pct_scores_1(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=500_000,
            basic_shares_outstanding=100_000_000,
            extraction_confidence="MEDIUM",
        )
        self.assertEqual(r.score, 1.0)

    def test_below_all_thresholds_scores_0(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=1_000,
            basic_shares_outstanding=100_000_000,
            current_share_price=10,
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 0.0)

    def test_no_ownership_data_unavailable(self) -> None:
        r = score_m02(extraction_confidence="HIGH")
        self.assertEqual(r.available, 0.0)

    def test_low_confidence_unavailable(self) -> None:
        r = score_m02(
            ceo_beneficial_shares=10_000_000,
            basic_shares_outstanding=100_000_000,
            extraction_confidence="LOW",
        )
        self.assertEqual(r.available, 0.0)


# ===================================================================
# M03 — Mission statement (max 3)
# ===================================================================


class M03MissionTests(unittest.TestCase):
    def test_high_quality_three_point_mission(self) -> None:
        r = score_m03(
            mission_text="Our mission is to improve health and deliver quality education to communities worldwide.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 3.0)
        self.assertEqual(r.simple_point, 1)
        self.assertEqual(r.clear_point, 1)
        self.assertEqual(r.inspirational_point, 1)

    def test_simple_but_vague_mission_scores_1_or_2(self) -> None:
        r = score_m03(
            mission_text="We aim to help people live better lives.",
            extraction_confidence="HIGH",
        )
        self.assertGreaterEqual(r.score, 1.0)

    def test_finance_only_mission_scores_lower(self) -> None:
        r = score_m03(
            mission_text="Our goal is to maximize shareholder value through operational excellence.",
            extraction_confidence="HIGH",
        )
        self.assertTrue(r.financial_only_flag)
        self.assertEqual(r.inspirational_point, 0)

    def test_no_mission_unavailable(self) -> None:
        r = score_m03(extraction_confidence="HIGH")
        self.assertEqual(r.available, 0.0)

    def test_too_short_mission_no_simple_point(self) -> None:
        r = score_m03(
            mission_text="We help people.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.simple_point, 0)

    def test_too_long_mission_no_simple_point(self) -> None:
        long_text = " ".join(["word"] * 35)
        r = score_m03(
            mission_text=f"Our mission is to {long_text}.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.simple_point, 0)

    def test_undefined_acronym_blocks_clear_point(self) -> None:
        r = score_m03(
            mission_text="We deliver innovative XYZ solutions to enable enterprise AI transformation.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.clear_point, 0)
        self.assertGreater(r.undefined_acronym_count, 0)

    def test_action_verb_without_beneficiary_no_clear_point(self) -> None:
        r = score_m03(
            mission_text="We build software.",
            extraction_confidence="HIGH",
        )
        self.assertEqual(r.score, 0.0)

    def test_explicit_purpose_statement_from_10k(self) -> None:
        r = score_m03(
            mission_text="Our purpose is to improve health and well-being through innovative medical solutions.",
            source_type="10-K",
            extraction_confidence="HIGH",
        )
        self.assertGreaterEqual(r.score, 2.0)


# ===================================================================
# Mission analysis (deterministic, no LLM)
# ===================================================================


class MissionAnalysisTests(unittest.TestCase):
    def test_action_verb_detection(self) -> None:
        analysis = analyse_mission("We build and enable better connections for everyone.")
        self.assertTrue(analysis["action_verb_found"])

    def test_beneficiary_detection(self) -> None:
        analysis = analyse_mission("Our mission is to deliver quality education to students worldwide.")
        self.assertTrue(analysis["beneficiary_found"])

    def test_outcome_detection(self) -> None:
        analysis = analyse_mission("To improve health and safety for communities.")
        self.assertTrue(analysis["outcome_found"])

    def test_vague_term_counting(self) -> None:
        analysis = analyse_mission("We lead innovation through excellence and world-class solutions.")
        self.assertGreaterEqual(analysis["vague_term_count"], 3)

    def test_finance_only_detection(self) -> None:
        analysis = analyse_mission("Our goal is to maximize shareholder value.")
        self.assertTrue(analysis["financial_only_flag"])

    def test_undefined_acronym_detection(self) -> None:
        analysis = analyse_mission("We provide AI and ML NLP solutions for B2B growth.")
        self.assertGreaterEqual(analysis["undefined_acronym_count"], 3)

    def test_word_and_sentence_counting(self) -> None:
        analysis = analyse_mission("Build better things. Help more people.")
        self.assertEqual(analysis["word_count"], 6)
        self.assertEqual(analysis["sentence_count"], 2)

    def test_punctuation_counting(self) -> None:
        analysis = analyse_mission("We build, enable, and connect.")
        self.assertEqual(analysis["punctuation_count"], 2)


if __name__ == "__main__":
    unittest.main()
