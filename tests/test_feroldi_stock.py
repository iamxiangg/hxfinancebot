from __future__ import annotations

"""Comprehensive tests for S01–S03 stock scoring functions."""

import unittest

from funnel.feroldi_stock import score_s01, score_s02, score_s03


# ===================================================================
# S01 — Five-year performance vs SPY (max 4)
# ===================================================================


class S01PerformanceTests(unittest.TestCase):
    def test_underperformance_scores_0(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=90,
            spy_start_price=100, spy_end_price=100,
            trading_days=1250,
        )
        self.assertEqual(r.score, 0.0)

    def test_exact_0_excess_scores_0(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=110,
            spy_start_price=100, spy_end_price=110,
            trading_days=1250,
        )
        self.assertEqual(r.score, 0.0)

    def test_exact_25_points_scores_2(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=135,
            spy_start_price=100, spy_end_price=110,
            trading_days=1250,
        )
        self.assertEqual(r.score, 2.0)

    def test_exact_50_points_scores_3(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=160,
            spy_start_price=100, spy_end_price=110,
            trading_days=1250,
        )
        self.assertEqual(r.score, 3.0)

    def test_exact_100_points_scores_4(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=210,
            spy_start_price=100, spy_end_price=110,
            trading_days=1250,
        )
        self.assertEqual(r.score, 4.0)

    def test_above_100_points_scores_4(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=300,
            spy_start_price=100, spy_end_price=100,
            trading_days=1250,
        )
        self.assertEqual(r.score, 4.0)

    def test_positive_but_below_25_scores_1(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=115,
            spy_start_price=100, spy_end_price=110,
            trading_days=1250,
        )
        self.assertEqual(r.score, 1.0)

    def test_short_listing_unavailable(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=110,
            spy_start_price=100, spy_end_price=100,
            trading_days=100,
        )
        self.assertEqual(r.available, 0.0)
        self.assertTrue(r.short_listing_flag)

    def test_zero_trading_days_unavailable(self) -> None:
        r = score_s01(trading_days=0)
        self.assertEqual(r.available, 0.0)

    def test_missing_prices_unavailable(self) -> None:
        r = score_s01(stock_start_price=None, trading_days=300)
        self.assertEqual(r.available, 0.0)

    def test_mismatched_dates_handled(self) -> None:
        r = score_s01(
            stock_start_price=100, stock_end_price=120,
            spy_start_price=100, spy_end_price=130,
            trading_days=300,
            start_date="2021-01-01", end_date="2026-01-01",
        )
        self.assertEqual(r.score, 0.0)


# ===================================================================
# S02 — Shareholder-friendly actions (max 3)
# ===================================================================


class S02ShareholderTests(unittest.TestCase):
    def test_diluted_shares_decline_buyback_point(self) -> None:
        r = score_s02(
            diluted_shares_current=98_000_000,
            diluted_shares_prior=100_000_000,
        )
        self.assertEqual(r.buyback_point, 1)
        self.assertLess(r.diluted_share_change_pct, -0.01)

    def test_net_repurchases_to_market_cap_buyback_point(self) -> None:
        r = score_s02(
            share_repurchases_ttm=5_000_000,
            share_issuance_ttm=1_000_000,
            market_cap=100_000_000,
        )
        self.assertEqual(r.buyback_point, 1)

    def test_repurchases_offset_by_issuance_no_buyback(self) -> None:
        r = score_s02(
            share_repurchases_ttm=5_000_000,
            share_issuance_ttm=5_000_000,
            market_cap=100_000_000,
        )
        self.assertEqual(r.buyback_point, 0)

    def test_dividend_growth_no_cut_point(self) -> None:
        r = score_s02(
            dividend_per_share_ttm=1.10,
            dividend_per_share_prior=1.00,
            dividend_data_valid=True,
        )
        self.assertEqual(r.dividend_point, 1)

    def test_dividend_growth_with_cut_no_point(self) -> None:
        r = score_s02(
            dividend_per_share_ttm=1.10,
            dividend_per_share_prior=1.00,
            dividend_cut_flag=True,
            dividend_data_valid=True,
        )
        self.assertEqual(r.dividend_point, 0)

    def test_non_dividend_payer_zero_not_unavailable(self) -> None:
        r = score_s02(dividend_data_valid=True, dividend_per_share_ttm=0, dividend_per_share_prior=1.0)
        self.assertEqual(r.dividend_point, 0)
        self.assertEqual(r.dividend_available, 1)
        self.assertEqual(r.available, 1.0)

    def test_debt_decline_point(self) -> None:
        r = score_s02(
            total_debt_current=90_000_000,
            total_debt_prior=100_000_000,
        )
        self.assertEqual(r.debt_reduction_point, 1)

    def test_effectively_debt_free_point(self) -> None:
        r = score_s02(
            total_debt_current=50_000,
            total_assets=10_000_000,
        )
        self.assertTrue(r.effectively_debt_free_flag)
        self.assertEqual(r.debt_reduction_point, 1)

    def test_all_three_points(self) -> None:
        r = score_s02(
            diluted_shares_current=97_000_000,
            diluted_shares_prior=100_000_000,
            dividend_per_share_ttm=1.10,
            dividend_per_share_prior=1.00,
            dividend_data_valid=True,
            total_debt_current=90_000_000,
            total_debt_prior=100_000_000,
        )
        self.assertEqual(r.score, 3.0)
        self.assertEqual(r.available, 3.0)

    def test_each_subtest_independently_available(self) -> None:
        r = score_s02(
            total_debt_current=90_000_000,
            total_debt_prior=100_000_000,
        )
        self.assertEqual(r.score, 1.0)
        # Only debt data present: buyback=0, dividend=0, debt=1
        self.assertEqual(r.available, 1.0)


