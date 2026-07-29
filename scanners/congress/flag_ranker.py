from __future__ import annotations

import os
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any

from scanners.congress.digest_models import PoliticalDigestFlag, PoliticalDigestPlan
from scanners.congress.models import PoliticalArchiveStats, PoliticalBackfillStatus, TickerPoliticalHistory
from scanners.congress.state_changes import detect_material_state_changes
from scanners.congress.watchlist import PoliticalWatchlistConfig, load_watchlist_state_from_row, update_watchlist_state


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
    if history.primary_classification in {"SINGLE_FILER_BULLISH_BET", "REPEAT_FILER_ACCUMULATION"}:
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
    if history.primary_classification in {"SINGLE_FILER_BULLISH_BET", "REPEAT_FILER_ACCUMULATION"} and _is_material_purchase(history):
        return True
    return False


def _exceptional(history: TickerPoliticalHistory) -> bool:
    return (
        _is_material_classification_change(history)
        or "LIVE_DISCLOSURE" in history.release_types
        or "MATERIAL_AMENDMENT" in history.release_types
        or history.primary_classification in {"BROAD_ACCUMULATION", "DISTRIBUTION", "MIXED_HIGH_ACTIVITY"}
    )


def _qualifies_for_other_activity(history: TickerPoliticalHistory) -> bool:
    if history.primary_classification == "INSUFFICIENT_EVIDENCE":
        return False
    if "HISTORICAL_BACKFILL" in history.release_types and set(history.release_types) == {"HISTORICAL_BACKFILL"}:
        return False
    window = history.windows.get(90) or history.windows.get(45)
    if window is None:
        return False
    compact_threshold = _float_env("POLITICAL_DIGEST_COMPACT_ACTIVITY_LOW", 25000.0)
    return (
        window.stock_purchase_low >= compact_threshold
        or window.call_purchase_low >= compact_threshold
        or window.sale_low >= compact_threshold
        or window.unique_buyer_count >= 2
        or history.primary_classification in {"DISTRIBUTION", "MIXED_HIGH_ACTIVITY"}
    )


def _age_days(value: str, observed_at: datetime) -> int | None:
    if not value:
        return None
    try:
        item_date = datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None
    return (observed_at.date() - item_date).days


def _has_rolling_activity(
    history: TickerPoliticalHistory,
    *,
    observed_at: datetime,
    filing_lookback_days: int,
    transaction_max_age_days: int,
) -> bool:
    filing_age = _age_days(history.latest_filing_date, observed_at)
    transaction_age = _age_days(history.latest_transaction_date, observed_at)
    if filing_age is None or filing_age < 0 or filing_age > filing_lookback_days:
        return False
    if transaction_age is None or transaction_age < 0 or transaction_age > transaction_max_age_days:
        return False
    window = history.windows.get(transaction_max_age_days) or history.windows.get(90) or history.windows.get(45)
    if window is None:
        return False
    compact_threshold = _float_env("POLITICAL_DIGEST_COMPACT_ACTIVITY_LOW", 25000.0)
    material_purchase_threshold = _float_env("POLITICAL_FLAG_PURCHASE_LOW", 100000.0)
    material_call_threshold = _float_env("POLITICAL_FLAG_CALL_LOW", 100000.0)
    material_sale_threshold = _float_env("POLITICAL_FLAG_SALE_LOW", 100000.0)
    return (
        window.stock_purchase_low >= min(compact_threshold, material_purchase_threshold)
        or window.call_purchase_low >= min(compact_threshold, material_call_threshold)
        or window.sale_low >= min(compact_threshold, material_sale_threshold)
        or window.unique_buyer_count >= 2
        or history.primary_classification in {"BROAD_ACCUMULATION", "DISTRIBUTION", "MIXED_HIGH_ACTIVITY"}
    )


