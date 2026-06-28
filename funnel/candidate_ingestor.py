# VERSION: 2026-06-22-CANDIDATE-ROUTING-FIX-1
# Funnel Pilot: compare scanner signals against Stock Summary USD

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Iterable

from funnel.signal_schema import Signal, normalise_ticker


CLASSIFICATION_RANK = {
    "actionable": 5,
    "wait": 4,
    "risk": 3,
    "near_miss": 2,
    "other": 1,
}

SCANNER_ORDER = {
    "congress": 1,
    "insider": 2,
    "vpma": 3,
    "fundamental_inflection": 4,
    "manual": 5,
    "gamma": 6,
    "earnings": 7,
}


STAGE_BY_CLASSIFICATION = {
    "actionable": "SHORTLISTED",
    "wait": "ENTRY_WATCH",
    "risk": "RESEARCH_RISK",
    "near_miss": "RESEARCH",
    "other": "MONITORING",
}


REVIEW_PRIORITY_BY_CLASSIFICATION = {
    "actionable": "HIGH",
    "wait": "MEDIUM",
    "near_miss": "OPTIONAL",
    "risk": "RISK_LOG_ONLY",
    "other": "MONITOR_ONLY",
}


# Only these classifications can place an absent ticker into the
# Pending_New_Tickers review queue.
PENDING_ELIGIBLE_CLASSIFICATIONS = {
    "actionable",
    "wait",
    "near_miss",
}


