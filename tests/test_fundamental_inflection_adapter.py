from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from funnel.fundamental_inflection_adapter import result_to_signal, run_fundamental_inflection_adapter
from scanners.fundamental_inflection.models import InflectionResult


class InflectionAdapterTests(unittest.TestCase):
    def test_strong_inflection_maps_to_actionable(self):
        result = InflectionResult(
            ticker="TEAM",
            classification="STRONG_INFLECTION",
            total_score=85.0,
            latest_filing_accession="a1",
            filing_date=date(2026, 4, 15),
            latest_quarterly_revenue=150_000_000,
            revenue_growth_yoy=0.30,
            prior_quarter_growth=0.25,
            growth_acceleration=0.05,
            gross_profit_growth=0.35,
            gross_margin_change_bps=150.0,
            operating_margin_change_bps=300.0,
            incremental_operating_margin=0.25,
            ttm_fcf_margin=0.08,
            ttm_fcf_margin_change_bps=200.0,
            diluted_share_growth=0.02,
            revenue_per_share_growth=0.27,
            cash=500_000_000,
            debt=100_000_000,
            cash_runway_months=None,
            positive_pillars=["growth", "gross_economics", "operating_leverage", "cash_flow"],
            pilllar_count=4,
            economic_confirmation=True,
            risk_flags=[],
            data_confidence="medium",
            valid_for_days=30,
            reason="Strong inflection: 4 pillars",
        )
        signal = result_to_signal(result, "2026-06-28T12:00:00+00:00")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.scanner, "fundamental_inflection")
        self.assertEqual(signal.classification, "actionable")
        self.assertEqual(signal.details["inflection_classification"], "STRONG_INFLECTION")

    def test_validated_inflection_maps_to_actionable(self):
        result = InflectionResult(
            ticker="TEAM",
            classification="VALIDATED_INFLECTION",
            total_score=75.0,
            latest_filing_accession="a1",
            filing_date=date(2026, 4, 15),
            latest_quarterly_revenue=150_000_000,
            revenue_growth_yoy=0.25,
            prior_quarter_growth=0.22,
            growth_acceleration=0.03,
            gross_profit_growth=None,
            gross_margin_change_bps=None,
            operating_margin_change_bps=250.0,
            incremental_operating_margin=None,
            ttm_fcf_margin=None,
            ttm_fcf_margin_change_bps=None,
            diluted_share_growth=None,
            revenue_per_share_growth=None,
            cash=None,
            debt=None,
            cash_runway_months=None,
            positive_pillars=["growth", "operating_leverage"],
            pilllar_count=2,
            economic_confirmation=True,
            risk_flags=[],
            data_confidence="medium",
            valid_for_days=30,
            reason="Validated inflection: 2 pillars",
        )
        signal = result_to_signal(result, "2026-06-28T12:00:00+00:00")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.classification, "actionable")

    def test_early_inflection_maps_to_near_miss(self):
        result = InflectionResult(
            ticker="TEAM",
            classification="EARLY_INFLECTION",
            total_score=60.0,
            latest_filing_accession="a1",
            filing_date=date(2026, 4, 15),
            latest_quarterly_revenue=150_000_000,
            revenue_growth_yoy=0.22,
            prior_quarter_growth=0.20,
            growth_acceleration=0.02,
            gross_profit_growth=None,
            gross_margin_change_bps=None,
            operating_margin_change_bps=None,
            incremental_operating_margin=None,
            ttm_fcf_margin=None,
            ttm_fcf_margin_change_bps=None,
            diluted_share_growth=None,
            revenue_per_share_growth=None,
            cash=None,
            debt=None,
            cash_runway_months=None,
            positive_pillars=["growth"],
            pilllar_count=1,
            economic_confirmation=True,
            risk_flags=[],
            data_confidence="medium",
            valid_for_days=30,
            reason="Early inflection: 1 pillar",
        )
        signal = result_to_signal(result, "2026-06-28T12:00:00+00:00")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.classification, "near_miss")

    def test_growth_without_inflection_is_suppressed(self):
        result = InflectionResult(
            ticker="TEAM",
            classification="GROWTH_WITHOUT_INFLECTION",
            total_score=40.0,
            latest_filing_accession="a1",
            filing_date=date(2026, 4, 15),
            latest_quarterly_revenue=150_000_000,
            revenue_growth_yoy=0.25,
            prior_quarter_growth=0.22,
            growth_acceleration=0.03,
            gross_profit_growth=None,
            gross_margin_change_bps=None,
            operating_margin_change_bps=None,
            incremental_operating_margin=None,
            ttm_fcf_margin=None,
            ttm_fcf_margin_change_bps=None,
            diluted_share_growth=None,
            revenue_per_share_growth=None,
            cash=None,
            debt=None,
            cash_runway_months=None,
            positive_pillars=["growth"],
            pilllar_count=1,
            economic_confirmation=False,
            risk_flags=[],
            data_confidence="medium",
            valid_for_days=30,
            reason="Growth without inflection",
        )
        signal = result_to_signal(result, "2026-06-28T12:00:00+00:00")
        self.assertIsNone(signal)

    def test_rejected_is_suppressed(self):
        result = InflectionResult(
            ticker="TEAM",
            classification="REJECTED",
            total_score=10.0,
            latest_filing_accession="a1",
            filing_date=date(2026, 4, 15),
            latest_quarterly_revenue=150_000_000,
            revenue_growth_yoy=0.10,
            prior_quarter_growth=None,
            growth_acceleration=None,
            gross_profit_growth=None,
            gross_margin_change_bps=None,
            operating_margin_change_bps=None,
            incremental_operating_margin=None,
            ttm_fcf_margin=None,
            ttm_fcf_margin_change_bps=None,
            diluted_share_growth=None,
            revenue_per_share_growth=None,
            cash=None,
            debt=None,
            cash_runway_months=None,
            positive_pillars=[],
            pilllar_count=0,
            economic_confirmation=False,
            risk_flags=[],
            data_confidence="low",
            valid_for_days=30,
            reason="Rejected",
        )
        signal = result_to_signal(result, "2026-06-28T12:00:00+00:00")
        self.assertIsNone(signal)

    @patch("funnel.fundamental_inflection_adapter.run_inflection_scan")
    def test_adapter_omits_rejected_results(self, mock_scan):
        mock_scan.return_value = [
            InflectionResult(
                ticker="TEAM",
                classification="VALIDATED_INFLECTION",
                total_score=75.0,
                latest_filing_accession="a1",
                filing_date=date(2026, 4, 15),
                latest_quarterly_revenue=150_000_000,
                revenue_growth_yoy=0.25,
                prior_quarter_growth=0.22,
                growth_acceleration=0.03,
                gross_profit_growth=None,
                gross_margin_change_bps=None,
                operating_margin_change_bps=250.0,
                incremental_operating_margin=None,
                ttm_fcf_margin=None,
                ttm_fcf_margin_change_bps=None,
                diluted_share_growth=None,
                revenue_per_share_growth=None,
                cash=None,
                debt=None,
                cash_runway_months=None,
                positive_pillars=["growth", "operating_leverage"],
                pilllar_count=2,
                economic_confirmation=True,
                risk_flags=[],
                data_confidence="medium",
                valid_for_days=30,
                reason="good",
            ),
            InflectionResult(
                ticker="REJECTED",
                classification="REJECTED",
                total_score=10.0,
                latest_filing_accession="a2",
                filing_date=date(2026, 4, 15),
                latest_quarterly_revenue=10_000_000,
                revenue_growth_yoy=0.05,
                prior_quarter_growth=None,
                growth_acceleration=None,
                gross_profit_growth=None,
                gross_margin_change_bps=None,
                operating_margin_change_bps=None,
                incremental_operating_margin=None,
                ttm_fcf_margin=None,
                ttm_fcf_margin_change_bps=None,
                diluted_share_growth=None,
                revenue_per_share_growth=None,
                cash=None,
                debt=None,
                cash_runway_months=None,
                positive_pillars=[],
                pilllar_count=0,
                economic_confirmation=False,
                risk_flags=[],
                data_confidence="low",
                valid_for_days=30,
                reason="bad",
            ),
        ]
        signals, count = run_fundamental_inflection_adapter()
        self.assertEqual(count, 2)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].ticker, "TEAM")


if __name__ == "__main__":
    unittest.main()