def _material_update_priority(flag: PoliticalDigestFlag) -> tuple[int, float, str]:
    priorities = {
        "ENTRY_BECAME_ACTIONABLE": 1,
        "CLASSIFICATION_DOWNGRADE": 2,
        "RISK_ESCALATION": 3,
        "CLASSIFICATION_UPGRADE": 4,
        "MATERIAL_AMENDMENT": 5,
        "BREADTH_CHANGE": 6,
        "CONCENTRATION_CHANGE": 7,
        "RISK_RESOLUTION": 8,
        "DATA_CORRECTION": 9,
        "WATCHLIST_REACTIVATED": 10,
    }
    best = min((priorities.get(change.change_type, 99) for change in flag.material_changes), default=99)
    return (-best, flag.rank_score, flag.ticker)


def _watchlist_priority(flag: PoliticalDigestFlag) -> tuple[float, float, float, float, float, float, str]:
    state = flag.watchlist_state
    history = flag.history
    if state is None:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, flag.ticker)
    return (
        1.0 if state.current_entry_category == "ACTIONABLE" else 0.0,
        1.0 if state.watchlist_retention_type == "EXCEPTIONAL" else 0.0,
        history.political_conviction,
        history.breadth_score,
        _rank_score(history),
        -float(state.watchlist_day),
        flag.ticker,
    )


def _rolling_activity_priority(history: TickerPoliticalHistory) -> tuple[float, float, float, float, str]:
    return (
        history.political_conviction,
        history.entry_quality,
        history.breadth_score,
        _rank_score(history),
        history.ticker,
    )


def _previous_detailed_hash(
    ticker: str,
    previous_summary_rows: dict[str, dict[str, Any]],
    previous_digest_rows: list[dict[str, Any]],
) -> str:
    row = previous_summary_rows.get(ticker, {})
    value = str(row.get("Last Detailed Summary Hash") or row.get("Summary Hash") or "").strip()
    if value:
        return value
    for digest_row in reversed(previous_digest_rows):
        if str(digest_row.get("Ticker") or "").strip().upper() == ticker:
            return str(digest_row.get("Summary Hash") or "").strip()
    return ""


