from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from funnel.insider_ledger import (
    load_qualified_purchases,
    load_processed_accessions,
    persist_ledger_rows,
    save_processed_accessions,
)
from funnel.signal_schema import Signal
from scanners.insider.engine import MODEL_VERSION, InsiderConfig, InsiderTickerResult, run_insider_scan


logger = logging.getLogger(__name__)


def _details_from_result(result: InsiderTickerResult) -> dict[str, Any]:
    details = dict(result.details)
    details.update(
        {
            "model_version": MODEL_VERSION,
            "reason": result.reason,
            "total_score": result.total_score,
            "insider_conviction": result.conviction_score,
            "economic_commitment": result.commitment_score,
            "market_context": result.market_context_score,
            "unique_insiders": result.unique_insiders,
            "operating_insiders": result.operating_insiders,
            "director_count": result.director_count,
            "purchase_event_count": result.purchase_event_count,
            "aggregate_purchase_value": result.aggregate_purchase_value,
            "largest_individual_purchase": result.largest_individual_purchase,
            "weighted_purchase_price": result.weighted_purchase_price,
            "cluster_span_days": result.cluster_span_days,
            "insider_names": result.insider_names,
            "insider_roles": result.insider_roles,
            "direct_purchase_count": result.direct_purchase_count,
            "indirect_purchase_count": result.indirect_purchase_count,
            "plan_10b5_1_count": result.plan_10b5_1_count,
            "entry_state": result.entry_state,
            "data_confidence": result.data_confidence,
            "risk_flags": result.risk_flags,
            "source_accessions": result.source_accessions,
        }
    )
    return details


def result_to_signal(result: InsiderTickerResult, observed_at: str) -> Signal | None:
    if result.classification not in {"actionable", "wait", "near_miss"}:
        return None
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    valid_until = observed + timedelta(days=max(1, int(result.valid_for_days)))
    return Signal(
        ticker=result.ticker,
        scanner="insider",
        classification=result.classification,
        score=result.total_score,
        observed_at=observed.isoformat(),
        valid_until=valid_until.isoformat(),
        details=_details_from_result(result),
    )


def run_insider_adapter(*, observed_at: str | None = None, persist_ledger: bool = True) -> tuple[list[Signal], int]:
    config = InsiderConfig.from_env()
    prior_accessions = load_processed_accessions()
    actual_observed_at = observed_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    observed_dt = datetime.fromisoformat(actual_observed_at.replace("Z", "+00:00"))
    prior_purchases = load_qualified_purchases(
        since=observed_dt.date() - timedelta(days=max(0, int(config.history_days))),
    )
    results, receipt = run_insider_scan(
        config=config,
        observed_at=actual_observed_at,
        prior_accessions=prior_accessions,
        prior_purchases=prior_purchases,
    )
    updated_accessions = set(prior_accessions).union(receipt.get("processed_accessions", []))
    save_processed_accessions(updated_accessions)

    if persist_ledger:
        persist_ledger_rows(list(receipt.get("ledger_rows", [])), observed_at=actual_observed_at)
    signals: list[Signal] = []
    for result in results:
        signal = result_to_signal(result, actual_observed_at)
        if signal is not None:
            signals.append(signal)

    logger.info(
        "Insider engine scan: entries=%d purchases=%d retained=%d",
        receipt.get("scanned_entries", 0),
        receipt.get("qualifying_purchases", 0),
        len(signals),
    )
    return signals, len(results)
