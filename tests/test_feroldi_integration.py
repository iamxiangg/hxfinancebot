from __future__ import annotations

"""Integration tests for Feroldi first-cut enrichment workflow.

Tests the enrich_feroldi_candidates() function and its interactions
with scoring, sheet serialization, and failure handling.
All tests use mocked yfinance and SEC providers — no live network calls.
"""

import os
import unittest
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from funnel.feroldi_models import FeroldiDetailResult
from funnel.feroldi_scoring import score_feroldi_detail
from funnel.review_schema import FEROLDI_FIRST_CUT_DETAIL_HEADERS
from providers.sec.models import CompanyFacts, FinancialFact


class TestDetailToSheetRow(unittest.TestCase):
    """Test the _detail_to_sheet_row serialization."""

    def setUp(self):
        # Import the function from feroldi_sheet_writer
        from funnel.feroldi_sheet_writer import detail_to_sheet_row
        self._detail_to_sheet_row = detail_to_sheet_row

    def test_empty_detail_produces_minimal_row(self):
        detail = FeroldiDetailResult(ticker="AAPL")
        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertIsInstance(row, dict)
        self.assertEqual(row.get("Ticker"), "AAPL")
        self.assertEqual(row.get("Feroldi Rubric Version"), "FEROLDI-38-V1")
        self.assertEqual(row.get("Feroldi Max Points"), 38.0)

    def test_scored_detail_has_aggregates(self):
        detail = FeroldiDetailResult(ticker="MSFT", candidate_id="MSFT-001")
        detail.financial_score = 14.0
        detail.financial_available = 17.0
        detail.management_score = 8.0
        detail.management_available = 10.0
        detail.stock_score = 9.0
        detail.stock_available = 11.0
        detail.first_cut_score = 31.0
        detail.available_points = 38.0
        detail.equivalent_score = 31.0
        detail.coverage = 1.0
        detail.missing_inputs = []

        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertEqual(row.get("Feroldi Financial Score"), 14.0)
        self.assertEqual(row.get("Feroldi Financial Available"), 17.0)
        self.assertEqual(row.get("Feroldi Management Score"), 8.0)
        self.assertEqual(row.get("Feroldi Management Available"), 10.0)
        self.assertEqual(row.get("Feroldi Stock Score"), 9.0)
        self.assertEqual(row.get("Feroldi Stock Available"), 11.0)
        self.assertEqual(row.get("Feroldi First Cut Score"), 31.0)
        self.assertEqual(row.get("Feroldi Available Points"), 38.0)
        self.assertEqual(row.get("Feroldi Max Points"), 38.0)
        self.assertEqual(row.get("Feroldi Equivalent Score"), 31.0)
        self.assertEqual(row.get("Feroldi Coverage"), 1.0)

    def test_detail_has_f01_fields(self):
        detail = FeroldiDetailResult(ticker="AAPL")
        detail.f01.cash_and_equivalents = 100000000.0
        detail.f01.long_term_debt = 50000000.0
        detail.f01.cash_to_lt_debt_ratio = 2.0
        detail.f01.score = 5.0
        detail.f01.available = 5.0
        detail.f01.reason = "Healthy ratio"

        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertEqual(row.get("F01 Cash And Cash Equivalents"), 100000000.0)
        self.assertEqual(row.get("F01 Long Term Debt"), 50000000.0)
        self.assertEqual(row.get("F01 Cash To Long Term Debt Ratio"), 2.0)
        self.assertEqual(row.get("F01 Score"), 5.0)
        self.assertEqual(row.get("F01 Available"), 5.0)
        self.assertEqual(row.get("F01 Reason"), "Healthy ratio")

    def test_detail_has_m01_fields(self):
        detail = FeroldiDetailResult(ticker="AAPL")
        detail.m01.ceo_name = "Tim Cook"
        detail.m01.founder_flag = False
        detail.m01.ceo_tenure_years = 13.0
        detail.m01.extraction_confidence = "HIGH"
        detail.m01.score = 3.0
        detail.m01.available = 4.0

        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertEqual(row.get("M01 Current CEO Name"), "Tim Cook")
        self.assertFalse(row.get("M01 Founder Flag"))
        self.assertEqual(row.get("M01 CEO Tenure Years"), 13.0)
        self.assertEqual(row.get("M01 Extraction Confidence"), "HIGH")
        self.assertEqual(row.get("M01 Score"), 3.0)

    def test_detail_has_s01_fields(self):
        detail = FeroldiDetailResult(ticker="AAPL")
        detail.s01.stock_start_adjusted_price = 50.0
        detail.s01.stock_end_adjusted_price = 100.0
        detail.s01.spy_start_adjusted_price = 200.0
        detail.s01.spy_end_adjusted_price = 250.0
        detail.s01.stock_total_return_pct = 100.0
        detail.s01.spy_total_return_pct = 25.0
        detail.s01.excess_return_points = 75.0
        detail.s01.trading_days = 1258
        detail.s01.score = 3.0
        detail.s01.available = 4.0

        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertEqual(row.get("S01 Stock Start Adjusted Price"), 50.0)
        self.assertEqual(row.get("S01 Stock End Adjusted Price"), 100.0)
        self.assertEqual(row.get("S01 SPY End Adjusted Price"), 250.0)
        self.assertEqual(row.get("S01 Excess Return Percentage Points"), 75.0)
        self.assertEqual(row.get("S01 Trading Days"), 1258)
        self.assertEqual(row.get("S01 Score"), 3.0)

    def test_none_values_become_empty_string(self):
        detail = FeroldiDetailResult(ticker="AAPL")
        detail.f01.cash_and_equivalents = None
        detail.f01.long_term_debt = None

        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        self.assertEqual(row.get("F01 Cash And Cash Equivalents"), "")
        self.assertEqual(row.get("F01 Long Term Debt"), "")

    def test_all_required_headers_present(self):
        """Every header in FEROLDI_FIRST_CUT_DETAIL_HEADERS should appear in the row."""
        detail = FeroldiDetailResult(ticker="TEST")
        row = self._detail_to_sheet_row(detail, now="2024-01-01T00:00:00Z")
        for header in FEROLDI_FIRST_CUT_DETAIL_HEADERS:
            self.assertIn(header, row, f"Header '{header}' missing from detail row")


