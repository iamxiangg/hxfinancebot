from __future__ import annotations

from scanners.no_llm_guard import require_no_llm
from research.regulatory.identifiers import stable_hash
from research.regulatory.models import (
    CompanyOperatingMode,
    FinancialSnapshot,
    ValuationAssumption,
    ValuationSnapshot,
    ValuationStatus,
)

require_no_llm()


def _market_implied_probability(current_ev: float | None, failure_ev: float | None, success_ev: float | None) -> float | None:
    if current_ev is None or failure_ev is None or success_ev is None:
        return None
    denominator = success_ev - failure_ev
    if denominator == 0:
        return None
    return (current_ev - failure_ev) / denominator


def compute_valuation_snapshot(
    *,
    company_id: str,
    programme_key: str,
    assumption: ValuationAssumption | None,
    financial_snapshot: FinancialSnapshot | None,
    economic_attribution_percentage: float | None = None,
) -> ValuationSnapshot:
    if assumption is None or not assumption.active:
        return ValuationSnapshot(
            valuation_id=stable_hash([company_id, programme_key, "missing"], prefix="val"),
            programme_key=programme_key,
            company_id=company_id,
            valuation_status=ValuationStatus.MODEL_INCOMPLETE,
            notes=["No active valuation assumptions."],
        )
    required = {
        CompanyOperatingMode.CLINICAL_STAGE: ["success_ev", "failure_ev", "current_ev"],
        CompanyOperatingMode.PRE_COMMERCIAL: ["success_ev", "failure_ev", "current_ev"],
        CompanyOperatingMode.HOLDING_COMPANY: ["success_ev", "failure_ev", "current_ev"],
    }.get(assumption.operating_mode, [])
    missing = [field for field in required if getattr(assumption, field) is None]
    if missing:
        return ValuationSnapshot(
            valuation_id=stable_hash([company_id, programme_key, "incomplete"], prefix="val"),
            programme_key=programme_key,
            company_id=company_id,
            valuation_status=ValuationStatus.MODEL_INCOMPLETE,
            notes=[f"Missing assumptions: {', '.join(missing)}"],
        )
    probability = _market_implied_probability(assumption.current_ev, assumption.failure_ev, assumption.success_ev)
    attributable = assumption.success_ev
    pct = economic_attribution_percentage if economic_attribution_percentage is not None else 100.0
    if attributable is not None:
        attributable = attributable * (pct / 100.0)
    equity_value = attributable
    if financial_snapshot is not None:
        if financial_snapshot.attributable_cash is not None:
            equity_value = (equity_value or 0.0) + financial_snapshot.attributable_cash
        if financial_snapshot.total_debt is not None:
            equity_value = (equity_value or 0.0) - financial_snapshot.total_debt
        if assumption.future_dilution is not None:
            equity_value = (equity_value or 0.0) - assumption.future_dilution
    per_share = None
    if equity_value is not None and financial_snapshot is not None and financial_snapshot.economic_shares > 0:
        per_share = equity_value / financial_snapshot.economic_shares
    status = ValuationStatus.INSUFFICIENT_DATA
    if probability is not None:
        if probability < 0.25:
            status = ValuationStatus.DE_RISKING_NOT_PRICED
        elif probability < 0.5:
            status = ValuationStatus.PARTIALLY_PRICED
        elif probability < 0.8:
            status = ValuationStatus.FAIRLY_PRICED
        else:
            status = ValuationStatus.PRICING_SUBSTANTIAL_SUCCESS
    return ValuationSnapshot(
        valuation_id=stable_hash([company_id, programme_key, assumption.updated_at or "now"], prefix="val"),
        programme_key=programme_key,
        company_id=company_id,
        valuation_status=status,
        attributable_value=attributable,
        success_ev=assumption.success_ev,
        failure_ev=assumption.failure_ev,
        current_ev=assumption.current_ev,
        market_implied_probability=probability,
        equity_value=equity_value,
        per_share_value=per_share,
        updated_at=assumption.updated_at,
    )

