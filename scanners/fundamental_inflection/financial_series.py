from __future__ import annotations

from datetime import date, datetime
from typing import Any

from providers.sec.base import SECProvider
from providers.sec.models import FinancialFact
from scanners.fundamental_inflection.concepts import (
    ACCOUNTS_RECEIVABLE_CONCEPTS,
    CASH_CONCEPTS,
    CAPEX_CONCEPTS,
    COST_OF_REVENUE_CONCEPTS,
    DILUTED_SHARES_CONCEPTS,
    GROSS_PROFIT_CONCEPTS,
    INVENTORY_CONCEPTS,
    NET_INCOME_CONCEPTS,
    OPERATING_CASH_FLOW_CONCEPTS,
    OPERATING_INCOME_CONCEPTS,
    REVENUE_CONCEPTS,
    SHORT_TERM_INVESTMENTS_CONCEPTS,
    STOCK_BASED_COMP_CONCEPTS,
    TOTAL_DEBT_CONCEPTS,
    TOTAL_DEBT_COMBINED_CONCEPTS,
)
from scanners.fundamental_inflection.models import (
    FinancialSeries,
    QuarterlySnapshot,
)


def _find_quarterly_facts(
    facts_by_concept: dict[str, list[FinancialFact]],
    concept_names: tuple[str, ...],
    *,
    as_of: datetime | None = None,
) -> list[FinancialFact]:
    for name in concept_names:
        candidates = facts_by_concept.get(name, [])
        if not candidates:
            continue
        filtered: list[FinancialFact] = []
        for fact in candidates:
            if as_of is not None and fact.filed_at > as_of:
                continue
            if fact.fiscal_period and fact.fiscal_period.upper() not in ("Q1", "Q2", "Q3", "Q4", "FY"):
                continue
            if not fact.unit or fact.unit.upper() not in ("USD", "USD/SHARES", "PURE"):
                continue
            filtered.append(fact)
        if filtered:
            return filtered
    return []


def _find_all_facts(
    facts_by_concept: dict[str, list[FinancialFact]],
    concept_names: tuple[str, ...],
    *,
    as_of: datetime | None = None,
) -> list[FinancialFact]:
    for name in concept_names:
        candidates = facts_by_concept.get(name, [])
        if not candidates:
            continue
        filtered: list[FinancialFact] = []
        for fact in candidates:
            if as_of is not None and fact.filed_at > as_of:
                continue
            unit_upper = fact.unit.upper() if fact.unit else ""
            if unit_upper not in ("USD", "USD/SHARES", "PURE", "SHARES", ""):
                continue
            filtered.append(fact)
        if filtered:
            return filtered
    return []


def _choose_fact(
    candidates: list[FinancialFact],
    period_end: date,
    *,
    fiscal_year: int,
    fiscal_period: str,
) -> FinancialFact | None:
    best: FinancialFact | None = None
    for fact in candidates:
        if fact.period_end and fact.period_end != period_end:
            continue
        if fact.fiscal_year is not None and fact.fiscal_year != fiscal_year:
            continue
        if fact.fiscal_period and fact.fiscal_period.upper() != fiscal_period.upper():
            continue
        if fact.form and fact.form.upper().endswith("/A"):
            continue
        if best is None or fact.filed_at >= best.filed_at:
            best = fact
    return best


def _is_generic_q4(fact: FinancialFact) -> bool:
    return fact.fiscal_period.upper() == "FY"


def _float_val(fact: FinancialFact | None) -> float | None:
    if fact is None or fact.value is None:
        return None
    return float(fact.value)


def _label(quarter_index: int) -> str:
    labels = {0: "Q1", 1: "Q2", 2: "Q3", 3: "Q4"}
    return labels.get(quarter_index, f"Q{quarter_index + 1}")