class TestEnrichFeroldiCandidates(unittest.TestCase):
    """Test the enrich_feroldi_candidates function."""

    def setUp(self):
        from funnel.review_candidates import enrich_feroldi_candidates
        self.enrich_feroldi_candidates = enrich_feroldi_candidates
        self.service = MagicMock()
        self.spreadsheet_id = "test_spreadsheet_id"

    def test_skips_final_status_candidates(self):
        """Candidates in NOTIFIED or REJECTED should be skipped."""
        candidates = [
            {"Candidate ID": "A-001", "Ticker": "A", "Status": "NOTIFIED", "Telegram Eligible": "YES"},
            {"Candidate ID": "R-001", "Ticker": "R", "Status": "REJECTED", "Telegram Eligible": "NO"},
        ]
        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=10,
        )
        self.assertEqual(len(result), 2)
        # No yfinance calls should have been made
        self.assertEqual(self.service.call_count, 0)

    def test_skips_non_btd_passed_candidates(self):
        """Candidates that didn't pass BTD gate should be skipped."""
        candidates = [
            {"Candidate ID": "X-001", "Ticker": "X", "Status": "NEW", "Telegram Eligible": "NO"},
            {"Candidate ID": "X-002", "Ticker": "X2", "Status": "ENRICHED", "Telegram Eligible": "NO"},
            {"Candidate ID": "X-003", "Ticker": "X3", "Status": "BTD_FAILED", "Telegram Eligible": "NO"},
        ]
        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=10,
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(self.service.call_count, 0)

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_processes_btd_passed_candidate(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """A BTD_PASSED candidate should be processed."""
        mock_yf.return_value = {
            "ticker": "AAPL",
            "company_name": "Apple Inc.",
            "cik": "0000320193",
            "currency": "USD",
            "totalCash": 70000000000,
            "longTermDebt": 95000000000,
            "totalRevenue": 383000000000,
            "costOfRevenue": 214000000000,
            "grossProfits": 169000000000,
            "netIncomeToCommon": 97000000000,
            "bookValue": 4.0,
            "sharesOutstanding": 15000000000,
            "operatingCashflow": 110000000000,
            "capitalExpenditure": -10000000000,
            "dilutedEPS": 6.50,
            "currentPrice": 200.0,
            "heldPercentInsiders": 0.07,
            "marketCap": 3000000000000,
            "totalDebt": 120000000000,
            "totalAssets": 350000000000,
        }
        mock_qtr.return_value = {}
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "2019-01-01",
            "end_date": "2024-01-01",
            "stock_start_price": 150.0,
            "stock_end_price": 200.0,
            "spy_start_price": 250.0,
            "spy_end_price": 450.0,
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        candidates = [
            {"Candidate ID": "AAPL-001", "Ticker": "AAPL", "Status": "BTD_PASSED", "Telegram Eligible": "YES"},
        ]

        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=10,
        )

        self.assertEqual(len(result), 1)
        candidate = result[0]
        # Aggregated scores should be populated
        self.assertIn("Feroldi First Cut Score", candidate)
        self.assertIn("Feroldi Financial Score", candidate)
        self.assertIn("Feroldi Management Score", candidate)
        self.assertIn("Feroldi Stock Score", candidate)
        self.assertEqual(candidate.get("Feroldi Max Points"), 38.0)


