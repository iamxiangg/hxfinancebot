from __future__ import annotations

import unittest

from funnel.btd_enrichment import (
    BtdMetrics,
    build_btd_summary,
    calculate_btd_components,
    calculate_btd_score,
    calculate_btd_ratio,
    compact_number,
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

        self.assertEqual(calculate_btd_score(metrics), 0.0)
        self.assertEqual(build_btd_summary(metrics, 0.0), "BTD 0.0")

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


if __name__ == "__main__":
    unittest.main()
