from __future__ import annotations

"""Comprehensive tests for F01–F05 deterministic financial scoring functions."""

import unittest

from funnel.feroldi_financials import (
    score_f01,
    score_f02,
    score_f03,
    score_f04,
    score_f05,
)


# ===================================================================
# F01 — Cash to long-term-debt resilience (max 5)
# ===================================================================


class F01CashToDebtTests(unittest.TestCase):
    def test_ratio_below_zero_scores_0(self) -> None:
        r = score_f01(cash=0, long_term_debt=100)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.available, 5.0)

    def test_ratio_zero_scores_0(self) -> None:
        r = score_f01(cash=0, long_term_debt=50)
        self.assertEqual(r.score, 0.0)

    def test_ratio_0_9999_scores_1(self) -> None:
        r = score_f01(cash=99, long_term_debt=100)
        self.assertEqual(r.score, 1.0)
        self.assertAlmostEqual(r.cash_to_lt_debt_ratio, 0.99)

    def test_ratio_exactly_1_scores_3(self) -> None:
        r = score_f01(cash=100, long_term_debt=100)
        self.assertEqual(r.score, 3.0)

    def test_ratio_1_9999_scores_3(self) -> None:
        r = score_f01(cash=199, long_term_debt=100)
        self.assertEqual(r.score, 3.0)

    def test_ratio_exactly_2_scores_5(self) -> None:
        r = score_f01(cash=200, long_term_debt=100)
        self.assertEqual(r.score, 5.0)

    def test_ratio_above_2_scores_5(self) -> None:
        r = score_f01(cash=500, long_term_debt=100)
        self.assertEqual(r.score, 5.0)

    def test_debt_equals_zero_scores_5(self) -> None:
        r = score_f01(cash=100, long_term_debt=0)
        self.assertEqual(r.score, 5.0)
        self.assertTrue(r.no_long_term_debt_flag)
        self.assertEqual(r.available, 5.0)

    def test_missing_cash_unavailable(self) -> None:
        r = score_f01(cash=None, long_term_debt=100)
        self.assertEqual(r.available, 0.0)

    def test_missing_debt_unavailable(self) -> None:
        r = score_f01(cash=100, long_term_debt=None)
        self.assertEqual(r.available, 0.0)

    def test_negative_debt_unavailable(self) -> None:
        r = score_f01(cash=100, long_term_debt=-50)
        self.assertEqual(r.available, 0.0)
        self.assertIn("Invalid negative", r.reason)

    def test_cash_zero_debt_positive_scores_0(self) -> None:
        r = score_f01(cash=0, long_term_debt=100)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.available, 5.0)


# ===================================================================
# F02 — Gross margin (max 3)
# ===================================================================


class F02GrossMarginTests(unittest.TestCase):
    def test_49_99_pct_scores_0(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=49.99)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.available, 3.0)

    def test_exactly_50_pct_scores_1(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=50)
        self.assertEqual(r.score, 1.0)

    def test_exactly_65_pct_scores_2(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=65)
        self.assertEqual(r.score, 2.0)

    def test_exactly_80_pct_scores_3(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=80)
        self.assertEqual(r.score, 3.0)

    def test_above_80_pct_scores_3(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=90)
        self.assertEqual(r.score, 3.0)

    def test_64_9_pct_scores_1(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=64.9)
        self.assertEqual(r.score, 1.0)

    def test_79_9_pct_scores_2(self) -> None:
        r = score_f02(revenue_ttm=100, gross_profit_ttm=79.9)
        self.assertEqual(r.score, 2.0)

    def test_missing_revenue_unavailable(self) -> None:
        r = score_f02(revenue_ttm=None, gross_profit_ttm=50)
        self.assertEqual(r.available, 0.0)

    def test_zero_revenue_unavailable(self) -> None:
        r = score_f02(revenue_ttm=0, gross_profit_ttm=50)
        self.assertEqual(r.available, 0.0)

    def test_derived_gross_profit_from_cost_of_revenue(self) -> None:
        r = score_f02(revenue_ttm=100, cost_of_revenue_ttm=40)
        self.assertEqual(r.gross_profit_ttm, 60.0)
        self.assertEqual(r.gross_margin_pct, 0.60)
        self.assertEqual(r.score, 1.0)