class TestFeroldiZeroValueFallbacks(unittest.TestCase):
    def setUp(self):
        from funnel.review_candidates import enrich_feroldi_candidates
        self.enrich_feroldi_candidates = enrich_feroldi_candidates
        self.service = MagicMock()
        self.spreadsheet_id = "test_spreadsheet_id"

    def test_f05_preserves_zero_quarterly_eps_instead_of_treating_it_as_missing(self):
        detail = FeroldiDetailResult(ticker="GBTG")
        detail.quarterly = {
            "eps_current": 0.0,
            "eps_prior": 0.16,
        }

        scored = score_feroldi_detail(detail, metrics={})

        self.assertEqual(scored.f05.current_diluted_eps_ttm, 0.0)
        self.assertEqual(scored.f05.prior_diluted_eps_ttm, 0.16)
        self.assertEqual(scored.f05.available, 3.0)
        self.assertIn("Current EPS 0.00 <= 0", scored.f05.reason)

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_preserves_previous_data_on_failure(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """On enrichment failure, previous Feroldi data should be preserved."""
        mock_yf.side_effect = RuntimeError("yfinance unavailable")

        candidates = [
            {
                "Candidate ID": "FAIL-001",
                "Ticker": "FAIL",
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi First Cut Score": "25.0",
                "Feroldi Financial Score": "12.0",
                "Feroldi Management Score": "7.0",
                "Feroldi Stock Score": "6.0",
                "Feroldi Available Points": "35.0",
                "Feroldi Max Points": "38.0",
                "Feroldi Last Updated": "2024-01-01T00:00:00Z",
            },
        ]

        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=10,
        )

        self.assertEqual(len(result), 1)
        candidate = result[0]
        # Previous data preserved
        self.assertEqual(candidate.get("Feroldi First Cut Score"), "25.0")
        self.assertEqual(candidate.get("Feroldi Financial Score"), "12.0")
        self.assertIn("Last Error", candidate)
        self.assertIn("yfinance unavailable", str(candidate.get("Last Error")))

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_respects_enrich_limit(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """Only `limit` candidates should be processed (detail_to_candidate_updates returns float scores)."""
        mock_yf.return_value = {
            "ticker": "TICK",
            "company_name": "Test Inc.",
            "cik": "",
            "currency": "USD",
            "totalCash": 1000000,
            "longTermDebt": 500000,
            "totalRevenue": 5000000,
            "costOfRevenue": 3000000,
            "grossProfits": 2000000,
            "netIncomeToCommon": 800000,
            "bookValue": 10.0,
            "sharesOutstanding": 1000000,
            "operatingCashflow": 1200000,
            "capitalExpenditure": -200000,
            "dilutedEPS": 0.80,
            "currentPrice": 50.0,
            "heldPercentInsiders": None,
            "marketCap": 50000000,
            "totalDebt": 800000,
            "totalAssets": 5000000,
        }
        mock_qtr.return_value = {}
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "2019-01-01", "end_date": "2024-01-01",
            "stock_start_price": 10.0, "stock_end_price": 20.0,
            "spy_start_price": 100.0, "spy_end_price": 150.0,
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        candidates = [
            {"Candidate ID": f"T-{i:03d}", "Ticker": f"TICK{i}", "Status": "BTD_PASSED", "Telegram Eligible": "YES"}
            for i in range(5)
        ]

        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=2,
        )

        self.assertEqual(len(result), 5)
        # detail_to_candidate_updates returns floats like 0.0, 12.0, etc.
        scored = [c for c in result if c.get("Feroldi First Cut Score") is not None]
        self.assertEqual(len(scored), 2)

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_skips_recently_scored_candidates(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """Candidates with recent Feroldi data should be skipped (not force_refresh)."""
        mock_yf.return_value = {
            "ticker": "AAPL", "company_name": "Apple", "cik": "", "currency": "USD",
            "totalCash": 1000000, "longTermDebt": 500000, "totalRevenue": 5000000,
            "costOfRevenue": 3000000, "grossProfits": 2000000, "netIncomeToCommon": 800000,
            "bookValue": 10.0, "sharesOutstanding": 1000000, "operatingCashflow": 1200000,
            "capitalExpenditure": -200000, "dilutedEPS": 0.80, "currentPrice": 50.0,
            "heldPercentInsiders": None, "marketCap": 50000000, "totalDebt": 800000, "totalAssets": 5000000,
        }
        mock_qtr.return_value = {}
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "2019-01-01", "end_date": "2024-01-01",
            "stock_start_price": 10.0, "stock_end_price": 20.0,
            "spy_start_price": 100.0, "spy_end_price": 150.0,
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        candidates = [
            {
                "Candidate ID": "RECENT-001",
                "Ticker": "RECENT",
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi Last Updated": "2026-06-29T00:00:00Z",
                "Feroldi First Cut Score": "30.0",
            },
        ]

        result = self.enrich_feroldi_candidates(
            candidates, service=self.service, spreadsheet_id=self.spreadsheet_id, limit=10,
        )

        self.assertEqual(len(result), 1)
        # Should NOT have been called since the data is recent
        mock_yf.assert_not_called()

    def test_force_refresh_overrides_recency_check(self):
        """With force_refresh=True, recently scored candidates should be re-processed."""
        # We can't easily test this without mocking the full pipeline,
        # but we verify the flag is passed through
        from funnel.review_candidates import enrich_feroldi_candidates

        candidates = [
            {
                "Candidate ID": "FORCE-001",
                "Ticker": "FORCE",
                "Status": "BTD_PASSED",
                "Telegram Eligible": "YES",
                "Feroldi Last Updated": "2024-06-29T00:00:00Z",
            },
        ]
        # With force_refresh=True, it should try to process but will fail
        # because there's no real yfinance. Just verifying it doesn't crash.
        with patch("funnel.feroldi_enrichment.collect_yfinance_metrics",
                   side_effect=RuntimeError("test")):
            result = enrich_feroldi_candidates(
                candidates, service=self.service, spreadsheet_id=self.spreadsheet_id,
                limit=10, force_refresh=True,
            )
            self.assertEqual(len(result), 1)
            self.assertIn("Last Error", result[0])


class TestFeroldiSecFallbacks(unittest.TestCase):
    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    @patch("providers.sec.get_sec_provider")
    def test_sec_company_facts_backfills_missing_f05_prior(
        self,
        mock_get_sec_provider,
        mock_filings,
        mock_prices,
        mock_earnings,
        mock_qtr,
        mock_yf,
    ):
        from funnel.feroldi_scoring import run_feroldi_first_cut

        mock_yf.return_value = {
            "ticker": "GBTG",
            "company_name": "Global Business Travel Group",
            "cik": "0000000001",
            "currency": "USD",
            "totalCash": 1000000,
            "longTermDebt": 500000,
            "totalRevenue": 5000000,
            "costOfRevenue": 3000000,
            "grossProfits": 2000000,
            "netIncomeToCommon": 800000,
            "bookValue": 10.0,
            "sharesOutstanding": 1000000,
            "operatingCashflow": 1200000,
            "capitalExpenditure": -200000,
            "dilutedEPS": None,
            "currentPrice": 50.0,
            "heldPercentInsiders": None,
            "marketCap": 50000000,
            "totalDebt": 800000,
            "totalAssets": 5000000,
        }
        mock_qtr.return_value = {
            "eps_current": 0.0,
            "eps_prior": None,
        }
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "",
            "end_date": "",
            "stock_start_price": None,
            "stock_end_price": None,
            "spy_start_price": None,
            "spy_end_price": None,
            "trading_days": 0,
        }
        mock_filings.return_value = {}

        facts = CompanyFacts(
            ticker="GBTG",
            cik="0000000001",
            facts={
                "us-gaap:EarningsPerShareDiluted": [
                    FinancialFact(
                        concept_name="us-gaap:EarningsPerShareDiluted",
                        original_concept="EarningsPerShareDiluted",
                        value=0.16,
                        unit="USD/shares",
                        period_start=date(2024, 1, 1),
                        period_end=date(2024, 12, 31),
                        filed_at=datetime(2025, 2, 25, tzinfo=UTC),
                        form="10-K",
                        accession="0000000001-25-000001",
                        fiscal_year=2024,
                        fiscal_period="FY",
                    ),
                    FinancialFact(
                        concept_name="us-gaap:EarningsPerShareDiluted",
                        original_concept="EarningsPerShareDiluted",
                        value=0.09,
                        unit="USD/shares",
                        period_start=date(2023, 1, 1),
                        period_end=date(2023, 12, 31),
                        filed_at=datetime(2024, 2, 25, tzinfo=UTC),
                        form="10-K",
                        accession="0000000001-24-000001",
                        fiscal_year=2023,
                        fiscal_period="FY",
                    ),
                ]
            },
        )
        fake_provider = MagicMock()
        fake_provider.company_facts.return_value = facts
        mock_get_sec_provider.return_value = fake_provider

        detail = run_feroldi_first_cut("GBTG", candidate_id="GBTG-001")

        self.assertEqual(detail.f05.current_diluted_eps_ttm, 0.0)
        self.assertEqual(detail.f05.prior_diluted_eps_ttm, 0.16)
        self.assertIsNone(detail.f05.two_year_diluted_eps_ttm)
        self.assertEqual(detail.f05.available, 3.0)
        self.assertNotIn("F05", detail.missing_inputs)


