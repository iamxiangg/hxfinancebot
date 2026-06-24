from __future__ import annotations

import unittest

from funnel.btd_enrichment import (
    BtdMetrics,
    build_btd_summary,
    calculate_btd_score,
    compact_number,
    percent_text,
)


class BtdEnrichmentTests(unittest.TestCase):
    def test_high_quality_metrics_score_well(self) -> None:
        metrics = BtdMetrics(
            ticker="MSFT",
            enterprise_value=3_000_000_000,
            total_revenue=1_000_000_000,
            revenue_growth=0.22,
            gross_margin=0.72,
            ebitda_margin=0.31,
        )

        self.assertGreaterEqual(calculate_btd_score(metrics), 85)

    def test_missing_metrics_do_not_crash(self) -> None:
        metrics = BtdMetrics(ticker="XYZ")

        self.assertEqual(calculate_btd_score(metrics), 0)
        self.assertEqual(build_btd_summary(metrics, 0), "BTD 0/100")

    def test_format_helpers(self) -> None:
        self.assertEqual(percent_text(0.1234), "12.3%")
        self.assertEqual(compact_number(1_250_000_000), "1.2B")


if __name__ == "__main__":
    unittest.main()