# ===================================================================
# S03 — Earnings-expectation record (max 4)
# ===================================================================


class S03EarningsSurpriseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_args: dict = {}

    def _score(self, **kwargs) -> "score_s03":
        args = {
            "q1_reported": None, "q1_estimated": None,
            "q2_reported": None, "q2_estimated": None,
            "q3_reported": None, "q3_estimated": None,
            "q4_reported": None, "q4_estimated": None,
        }
        args.update(kwargs)
        return score_s03(**args)

    def test_large_beat_scores_1(self) -> None:
        r = self._score(q1_reported=1.50, q1_estimated=1.00)
        self.assertEqual(r.q1_point, 1.0)

    def test_small_beat_scores_0_5(self) -> None:
        r = self._score(q1_reported=1.06, q1_estimated=1.00)
        self.assertEqual(r.q1_point, 0.5)

    def test_exact_meet_scores_0(self) -> None:
        r = self._score(q1_reported=1.00, q1_estimated=1.00)
        self.assertEqual(r.q1_point, 0.0)

    def test_miss_scores_0(self) -> None:
        r = self._score(q1_reported=0.90, q1_estimated=1.00)
        self.assertEqual(r.q1_point, 0.0)

    def test_zero_estimate_large_beat_scores_1(self) -> None:
        r = self._score(q1_reported=0.05, q1_estimated=0)
        self.assertEqual(r.q1_point, 1.0)

    def test_zero_estimate_small_beat_scores_0_5(self) -> None:
        r = self._score(q1_reported=0.01, q1_estimated=0)
        self.assertEqual(r.q1_point, 0.5)

    def test_one_quarter_available_only(self) -> None:
        r = self._score(q1_reported=1.50, q1_estimated=1.00)
        self.assertEqual(r.available, 1.0)
        self.assertEqual(r.score, 1.0)

    def test_three_quarters_available(self) -> None:
        r = self._score(
            q1_reported=1.50, q1_estimated=1.00,
            q2_reported=1.10, q2_estimated=1.05,
            q3_reported=0.90, q3_estimated=1.00,
        )
        self.assertEqual(r.available, 3.0)
        self.assertAlmostEqual(r.score, 1.5)

    def test_four_quarters_available(self) -> None:
        r = self._score(
            q1_reported=1.50, q1_estimated=1.00,
            q2_reported=2.00, q2_estimated=1.80,
            q3_reported=2.50, q3_estimated=2.30,
            q4_reported=1.10, q4_estimated=1.00,
        )
        self.assertEqual(r.available, 4.0)

    def test_no_data_unavailable(self) -> None:
        r = self._score()
        self.assertEqual(r.available, 0.0)
        self.assertEqual(r.score, 0.0)


if __name__ == "__main__":
    unittest.main()