class TestFeroldiTrajectoryIntegration(unittest.TestCase):
    """End-to-end tests verifying trajectory labels flow through the pipeline."""

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_trajectory_labels_in_detail_updates(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """Trajectory labels and weighted growth should appear in detail_to_candidate_updates."""
        from funnel.feroldi_scoring import run_feroldi_first_cut, detail_to_candidate_updates

        mock_yf.return_value = {
            "ticker": "GROW", "company_name": "Growth Co", "cik": "", "currency": "USD",
            "totalCash": 1000000, "longTermDebt": 500000, "totalRevenue": 5000000,
            "costOfRevenue": 3000000, "grossProfits": 2000000, "netIncomeToCommon": 800000,
            "bookValue": 10.0, "sharesOutstanding": 1000000, "operatingCashflow": 1200000,
            "capitalExpenditure": -200000, "dilutedEPS": 0.80, "currentPrice": 50.0,
            "heldPercentInsiders": None, "marketCap": 50000000, "totalDebt": 800000, "totalAssets": 5000000,
        }
        # Provide 3 years of data for trajectory analysis
        mock_qtr.return_value = {
            "ni_current": 120, "ni_prior": 115, "ni_2y": 100,
            "ocf_current": 120, "ocf_prior": 100, "ocf_2y": 80,
            "capex_current": -20, "capex_prior": -20, "capex_2y": -20,
            "eps_current": 1.20, "eps_prior": 1.00, "eps_2y": 0.80,
            "equity_current": 500, "equity_prior": 400, "equity_2y": 350,
        }
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "2019-01-01", "end_date": "2024-01-01",
            "stock_start_price": 10.0, "stock_end_price": 20.0,
            "spy_start_price": 100.0, "spy_end_price": 150.0,
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        detail = run_feroldi_first_cut("GROW", candidate_id="GROW-001")
        updates = detail_to_candidate_updates(detail)

        # Trajectory columns should be present
        self.assertIn("Feroldi F03 Trajectory", updates)
        self.assertIn("Feroldi F04 Trajectory", updates)
        self.assertIn("Feroldi F05 Trajectory", updates)
        self.assertIn("Feroldi F03 Weighted ROE Growth %", updates)
        self.assertIn("Feroldi F04 Weighted FCF Growth %", updates)
        self.assertIn("Feroldi F05 Weighted EPS Growth %", updates)

        # Trajectory labels should be strings (possibly empty)
        self.assertIsInstance(updates["Feroldi F03 Trajectory"], str)
        self.assertIsInstance(updates["Feroldi F04 Trajectory"], str)
        self.assertIsInstance(updates["Feroldi F05 Trajectory"], str)

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_no_trajectory_without_2y_data(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """Without 2-year-ago data, trajectory labels should be empty."""
        from funnel.feroldi_scoring import run_feroldi_first_cut

        mock_yf.return_value = {
            "ticker": "NO2Y", "company_name": "No History", "cik": "", "currency": "USD",
            "totalCash": 1000000, "longTermDebt": 500000, "totalRevenue": 5000000,
            "costOfRevenue": 3000000, "grossProfits": 2000000, "netIncomeToCommon": 800000,
            "bookValue": 10.0, "sharesOutstanding": 1000000, "operatingCashflow": 1200000,
            "capitalExpenditure": -200000, "dilutedEPS": 0.80, "currentPrice": 50.0,
            "heldPercentInsiders": None, "marketCap": 50000000, "totalDebt": 800000, "totalAssets": 5000000,
        }
        # Only 2 years — no 2y data
        mock_qtr.return_value = {
            "ni_current": 120, "ni_prior": 115,
            "ocf_current": 120, "ocf_prior": 100,
            "capex_current": -20, "capex_prior": -20,
            "eps_current": 1.20, "eps_prior": 1.00,
            "equity_current": 500, "equity_prior": 400,
        }
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "2019-01-01", "end_date": "2024-01-01",
            "stock_start_price": 10.0, "stock_end_price": 20.0,
            "spy_start_price": 100.0, "spy_end_price": 150.0,
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        detail = run_feroldi_first_cut("NO2Y", candidate_id="NO2Y-001")

        # Without 2y data, trajectory labels should be empty strings
        self.assertEqual(detail.f03.trajectory_label, "")
        self.assertEqual(detail.f04.trajectory_label, "")
        self.assertEqual(detail.f05.trajectory_label, "")
        self.assertIsNone(detail.f03.weighted_roe_growth_pct)
        self.assertIsNone(detail.f04.weighted_fcf_growth_pct)
        self.assertIsNone(detail.f05.weighted_eps_growth_pct)


class TestFeroldiScoringIntegration(unittest.TestCase):
    """End-to-end tests of the scoring pipeline with mocked data sources."""

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_full_pipeline_healthy_company(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """A company with strong financials should score well."""
        from funnel.feroldi_scoring import run_feroldi_first_cut

        # Strong financials
        mock_yf.return_value = {
            "ticker": "STRONG",
            "company_name": "Strong Corp",
            "cik": "0000000000",
            "currency": "USD",
            "totalCash": 50000000000,    # $50B cash
            "longTermDebt": 10000000000, # $10B debt → ratio 5
            "totalRevenue": 100000000000,
            "costOfRevenue": 40000000000,
            "grossProfits": 60000000000, # 60% margin
            "netIncomeToCommon": 20000000000,
            "bookValue": 20.0,
            "sharesOutstanding": 2000000000,
            "operatingCashflow": 30000000000,
            "capitalExpenditure": -5000000000,
            "dilutedEPS": 10.0,
            "currentPrice": 200.0,
            "heldPercentInsiders": 15.0,  # 15% insider ownership
            "marketCap": 400000000000,
            "totalDebt": 15000000000,
            "totalAssets": 200000000000,
        }
        # Prior period data shows growth
        mock_qtr.return_value = {
            "ni_current": 20000000000, "ni_prior": 16000000000,
            "ocf_current": 30000000000, "ocf_prior": 24000000000,
            "capex_current": -5000000000, "capex_prior": -4000000000,
            "eps_current": 10.0, "eps_prior": 8.0,
            "equity_current": 40000000000, "equity_prior": 32000000000,
        }
        mock_earnings.return_value = {
            "q1_reported": 2.60, "q1_estimated": 2.40,
            "q2_reported": 2.70, "q2_estimated": 2.50,
            "q3_reported": 2.80, "q3_estimated": 2.60,
            "q4_reported": 1.90, "q4_estimated": 2.00,
        }
        mock_prices.return_value = {
            "start_date": "2019-01-01", "end_date": "2024-01-01",
            "stock_start_price": 80.0, "stock_end_price": 200.0,     # 150%
            "spy_start_price": 250.0, "spy_end_price": 400.0,        # 60%
            "trading_days": 1258,
        }
        mock_filings.return_value = {}

        detail = run_feroldi_first_cut("STRONG", candidate_id="STRONG-001")

        # Check that the result has scores
        self.assertIsNotNone(detail)
        self.assertGreaterEqual(detail.first_cut_score, 0)
        self.assertGreaterEqual(detail.available_points, 0)
        self.assertLessEqual(detail.available_points, 38)

        # F01 should score 5 (ratio > 2)
        self.assertEqual(detail.f01.score, 5)
        self.assertEqual(detail.f01.available, 5)

        # F02 should score 1 (60% gross margin → 50-65% range)
        self.assertEqual(detail.f02.score, 1)
        self.assertEqual(detail.f02.available, 3)

        # S01 should have excess return
        self.assertGreater(detail.s01.excess_return_points, 0)

    @patch("funnel.feroldi_enrichment.collect_yfinance_metrics")
    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    @patch("funnel.feroldi_enrichment.collect_earnings_surprise")
    @patch("funnel.feroldi_enrichment.collect_price_history")
    @patch("funnel.feroldi_sec.extract_filing_text")
    def test_full_pipeline_missing_data_reduces_available(
        self, mock_filings, mock_prices, mock_earnings, mock_qtr, mock_yf,
    ):
        """A company with missing financial data should have reduced available points."""
        from funnel.feroldi_scoring import run_feroldi_first_cut

        # Minimal data — only cash and debt
        mock_yf.return_value = {
            "ticker": "MINIMAL",
            "company_name": "Minimal Inc",
            "cik": "",
            "currency": "USD",
            "totalCash": None,           # Missing!
            "longTermDebt": None,        # Missing!
            "totalRevenue": None,
            "costOfRevenue": None,
            "grossProfits": None,
            "netIncomeToCommon": None,
            "bookValue": None,
            "sharesOutstanding": None,
            "operatingCashflow": None,
            "capitalExpenditure": None,
            "dilutedEPS": None,
            "currentPrice": 10.0,
            "heldPercentInsiders": None,
            "marketCap": None,
            "totalDebt": None,
            "totalAssets": None,
        }
        mock_qtr.return_value = {}
        mock_earnings.return_value = {}
        mock_prices.return_value = {
            "start_date": "", "end_date": "",
            "stock_start_price": None, "stock_end_price": None,
            "spy_start_price": None, "spy_end_price": None,
            "trading_days": 0,
        }
        mock_filings.return_value = {}

        detail = run_feroldi_first_cut("MINIMAL", candidate_id="MINIMAL-001")

        self.assertIsNotNone(detail)
        # Most available points should be 0 due to missing data
        self.assertEqual(detail.f01.available, 0)
        self.assertEqual(detail.f02.available, 0)
        self.assertEqual(detail.f03.available, 0)  # all data is None
        self.assertEqual(detail.f04.available, 0)  # all data is None
        self.assertEqual(detail.f05.available, 0)  # all data is None
        self.assertEqual(detail.s01.available, 0)  # no price history
        self.assertEqual(detail.available_points, 0)


if __name__ == "__main__":
    unittest.main()
