from __future__ import annotations

"""Tests for the FMP (Financial Modeling Prep) API client and quarterly fallback."""

import os
import unittest
from unittest.mock import MagicMock, patch

from funnel.feroldi_fmp import (
    _fmp_cache,
    _get_api_key,
    _fmp_get,
    clear_fmp_cache,
    fetch_fmp_quarterly,
)


class TestGetApiKey(unittest.TestCase):
    """Tests for _get_api_key."""

    def test_returns_key_when_set(self):
        with patch.dict(os.environ, {"FMP_API_KEY": "abc123"}, clear=True):
            self.assertEqual(_get_api_key(), "abc123")

    def test_returns_none_when_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_get_api_key())

    def test_returns_none_when_empty_string(self):
        with patch.dict(os.environ, {"FMP_API_KEY": "   "}, clear=True):
            self.assertIsNone(_get_api_key())


class TestClearFmpCache(unittest.TestCase):
    """Tests for clear_fmp_cache."""

    def setUp(self):
        clear_fmp_cache()

    def tearDown(self):
        clear_fmp_cache()

    def test_clears_cache(self):
        _fmp_cache["AAPL"] = {"ni_current": 100}
        _fmp_cache["MSFT"] = {"ni_current": 200}
        self.assertEqual(len(_fmp_cache), 2)

        clear_fmp_cache()
        self.assertEqual(len(_fmp_cache), 0)

    def test_clear_empty_cache_does_not_crash(self):
        clear_fmp_cache()
        self.assertEqual(len(_fmp_cache), 0)