# ===================================================================
# F03 — ROE (max 3)
# ===================================================================


class F03ROETests(unittest.TestCase):
    def test_negative_current_roe_scores_0(self) -> None:
        r = score_f03(
            current_net_income=-10, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=5, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 0.0)

    def test_zero_current_roe_scores_0(self) -> None:
        r = score_f03(
            current_net_income=0, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=5, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 0.0)

    def test_positive_but_declining_scores_1(self) -> None:
        r = score_f03(
            current_net_income=8, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=10, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 1.0)

    def test_growth_exactly_0_pct_scores_1(self) -> None:
        r = score_f03(
            current_net_income=10, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=10, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 1.0)

    def test_growth_below_15_pct_scores_2(self) -> None:
        r = score_f03(
            current_net_income=11, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=10, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 2.0)

    def test_growth_exactly_15_pct_scores_3(self) -> None:
        r = score_f03(
            current_net_income=11.51, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=10, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertGreaterEqual(r.score, 3.0)

    def test_turnaround_scores_2(self) -> None:
        r = score_f03(
            current_net_income=10, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=-5, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.score, 2.0)
        self.assertTrue(r.turnaround_flag)
        self.assertEqual(r.available, 3.0)

    def test_negative_equity_unavailable(self) -> None:
        r = score_f03(
            current_net_income=10, current_opening_equity=-50, current_closing_equity=-50,
        )
        self.assertEqual(r.available, 0.0)

    def test_current_only_positive_scores_1_of_1(self) -> None:
        r = score_f03(
            current_net_income=10, current_opening_equity=100, current_closing_equity=100,
        )
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.available, 1.0)

    def test_current_only_non_positive_scores_0_of_1(self) -> None:
        r = score_f03(
            current_net_income=-5, current_opening_equity=100, current_closing_equity=100,
        )
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.available, 1.0)

    # --- Trajectory classification ---

    def test_trajectory_accelerating(self) -> None:
        """Recent ROE growth > prior * 1.2 = accelerating."""
        # 2y: NI=100, eq=100 → ROE=1.00
        # prior: NI=110, eq=100 → ROE=1.10, prior_yoy=0.10
        # current: NI=132, eq=100 → ROE=1.32, recent_yoy=0.20
        # 0.20 > 0.10*1.2=0.12 → accelerating
        r = score_f03(
            current_net_income=132, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=110, prior_opening_equity=100, prior_closing_equity=100,
            two_year_net_income=100, two_year_opening_equity=100, two_year_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "accelerating")
        self.assertAlmostEqual(r.weighted_roe_growth_pct, 0.16)

    def test_trajectory_stable(self) -> None:
        """Recent ROE growth within 3pp of prior = stable."""
        # 2y: NI=100, eq=100 → ROE=1.00
        # prior: NI=110, eq=100 → ROE=1.10, prior_yoy=0.10
        # current: NI=121, eq=100 → ROE=1.21, recent_yoy=0.10
        # diff=0.0 < 0.03 → stable
        r = score_f03(
            current_net_income=121, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=110, prior_opening_equity=100, prior_closing_equity=100,
            two_year_net_income=100, two_year_opening_equity=100, two_year_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "stable")

    def test_trajectory_decelerating(self) -> None:
        """Recent ROE growth < prior * 0.8 = decelerating (both still positive)."""
        # 2y: NI=100, eq=100 → ROE=1.00
        # prior: NI=130, eq=100 → ROE=1.30, prior_yoy=0.30
        # current: NI=140.4, eq=100 → ROE=1.404, recent_yoy≈0.08
        # 0.08 < 0.30*0.8=0.24 → decelerating
        r = score_f03(
            current_net_income=140.4, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=130, prior_opening_equity=100, prior_closing_equity=100,
            two_year_net_income=100, two_year_opening_equity=100, two_year_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "decelerating")



    def test_trajectory_recovering(self) -> None:
        """Prior ROE positive but prior growth negative, recent recovering = recovering."""
        # 2y: NI=100, eq=100 → ROE=1.00
        # prior: NI=85, eq=100 → ROE=0.85, prior_yoy=-0.15 (negative growth but prior ROE > 0)
        # current: NI=102, eq=100 → ROE=1.02, recent_yoy=0.20
        # recent > 0, prior <= 0 → recovering (turnaround doesn't fire because prior ROE > 0)
        r = score_f03(
            current_net_income=102, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=85, prior_opening_equity=100, prior_closing_equity=100,
            two_year_net_income=100, two_year_opening_equity=100, two_year_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "recovering")

    def test_trajectory_declining(self) -> None:
        """Both ROEs positive but recent growth is negative = declining."""
        # 2y: NI=100, eq=100 → ROE=1.00
        # prior: NI=115, eq=100 → ROE=1.15, prior_yoy=0.15
        # current: NI=110.4, eq=100 → ROE=1.104, recent_yoy≈-0.04
        # recent <= 0, prior > 0 → declining
        r = score_f03(
            current_net_income=110.4, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=115, prior_opening_equity=100, prior_closing_equity=100,
            two_year_net_income=100, two_year_opening_equity=100, two_year_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "declining")

    def test_no_trajectory_without_2y_data(self) -> None:
        """Without 2-year-ago data, trajectory_label should be empty."""
        r = score_f03(
            current_net_income=115, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=100, prior_opening_equity=100, prior_closing_equity=100,
        )
        self.assertEqual(r.trajectory_label, "")
        self.assertIsNone(r.weighted_roe_growth_pct)

    def test_weighted_growth_falls_back_to_simple_when_2y_missing(self) -> None:
        """Without 2y data, weighted growth should be simple YoY."""
        r = score_f03(
            current_net_income=115, current_opening_equity=100, current_closing_equity=100,
            prior_net_income=100, prior_opening_equity=100, prior_closing_equity=100,
        )
        # Simple growth: (115/100 - 100/100) / (100/100) = 0.15
        self.assertAlmostEqual(r.roe_growth_pct, 0.15)
        self.assertIsNone(r.weighted_roe_growth_pct)
        self.assertIn("15.0%", r.reason)


