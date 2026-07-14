from __future__ import annotations

from datetime import UTC, datetime

from providers.sec.base import SECProvider
from providers.sec.models import CompanyFacts, FinancialFact
from research.regulatory.identifiers import stable_hash
from research.regulatory.models import FinancialSnapshot


SHARE_COUNT_CONCEPTS = [
    "dei:EntityCommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesOutstanding",
]
TOTAL_DEBT_CONCEPTS = [
    "us-gaap:LongTermDebtAndCapitalLeaseObligations",
    "us-gaap:LongTermDebt",
    "us-gaap:DebtInstrumentCarryingAmount",
]
CASH_CONCEPTS = [
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]


def _latest_fact(facts: CompanyFacts, concepts: list[str]) -> FinancialFact | None:
    candidates: list[FinancialFact] = []
    for concept in concepts:
        candidates.extend(facts.facts.get(concept, []))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.filed_at)[-1]


def build_financial_snapshot(
    *,
    company_id: str,
    ticker: str,
    sec_provider: SECProvider,
    as_of: datetime | None = None,
    issued_shares_post_offering: float | None = None,
    pre_funded_warrants: float | None = None,
    exercised_unsettled_shares: float | None = None,
) -> FinancialSnapshot:
    now = as_of or datetime.now(UTC)
    facts = sec_provider.company_facts(ticker, as_of=now)
    share_fact = _latest_fact(facts, SHARE_COUNT_CONCEPTS)
    cash_fact = _latest_fact(facts, CASH_CONCEPTS)
    debt_fact = _latest_fact(facts, TOTAL_DEBT_CONCEPTS)
    cash_value = float(cash_fact.value) if cash_fact and cash_fact.value is not None else None
    return FinancialSnapshot(
        snapshot_id=stable_hash([company_id, now.date().isoformat()], prefix="fin"),
        company_id=company_id,
        as_of=now.date().isoformat(),
        common_shares=float(share_fact.value) if share_fact and share_fact.value is not None else None,
        issued_shares_post_offering=issued_shares_post_offering,
        pre_funded_warrants=pre_funded_warrants,
        exercised_unsettled_shares=exercised_unsettled_shares,
        parent_cash=cash_value,
        consolidated_cash=cash_value,
        attributable_cash=cash_value,
        total_debt=float(debt_fact.value) if debt_fact and debt_fact.value is not None else None,
        source_url=(share_fact.evidence.source_url if share_fact and share_fact.evidence else ""),
    )

