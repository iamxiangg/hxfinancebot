# NEW — Funnel Pilot Step 5: compare scanner signals with Stock Summary USD

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from funnel.signal_schema import Signal, normalise_ticker

CLASSIFICATION_RANK = {
    "actionable": 5,
    "wait": 4,
    "risk": 3,
    "near_miss": 2,
    "other": 1,
}

STAGE_BY_CLASSIFICATION = {
    "actionable": "SHORTLISTED",
    "wait": "ENTRY_WATCH",
    "risk": "RESEARCH_RISK",
    "near_miss": "RESEARCH",
    "other": "MONITORING",
}


def build_ticker_index(
    ticker_records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        normalise_ticker(record["ticker"]): dict(record)
        for record in ticker_records
    }


def select_primary_signal(signals: Iterable[Signal]) -> Signal:
    signal_list = list(signals)
    if not signal_list:
        raise ValueError("Cannot select a primary signal from an empty list")
    return max(
        signal_list,
        key=lambda signal: (
            CLASSIFICATION_RANK.get(signal.classification, 0),
            signal.score if signal.score is not None else -1.0,
            signal.observed_at,
        ),
    )


def classify_signals(
    signals: Iterable[Signal],
    ticker_records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one consolidated comparison record per signalled ticker."""
    ticker_index = build_ticker_index(ticker_records)
    by_ticker: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        by_ticker[signal.ticker].append(signal)

    comparison: list[dict[str, Any]] = []
    for ticker, ticker_signals in sorted(by_ticker.items()):
        primary = select_primary_signal(ticker_signals)
        existing = ticker_index.get(ticker)
        comparison.append(
            {
                "ticker": ticker,
                "already_in_stock_summary": bool(existing),
                "sheet_row": existing.get("sheet_row") if existing else "",
                "google_ticker": existing.get("google_ticker", "") if existing else "",
                "stock_name": existing.get("stock_name", "") if existing else "",
                "candidate_status": (
                    "EXISTING_MONITORED_TICKER"
                    if existing
                    else "NEW_CANDIDATE"
                ),
                "primary_scanner": primary.scanner,
                "primary_classification": primary.classification,
                "primary_score": primary.score,
                "latest_signal_date": primary.observed_at,
                "valid_until": primary.valid_until or "",
                "signal_count": len(ticker_signals),
                "opportunity_stage": STAGE_BY_CLASSIFICATION.get(
                    primary.classification, "MONITORING"
                ),
                "discovery_reason": _build_reason(primary),
            }
        )

    return sorted(
        comparison,
        key=lambda row: (
            row["already_in_stock_summary"],
            CLASSIFICATION_RANK.get(row["primary_classification"], 0),
            row["primary_score"] or 0.0,
        ),
        reverse=True,
    )


def _build_reason(signal: Signal) -> str:
    details = signal.details
    conviction = details.get("conviction")
    entry = details.get("entry_quality")
    flow = str(details.get("flow") or "").strip()
    parts = [signal.classification.replace("_", " ").title()]
    if conviction is not None:
        parts.append(f"Congress conviction {float(conviction):.0f}")
    if entry is not None:
        parts.append(f"entry quality {float(entry):.0f}")
    if flow:
        parts.append(flow)
    return " | ".join(parts)
