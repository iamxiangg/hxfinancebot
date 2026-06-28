from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from funnel.signal_schema import Signal
from scanners.fundamental_inflection.engine import MODEL_VERSION, run_inflection_scan
from scanners.fundamental_inflection.models import InflectionResult


logger = logging.getLogger(__name__)

FUNNEL_CLASSIFICATIONS = {
    "STRONG_INFLECTION",
    "VALIDATED_INFLECTION",
    "EARLY_INFLECTION",
}


def result_to_signal(result: InflectionResult, observed_at: str) -> Signal | None:
    if result.classification not in FUNNEL_CLASSIFICATIONS:
        return None

    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    valid_until = observed + timedelta(days=max(1, int(result.valid_for_days)))

    signal_classification = "actionable"
    if result.classification == "EARLY_INFLECTION":
        signal_classification = "near_miss"

    return Signal(
        ticker=result.ticker,
        scanner="fundamental_inflection",
        classification=signal_classification,
        score=result.total_score,
        observed_at=observed.isoformat(),
        valid_until=valid_until.isoformat(),
        details={
            "model_version": MODEL_VERSION,
            "inflection_classification": result.classification,
            "total_score": result.total_score,
            "pillar_count": result.pilllar_count,
            "positive_pillars": result.positive_pillars,
            "economic_confirmation": result.economic_confirmation,
            "revenue_growth_yoy": result.revenue_growth_yoy,
            "prior_quarter_growth": result.prior_quarter_growth,
            "growth_acceleration": result.growth_acceleration,
            "gross_profit_growth": result.gross_profit_growth,
            "gross_margin_change_bps": result.gross_margin_change_bps,
            "operating_margin_change_bps": result.operating_margin_change_bps,
            "incremental_operating_margin": result.incremental_operating_margin,
            "ttm_fcf_margin": result.ttm_fcf_margin,
            "ttm_fcf_margin_change_bps": result.ttm_fcf_margin_change_bps,
            "diluted_share_growth": result.diluted_share_growth,
            "revenue_per_share_growth": result.revenue_per_share_growth,
            "cash": result.cash,
            "debt": result.debt,
            "cash_runway_months": result.cash_runway_months,
            "risk_flags": result.risk_flags,
            "data_confidence": result.data_confidence,
            "reason": result.reason,
            **result.details,
        },
    )


def run_fundamental_inflection_adapter(
    *,
    observed_at: str | None = None,
) -> tuple[list[Signal], int]:
    results = run_inflection_scan(observed_at=observed_at)
    actual_observed_at = observed_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    signals: list[Signal] = []
    for result in results:
        signal = result_to_signal(result, actual_observed_at)
        if signal is not None:
            signals.append(signal)

    logger.info(
        "Fundamental inflection scan: results=%d retained=%d",
        len(results),
        len(signals),
    )
    return signals, len(results)
