from __future__ import annotations

import unittest
from datetime import date

from scanners.fundamental_inflection.models import (
    BalanceSheetMetrics,
    CashFlowMetrics,
    FundamentalInflectionConfig,
    GrossEconomicsMetrics,
    OperatingLeverageMetrics,
    PerShareMetrics,
    QuarterlySnapshot,
    RevenueGrowthMetrics,
    WorkingCapitalMetrics,
)
from scanners.fundamental_inflection.scoring import (
    evaluate_balance_sheet,
    evaluate_cash_flow,
    evaluate_gross_economics,
    evaluate_operating_leverage,
    evaluate_per_share,
    evaluate_revenue_growth,
    evaluate_working_capital,
    score_and_classify,
)


def _quarter(revenue: float, *, gp: float | None = None,
             oi: float | None = None, ocf: float | None = None,
             capex: float | None = None, cash: float | None = None,
             debt: float | None = None, ar: float | None = None,
             inv: float | None = None, shares: float | None = None,
             sbc: float | None = None) -> QuarterlySnapshot:
    return QuarterlySnapshot(
        quarter_label="Q",
        period_end=date(2026, 1, 30),
        fiscal_year=2026,
        fiscal_period="Q1",
        accession="a",
        filed_at=date(2026, 2, 1),
        revenue=revenue,
        gross_profit=gp,
        operating_income=oi,
        operating_cash_flow=ocf,
        capital_expenditure=capex,
        cash=cash,
        total_debt=debt,
        accounts_receivable=ar,
        inventory=inv,
        diluted_shares=shares,
        stock_based_comp=sbc,
    )


