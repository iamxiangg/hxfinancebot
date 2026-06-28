from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from providers.sec.models import CompanyFacts, FinancialFact
from scanners.fundamental_inflection.financial_series import build_financial_series
from scanners.fundamental_inflection.models import FinancialSeries


class _FakeFactsProvider:
    def __init__(self, facts: dict[str, list[FinancialFact]] | None = None,
                 ticker: str = "TEAM", cik: str = "0001650372",
                 should_fail: bool = False):
        self._facts = facts or {}
        self._ticker = ticker
        self._cik = cik
        self._should_fail = should_fail

    def company_profile(self, ticker: str):
        from providers.sec.models import CompanyProfile
        return CompanyProfile(ticker=ticker, cik=self._cik, name=ticker)

    def company_facts(self, ticker: str, *, as_of=None):
        if self._should_fail:
            raise RuntimeError("facts down")
        return CompanyFacts(ticker=ticker, cik=self._cik, facts=self._facts)

    def recent_filings(self, *args, **kwargs):
        return []


def _make_revenue_fact(fy: int, fp: str, value: float, accession: str = "a1",
                       filed_at: datetime | None = None) -> FinancialFact:
    period_map = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12, "FY": 12}
    month = period_map.get(fp, 12)
    filed_month = month
    filed_dt = filed_at or datetime(fy, min(12, filed_month + 1), 15, tzinfo=UTC)
    return FinancialFact(
        concept_name="us-gaap:Revenues",
        original_concept="Revenues",
        value=value,
        unit="USD",
        period_end=date(fy, month, 30),
        period_start=None,
        filed_at=filed_dt,
        form="10-Q" if fp != "FY" else "10-K",
        accession=accession,
        fiscal_year=fy,
        fiscal_period=fp,
        source_provider="official",
        frame="",
    )


def _make_fact(concept_name: str, original: str, unit: str, fy: int, fp: str,
               value: float, acc: str = "a1") -> FinancialFact:
    pm = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}
    month = pm.get(fp, 12)
    fm = min(12, month + 1)
    return FinancialFact(
        concept_name=concept_name,
        original_concept=original,
        value=value,
        unit=unit,
        period_start=None,
        period_end=date(fy, month, 30),
        filed_at=datetime(fy, fm, 15, tzinfo=UTC),
        form="10-Q",
        accession=acc,
        fiscal_year=fy,
        fiscal_period=fp,
        source_provider="official",
        frame="",
    )


class FinancialSeriesTests(unittest.TestCase):
    def test_build_series_requires_min_quarters(self):
        facts: dict[str, list[FinancialFact]] = {}
        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        self.assertEqual(series.data_confidence, "low")
        self.assertIn("no_revenue_facts", series.errors)

    def test_six_quarters_enables_medium_confidence(self):
        facts = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            facts.setdefault("us-gaap:Revenues", []).append(_make_revenue_fact(fy, fp, 100 + i * 10))
        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        self.assertEqual(len(series.usable_quarters), 6)

    def test_gross_profit_calculated_from_revenue_minus_cost(self):
        facts = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            facts.setdefault("us-gaap:Revenues", []).append(_make_revenue_fact(fy, fp, 200.0 + i))
            facts.setdefault("us-gaap:CostOfRevenue", []).append(
                _make_fact("us-gaap:CostOfRevenue", "CostOfRevenue", "USD", fy, fp, 100.0 + i * 5)
            )

        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        last = series.usable_quarters[-1]
        self.assertIsNotNone(last.gross_profit)
        self.assertGreater(last.gross_profit, 0)

    def test_unit_mismatch_is_filtered(self):
        facts: dict[str, list] = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            facts.setdefault("us-gaap:Revenues", []).append(
                _make_fact("us-gaap:Revenues", "Revenues", "EUR", fy, fp, 100.0)
            )
        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        self.assertEqual(len(series.usable_quarters), 0)

    def test_point_in_time_filters_later_filings(self):
        facts = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            filed = datetime(2026, 1, 15, tzinfo=UTC) if i <= 4 else datetime(2026, 7, 15, tzinfo=UTC)
            facts.setdefault("us-gaap:Revenues", []).append(
                _make_revenue_fact(fy, fp, 100.0 + i * 10, filed_at=filed)
            )
        provider = _FakeFactsProvider(facts=facts)
        as_of = datetime(2026, 4, 1, tzinfo=UTC)
        series = build_financial_series(provider, "TEAM", as_of=as_of, min_quarters=5)
        self.assertGreaterEqual(len(series.usable_quarters), 5)

    def test_provider_failure_is_captured(self):
        provider = _FakeFactsProvider(should_fail=True)
        series = build_financial_series(provider, "TEAM")
        self.assertIn("company_facts_unavailable", series.errors)

    def test_operating_income_assigned_to_quarter(self):
        facts = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            facts.setdefault("us-gaap:Revenues", []).append(_make_revenue_fact(fy, fp, 200.0 + i))
            facts.setdefault("us-gaap:OperatingIncomeLoss", []).append(
                _make_fact("us-gaap:OperatingIncomeLoss", "OperatingIncomeLoss", "USD", fy, fp, 40.0 + i * 5))
        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        last = series.usable_quarters[-1]
        self.assertIsNotNone(last.operating_income)

    def test_cash_and_shares_assigned(self):
        facts = {}
        for i in range(6):
            fy = 2025 + (i // 4)
            fp = f"Q{(i % 4) + 1}"
            facts.setdefault("us-gaap:Revenues", []).append(_make_revenue_fact(fy, fp, 200.0 + i))
            facts.setdefault("us-gaap:CashAndCashEquivalentsAtCarryingValue", []).append(
                _make_fact("us-gaap:CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtCarryingValue", "USD", fy, fp, 500.0 + i))
            facts.setdefault("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", []).append(
                _make_fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding", "SHARES", fy, fp, 100.0 + i * 2))
        provider = _FakeFactsProvider(facts=facts)
        series = build_financial_series(provider, "TEAM", min_quarters=6)
        last = series.usable_quarters[-1]
        self.assertIsNotNone(last.cash)
        self.assertIsNotNone(last.diluted_shares)


if __name__ == "__main__":
    unittest.main()
