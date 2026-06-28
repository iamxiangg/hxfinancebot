from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from funnel.signal_schema import Signal
from scanners.vpma.engine import MODEL_VERSION, VpmaTickerResult, run_vpma_scan


logger = logging.getLogger(__name__)


RETAINED_CLASSIFICATIONS = {
    "actionable",
    "wait",
    "near_miss",
    "risk",
}


def _details_from_result(result: VpmaTickerResult) -> dict[str, Any]:
    details = dict(result.details)
    details.update(
        {
            "model_version": details.get("model_version") or MODEL_VERSION,
            "setup_type": result.setup_type,
            "reason": result.reason,
            "core_score": result.core_score,
            "event_score": result.event_score,
            "drift_score": result.drift_score,
            "entry_score": result.entry_score,
            "confirmation_score": result.confirmation_score,
            "data_confidence": result.data_confidence,
            "economic_classification": result.economic_classification,
            "economic_confirmation_score": result.economic_confirmation_score,
            "conflict_classification": result.conflict_classification,
            "guidance_action": result.guidance_action,
            "downgrade_reason": result.downgrade_reason,
        }
    )
    return details


def result_to_signal(result: VpmaTickerResult, observed_at: str) -> Signal | None:
    if result.classification not in RETAINED_CLASSIFICATIONS:
        return None

    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    valid_until = observed + timedelta(days=max(1, int(result.valid_for_days)))
    return Signal(
        ticker=result.ticker,
        scanner="vpma",
        classification=result.classification,
        score=result.core_score,
        observed_at=observed.isoformat(),
        valid_until=valid_until.isoformat(),
        details=_details_from_result(result),
    )


def run_vpma_adapter(*, observed_at: str | None = None) -> tuple[list[Signal], int]:
    scan = run_vpma_scan(observed_at=observed_at)
    actual_observed_at = observed_at or scan.observed_at
    signals: list[Signal] = []
    for result in scan.results:
        signal = result_to_signal(result, actual_observed_at)
        if signal is not None:
            signals.append(signal)

    logger.info(
        "VPMA engine scan: raw=%d eligible=%d liquid=%d recent_events=%d enriched=%d retained=%d",
        scan.counts.get("raw_universe_rows", 0),
        scan.counts.get("eligible_universe_tickers", 0),
        scan.counts.get("liquid_histories", 0),
        scan.counts.get("recent_event_tickers", 0),
        scan.counts.get("enriched_tickers", 0),
        len(signals),
    )
    return signals, scan.analysed_tickers
