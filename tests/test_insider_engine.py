from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

from scanners.insider.engine import (
    InsiderConfig,
    QualifyingPurchase,
    parse_filing_purchases,
    score_cluster,
)
from scanners.insider.parser import ParsedOwnershipFiling, ReportingOwner, NonDerivativeTransaction


class InsiderEngineTests(unittest.TestCase):
    def test_private_placement_purchase_is_excluded(self) -> None:
        filing = ParsedOwnershipFiling(
            accession="0001",
            issuer_cik="1",
            issuer_ticker="TEAM",
            acceptance_datetime="2026-06-20T00:00:00",
            reporting_owners=[
                ReportingOwner(
                    cik="10",
                    name="Jane Doe",
                    is_director=True,
                    is_officer=True,
                    is_ten_percent_owner=False,
                    officer_title="CEO",
                )
            ],
            transactions=[
                NonDerivativeTransaction(
                    security_title="Common Stock",
                    transaction_date="2026-06-20",
                    transaction_code="P",
                    acquired_disposed="A",
                    shares=1000,
                    price_per_share=10,
                    shares_owned_after=5000,
                    direct_or_indirect="D",
                    footnotes=["Private placement financing"],
                )
            ],
        )

        purchases, ledger_rows = parse_filing_purchases(filing)

        self.assertEqual(purchases, [])
        self.assertEqual(ledger_rows[0]["decision"], "EXCLUDED")

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_ceo_cfo_cluster_can_be_actionable(self, _mock_market) -> None:
        purchases = [
            QualifyingPurchase(
                ticker="TEAM",
                issuer_cik="1",
                accession="a1",
                owner_cik="10",
                owner_name="CEO",
                owner_role="CEO",
                owner_is_operating=True,
                transaction_date=date(2026, 6, 20),
                security_title="Common Stock",
                shares=10_000,
                price_per_share=40,
                transaction_value=400_000,
                direct_or_indirect="D",
                plan_10b5_1=False,
                confidence="OPEN_MARKET_HIGH_CONFIDENCE",
                shares_owned_after=200_000,
                transaction_row_count=1,
                footnotes=[],
            ),
            QualifyingPurchase(
                ticker="TEAM",
                issuer_cik="1",
                accession="a2",
                owner_cik="11",
                owner_name="CFO",
                owner_role="CFO",
                owner_is_operating=True,
                transaction_date=date(2026, 6, 24),
                security_title="Common Stock",
                shares=15_000,
                price_per_share=42,
                transaction_value=630_000,
                direct_or_indirect="D",
                plan_10b5_1=False,
                confidence="OPEN_MARKET_HIGH_CONFIDENCE",
                shares_owned_after=150_000,
                transaction_row_count=1,
                footnotes=[],
            ),
        ]

        result = score_cluster("TEAM", purchases, InsiderConfig())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn(result.classification, {"actionable", "wait"})
        self.assertEqual(result.unique_insiders, 2)
        self.assertGreaterEqual(result.total_score, 65.0)


if __name__ == "__main__":
    unittest.main()