def build_financial_series(
    provider: SECProvider,
    ticker: str,
    *,
    as_of: datetime | None = None,
    min_quarters: int = 6,
) -> FinancialSeries:
    try:
        facts_data = provider.company_facts(ticker, as_of=as_of)
    except Exception:
        return FinancialSeries(
            ticker=ticker,
            cik="",
            data_confidence="low",
            errors=["company_facts_unavailable"],
        )

    facts = facts_data.facts
    series = FinancialSeries(ticker=ticker, cik=facts_data.cik)

    revenue_facts = _find_quarterly_facts(facts, REVENUE_CONCEPTS, as_of=as_of)
    cost_rev_facts = _find_quarterly_facts(facts, COST_OF_REVENUE_CONCEPTS, as_of=as_of)
    gross_profit_facts = _find_quarterly_facts(facts, GROSS_PROFIT_CONCEPTS, as_of=as_of)
    op_income_facts = _find_quarterly_facts(facts, OPERATING_INCOME_CONCEPTS, as_of=as_of)
    net_income_facts = _find_quarterly_facts(facts, NET_INCOME_CONCEPTS, as_of=as_of)
    ocf_facts = _find_quarterly_facts(facts, OPERATING_CASH_FLOW_CONCEPTS, as_of=as_of)
    capex_facts = _find_quarterly_facts(facts, CAPEX_CONCEPTS, as_of=as_of)
    cash_facts = _find_quarterly_facts(facts, CASH_CONCEPTS, as_of=as_of)
    st_inv_facts = _find_all_facts(facts, SHORT_TERM_INVESTMENTS_CONCEPTS, as_of=as_of)
    ar_facts = _find_quarterly_facts(facts, ACCOUNTS_RECEIVABLE_CONCEPTS, as_of=as_of)
    inv_facts = _find_quarterly_facts(facts, INVENTORY_CONCEPTS, as_of=as_of)
    shares_facts = _find_all_facts(facts, DILUTED_SHARES_CONCEPTS, as_of=as_of)
    sbc_facts = _find_quarterly_facts(facts, STOCK_BASED_COMP_CONCEPTS, as_of=as_of)
    debt_combined = _find_all_facts(facts, TOTAL_DEBT_COMBINED_CONCEPTS, as_of=as_of)

    revenue_map: dict[tuple[int, str], FinancialFact] = {}
    for fact in revenue_facts:
        if fact.fiscal_year is None or not fact.fiscal_period:
            continue
        fp_upper = fact.fiscal_period.upper()
        if fp_upper in ("Q1", "Q2", "Q3", "Q4", "FY"):
            key = (fact.fiscal_year, fp_upper)
            existing = revenue_map.get(key)
            if existing is None or fact.filed_at >= existing.filed_at:
                revenue_map[key] = fact

    if not revenue_map:
        return FinancialSeries(
            ticker=ticker,
            cik=facts_data.cik,
            data_confidence="low",
            errors=["no_revenue_facts"],
        )

    period_keys = sorted(revenue_map.keys(), key=lambda k: (k[0], {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}.get(k[1], 0)))
    snapshots: list[QuarterlySnapshot] = []

    for fy, fp in period_keys:
        rev_fact = revenue_map.get((fy, fp))
        if rev_fact is None or rev_fact.value is None:
            continue
        period_end = rev_fact.period_end or date(fy, {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12, "FY": 12}[fp], 30)

        snapshot = QuarterlySnapshot(
            quarter_label=f"FY{fy}-{fp}",
            period_end=period_end,
            fiscal_year=fy,
            fiscal_period=fp,
            accession=rev_fact.accession,
            filed_at=rev_fact.filed_at.date() if rev_fact.filed_at else period_end,
            revenue=float(rev_fact.value),
        )

        cost_fact = _choose_fact(cost_rev_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        GP_fact = _choose_fact(gross_profit_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        snapshot.cost_of_revenue = _float_val(cost_fact)
        snapshot.gross_profit = _float_val(GP_fact)

        if snapshot.gross_profit is None and snapshot.cost_of_revenue is not None:
            snapshot.gross_profit = snapshot.revenue - snapshot.cost_of_revenue

        oi_fact = _choose_fact(op_income_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        ni_fact = _choose_fact(net_income_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        ocf_f = _choose_fact(ocf_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        capex_f = _choose_fact(capex_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        cash_f = _choose_fact(cash_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        ar_f = _choose_fact(ar_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        inv_f = _choose_fact(inv_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        shares_f = _choose_fact(shares_facts, period_end, fiscal_year=fy, fiscal_period=fp)
        sbc_f = _choose_fact(sbc_facts, period_end, fiscal_year=fy, fiscal_period=fp)

        snapshot.operating_income = _float_val(oi_fact)
        snapshot.net_income = _float_val(ni_fact)
        snapshot.operating_cash_flow = _float_val(ocf_f)
        snapshot.capital_expenditure = _float_val(capex_f)
        snapshot.accounts_receivable = _float_val(ar_f)
        snapshot.inventory = _float_val(inv_f)
        snapshot.diluted_shares = _float_val(shares_f)
        snapshot.stock_based_comp = _float_val(sbc_f)

        cash_val = _float_val(cash_f)
        if cash_val is not None:
            sti_val = 0.0
            for sti_candidates in st_inv_facts:
                if sti_candidates.period_end == period_end:
                    sti_val += _float_val(sti_candidates) or 0.0
            snapshot.cash = cash_val + sti_val

        total_debt = 0.0
        debt_has_data = False
        for dt_fact in debt_combined:
            if dt_fact.period_end and dt_fact.period_end == period_end:
                dv = _float_val(dt_fact) or 0.0
                total_debt += dv
                debt_has_data = True
        if debt_has_data:
            snapshot.total_debt = total_debt

        snapshots.append(snapshot)

    if len(snapshots) < min_quarters:
        series.quarters = snapshots
        series.data_confidence = "low"
        series.errors.append(f"only {len(snapshots)} quarters available, need {min_quarters}")
        return series

    series.quarters = snapshots
    series.data_confidence = "medium" if len(snapshots) >= 8 else "low"
    return series