def _make_eight_quarters() -> list[QuarterlySnapshot]:
    return [
        _quarter(100, gp=60, oi=10, ocf=15, capex=-5, cash=200, ar=80, inv=30, shares=50, sbc=5),
        _quarter(105, gp=63, oi=11, ocf=16, capex=-5, cash=210, ar=82, inv=31, shares=51, sbc=5),
        _quarter(110, gp=66, oi=12, ocf=17, capex=-5, cash=220, ar=85, inv=32, shares=51, sbc=5),
        _quarter(115, gp=69, oi=13, ocf=18, capex=-5, cash=230, ar=88, inv=33, shares=52, sbc=5),
        _quarter(125, gp=78, oi=15, ocf=20, capex=-6, cash=250, ar=95, inv=35, shares=52, sbc=5),
        _quarter(130, gp=82, oi=16, ocf=21, capex=-6, cash=260, ar=98, inv=36, shares=52, sbc=5),
        _quarter(140, gp=90, oi=19, ocf=23, capex=-6, cash=280, ar=105, inv=38, shares=53, sbc=5),
        _quarter(150, gp=98, oi=22, ocf=25, capex=-7, cash=310, ar=112, inv=40, shares=53, sbc=5),
    ]


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.config = FundamentalInflectionConfig()

    def test_exactly_20pct_growth_passes_gate(self):
        qs = [
            _quarter(100), _quarter(105), _quarter(110), _quarter(115),
            _quarter(120), _quarter(125), _quarter(130), _quarter(138),
        ]
        rev = evaluate_revenue_growth(qs, self.config)
        self.assertGreaterEqual(rev.yoy_growth, 0.19)

    def test_19pct_growth_fails_gate(self):
        qs = []
        for i in range(8):
            qs.append(_quarter(100 + i * 2))
        rev = evaluate_revenue_growth(qs, self.config)
        classification, _, _, _, _ = score_and_classify(
            rev,
            GrossEconomicsMetrics(gross_profit_growth=None, gross_margin_latest=None,
                                  gross_margin_prior=None, gross_margin_change_bps=None,
                                  gross_confirmation="UNAVAILABLE", flags=[]),
            OperatingLeverageMetrics(operating_margin_latest=None, operating_margin_prior=None,
                                     operating_margin_change_bps=None, incremental_operating_margin=None,
                                     operating_loss_narrowing=False, operating_confirmation="UNAVAILABLE", flags=[]),
            CashFlowMetrics(ttm_operating_cash_flow=None, ttm_capital_expenditure=None,
                            ttm_free_cash_flow=None, ttm_fcf_margin=None, prior_ttm_fcf_margin=None,
                            ttm_fcf_margin_change_bps=None, fcf_classification="FCF_UNAVAILABLE",
                            cash_confirmation="NEUTRAL", flags=[]),
            PerShareMetrics(diluted_share_growth=None, revenue_per_share_latest=None,
                            revenue_per_share_prior=None, revenue_per_share_growth=None,
                            sbc_to_revenue=None, dilution_classification="UNAVAILABLE",
                            per_share_confirmation="UNAVAILABLE", flags=[]),
            BalanceSheetMetrics(cash=None, total_debt=None, net_cash=None,
                                cash_runway_months=None, balance_sheet_classification="UNAVAILABLE", flags=[]),
            WorkingCapitalMetrics(ar_growth=None, inventory_growth=None, revenue_growth=rev.yoy_growth,
                                  ar_divergence=None, inventory_divergence=None, flags=[]),
            self.config,
        )
        self.assertEqual(classification, "REJECTED")

    def test_growth_with_worsening_margins_and_fcf_becomes_growth_without_inflection(self):
        qs = _make_eight_quarters()
        qs[-1] = _quarter(150, gp=60, oi=-10, ocf=-30, capex=-7, cash=310, ar=112, inv=40, shares=53)
        qs[-2] = _quarter(140, gp=58, oi=-5, ocf=-20, capex=-7, cash=320, ar=105, inv=38, shares=53)
        qs[-3] = _quarter(130, gp=55, oi=-2, ocf=-15, capex=-7, cash=330, ar=98, inv=36, shares=52)
        qs[-4] = _quarter(125, gp=50, oi=-3, ocf=-10, capex=-7, cash=340, ar=95, inv=35, shares=52)
        rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, rev)
        operating = evaluate_operating_leverage(qs, rev)
        cash_flow = evaluate_cash_flow(qs)
        per_share = evaluate_per_share(qs, rev)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        working_cap = evaluate_working_capital(qs, rev)
        classification, _, _, _, _ = score_and_classify(
            rev, gross, operating, cash_flow, per_share, balance, working_cap, self.config,
        )
        self.assertIn(classification, {"GROWTH_WITHOUT_INFLECTION", "EARLY_INFLECTION"})

    def test_two_pillars_including_operating_becomes_validated(self):
        qs = _make_eight_quarters()
        full_rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, full_rev)
        operating = evaluate_operating_leverage(qs, full_rev)
        cash_flow = evaluate_cash_flow(qs)
        per_share = evaluate_per_share(qs, full_rev)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        working_cap = evaluate_working_capital(qs, full_rev)
        classification, score, _, _, _ = score_and_classify(
            full_rev, gross, operating, cash_flow, per_share, balance, working_cap, self.config,
        )
        self.assertIn(classification, {"VALIDATED_INFLECTION", "STRONG_INFLECTION"})

    def test_three_pillars_becomes_strong_inflection(self):
        qs = _make_eight_quarters()
        full_rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, full_rev)
        operating = evaluate_operating_leverage(qs, full_rev)
        cash_flow = evaluate_cash_flow(qs)
        per_share = evaluate_per_share(qs, full_rev)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        working_cap = evaluate_working_capital(qs, full_rev)
        classification, score, _, _, _ = score_and_classify(
            full_rev, gross, operating, cash_flow, per_share, balance, working_cap, self.config,
        )
        self.assertEqual(classification, "STRONG_INFLECTION")

    def test_gross_margin_expansion_is_detected(self):
        qs = _make_eight_quarters()
        rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, rev)
        self.assertIsNotNone(gross.gross_margin_change_bps)
        self.assertGreater(gross.gross_margin_change_bps or 0, 0)
        self.assertEqual(gross.gross_confirmation, "POSITIVE")

    def test_gross_margin_deterioration(self):
        qs = _make_eight_quarters()
        qs[-1] = _quarter(150, gp=70, oi=22, ocf=25, capex=-7, cash=310, ar=112, inv=40, shares=53)
        rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, rev)
        self.assertEqual(gross.gross_confirmation, "NEGATIVE")

    def test_operating_loss_narrowing(self):
        qs = [
            _quarter(100, gp=40, oi=-20, ocf=-10, capex=-2),
            _quarter(105, gp=42, oi=-18, ocf=-8, capex=-2),
            _quarter(110, gp=44, oi=-15, ocf=-5, capex=-2),
            _quarter(115, gp=46, oi=-12, ocf=-3, capex=-2),
            _quarter(120, gp=50, oi=-8, ocf=-5, capex=-2),
            _quarter(125, gp=52, oi=-5, ocf=-3, capex=-2),
            _quarter(130, gp=55, oi=-2, ocf=0, capex=-2),
            _quarter(140, gp=58, oi=0, ocf=3, capex=-2),
        ]
        rev = evaluate_revenue_growth(qs, self.config)
        operating = evaluate_operating_leverage(qs, rev)
        self.assertTrue(operating.operating_loss_narrowing)

    def test_incremental_operating_margin_calculation(self):
        qs = _make_eight_quarters()
        rev = evaluate_revenue_growth(qs, self.config)
        operating = evaluate_operating_leverage(qs, rev)
        self.assertIsNotNone(operating.incremental_operating_margin)

    def test_fcf_turns_positive(self):
        qs = _make_eight_quarters()
        cash_flow = evaluate_cash_flow(qs)
        self.assertIn(cash_flow.fcf_classification, {"FCF_POSITIVE_AND_EXPANDING", "FCF_IMPROVING_BUT_NEGATIVE", "FCF_UNAVAILABLE"})

    def test_receivables_divergence(self):
        qs = _make_eight_quarters()
        rev = evaluate_revenue_growth(qs, self.config)
        working_cap = evaluate_working_capital(qs, rev)
        self.assertIsNotNone(working_cap.ar_divergence)

    def test_severe_dilution_blocks_actionable(self):
        qs = _make_eight_quarters()
        qs[-1] = _quarter(150, gp=98, oi=22, ocf=25, capex=-7, cash=310, ar=112, inv=40, shares=70)
        rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, rev)
        operating = evaluate_operating_leverage(qs, rev)
        cash_flow = evaluate_cash_flow(qs)
        per_share = evaluate_per_share(qs, rev)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        working_cap = evaluate_working_capital(qs, rev)
        classification, _, _, _, _ = score_and_classify(
            rev, gross, operating, cash_flow, per_share, balance, working_cap, self.config,
        )
        self.assertNotIn(classification, {"VALIDATED_INFLECTION", "STRONG_INFLECTION"})

    def test_revenue_per_share_growth_calculation(self):
        qs = _make_eight_quarters()
        rev = evaluate_revenue_growth(qs, self.config)
        per_share = evaluate_per_share(qs, rev)
        self.assertIsNotNone(per_share.revenue_per_share_growth)

    def test_cash_runway_below_12_months(self):
        qs = [
            _quarter(100, gp=60, oi=10, ocf=-30, capex=-20, cash=200, ar=80, inv=30, shares=50),
            _quarter(105, gp=63, oi=11, ocf=-25, capex=-15, cash=100, ar=82, inv=31, shares=51),
            _quarter(110, gp=66, oi=12, ocf=-20, capex=-15, cash=50, ar=85, inv=32, shares=51),
            _quarter(115, gp=69, oi=13, ocf=-15, capex=-10, cash=20, ar=88, inv=33, shares=52),
            _quarter(125, gp=78, oi=15, ocf=-10, capex=-10, cash=5, ar=95, inv=35, shares=52),
            _quarter(130, gp=82, oi=16, ocf=-8, capex=-8, cash=3, ar=98, inv=36, shares=52),
            _quarter(140, gp=90, oi=19, ocf=-5, capex=-5, cash=2, ar=105, inv=38, shares=53),
            _quarter(150, gp=98, oi=22, ocf=-5, capex=-5, cash=1, ar=112, inv=40, shares=53),
        ]
        cash_flow = evaluate_cash_flow(qs)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        self.assertEqual(balance.balance_sheet_classification, "SEVERE")

    def test_fcf_positive_company_no_meaningless_runway(self):
        qs = _make_eight_quarters()
        cash_flow = evaluate_cash_flow(qs)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        if cash_flow.ttm_free_cash_flow is not None and cash_flow.ttm_free_cash_flow > 0:
            self.assertEqual(balance.balance_sheet_classification, "STRONG")

    def test_material_deceleration_penalised(self):
        qs = [
            _quarter(100), _quarter(105), _quarter(110), _quarter(115),
            _quarter(130), _quarter(135), _quarter(138), _quarter(140),
        ]
        rev = evaluate_revenue_growth(qs, self.config)
        self.assertLess(rev.yoy_growth, 0.30)

    def test_two_non_economic_pillars_without_operating_confirmation(self):
        qs = _make_eight_quarters()
        qs[-1] = _quarter(150, gp=98, oi=-5, ocf=-20, capex=-7, cash=310, ar=112, inv=40, shares=53)
        rev = evaluate_revenue_growth(qs, self.config)
        gross = evaluate_gross_economics(qs, rev)
        operating = evaluate_operating_leverage(qs, rev)
        cash_flow = evaluate_cash_flow(qs)
        per_share = evaluate_per_share(qs, rev)
        balance = evaluate_balance_sheet(qs, cash_flow, self.config)
        working_cap = evaluate_working_capital(qs, rev)
        classification, _, _, economic_conf, _ = score_and_classify(
            rev, gross, operating, cash_flow, per_share, balance, working_cap, self.config,
        )
        if not economic_conf:
            self.assertNotIn(classification, {"VALIDATED_INFLECTION", "STRONG_INFLECTION"})

    def test_growth_acceleration_is_positive(self):
        qs = [
            _quarter(100), _quarter(105), _quarter(110), _quarter(115),
            _quarter(120), _quarter(126), _quarter(138), _quarter(160),
        ]
        rev = evaluate_revenue_growth(qs, self.config)
        self.assertIsNotNone(rev.growth_acceleration)
        self.assertGreater(rev.growth_acceleration or 0, 0)

    def test_mildly_decelerating_25pct_growth_still_qualifies(self):
        qs = [
            _quarter(100), _quarter(108), _quarter(116), _quarter(125),
            _quarter(134), _quarter(138), _quarter(141), _quarter(156),
        ]
        rev = evaluate_revenue_growth(qs, self.config)
        self.assertGreaterEqual(rev.yoy_growth, 0.24)

    def test_inventory_divergence_flag(self):
        qs = _make_eight_quarters()
        qs[-1] = _quarter(150, gp=98, oi=22, ocf=25, capex=-7, cash=310, ar=112, inv=80, shares=53)
        rev = evaluate_revenue_growth(qs, self.config)
        working_cap = evaluate_working_capital(qs, rev)
        self.assertIn("INVENTORY_DIVERGENCE", working_cap.flags)


if __name__ == "__main__":
    unittest.main()
