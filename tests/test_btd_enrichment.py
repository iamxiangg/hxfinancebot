from __future__ import annotations

import unittest

from funnel.btd_enrichment import (
    BtdMetrics,
    build_btd_summary,
    calculate_btd_components,
    calculate_btd_score,
    calculate_btd_ratio,
    compact_number,
    determine_btd_applicability,
    metrics_to_candidate_updates,
    percent_text,
)


class BtdEnrichmentTests(unittest.TestCase):
    def test_high_quality_metrics_score_well(self) -> None:
        metrics = BtdMetrics(
            ticker="MSFT",
            enterprise_value=30_000_000_000,
            total_revenue=10_000_000_000,
            revenue_growth=0.25,
            gross_margin=0.60,
        )

        ratio = calculate_btd_ratio(metrics)
        self.assertIsNotNone(ratio)
        assert ratio is not None
        self.assertLess(ratio, 0.3)
        self.assertAlmostEqual(calculate_btd_score(metrics), round(ratio, 2))

    def test_missing_metrics_do_not_crash(self) -> None:
        metrics = BtdMetrics(ticker="XYZ")

        self.assertIsNone(calculate_btd_score(metrics))
        self.assertEqual(build_btd_summary(metrics, None), "BTD unavailable")
        self.assertEqual(determine_btd_applicability(metrics), "UNAVAILABLE")
        self.assertEqual(metrics_to_candidate_updates(metrics)["BTD Ratio"], "")

    def test_format_helpers(self) -> None:
        self.assertEqual(percent_text(0.1234), "12.3%")
        self.assertEqual(compact_number(1_250_000_000), "1.2B")

    def test_btd_components_explain_formula(self) -> None:
        metrics = BtdMetrics(
            ticker="TEAM",
            enterprise_value=20_750_000_000,
            total_revenue=6_190_000_000,
            revenue_growth=0.317,
            gross_margin=0.8481,
        )

        components = calculate_btd_components(metrics)

        self.assertEqual(components["EV (B)"], 20.75)
        self.assertEqual(components["Revenue TTM (B)"], 6.19)
        self.assertEqual(components["Gross Margin %"], 84.8)
        self.assertEqual(components["Revenue Growth %"], 31.7)
        self.assertIn("20.75 / (6.19 * 0.8481 * 31.7)", components["BTD Formula"])
        self.assertAlmostEqual(calculate_btd_score(metrics), 0.12)

    def test_not_applicable_business_model_is_identified(self) -> None:
        metrics = BtdMetrics(
            ticker="O",
            company_name="Realty Income",
            sector="Real Estate",
            industry="REIT - Retail",
            quote_type="equity",
            enterprise_value=1,
            total_revenue=1,
            revenue_growth=0.1,
            gross_margin=0.5,
        )

        self.assertEqual(determine_btd_applicability(metrics), "NOT_APPLICABLE")

    def test_non_positive_growth_and_margin_do_not_pass(self) -> None:
        metrics = BtdMetrics(
            ticker="XYZ",
            enterprise_value=5_000_000_000,
            total_revenue=1_000_000_000,
            revenue_growth=0.0,
            gross_margin=-0.2,
        )

        self.assertIsNone(calculate_btd_ratio(metrics))
        self.assertIsNone(calculate_btd_score(metrics))


if __name__ == "__main__":
    unittest.main()