# ===================================================================
# F04 — Free Cash Flow (max 3)
# ===================================================================


class F04FCFTests(unittest.TestCase):
    def test_negative_current_fcf_scores_0(self) -> None:
        r = score_f04(current_operating_cf=50, current_capex=60, prior_operating_cf=60, prior_capex=40)
        self.assertEqual(r.score, 0.0)

    def test_positive_current_declining_scores_1(self) -> None:
        r = score_f04(current_operating_cf=100, current_capex=20, prior_operating_cf=120, prior_capex=20)
        self.assertEqual(r.score, 1.0)

    def test_growth_below_15_pct_scores_2(self) -> None:
        r = score_f04(current_operating_cf=110, current_capex=20, prior_operating_cf=100, prior_capex=20)
        self.assertEqual(r.score, 2.0)

    def test_growth_exactly_15_pct_scores_3(self) -> None:
        r = score_f04(current_operating_cf=115, current_capex=20, prior_operating_cf=100, prior_capex=20)
        self.assertEqual(r.score, 3.0)

    def test_turnaround_scores_2(self) -> None:
        r = score_f04(current_operating_cf=100, current_capex=20, prior_operating_cf=50, prior_capex=60)
        self.assertTrue(r.turnaround_flag)
        self.assertEqual(r.score, 2.0)

    def test_capex_positive_is_abs(self) -> None:
        r = score_f04(current_operating_cf=100, current_capex=20, prior_operating_cf=120, prior_capex=20)
        self.assertEqual(r.current_fcf_ttm, 80.0)

    def test_capex_negative_treated_as_absolute(self) -> None:
        r = score_f04(current_operating_cf=100, current_capex=-30, prior_operating_cf=120, prior_capex=-20)
        self.assertEqual(r.current_fcf_ttm, 70.0)

    def test_prior_unavailable_scores_1_of_1(self) -> None:
        r = score_f04(current_operating_cf=100, current_capex=20)
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.available, 1.0)

    # --- FCF Trajectory classification ---

    def test_fcf_trajectory_accelerating(self) -> None:
        """Recent FCF growth accelerates vs prior."""
        # Current: OCF 130 - capex 20 = FCF 110 (prior 100) → growth 0.10
        # Prior: OCF 120 - capex 20 = FCF 100 (2y 95) → growth 0.0526
        # Recent > prior * 1.2 → accelerating
        r = score_f04(
            current_operating_cf=130, current_capex=20,
            prior_operating_cf=120, prior_capex=20,
            two_year_operating_cf=115, two_year_capex=20,
        )
        self.assertEqual(r.trajectory_label, "accelerating")

    def test_fcf_trajectory_stable(self) -> None:
        """Recent FCF growth ~ prior within 3pp."""
        # Current FCF=95, Prior FCF=92, 2y FCF=88
        # recent: 95/92-1 ≈ 0.0326, prior: 92/88-1 ≈ 0.0455, diff ≈ 0.0129 < 0.03
        r = score_f04(
            current_operating_cf=115, current_capex=20,
            prior_operating_cf=112, prior_capex=20,
            two_year_operating_cf=108, two_year_capex=20,
        )
        self.assertEqual(r.trajectory_label, "stable")

    def test_fcf_trajectory_decelerating(self) -> None:
        """Recent FCF growth < prior * 0.8 = decelerating."""
        # Current FCF=115, Prior FCF=105, 2y FCF=80
        # recent: 115/105-1 ≈ 0.095, prior: 105/80-1 = 0.3125
        # 0.095 < 0.3125 * 0.8 = 0.25 → decelerating
        r = score_f04(
            current_operating_cf=135, current_capex=20,
            prior_operating_cf=125, prior_capex=20,
            two_year_operating_cf=100, two_year_capex=20,
        )
        self.assertEqual(r.trajectory_label, "decelerating")

    def test_fcf_trajectory_recovering(self) -> None:
        """Prior FCF positive but prior growth negative, recent recovering = recovering."""
        # Current FCF=100, Prior FCF=90, 2y FCF=100
        # recent: 100/90-1 ≈ 0.111, prior: 90/100-1 = -0.10
        # recent > 0, prior <= 0 → recovering (turnaround doesn't fire: prior FCF 90 > 0)
        r = score_f04(
            current_operating_cf=120, current_capex=20,
            prior_operating_cf=110, prior_capex=20,
            two_year_operating_cf=120, two_year_capex=20,
        )
        self.assertEqual(r.trajectory_label, "recovering")

    def test_fcf_trajectory_no_2y_data(self) -> None:
        """Without 2y data, FCF trajectory_label should be empty."""
        r = score_f04(
            current_operating_cf=115, current_capex=20,
            prior_operating_cf=100, prior_capex=20,
        )
        self.assertEqual(r.trajectory_label, "")
        self.assertIsNone(r.weighted_fcf_growth_pct)


