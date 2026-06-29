from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from unittest.mock import patch

from providers.sec.models import FilingMetadata, SECInsiderTransaction
from scanners.insider.engine import InsiderConfig, run_insider_scan


class _IntegrationProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def daily_index_filings(self, day: date, *, forms=None):
        self.calls.append(f"daily:{day.isoformat()}")
        return [
            FilingMetadata(
                ticker="",
                cik="0001650372",
                accession="0001650372-24-000999",
                form="4",
                filed_at=datetime(2026, 6, 27, tzinfo=UTC),
                report_date=date(2026, 6, 27),
                primary_document="index.txt",
                is_amendment=False,
                source_url="https://www.sec.gov/Archives/example.txt",
            )
        ]

    def form4_transactions(self, filing: FilingMetadata):
        self.calls.append(f"form4:{filing.accession}")
        return [
            SECInsiderTransaction(
                ticker="TEAM",
                issuer_cik="1650372",
                accession=filing.accession,
                owner_cik="2002",
                owner_name="Jane Doe",
                owner_is_director=True,
                owner_is_officer=True,
                owner_is_ten_percent_owner=False,
                officer_title="CEO",
                security_title="Common Stock",
                transaction_date=date(2026, 6, 27),
                transaction_code="P",
                acquired_disposed="A",
                shares=12_000,
                price_per_share=40.0,
                shares_owned_after=110_000.0,
                direct_or_indirect="D",
                footnotes=[],
            ),
            SECInsiderTransaction(
                ticker="TEAM",
                issuer_cik="1650372",
                accession=filing.accession,
                owner_cik="2003",
                owner_name="John Doe",
                owner_is_director=False,
                owner_is_officer=True,
                owner_is_ten_percent_owner=False,
                officer_title="CFO",
                security_title="Common Stock",
                transaction_date=date(2026, 6, 27),
                transaction_code="P",
                acquired_disposed="A",
                shares=15_000,
                price_per_share=41.0,
                shares_owned_after=130_000.0,
                direct_or_indirect="D",
                footnotes=[],
            ),
        ]


class InsiderSECProviderIntegrationTests(unittest.TestCase):
    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_insider_scanner_uses_provider_and_returns_native_results(self, _mock_market) -> None:
        provider = _IntegrationProvider()

        results, receipt = run_insider_scan(
            config=InsiderConfig(lookback_days=1),
            observed_at="2026-06-26T12:00:00+00:00",
            sec_provider=provider,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ticker, "TEAM")
        self.assertIsInstance(receipt["ledger_rows"], list)
        self.assertEqual(provider.calls[0], "daily:2026-06-26")
        self.assertEqual(provider.calls[1], "form4:0001650372-24-000999")


if __name__ == "__main__":
    unittest.main()
