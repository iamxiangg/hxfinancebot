from __future__ import annotations

import unittest

from funnel.btd_enrichment import (
    BtdMetrics,
    build_btd_summary,
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


if __name__ == "__main__":
    unittest.main()
