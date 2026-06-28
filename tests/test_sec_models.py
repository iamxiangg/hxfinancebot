from __future__ import annotations

from datetime import UTC, date, datetime
import unittest

from providers.sec.models import CompanyFacts, FilingMetadata, FinancialFact


class SECModelTests(unittest.TestCase):
    def test_filing_metadata_exposes_accession_without_dashes(self) -> None:
        filing = FilingMetadata(
            ticker="TEAM",
            cik="0001650372",
            accession="0001650372-24-000123",
            form="10-Q",
            filed_at=datetime(2024, 8, 1, tzinfo=UTC),
            report_date=date(2024, 6, 30),
            primary_document="q2.htm",
            is_amendment=False,
            source_url="https://www.sec.gov/Archives/example.txt",
        )

        self.assertEqual(filing.accession_no_dashes, "000165037224000123")

    def test_company_facts_flattens_all_facts(self) -> None:
        fact = FinancialFact(
            concept_name="us-gaap:Revenue",
            original_concept="Revenue",
            value=100,
            unit="USD",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 3, 31),
            filed_at=datetime(2024, 4, 30, tzinfo=UTC),
            form="10-Q",
            accession="0001-24-000001",
            fiscal_year=2024,
            fiscal_period="Q1",
        )
        facts = CompanyFacts(ticker="TEAM", cik="0001650372", facts={"us-gaap:Revenue": [fact]})

        self.assertEqual(facts.all_facts(), [fact])


if __name__ == "__main__":
    unittest.main()