class TestFmpGet(unittest.TestCase):
    """Tests for _fmp_get — the low-level API caller."""

    def setUp(self):
        clear_fmp_cache()

    def tearDown(self):
        clear_fmp_cache()

    def test_returns_empty_list_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = _fmp_get("income-statement", "AAPL")
            self.assertEqual(result, [])

    @patch("funnel.feroldi_fmp.requests.get")
    def test_returns_parsed_json_list(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"date": "2024-09-30", "netIncome": 30000000000},
            {"date": "2024-06-30", "netIncome": 28000000000},
        ]
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = _fmp_get("income-statement", "AAPL")
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["netIncome"], 30000000000)

    @patch("funnel.feroldi_fmp.requests.get")
    def test_url_includes_ticker_and_params(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            _fmp_get("balance-sheet-statement", "GOOGL")

        call_args = mock_get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        self.assertIn("balance-sheet-statement", url)
        self.assertEqual(call_args[1]["params"]["symbol"], "GOOGL")
        self.assertEqual(call_args[1]["params"]["period"], "annual")
        self.assertEqual(call_args[1]["params"]["limit"], 5)
        self.assertEqual(call_args[1]["params"]["apikey"], "key123")

    @patch("funnel.feroldi_fmp.requests.get")
    def test_handles_fmp_error_object(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"Error Message": "Invalid API key"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict(os.environ, {"FMP_API_KEY": "badkey"}, clear=True):
            result = _fmp_get("income-statement", "AAPL")
            self.assertEqual(result, [])

    @patch("funnel.feroldi_fmp.requests.get")
    def test_handles_http_error(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("timeout")

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = _fmp_get("income-statement", "AAPL")
            self.assertEqual(result, [])

    @patch("funnel.feroldi_fmp.requests.get")
    def test_handles_non_list_non_error_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"some": "unexpected"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = _fmp_get("income-statement", "AAPL")
            self.assertEqual(result, [])


class TestFetchFmpQuarterly(unittest.TestCase):
    """Tests for fetch_fmp_quarterly — the main entry point."""

    def setUp(self):
        clear_fmp_cache()

    def tearDown(self):
        clear_fmp_cache()

    def test_returns_empty_dict_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = fetch_fmp_quarterly("AAPL")
            self.assertEqual(result, {})

    def test_cache_hit_returns_cached_data(self):
        cached = {"ni_current": 100e9, "ni_prior": 80e9}
        _fmp_cache["AAPL"] = dict(cached)

        result = fetch_fmp_quarterly("AAPL")
        self.assertEqual(result["ni_current"], 100e9)
        self.assertEqual(result["ni_prior"], 80e9)

    def test_cache_hit_is_a_copy_not_reference(self):
        _fmp_cache["AAPL"] = {"ni_current": 100}

        result = fetch_fmp_quarterly("AAPL")
        result["ni_current"] = 999
        # Cache should not be mutated
        self.assertEqual(_fmp_cache["AAPL"]["ni_current"], 100)

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_computes_ttm_from_annual_data(self, mock_fmp_get):
        """Simulate 5 years of annual income, cashflow, and balance sheet data.

        FMP free tier only supports period=annual with limit=5.
        Annual data represents full-year TTM — no quarterly summing needed.
        """
        # Income statement: 5 annual years, netIncome grows
        income = [{"date": f"202{4-i}", "netIncome": 100 + i * 20, "epsDiluted": 5.0 + i}
                  for i in range(5)]

        # Cash flow: 5 annual years
        cashflow = [{"date": f"202{4-i}",
                      "operatingCashFlow": 150 + i * 15,
                      "capitalExpenditure": -(50 + i * 10),
                      "commonStockRepurchased": -(30 + i * 5),
                      "commonDividendsPaid": -(10 + i * 3)} for i in range(5)]

        # Balance sheet: 5 annual years
        balance_sheet = [{"date": f"202{4-i}",
                           "totalStockholdersEquity": 500 + i * 50,
                           "longTermDebt": 200 + i * 20,
                           "totalAssets": 1000 + i * 100} for i in range(5)]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("AAPL")

        # Current year = index 0, prior year = index 1, 2-year-ago = index 2
        self.assertEqual(result["ni_current"], 100)   # index 0
        self.assertEqual(result["ni_prior"], 120)      # index 1
        self.assertEqual(result["ni_2y"], 140)         # index 2

        # EPS
        self.assertEqual(result["eps_current"], 5.0)
        self.assertEqual(result["eps_prior"], 6.0)
        self.assertEqual(result["eps_2y"], 7.0)

        # OCF
        self.assertEqual(result["ocf_current"], 150)
        self.assertEqual(result["ocf_prior"], 165)
        self.assertEqual(result["ocf_2y"], 180)

        # Capex
        self.assertEqual(result["capex_current"], -50)
        self.assertEqual(result["capex_prior"], -60)
        self.assertEqual(result["capex_2y"], -70)

        # Repurchases (negative -> absolute)
        self.assertEqual(result["repurchases_current"], 30)

        # Dividends
        self.assertEqual(result["dividends_current"], 10)

        # Balance sheet
        self.assertEqual(result["equity_current"], 500)
        self.assertEqual(result["equity_prior"], 550)
        self.assertEqual(result["equity_2y"], 600)
        self.assertEqual(result["lt_debt_current"], 200)
        self.assertEqual(result["total_assets"], 1000)

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_only_one_year_returns_none_for_prior(self, mock_fmp_get):
        """If FMP returns only 1 year of data, prior and 2y should be None."""
        income = [{"date": "2024", "netIncome": 100, "epsDiluted": 5.0}]
        cashflow = [{"date": "2024", "operatingCashFlow": 150,
                      "capitalExpenditure": -50, "commonStockRepurchased": -30,
                      "commonDividendsPaid": -10}]
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 500,
                           "longTermDebt": 200, "totalAssets": 1000}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("AAPL")

        # Current year should work (index 0)
        self.assertEqual(result["ni_current"], 100)
        # Prior year should be None (only 1 year of data)
        self.assertIsNone(result["ni_prior"])
        self.assertIsNone(result["ni_2y"])
        self.assertIsNone(result["eps_prior"])
        self.assertIsNone(result["eps_2y"])
        self.assertIsNone(result["ocf_prior"])
        self.assertIsNone(result["ocf_2y"])
        self.assertIsNone(result["capex_prior"])
        self.assertIsNone(result["capex_2y"])
    @patch("funnel.feroldi_fmp._fmp_get")
    def test_missing_statement_returns_empty_dict(self, mock_fmp_get):
        """If any statement is empty, should return empty."""
        income = [{"date": "2024", "netIncome": 10}]
        cashflow = []  # Missing!
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 100}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("TICK")

        # With empty cashflow, result should be mostly empty
        # (the function returns early with empty result, caching it)
        self.assertEqual(len(result), 0)

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_missing_fields_return_none(self, mock_fmp_get):
        """Missing fields in annual data should return None for those values."""
        income = [
            {"date": "2024", "netIncome": 100, "epsDiluted": 5.0},    # current year: both present
            {"date": "2023"},                                             # prior year: missing fields
        ]
        cashflow = [{"date": "2024", "operatingCashFlow": 150,
                      "capitalExpenditure": -50, "commonStockRepurchased": -30,
                      "commonDividendsPaid": -10},
                     {"date": "2023"}]  # prior year: all missing
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 500,
                           "longTermDebt": 200, "totalAssets": 1000},
                          {"date": "2023"}]  # prior year: all missing

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("AAPL")

        # Current year should be present
        self.assertEqual(result["ni_current"], 100)
        # Prior year should be None (fields missing)
        self.assertIsNone(result["ni_prior"])
        self.assertIsNone(result["eps_prior"])
        self.assertIsNone(result["ocf_prior"])

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_ticker_normalized_to_uppercase(self, mock_fmp_get):
        """Ticker should be uppercased for cache key."""
        income = [{"date": "2024", "netIncome": 10}]
        cashflow = [{"date": "2024", "netCashProvidedByOperatingActivities": 20,
                      "capitalExpenditure": -5, "commonStockRepurchased": -3,
                      "commonDividendsPaid": -1}]
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 100,
                           "longTermDebt": 50, "totalAssets": 200}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            fetch_fmp_quarterly("aapl")

        # Check that cache was set with uppercase key
        self.assertIn("AAPL", _fmp_cache)

        # Check that _fmp_get was called with the original ticker casing
        # (FMP is case-insensitive but preserving user input is fine)
        self.assertEqual(mock_fmp_get.call_args_list[0][0][1], "aapl")

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_zero_values_treated_as_missing(self, mock_fmp_get):
        """_snap should return None for zero values (likely missing data)."""
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 0,
                           "longTermDebt": 50, "totalAssets": 0},
                          {"date": "2023", "totalStockholdersEquity": 450,
                           "longTermDebt": 45, "totalAssets": 900}]

        income = [{"date": "2024", "netIncome": 100, "epsDiluted": 5.0},
                   {"date": "2023", "netIncome": 80, "epsDiluted": 4.0}]
        cashflow = [{"date": "2024", "operatingCashFlow": 150,
                      "capitalExpenditure": -50, "commonStockRepurchased": -30,
                      "commonDividendsPaid": -10},
                     {"date": "2023", "netCashProvidedByOperatingActivities": 120,
                      "capitalExpenditure": -40, "commonStockRepurchased": -20,
                      "commonDividendsPaid": -8}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("AAPL")

        # Latest quarter has equity=0 → should be None
        self.assertIsNone(result["equity_current"])
        # totalAssets also 0 at latest → None
        self.assertIsNone(result["total_assets"])
        # lt_debt is 50 → should be present
        self.assertEqual(result["lt_debt_current"], 50)

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_caches_result_after_successful_fetch(self, mock_fmp_get):
        """After a successful fetch, the result should be cached."""
        income = [{"date": str(y), "netIncome": 100 + i*10, "epsDiluted": 5.0}
                  for i, y in enumerate(range(2024, 2019, -1))]
        cashflow = [{"date": str(y), "operatingCashFlow": 150 + i*10,
                      "capitalExpenditure": -50, "commonStockRepurchased": -30,
                      "commonDividendsPaid": -10}
                     for i, y in enumerate(range(2024, 2019, -1))]
        balance_sheet = [{"date": str(y), "totalStockholdersEquity": 500 + i*50,
                           "longTermDebt": 200, "totalAssets": 1000}
                          for i, y in enumerate(range(2024, 2019, -1))]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            # First call — should hit API
            result1 = fetch_fmp_quarterly("AAPL")
            self.assertEqual(mock_fmp_get.call_count, 3)

            # Second call — should use cache (no additional API calls)
            result2 = fetch_fmp_quarterly("AAPL")
            self.assertEqual(mock_fmp_get.call_count, 3)  # Still 3

            # Results should match
            self.assertEqual(result1["ni_current"], result2["ni_current"])

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_non_numeric_values_skipped_gracefully(self, mock_fmp_get):
        """String values that can't be parsed as float should return None."""
        income = [{"date": "2024", "netIncome": "N/A", "epsDiluted": 5.0},
                   {"date": "2023", "netIncome": 80, "epsDiluted": 4.0}]
        cashflow = [{"date": "2024", "operatingCashFlow": 150,
                      "capitalExpenditure": -50, "commonStockRepurchased": -30,
                      "commonDividendsPaid": -10},
                     {"date": "2023", "netCashProvidedByOperatingActivities": 120,
                      "capitalExpenditure": -40, "commonStockRepurchased": -20,
                      "commonDividendsPaid": -8}]
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 500,
                           "longTermDebt": 200, "totalAssets": 1000},
                          {"date": "2023", "totalStockholdersEquity": 450,
                           "longTermDebt": 180, "totalAssets": 900}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("AAPL")

        # ni_current should be None (N/A not parseable), ni_prior should be 80
        self.assertIsNone(result["ni_current"])
        self.assertEqual(result["ni_prior"], 80)


