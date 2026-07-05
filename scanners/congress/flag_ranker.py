from __future__ import annotations

import os
from collections import Counter
from datetime import date, datetime
from typing import Any

from scanners.congress.digest_models import PoliticalDigestFlag, PoliticalDigestPlan
from scanners.congress.models import PoliticalArchiveStats, PoliticalBackfillStatus, TickerPoliticalHistory


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


def classify_release_type(event: dict[str, Any], *, bootstrap_run: bool = False) -> str:
    if str(event.get("event_type") or "").strip().upper() == "REMOVED_FROM_PAYLOAD":
        return "DATA_CORRECTION"
    if bool(event.get("is_materially_amended")):
        return "MATERIAL_AMENDMENT"
    transaction_age = int(event.get("transaction_age") or 0)
    days_to_file = event.get("days_to_file")
    if bootstrap_run and transaction_age > 14:
        return "HISTORICAL_BACKFILL"
    if transaction_age > 90:
        return "HISTORICAL_BACKFILL"
    if days_to_file not in ("", None):
        try:
            if int(days_to_file) > 45:
                return "LATE_DISCLOSURE"
        except (TypeError, ValueError):
            pass
    return "LIVE_DISCLOSURE"


def detect_backfill_status(
    *,
    bootstrap_run: bool,
    new_records: list[dict[str, Any]],
    material_amendments: list[dict[str, Any]],
    removed_events: list[dict[str, Any]],
    affected_tickers: list[str],
) -> PoliticalBackfillStatus:
    reasons: list[str] = []
    new_filing_count = len({str(item.get("filing_id") or item.get("filing_date") or item.get("trade_key") or "") for item in new_records if any(item.values())})
    if len(new_records) > int(_float_env("POLITICAL_BACKFILL_TRADE_THRESHOLD", 200.0)):
        reasons.append("trade_threshold")
    if new_filing_count > int(_float_env("POLITICAL_BACKFILL_FILING_THRESHOLD", 25.0)):
        reasons.append("filing_threshold")
    if len(affected_tickers) > int(_float_env("POLITICAL_BACKFILL_TICKER_THRESHOLD", 50.0)):
        reasons.append("ticker_threshold")
    return PoliticalBackfillStatus(
        probable_backfill=bool(reasons),
        bootstrap_run=bootstrap_run,
        new_trade_count=len(new_records),
        amended_trade_count=len(material_amendments),
        removed_trade_count=len(removed_events),
        new_filing_count=new_filing_count,
        affected_ticker_count=len(affected_tickers),
        reasons=tuple(reasons),
    )


def _rank_score(history: TickerPoliticalHistory) -> float:
    release_bonus = 0.0
    release_weights = {
        "LIVE_DISCLOSURE": 18.0,
        "MATERIAL_AMENDMENT": 14.0,
        "DATA_CORRECTION": 10.0,
        "LATE_DISCLOSURE": 6.0,
        "HISTORICAL_BACKFILL": -8.0,
    }
    for release_type in history.release_types:
        release_bonus = max(release_bonus, release_weights.get(release_type, 0.0))
    classification_bonus = {
        "BROAD_ACCUMULATION": 18.0,
        "REPEAT_FILER_ACCUMULATION": 14.0,
        "SINGLE_FILER_BULLISH_BET": 12.0,
        "MIXED_HIGH_ACTIVITY": 10.0,
        "DISTRIBUTION": 12.0,
    }.get(history.primary_classification, 0.0)
    change_bonus = 8.0 if history.classification_changed else 0.0
    options_bonus = 4.0 if history.structure_classification == "OPTIONS_LED" else 0.0
    confidence_bonus = {"HIGH": 8.0, "MEDIUM": 4.0}.get(history.inference_confidence, 0.0)
    data_bonus = {"HIGH": 6.0, "MEDIUM": 3.0}.get(history.data_confidence, 0.0)
    return (
        release_bonus
        + classification_bonus
        + change_bonus
        + options_bonus
        + confidence_bonus
        + data_bonus
        + history.bullish_evidence_score * 0.35
        + history.distribution_evidence_score * 0.25
        + history.breadth_score * 0.15
        + history.political_conviction * 0.15
        + history.entry_quality * 0.05
    )


def _flag_category(history: TickerPoliticalHistory) -> str:
    if history.primary_classification == "DISTRIBUTION":
        return "sale_flag"
    if history.primary_classification == "MIXED_HIGH_ACTIVITY":
        return "mixed_flag"
    if {"MATERIAL_AMENDMENT", "DATA_CORRECTION"} & set(history.release_types):
        return "data_quality_flag"
    return "purchase_flag"


def _is_material_purchase(history: TickerPoliticalHistory) -> bool:
    purchase_threshold = _float_env("POLITICAL_FLAG_PURCHASE_LOW", 100000.0)
    call_threshold = _float_env("POLITICAL_FLAG_CALL_LOW", 100000.0)
    window = history.windows.get(90) or history.windows.get(45)
    if window is None:
        return False
    return window.stock_purchase_low >= purchase_threshold or window.call_purchase_low >= call_threshold


def _is_material_sale(history: TickerPoliticalHistory) -> bool:
    sale_threshold = _float_env("POLITICAL_FLAG_SALE_LOW", 100000.0)
    window = history.windows.get(90) or history.windows.get(45)
    if window is None:
        return False
    return window.sale_low >= sale_threshold or history.distribution_evidence_score >= 45.0