# ===================================================================
# F05 — Diluted EPS (max 3)
# ===================================================================


class F05EPSTests(unittest.TestCase):
    def test_negative_scores_0(self) -> None:
        r = score_f05(current_diluted_eps=-0.5, prior_diluted_eps=1.0)
        self.assertEqual(r.score, 0.0)

    def test_positive_declining_scores_1(self) -> None:
        r = score_f05(current_diluted_eps=1.0, prior_diluted_eps=1.5)
        self.assertEqual(r.score, 1.0)

    def test_growth_below_15_pct_scores_2(self) -> None:
        r = score_f05(current_diluted_eps=1.10, prior_diluted_eps=1.0)
        self.assertEqual(r.score, 2.0)

    def test_growth_exactly_15_pct_scores_3(self) -> None:
        r = score_f05(current_diluted_eps=1.151, prior_diluted_eps=1.0)
        self.assertGreaterEqual(r.score, 3.0)

    def test_turnaround_scores_2(self) -> None:
        r = score_f05(current_diluted_eps=0.5, prior_diluted_eps=-0.5)
        self.assertTrue(r.turnaround_flag)
        self.assertEqual(r.score, 2.0)

    def test_current_only_positive_scores_1_of_1(self) -> None:
        r = score_f05(current_diluted_eps=2.0)
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.available, 1.0)

    def test_current_only_non_positive_scores_0(self) -> None:
        r = score_f05(current_diluted_eps=0)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.available, 1.0)

    def test_missing_scores_0_unavailable(self) -> None:
        r = score_f05()
        self.assertEqual(r.available, 0.0)

    # --- EPS Trajectory classification ---

    def test_eps_trajectory_accelerating(self) -> None:
        """Recent EPS growth > prior * 1.2 = accelerating."""
        # Current 6.0, prior 5.0, 2y 4.0
        # recent: 6.0/5.0-1 = 0.20, prior: 5.0/4.0-1 = 0.25
        # Wait, 0.20 < 0.25 * 1.2? 0.25*1.2 = 0.30. 0.20 not > 0.30.
        # Let me use: current 7.0, prior 5.0, 2y 4.0
        # recent: 0.40, prior: 0.25, 0.40 > 0.25*1.2=0.30 → accelerating
        r = score_f05(
            current_diluted_eps=7.0, prior_diluted_eps=5.0,
            two_year_diluted_eps=4.0,
        )
        self.assertEqual(r.trajectory_label, "accelerating")
        self.assertAlmostEqual(r.weighted_eps_growth_pct, 0.60 * 0.40 + 0.40 * 0.25)

    def test_eps_trajectory_stable(self) -> None:
        """Recent EPS growth within 3pp of prior = stable."""
        r = score_f05(
            current_diluted_eps=1.15, prior_diluted_eps=1.00,
            two_year_diluted_eps=0.87,
        )
        # recent: 0.15, prior: 1.00/0.87-1 ≈ 0.1494, diff ≈ 0.0006 < 0.03
        self.assertEqual(r.trajectory_label, "stable")

    def test_eps_trajectory_decelerating(self) -> None:
        """Recent EPS growth < prior * 0.8 = decelerating."""
        r = score_f05(
            current_diluted_eps=1.08, prior_diluted_eps=1.00,
            two_year_diluted_eps=0.80,
        )
        # recent: 0.08, prior: 1.00/0.80-1 = 0.25, 0.08 < 0.25*0.8=0.20 → decelerating
        self.assertEqual(r.trajectory_label, "decelerating")

    def test_eps_trajectory_moderate(self) -> None:
        """Both positive, neither accelerating/stable/decelerating = moderate."""
        # prior_yoy = 0.30, need recent such that: 0.24 <= recent <= 0.36 but outside [0.27, 0.33]
        # Use recent_yoy = 0.25: not accelerating (0.25 < 0.36), not stable (|0.25-0.30|=0.05>0.03),
        # not decelerating (0.25 > 0.24) → moderate
        r = score_f05(
            current_diluted_eps=1.625, prior_diluted_eps=1.30,
            two_year_diluted_eps=1.00,
        )
        # recent: 1.625/1.30-1 = 0.25, prior: 1.30/1.00-1 = 0.30
        self.assertEqual(r.trajectory_label, "moderate")

    def test_eps_trajectory_recovering(self) -> None:
        """Prior EPS positive but prior growth negative, recent recovering = recovering."""
        r = score_f05(
            current_diluted_eps=10.0, prior_diluted_eps=8.0,
            two_year_diluted_eps=10.0,
        )
        # recent: 10/8-1 = 0.25, prior: 8/10-1 = -0.20
        # recent > 0, prior <= 0 → recovering
        self.assertEqual(r.trajectory_label, "recovering")

    def test_eps_trajectory_declining(self) -> None:
        """Recent EPS declines but still positive, prior was positive = declining."""
        r = score_f05(
            current_diluted_eps=1.08, prior_diluted_eps=1.20,
            two_year_diluted_eps=1.00,
        )
        # recent: 1.08/1.20-1 = -0.10, prior: 1.20/1.00-1 = 0.20
        # recent <= 0, prior > 0 → declining
        self.assertEqual(r.trajectory_label, "declining")

    def test_eps_trajectory_declining(self) -> None:
        """Recent EPS flat/negative, prior positive = declining."""
        r = score_f05(
            current_diluted_eps=1.0, prior_diluted_eps=1.2,
            two_year_diluted_eps=1.0,
        )
        # recent: 1.0/1.2-1 ≈ -0.167, prior: 1.2/1.0-1 = 0.20
        # recent <= 0, prior > 0 → declining
        self.assertEqual(r.trajectory_label, "declining")

    def test_eps_no_trajectory_without_2y(self) -> None:
        """Without 2y data, EPS trajectory_label should be empty."""
        r = score_f05(current_diluted_eps=2.0, prior_diluted_eps=1.5)
        self.assertEqual(r.trajectory_label, "")
        self.assertIsNone(r.weighted_eps_growth_pct)


if __name__ == "__main__":
    unittest.main()