def build_digest_plan(
    *,
    histories: dict[str, TickerPoliticalHistory],
    affected_tickers: list[str],
    backfill_status: PoliticalBackfillStatus,
    previous_digest_rows: list[dict[str, Any]],
    previous_summary_rows: dict[str, dict[str, Any]],
    digest_date: str,
    archive_stats: PoliticalArchiveStats,
    observed_at: datetime,
    review_required_items: list[dict[str, Any]] | None = None,
    excluded_items: list[dict[str, Any]] | None = None,
    source_health: str = "HEALTHY",
    payload_refreshed: bool = True,
    source_record_count: int = 0,
    unnotified_trade_keys: set[str] | None = None,
) -> PoliticalDigestPlan:
    config = PoliticalWatchlistConfig.from_env()
    observed_iso = observed_at.replace(microsecond=0).isoformat()
    affected_set = {ticker.upper() for ticker in affected_tickers}
    review_required_items = review_required_items or []
    excluded_items = excluded_items or []
    unnotified_trade_keys = unnotified_trade_keys or set()
    max_detailed = int(_float_env("POLITICAL_DIGEST_MAX_DETAILED_FLAGS", 3.0))
    hard_max = int(_float_env("POLITICAL_DIGEST_HARD_MAX_DETAILED_FLAGS", 5.0))
    detail_limit = max(0, min(max_detailed, hard_max))
    rolling_filing_lookback_days = int(_float_env("POLITICAL_DIGEST_ROLLING_LOOKBACK_DAYS", 45.0))
    rolling_transaction_max_age_days = int(_float_env("POLITICAL_DIGEST_ROLLING_TRANSACTION_MAX_AGE_DAYS", 90.0))
    rolling_activity_limit = int(_float_env("POLITICAL_DIGEST_MAX_ROLLING_ACTIVITY_ITEMS", 12.0))

    material_candidates: list[TickerPoliticalHistory] = []
    other_new_candidates: list[TickerPoliticalHistory] = []
    for ticker, history in sorted(histories.items()):
        fresh_trigger = ticker.upper() in affected_set and bool(history.new_events)
        rolling_trigger = (
            rolling_filing_lookback_days > 0
            and rolling_transaction_max_age_days > 0
            and not fresh_trigger
            and _has_rolling_activity(
                history,
                observed_at=observed_at,
                filing_lookback_days=rolling_filing_lookback_days,
                transaction_max_age_days=rolling_transaction_max_age_days,
            )
        )
        if not fresh_trigger and not rolling_trigger:
            continue
        if fresh_trigger and history.summary_hash == _previous_detailed_hash(ticker, previous_summary_rows, previous_digest_rows) and not (
            set(history.latest_trigger_trade_keys) & unnotified_trade_keys
        ):
            continue
        if _qualifies_for_digest(history):
            if rolling_trigger and not fresh_trigger:
                other_new_candidates.append(history)
            elif not backfill_status.bootstrap_run or {"LIVE_DISCLOSURE", "MATERIAL_AMENDMENT", "DATA_CORRECTION"} & set(history.release_types):
                material_candidates.append(history)
            elif _qualifies_for_other_activity(history):
                other_new_candidates.append(history)
        elif _qualifies_for_other_activity(history):
            other_new_candidates.append(history)

    ranked_material = sorted(material_candidates, key=lambda history: (_rank_score(history), history.ticker), reverse=True)
    new_material_flags: list[PoliticalDigestFlag] = []
    for history in ranked_material:
        if len(new_material_flags) >= detail_limit:
            break
        if backfill_status.probable_backfill and not _exceptional(history):
            continue
        new_material_flags.append(
            PoliticalDigestFlag(
                ticker=history.ticker,
                rank_score=_rank_score(history),
                section="NEW_MATERIAL_SIGNALS",
                flag_category=_flag_category(history),
                flag_reasons=tuple(history.flag_reasons),
                history=history,
                release_types=history.release_types,
                trigger_trade_keys=history.latest_trigger_trade_keys,
                detailed=True,
                exceptional=_exceptional(history),
            )
        )
    new_material_tickers = {flag.ticker for flag in new_material_flags}

    current_states: dict[str, Any] = {}
    material_updates: list[PoliticalDigestFlag] = []
    expired_watchlist_items: list[PoliticalDigestFlag] = []
    for ticker, history in sorted(histories.items()):
        previous_state = load_watchlist_state_from_row(previous_summary_rows.get(ticker))
        current_state = update_watchlist_state(
            previous_state,
            history,
            observed_at=observed_at,
            config=config,
            detailed_material_flag=ticker in new_material_tickers,
            bootstrap_run=backfill_status.bootstrap_run,
        )
        material_changes = detect_material_state_changes(previous_state, current_state, history, config=config)
        if ticker in new_material_tickers:
            release_type = "NEW_DISCLOSURE"
            if "DATA_CORRECTION" in history.release_types:
                release_type = "DATA_CORRECTION"
            elif "MATERIAL_AMENDMENT" in history.release_types:
                release_type = "MATERIAL_AMENDMENT"
            elif previous_state is not None and previous_state.watchlist_status in {"EXPIRED", "RESOLVED"}:
                release_type = "WATCHLIST_REACTIVATED"
            current_state = replace(
                current_state,
                last_material_change_at=observed_iso,
                last_material_change_type=release_type,
                last_material_change_reason=current_state.latest_material_event,
            )
        elif material_changes:
            first_change = material_changes[0]
            current_state = replace(
                current_state,
                last_material_change_at=observed_iso,
                last_material_change_type=first_change.change_type,
                last_material_change_reason=first_change.reason,
                material_change_types=tuple(change.change_type for change in material_changes),
                material_change_reasons=tuple(change.reason for change in material_changes),
            )
        current_states[ticker] = current_state
        if previous_state is not None and previous_state.watchlist_status == "ACTIVE" and current_state.watchlist_status == "EXPIRED" and config.send_expired_notice:
            expired_watchlist_items.append(
                PoliticalDigestFlag(
                    ticker=ticker,
                    rank_score=_rank_score(history),
                    section="EXPIRED_WATCHLIST_ITEMS",
                    flag_category="watchlist_expired",
                    flag_reasons=("Watchlist retention elapsed.",),
                    history=history,
                    release_types=history.release_types,
                    trigger_trade_keys=history.latest_trigger_trade_keys,
                    detailed=False,
                    exceptional=False,
                    watchlist_state=current_state,
                )
            )
        if ticker in new_material_tickers or previous_state is None:
            continue
        if not (previous_state.first_flagged_at or previous_state.watchlist_status):
            continue
        if not material_changes:
            continue
        if current_state.current_detailed_summary_hash == previous_state.last_detailed_summary_hash:
            continue
        material_updates.append(
            PoliticalDigestFlag(
                ticker=ticker,
                rank_score=_rank_score(history),
                section="MATERIAL_SIGNAL_UPDATES",
                flag_category="material_update",
                flag_reasons=tuple(change.reason for change in material_changes),
                history=history,
                release_types=history.release_types,
                trigger_trade_keys=history.latest_trigger_trade_keys,
                detailed=True,
                exceptional=_exceptional(history),
                watchlist_state=current_state,
                material_changes=tuple(material_changes),
                update_heading="POLITICAL SIGNAL UPDATE",
            )
        )
    material_updates = sorted(material_updates, key=_material_update_priority, reverse=True)
    material_update_tickers = {flag.ticker for flag in material_updates}

    watchlist_candidates: list[PoliticalDigestFlag] = []
    for ticker, state in sorted(current_states.items()):
        if ticker in new_material_tickers or ticker in material_update_tickers:
            continue
        if not config.enabled or config.max_watchlist_items == 0:
            continue
        if state.watchlist_status != "ACTIVE" or not state.reminder_due:
            continue
        history = histories[ticker]
        watchlist_candidates.append(
            PoliticalDigestFlag(
                ticker=ticker,
                rank_score=_rank_score(history),
                section="ACTIVE_POLITICAL_WATCHLIST",
                flag_category="watchlist_reminder",
                flag_reasons=(state.latest_material_event or "No new political disclosure",),
                history=history,
                release_types=history.release_types,
                trigger_trade_keys=history.latest_trigger_trade_keys,
                detailed=False,
                exceptional=state.watchlist_retention_type == "EXCEPTIONAL",
                watchlist_state=state,
            )
        )
    ranked_watchlist = sorted(watchlist_candidates, key=_watchlist_priority, reverse=True)
    active_watchlist_items = ranked_watchlist[: max(0, config.max_watchlist_items)]
    hidden_watchlist_count = max(0, len(ranked_watchlist) - len(active_watchlist_items))

    shown_tickers = new_material_tickers | material_update_tickers | {flag.ticker for flag in active_watchlist_items}
    ranked_other_activity = [
        PoliticalDigestFlag(
            ticker=history.ticker,
            rank_score=_rank_score(history),
            section="OTHER_NEW_ACTIVITY",
            flag_category=_flag_category(history),
            flag_reasons=tuple(history.flag_reasons),
            history=history,
            release_types=history.release_types,
            trigger_trade_keys=history.latest_trigger_trade_keys,
            detailed=False,
            exceptional=_exceptional(history),
            watchlist_state=current_states.get(history.ticker),
        )
        for history in sorted(
            [*other_new_candidates, *(history for history in ranked_material if history.ticker not in new_material_tickers)],
            key=_rolling_activity_priority,
            reverse=True,
        )
        if history.ticker not in shown_tickers
    ]
    other_new_activity = ranked_other_activity[: max(0, rolling_activity_limit)]
    hidden_rolling_count = max(0, len(ranked_other_activity) - len(other_new_activity))

    data_status = {
        "fetched_records": source_record_count,
        "new_records": backfill_status.new_trade_count,
        "material_amendments": backfill_status.amended_trade_count,
        "historical_backfills": sum("HISTORICAL_BACKFILL" in history.release_types for history in material_candidates + other_new_candidates),
        "data_corrections": sum("DATA_CORRECTION" in history.release_types for history in material_candidates + other_new_candidates),
        "affected_tickers": len(affected_set),
        "review_required": len(review_required_items),
        "excluded_records": len(excluded_items),
        "new_material_signals": len(new_material_flags),
        "material_updates": len(material_updates),
        "active_watchlist_reminders": len(active_watchlist_items),
        "other_new_activity": len(other_new_activity),
        "detailed_flags": len(new_material_flags),
        "compact_activity_count": len(other_new_activity),
        "recorded_only_count": max(0, len(affected_set) - len(new_material_tickers | material_update_tickers | {flag.ticker for flag in other_new_activity})),
        "hidden_rolling_activity": hidden_rolling_count,
    }
    changes_since_previous = {
        "new_qualifying_tickers": len(new_material_flags),
        "new_disclosures_on_active_tickers": sum(1 for flag in new_material_flags if str(previous_summary_rows.get(flag.ticker, {}).get("Watchlist Status") or "").strip().upper() == "ACTIVE"),
        "classification_changes": sum(1 for flag in [*new_material_flags, *material_updates] if flag.history.classification_changed),
        "material_amendments": sum("MATERIAL_AMENDMENT" in flag.release_types for flag in [*new_material_flags, *material_updates]),
        "expired_tickers": len(expired_watchlist_items),
    }
    release_counter = Counter(release for history in material_candidates + other_new_candidates for release in history.release_types)
    summary_lines = tuple(
        [*(f"Release mix {name}: {release_counter.get(name, 0)}" for name in sorted(release_counter))]
        + ([f"{hidden_watchlist_count} additional active signals remain recorded in Political_Ticker_Summary."] if hidden_watchlist_count else [])
        + ([f"{hidden_rolling_count} additional rolling activity ticker(s) hidden by the digest cap."] if hidden_rolling_count else [])
    )
    send_digest = bool(new_material_flags or material_updates or active_watchlist_items or other_new_activity)
    if _bool_env("POLITICAL_DIGEST_SEND_EMPTY", False) and not send_digest:
        send_digest = True

    return PoliticalDigestPlan(
        digest_date=digest_date,
        data_status=data_status,
        source_health=source_health,
        payload_refreshed=payload_refreshed,
        changes_since_previous=changes_since_previous,
        new_material_flags=tuple(new_material_flags),
        material_updates=tuple(material_updates),
        active_watchlist_items=tuple(active_watchlist_items),
        other_new_activity=tuple(other_new_activity),
        review_required_items=tuple(dict(item) for item in review_required_items),
        excluded_items=tuple(dict(item) for item in excluded_items),
        expired_watchlist_items=tuple(expired_watchlist_items),
        watchlist_state_changes=tuple(material_updates + expired_watchlist_items),
        recorded_only_count=data_status["recorded_only_count"],
        backfill_status=backfill_status,
        archive_stats=archive_stats,
        send_digest=send_digest,
        summary_lines=summary_lines,
        current_watchlist_states=current_states,
        delivered_watchlist_updates=dict(previous_summary_rows),
        hidden_watchlist_count=hidden_watchlist_count,
        delivery_reconciliation={
            "valid_new_or_amended_records": backfill_status.new_trade_count + backfill_status.amended_trade_count,
            "included_in_digest": len(new_material_flags) + len(material_updates) + len(active_watchlist_items) + len(other_new_activity),
            "review_required": len(review_required_items),
            "successfully_delivered": 0,
            "pending_retry": backfill_status.new_trade_count + backfill_status.amended_trade_count,
        },
    )