def _is_material_classification_change(history: TickerPoliticalHistory) -> bool:
    if not history.classification_changed:
        return False
    if history.primary_classification in {"BROAD_ACCUMULATION", "DISTRIBUTION", "MIXED_HIGH_ACTIVITY"}:
        return True
    if history.primary_classification == "SINGLE_FILER_BULLISH_BET":
        return _is_material_purchase(history)
    if history.primary_classification == "REPEAT_FILER_ACCUMULATION":
        return _is_material_purchase(history)
    return False


def _qualifies_for_digest(history: TickerPoliticalHistory) -> bool:
    if {"MATERIAL_AMENDMENT", "DATA_CORRECTION"} & set(history.release_types):
        return True
    if _is_material_classification_change(history):
        return True
    if history.primary_classification == "BROAD_ACCUMULATION":
        return True
    if history.primary_classification in {"DISTRIBUTION", "MIXED_HIGH_ACTIVITY"} and _is_material_sale(history):
        return True
    if history.primary_classification == "SINGLE_FILER_BULLISH_BET" and _is_material_purchase(history):
        return True
    if history.primary_classification == "REPEAT_FILER_ACCUMULATION" and _is_material_purchase(history):
        return True
    return False


def _exceptional(history: TickerPoliticalHistory) -> bool:
    return (
        _is_material_classification_change(history)
        or "LIVE_DISCLOSURE" in history.release_types
        or "MATERIAL_AMENDMENT" in history.release_types
        or history.primary_classification in {"BROAD_ACCUMULATION", "DISTRIBUTION", "MIXED_HIGH_ACTIVITY"}
    )


def build_digest_plan(
    *,
    histories: dict[str, TickerPoliticalHistory],
    affected_tickers: list[str],
    backfill_status: PoliticalBackfillStatus,
    previous_digest_rows: list[dict[str, Any]],
    digest_date: str,
    archive_stats: PoliticalArchiveStats,
) -> PoliticalDigestPlan:
    previous_hash_by_ticker = {
        str(row.get("Ticker") or "").strip().upper(): str(row.get("Summary Hash") or "").strip()
        for row in previous_digest_rows
        if str(row.get("Ticker") or "").strip()
    }
    affected_set = {ticker.upper() for ticker in affected_tickers}
    relevant_histories = [history for ticker, history in sorted(histories.items()) if ticker.upper() in affected_set and history.new_events]
    relevant_histories = [
        history
        for history in relevant_histories
        if history.summary_hash != previous_hash_by_ticker.get(history.ticker.upper()) or history.classification_changed
    ]
    relevant_histories = [history for history in relevant_histories if _qualifies_for_digest(history)]
    if backfill_status.bootstrap_run:
        relevant_histories = [
            history
            for history in relevant_histories
            if {"LIVE_DISCLOSURE", "MATERIAL_AMENDMENT", "DATA_CORRECTION"} & set(history.release_types)
        ]
    ranked = sorted(
        relevant_histories,
        key=lambda history: (_rank_score(history), history.ticker),
        reverse=True,
    )

    max_detailed = int(_float_env("POLITICAL_DIGEST_MAX_DETAILED_FLAGS", 3.0))
    hard_max = int(_float_env("POLITICAL_DIGEST_HARD_MAX_DETAILED_FLAGS", 5.0))
    detail_limit = max(0, min(max_detailed, hard_max))
    detailed_flags: list[PoliticalDigestFlag] = []
    compact_flags: list[PoliticalDigestFlag] = []
    for index, history in enumerate(ranked):
        flag = PoliticalDigestFlag(
            ticker=history.ticker,
            rank_score=_rank_score(history),
            flag_category=_flag_category(history),
            flag_reasons=tuple(history.flag_reasons),
            history=history,
            release_types=history.release_types,
            trigger_trade_keys=history.latest_trigger_trade_keys,
            detailed=index < detail_limit and (not backfill_status.probable_backfill or _exceptional(history)),
            exceptional=_exceptional(history),
        )
        if flag.detailed:
            detailed_flags.append(flag)
        else:
            compact_flags.append(flag)

    if backfill_status.probable_backfill:
        compact_flags.extend(flag for flag in detailed_flags[detail_limit:])
        detailed_flags = detailed_flags[:detail_limit]

    data_status = {
        "new_records": backfill_status.new_trade_count,
        "material_amendments": backfill_status.amended_trade_count,
        "historical_backfills": sum("HISTORICAL_BACKFILL" in history.release_types for history in relevant_histories),
        "data_corrections": sum("DATA_CORRECTION" in history.release_types for history in relevant_histories),
        "affected_tickers": len(affected_tickers),
        "detailed_flags": len(detailed_flags),
        "compact_activity_count": len(compact_flags),
        "recorded_only_count": max(0, len(affected_tickers) - len(relevant_histories)),
    }
    release_counter = Counter(release for history in relevant_histories for release in history.release_types)
    summary_lines = tuple(f"{name}: {release_counter.get(name, 0)}" for name in sorted(release_counter))

    send_digest = bool(detailed_flags or compact_flags)
    if backfill_status.probable_backfill or (_bool_env("POLITICAL_DIGEST_SEND_EMPTY", False) and not send_digest):
        send_digest = True

    return PoliticalDigestPlan(
        digest_date=digest_date,
        data_status=data_status,
        detailed_flags=tuple(detailed_flags),
        compact_flags=tuple(compact_flags),
        recorded_only_count=data_status["recorded_only_count"],
        backfill_status=backfill_status,
        archive_stats=archive_stats,
        send_digest=send_digest,
        summary_lines=summary_lines,
    )