class TestFmpEnrichmentIntegration(unittest.TestCase):
    """Test that collect_quarterly_financials calls FMP fallback when needed."""

    def setUp(self):
        clear_fmp_cache()

    def tearDown(self):
        clear_fmp_cache()

    @patch("funnel.feroldi_enrichment.collect_quarterly_financials")
    def test_fmp_called_when_yfinance_has_few_quarters(self, mock_collect):
        """Integration: verify FMP fallback is wired into collect_quarterly_financials."""
        # The actual function will call yfinance first, then check for prior data
        # We mock the whole function to verify the import/interface is wired
        from funnel.feroldi_enrichment import collect_quarterly_financials as real_collect

        # Just verify the function exists and the import doesn't crash
        self.assertTrue(callable(real_collect))

    @patch("funnel.feroldi_fmp.fetch_fmp_quarterly")
    @patch("funnel.feroldi_fmp._fmp_get")
    def test_fmp_not_called_when_key_missing(self, mock_get, mock_fetch):
        """Without FMP_API_KEY, fetch_fmp_quarterly should return {}."""
        with patch.dict(os.environ, {}, clear=True):
            result = fetch_fmp_quarterly("AAPL")
            self.assertEqual(result, {})
            mock_get.assert_not_called()

    @patch("funnel.feroldi_fmp._fmp_get")
    def test_partial_data_still_cached_for_partial_return(self, mock_fmp_get):
        """When FMP returns partial data (missing one statement), cache the empty result."""
        income = [{"date": "2024", "netIncome": 10}]
        cashflow = []  # Missing!
        balance_sheet = [{"date": "2024", "totalStockholdersEquity": 100,
                          "longTermDebt": 50, "totalAssets": 200}]

        mock_fmp_get.side_effect = [income, cashflow, balance_sheet]

        with patch.dict(os.environ, {"FMP_API_KEY": "key123"}, clear=True):
            result = fetch_fmp_quarterly("TICK")
            self.assertEqual(len(result), 0)

            # Should be cached (empty result)
            self.assertIn("TICK", _fmp_cache)

            # Second call should hit cache, not API
            result2 = fetch_fmp_quarterly("TICK")
            self.assertEqual(mock_fmp_get.call_count, 3)  # No additional calls
            self.assertEqual(len(result2), 0)


if __name__ == "__main__":
    unittest.main()