def build_ticker_index(
    ticker_records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Build a lookup table for the permanent Stock Summary USD universe.

    Column A ticker values are treated as the master matching key.
    Blank ticker rows are ignored.
    """
    ticker_index: dict[str, dict[str, Any]] = {}

    for record in ticker_records:
        raw_ticker = str(
            record.get("ticker") or ""
        ).strip()

        if not raw_ticker:
            continue

        ticker = normalise_ticker(
            raw_ticker
        )

        ticker_index[ticker] = dict(
            record
        )

    return ticker_index


def select_primary_signal(
    signals: Iterable[Signal],
) -> Signal:
    """
    Select the most important signal for a ticker.

    Classification takes precedence over score. Risk ranks above near-miss
    so that an adverse signal is not hidden by a weaker positive signal.
    """
    signal_list = list(
        signals
    )

    if not signal_list:
        raise ValueError(
            "Cannot select a primary signal from an empty list."
        )

    return max(
        signal_list,
        key=lambda signal: (
            CLASSIFICATION_RANK.get(
                signal.classification,
                0,
            ),
            (
                signal.score
                if signal.score is not None
                else -1.0
            ),
            signal.observed_at,
        ),
    )


def _is_pending_eligible(
    signal: Signal,
    existing: bool,
) -> bool:
    """
    Determine whether a signal should enter Pending_New_Tickers.

    Existing monitored tickers never enter the new-ticker queue.
    Risk-only signals remain in the signal log.
    """
    return (
        not existing
        and signal.classification
        in PENDING_ELIGIBLE_CLASSIFICATIONS
    )


def _review_route(
    signal: Signal,
    existing: bool,
) -> str:
    """Return the appropriate destination for the signal."""
    if existing:
        return "EXISTING_FUNNEL"

    if _is_pending_eligible(
        signal,
        existing=False,
    ):
        return "PENDING_NEW_TICKERS"

    return "SIGNAL_LOG_ONLY"


def _build_reason(
    signal: Signal,
) -> str:
    """Build a readable discovery explanation."""
    details = signal.details

    conviction = details.get(
        "conviction"
    )

    entry_quality = details.get(
        "entry_quality"
    )

    flow = str(
        details.get("flow") or ""
    ).strip()

    if signal.scanner == "congress":
        parts: list[str] = []
        buyers = details.get("buyers")
        cluster_buyers = details.get("cluster_buyers")
        active_purchases = details.get("active_trade_count")
        member_names = _names_as_text(signal)

        if buyers not in ("", None):
            try:
                buyers_int = int(float(buyers))
                parts.append(_count_phrase(buyers_int, "unique member"))
            except (TypeError, ValueError):
                pass
        if cluster_buyers not in ("", None):
            try:
                cluster_int = int(float(cluster_buyers))
                parts.append(_count_phrase(cluster_int, "recent cluster member"))
            except (TypeError, ValueError):
                pass
        if active_purchases not in ("", None):
            try:
                trades_int = int(float(active_purchases))
                parts.append(_count_phrase(trades_int, "active purchase"))
            except (TypeError, ValueError):
                pass
        if member_names:
            parts.append(f"Members: {member_names}")
        if conviction is not None:
            try:
                parts.append(f"Conviction {float(conviction):.1f}")
            except (TypeError, ValueError):
                pass
        if entry_quality is not None:
            try:
                parts.append(f"Entry quality {float(entry_quality):.1f}")
            except (TypeError, ValueError):
                pass
        if flow:
            parts.append(flow)
        return f"Political Disclosures: {', '.join(parts)}" if parts else "Political Disclosures"

    if signal.scanner == "vpma":
        setup_type = str(details.get("setup_type") or "").strip().replace("_", " ")
        confirmation_score = details.get("confirmation_score")
        parts = ["VPMA"]
        summary = setup_type or signal.classification.replace("_", " ")
        if summary:
            parts.append(summary)
        score_bits: list[str] = []
        if signal.score is not None:
            try:
                score_bits.append(f"core {float(signal.score):.1f}")
            except (TypeError, ValueError):
                pass
        if confirmation_score not in ("", None):
            try:
                score_bits.append(f"confirmation {float(confirmation_score):.1f}")
            except (TypeError, ValueError):
                pass
        text = ", ".join(score_bits)
        return f"VPMA: {summary}{', ' + text if text else ''}".strip()

    if signal.scanner == "insider":
        parts: list[str] = []
        unique_insiders = details.get("unique_insiders")
        roles = details.get("insider_roles")
        aggregate_purchase = details.get("aggregate_purchase_value")
        total_score = details.get("total_score") or signal.score
        entry_state = str(details.get("entry_state") or "").strip().replace("_", " ")

        if unique_insiders not in ("", None):
            try:
                parts.append(_count_phrase(int(float(unique_insiders)), "independent insider"))
            except (TypeError, ValueError):
                pass
        if roles:
            parts.append(f"Roles: {_csv_text(roles if isinstance(roles, (list, tuple, set)) else [roles])}")
        if aggregate_purchase not in ("", None):
            try:
                parts.append(f"Aggregate purchase ${float(aggregate_purchase):,.0f}")
            except (TypeError, ValueError):
                pass
        if total_score not in ("", None):
            try:
                parts.append(f"Score {float(total_score):.1f}")
            except (TypeError, ValueError):
                pass
        if entry_state:
            parts.append(entry_state.title())
        return f"Insider: {', '.join(parts)}" if parts else "Insider"

    parts = [
        signal.classification
        .replace("_", " ")
        .title()
    ]
    if flow:
        parts.append(flow)
    if entry_quality is not None:
        try:
            parts.append(f"entry quality {float(entry_quality):.1f}")
        except (TypeError, ValueError):
            pass
    return " | ".join(parts)


def _sorted_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in sorted(
        (str(value or "").strip() for value in values if str(value or "").strip()),
        key=lambda item: (SCANNER_ORDER.get(item, 999), item),
    ):
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _sorted_unique_text(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in sorted(str(value or "").strip() for value in values if str(value or "").strip()):
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _count_phrase(count: int, singular: str) -> str:
    label = singular if count == 1 else f"{singular}s"
    return f"{count} {label}"


def _csv_text(values: Iterable[Any]) -> str:
    return ", ".join(str(value).strip() for value in values if str(value).strip())


def _jsonish_text(values: Iterable[Any]) -> str:
    return json.dumps(list(values), sort_keys=False)


def _congress_details(signals: Iterable[Signal]) -> dict[str, Any]:
    congress_signal = next((signal for signal in signals if signal.scanner == "congress"), None)
    if congress_signal is None:
        return {}
    return {
        "congress_unique_members": _detail_value(congress_signal, "buyers"),
        "congress_recent_cluster_members": _detail_value(congress_signal, "cluster_buyers"),
        "congress_active_purchases": _detail_value(congress_signal, "active_trade_count"),
        "congress_member_names": _names_as_text(congress_signal),
    }


def _insider_details(signals: Iterable[Signal]) -> dict[str, Any]:
    insider_signal = next((signal for signal in signals if signal.scanner == "insider"), None)
    if insider_signal is None:
        return {}
    details = insider_signal.details
    return {
        "insider_total_score": _detail_value(insider_signal, "total_score"),
        "insider_conviction": _detail_value(insider_signal, "insider_conviction"),
        "insider_economic_commitment": _detail_value(insider_signal, "economic_commitment"),
        "insider_market_context": _detail_value(insider_signal, "market_context"),
        "insider_unique_insiders": _detail_value(insider_signal, "unique_insiders"),
        "insider_roles": _csv_text(details.get("insider_roles", [])),
        "insider_aggregate_purchase": _detail_value(insider_signal, "aggregate_purchase_value"),
        "insider_cluster_span_days": _detail_value(insider_signal, "cluster_span_days"),
        "insider_weighted_purchase_price": _detail_value(insider_signal, "weighted_purchase_price"),
        "insider_entry_state": str(details.get("entry_state") or "").strip(),
    }


def _source_breakdown(ticker_signals: list[Signal]) -> dict[str, Any]:
    positive = [signal.scanner for signal in ticker_signals if signal.classification in PENDING_ELIGIBLE_CLASSIFICATIONS]
    risk = [signal.scanner for signal in ticker_signals if signal.classification == "risk"]
    positive_sources = _sorted_unique(positive)
    risk_sources = _sorted_unique(risk)
    positive_count = len(positive_sources)
    corroboration = "NONE"
    if positive_count == 1:
        corroboration = "STANDARD"
    elif positive_count == 2:
        corroboration = "STRONG"
    elif positive_count >= 3:
        corroboration = "EXCEPTIONAL"
    return {
        "positive_sources": ", ".join(positive_sources),
        "risk_sources": ", ".join(risk_sources),
        "corroboration_level": corroboration,
        "conflict_status": "MIXED" if positive_sources and risk_sources else "CLEAR",
        "supporting_classifications": _csv_text(_sorted_unique_text(signal.classification for signal in ticker_signals)),
        "supporting_scores": _csv_text(
            f"{signal.scanner}:{float(signal.score):.1f}" if signal.score is not None else f"{signal.scanner}:"
            for signal in sorted(
                ticker_signals,
                key=lambda item: (SCANNER_ORDER.get(item.scanner, 999), item.ticker),
            )
        ),
        "supporting_reasons_text": " || ".join(_build_reason(signal) for signal in ticker_signals),
        "supporting_signal_ids_text": _csv_text(signal.signal_id for signal in ticker_signals),
    }


def _names_as_text(
    signal: Signal,
) -> str:
    """Convert disclosed names into a CSV-friendly string."""
    names = signal.details.get(
        "names"
    )

    if isinstance(
        names,
        (list, tuple, set),
    ):
        return ", ".join(
            str(name).strip()
            for name in names
            if str(name).strip()
        )

    return str(
        names or ""
    ).strip()


def _detail_value(
    signal: Signal,
    primary_key: str,
    fallback_key: str | None = None,
) -> Any:
    """Retrieve a scanner detail with optional backwards compatibility."""
    value = signal.details.get(
        primary_key
    )

    if (
        value is None
        and fallback_key is not None
    ):
        value = signal.details.get(
            fallback_key
        )

    return (
        ""
        if value is None
        else value
    )


def classify_signals(
    signals: Iterable[Signal],
    ticker_records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return one consolidated comparison record per signalled ticker.

    Important distinction:

    NEW_SIGNAL_TICKER
        The scanner found a ticker absent from Stock Summary USD.

    PENDING_NEW_TICKERS
        The absent ticker has an actionable, wait or qualifying near-miss
        signal and is suitable for manual review.

    SIGNAL_LOG_ONLY
        The absent ticker currently has a risk or monitoring-only signal.
    """
    ticker_index = build_ticker_index(
        ticker_records
    )

    by_ticker: dict[
        str,
        list[Signal],
    ] = defaultdict(list)

    for signal in signals:
        by_ticker[
            signal.ticker
        ].append(signal)

    comparison: list[
        dict[str, Any]
    ] = []

    for ticker, ticker_signals in by_ticker.items():
        primary = select_primary_signal(
            ticker_signals
        )
        source_breakdown = _source_breakdown(ticker_signals)

        existing_record = ticker_index.get(
            ticker
        )

        existing = (
            existing_record is not None
        )

        pending_eligible = (
            _is_pending_eligible(
                primary,
                existing,
            )
        )

        comparison.append(
            {
                "ticker": ticker,
                "already_in_stock_summary": (
                    "YES"
                    if existing
                    else "NO"
                ),
                "stock_summary_row": (
                    existing_record.get(
                        "sheet_row",
                        "",
                    )
                    if existing_record
                    else ""
                ),
                "google_ticker": (
                    existing_record.get(
                        "google_ticker",
                        "",
                    )
                    if existing_record
                    else ""
                ),
                "stock_name": (
                    existing_record.get(
                        "stock_name",
                        "",
                    )
                    if existing_record
                    else ""
                ),
                "candidate_status": (
                    "EXISTING_MONITORED_TICKER"
                    if existing
                    else "NEW_SIGNAL_TICKER"
                ),
                "pending_new_ticker": (
                    "YES"
                    if pending_eligible
                    else "NO"
                ),
                "review_route": _review_route(
                    primary,
                    existing,
                ),
                "review_priority": (
                    REVIEW_PRIORITY_BY_CLASSIFICATION.get(
                        primary.classification,
                        "MONITOR_ONLY",
                    )
                ),
                "scanner": primary.scanner,
                "all_sources": _sorted_unique(signal.scanner for signal in ticker_signals),
                "all_classifications": _sorted_unique_text(signal.classification for signal in ticker_signals),
                "all_signal_ids": [signal.signal_id for signal in ticker_signals],
                "supporting_reasons": [_build_reason(signal) for signal in ticker_signals],
                "positive_sources": source_breakdown["positive_sources"],
                "risk_sources": source_breakdown["risk_sources"],
                "corroboration_level": source_breakdown["corroboration_level"],
                "conflict_status": source_breakdown["conflict_status"],
                "supporting_classifications_text": source_breakdown["supporting_classifications"],
                "supporting_scores_text": source_breakdown["supporting_scores"],
                "supporting_reasons_text": source_breakdown["supporting_reasons_text"],
                "supporting_signal_ids_text": source_breakdown["supporting_signal_ids_text"],
                "classification": (
                    primary.classification
                ),
                "score": primary.score,
                "entry_quality": _detail_value(
                    primary,
                    "entry_quality",
                ),
                "estimated_capital_mid": (
                    _detail_value(
                        primary,
                        "estimated_capital_mid",
                        "bullish_capital_mid",
                    )
                ),
                "buyers": _detail_value(
                    primary,
                    "buyers",
                ),
                "cluster_buyers": (
                    _detail_value(
                        primary,
                        "cluster_buyers",
                    )
                ),
                "flow": str(
                    primary.details.get(
                        "flow"
                    )
                    or ""
                ).strip(),
                "names": _names_as_text(
                    primary
                ),
                "opportunity_stage": (
                    STAGE_BY_CLASSIFICATION.get(
                        primary.classification,
                        "MONITORING",
                    )
                ),
                "discovery_reason": " | ".join(
                    _build_reason(signal)
                    for signal in sorted(
                        ticker_signals,
                        key=lambda signal: (
                            SCANNER_ORDER.get(signal.scanner, 999),
                            -CLASSIFICATION_RANK.get(signal.classification, 0),
                            -(signal.score if signal.score is not None else -1.0),
                        ),
                    )
                ),
                "signal_count": len(
                    ticker_signals
                ),
                "observed_at": (
                    primary.observed_at
                ),
                "valid_until": (
                    primary.valid_until
                    or ""
                ),
                "signal_id": (
                    primary.signal_id
                ),
                **_congress_details(ticker_signals),
                **_insider_details(ticker_signals),
            }
        )

    return sorted(
        comparison,
        key=lambda row: (
            row["pending_new_ticker"]
            == "YES",
            CLASSIFICATION_RANK.get(
                str(
                    row["classification"]
                ),
                0,
            ),
            float(
                row["score"]
                or 0.0
            ),
            str(
                row["ticker"]
            ),
        ),
        reverse=True,
    )


def get_pending_new_ticker_records(
    comparison: Iterable[
        dict[str, Any]
    ],
) -> list[dict[str, Any]]:
    """
    Return only absent tickers approved by the routing rules for manual review.

    This function does not approve addition to Stock Summary USD. It only
    creates a review queue.
    """
    pending = [
        dict(record)
        for record in comparison
        if (
            record.get(
                "already_in_stock_summary"
            )
            == "NO"
            and record.get(
                "pending_new_ticker"
            )
            == "YES"
            and record.get(
                "review_route"
            )
            == "PENDING_NEW_TICKERS"
        )
    ]

    return sorted(
        pending,
        key=lambda row: (
            CLASSIFICATION_RANK.get(
                str(
                    row.get(
                        "classification"
                    )
                ),
                0,
            ),
            float(
                row.get("score")
                or 0.0
            ),
            str(
                row.get("ticker")
                or ""
            ),
        ),
        reverse=True,
    )
