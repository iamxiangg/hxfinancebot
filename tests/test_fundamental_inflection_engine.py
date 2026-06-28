from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from providers.sec.models import CompanyFacts, CompanyProfile, FinancialFact
from scanners.fundamental_inflection.engine import _is_excluded_business_model, run_inflection_scan
from scanners.fundamental_inflection.models import FundamentalInflectionConfig


class _FakeEngineProvider:
    def __init__(self, ticker_to_facts: dict | None = None,
                 profile_name: str = "Example Corp", profile_sic: str = "7370",
                 profile_cik: str = "0001650372"):
        self._ticker_to_facts = ticker_to_facts or {}
        self._profile_name = profile_name
        self._profile_sic = profile_sic
        self._profile_cik = profile_cik

    def company_profile(self, ticker: str):
        return CompanyProfile(
            ticker=ticker,
            cik=self._profile_cik,
            name=self._profile_name,
            sic=self._profile_sic,
        )

    def company_facts(self, ticker: str, *, as_of=None):
        facts = self._ticker_to_facts.get(ticker, {})
        return CompanyFacts(ticker=ticker, cik=self._profile_cik, facts=facts)

    def recent_filings(self, *args, **kwargs):
        return []


def _make_fact(concept: str, orig: str, unit: str, fy: int, fp: str,
               val: float, acc: str = "a1") -> FinancialFact:
    pm = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}
    month = pm.get(fp, 12)
    fm = min(12, month + 1)
    return FinancialFact(
        concept_name=concept, original_concept=orig,
        value=val, unit=unit,
        period_start=None,
        period_end=date(fy, month, 30),
        filed_at=datetime(fy, fm, 15, tzinfo=UTC),
        form="10-Q", accession=acc, fiscal_year=fy, fiscal_period=fp,
        source_provider="official", frame="",
    )


class InflectionEngineTests(unittest.TestCase):
    def _make_facts(self, revenue_mult: float = 1.0,
                    with_gp: bool = True, with_oi: bool = True,
                    with_cash: bool = True, with_shares: bool = True,
                    with_ocf: bool = True):
        facts: dict[str, list] = {}
        base = [100, 105, 110, 115, 125, 130, 140, 150 + 20 * (revenue_mult - 1)]
        for i, val in enumerate(base):
            fy = 2024 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            rv = val * revenue_mult
            facts.setdefault("us-gaap:Revenues", []).append(_make_fact("us-gaap:Revenues", "Revenues", "USD", fy, fp, rv))
            if with_gp:
                facts.setdefault("us-gaap:GrossProfit", []).append(_make_fact("us-gaap:GrossProfit", "GrossProfit", "USD", fy, fp, rv * 0.65))
            if with_oi:
                facts.setdefault("us-gaap:OperatingIncomeLoss", []).append(_make_fact("us-gaap:OperatingIncomeLoss", "OperatingIncomeLoss", "USD", fy, fp, rv * 0.12))
            if with_cash:
                facts.setdefault("us-gaap:CashAndCashEquivalentsAtCarryingValue", []).append(
                    _make_fact("us-gaap:CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtCarryingValue", "USD", fy, fp, 300))
            if with_shares:
                facts.setdefault("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", []).append(
                    _make_fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding", "SHARES", fy, fp, 50))
            if with_ocf:
                facts.setdefault("us-gaap:NetCashProvidedByUsedInOperatingActivities", []).append(
                    _make_fact("us-gaap:NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivities", "USD", fy, fp, rv * 0.15))
        return facts

    def test_scan_returns_results_for_qualifying_ticker(self):
        provider = _FakeEngineProvider(
            ticker_to_facts={"TEAM": self._make_facts()},
        )
        config = FundamentalInflectionConfig(min_quarters=6)
        results = run_inflection_scan(
            sec_provider=provider,
            test_tickers=["TEAM"],
            config=config,
            observed_at="2026-06-28T12:00:00+00:00",
        )
        self.assertGreater(len(results), 0, f"Got {len(results)} results")
        self.assertEqual(results[0].ticker, "TEAM")
        self.assertIn(results[0].classification, {
            "STRONG_INFLECTION", "VALIDATED_INFLECTION", "EARLY_INFLECTION",
            "GROWTH_WITHOUT_INFLECTION",
        })

    def test_bank_is_excluded(self):
        self.assertTrue(_is_excluded_business_model("First National Bank", "6020"))

    def test_reit_is_excluded(self):
        self.assertTrue(_is_excluded_business_model("Example REIT Inc", "6798"))

    def test_tech_company_is_not_excluded(self):
        self.assertFalse(_is_excluded_business_model("Tech Corp", "7370"))

    def test_scan_excludes_bank_even_with_good_facts(self):
        provider = _FakeEngineProvider(
            ticker_to_facts={"BANK": self._make_facts()},
            profile_name="Bank of Example",
            profile_sic="6021",
        )
        config = FundamentalInflectionConfig(min_quarters=6)
        results = run_inflection_scan(
            sec_provider=provider,
            test_tickers=["BANK"],
            config=config,
        )
        self.assertEqual(len(results), 0)

    def test_scan_requires_min_quarters(self):
        provider = _FakeEngineProvider(
            ticker_to_facts={
                "TEAM": {"us-gaap:Revenues": [
                    _make_fact("us-gaap:Revenues", "Revenues", "USD", 2026, "Q1", 100),
                    _make_fact("us-gaap:Revenues", "Revenues", "USD", 2026, "Q2", 105),
                    _make_fact("us-gaap:Revenues", "Revenues", "USD", 2026, "Q3", 110),
                ]},
            },
        )
        config = FundamentalInflectionConfig(min_quarters=6)
        results = run_inflection_scan(
            sec_provider=provider,
            test_tickers=["TEAM"],
            config=config,
        )
        self.assertEqual(len(results), 0)

    def test_ticker_level_failure_is_isolated(self):
        facts_good = self._make_facts()
        provider = _FakeEngineProvider(
            ticker_to_facts={"GOOD": facts_good},
        )
        config = FundamentalInflectionConfig(min_quarters=6)
        results = run_inflection_scan(
            sec_provider=provider,
            test_tickers=["GOOD", "BAD"],
            config=config,
        )
        self.assertGreater(len(results), 0)
        tickers = {r.ticker for r in results}
        self.assertIn("GOOD", tickers)
        self.assertNotIn("BAD", tickers)


if __name__ == "__main__":
    unittest.main()
