from __future__ import annotations

import json
import os
import hashlib
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from scanners.congress.models import PoliticalWatchlistState, TickerPoliticalHistory


ENTRY_CATEGORIES = {"ACTIONABLE", "WAIT", "RISK", "CONTEXT", "OTHER"}
WATCHLIST_STATUSES = {"ACTIVE", "EXPIRED", "RESOLVED", "SUPPRESSED"}
RETENTION_TYPES = {"STANDARD", "EXCEPTIONAL", "RISK", "MANUAL"}
MATERIAL_RELEASE_TYPES = {"LIVE_DISCLOSURE", "MATERIAL_AMENDMENT", "DATA_CORRECTION", "LATE_DISCLOSURE"}


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; received {raw!r}.") from exc


def _float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric; received {raw!r}.") from exc


def _parse_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _normalise_status(value: str) -> str:
    status = str(value or "").strip().upper()
    return status if status in WATCHLIST_STATUSES else ""


def _normalise_entry_category(value: str) -> str:
    category = str(value or "").strip().upper()
    if category not in ENTRY_CATEGORIES:
        return "OTHER"
    return category


def _json_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if isinstance(payload, list):
        return tuple(str(item) for item in payload if str(item).strip())
    return ()


class PoliticalWatchlistConfig:
    def __init__(
        self,
        *,
        enabled: bool,
        standard_retention_trading_days: int,
        exceptional_retention_trading_days: int,
        risk_retention_trading_days: int,
        max_watchlist_items: int,
        compact_reminder_interval_days: int,
        repeat_full_on_entry_change: bool,
        repeat_full_on_classification_change: bool,
        repeat_full_on_new_trade: bool,
        repeat_full_on_material_amendment: bool,
        repeat_full_on_major_evidence_change: bool,
        send_expired_notice: bool,
        bullish_evidence_threshold: float,
        distribution_evidence_threshold: float,
        breadth_threshold: float,
        concentration_threshold: float,
        conviction_threshold: float,
    ) -> None:
        self.enabled = enabled
        self.standard_retention_trading_days = standard_retention_trading_days
        self.exceptional_retention_trading_days = exceptional_retention_trading_days
        self.risk_retention_trading_days = risk_retention_trading_days
        self.max_watchlist_items = max_watchlist_items
        self.compact_reminder_interval_days = compact_reminder_interval_days
        self.repeat_full_on_entry_change = repeat_full_on_entry_change
        self.repeat_full_on_classification_change = repeat_full_on_classification_change
        self.repeat_full_on_new_trade = repeat_full_on_new_trade
        self.repeat_full_on_material_amendment = repeat_full_on_material_amendment
        self.repeat_full_on_major_evidence_change = repeat_full_on_major_evidence_change
        self.send_expired_notice = send_expired_notice
        self.bullish_evidence_threshold = bullish_evidence_threshold
        self.distribution_evidence_threshold = distribution_evidence_threshold
        self.breadth_threshold = breadth_threshold
        self.concentration_threshold = concentration_threshold
        self.conviction_threshold = conviction_threshold
        self.watchlist_calendar_days = standard_retention_trading_days

    @classmethod
    def from_env(cls) -> PoliticalWatchlistConfig:
        config = cls(
            enabled=_bool_env("POLITICAL_DIGEST_WATCHLIST_ENABLED", True),
            standard_retention_trading_days=_int_env("POLITICAL_DIGEST_WATCHLIST_CALENDAR_DAYS", 7),
            exceptional_retention_trading_days=_int_env("POLITICAL_DIGEST_WATCHLIST_CALENDAR_DAYS", 7),
            risk_retention_trading_days=_int_env("POLITICAL_DIGEST_WATCHLIST_CALENDAR_DAYS", 7),
            max_watchlist_items=_int_env("POLITICAL_DIGEST_MAX_WATCHLIST_ITEMS", 8),
            compact_reminder_interval_days=_int_env("POLITICAL_DIGEST_COMPACT_REMINDER_INTERVAL_DAYS", 1),
            repeat_full_on_entry_change=_bool_env("POLITICAL_DIGEST_REPEAT_FULL_ON_ENTRY_CHANGE", True),
            repeat_full_on_classification_change=_bool_env("POLITICAL_DIGEST_REPEAT_FULL_ON_CLASSIFICATION_CHANGE", True),
            repeat_full_on_new_trade=_bool_env("POLITICAL_DIGEST_REPEAT_FULL_ON_NEW_TRADE", True),
            repeat_full_on_material_amendment=_bool_env("POLITICAL_DIGEST_REPEAT_FULL_ON_MATERIAL_AMENDMENT", True),
            repeat_full_on_major_evidence_change=_bool_env("POLITICAL_DIGEST_REPEAT_FULL_ON_MAJOR_EVIDENCE_CHANGE", True),
            send_expired_notice=_bool_env("POLITICAL_DIGEST_SEND_EXPIRED_NOTICE", False),
            bullish_evidence_threshold=_float_env("POLITICAL_DIGEST_BULLISH_EVIDENCE_DELTA", 15.0),
            distribution_evidence_threshold=_float_env("POLITICAL_DIGEST_DISTRIBUTION_EVIDENCE_DELTA", 15.0),
            breadth_threshold=_float_env("POLITICAL_DIGEST_BREADTH_DELTA", 20.0),
            concentration_threshold=_float_env("POLITICAL_DIGEST_CONCENTRATION_DELTA", 0.20),
            conviction_threshold=_float_env("POLITICAL_DIGEST_CONVICTION_DELTA", 15.0),
        )
        for name in (
            "standard_retention_trading_days",
            "exceptional_retention_trading_days",
            "risk_retention_trading_days",
            "max_watchlist_items",
            "compact_reminder_interval_days",
        ):
            value = int(getattr(config, name))
            if value < 0:
                raise ValueError(f"{name} cannot be negative.")
        return config


def count_watchlist_days(start: date, end: date) -> int:
    if end < start:
        raise ValueError("end date cannot be earlier than start date.")
    return (end - start).days


def add_watchlist_days(start: date, days: int) -> date:
    if days < 0:
        raise ValueError("days cannot be negative.")
    return start + timedelta(days=days)


def count_trading_sessions(start: date, end: date) -> int:
    if end < start:
        raise ValueError("end date cannot be earlier than start date.")
    sessions = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            sessions += 1
        cursor += timedelta(days=1)
    return sessions


def add_trading_sessions(start: date, sessions: int) -> date:
    if sessions < 0:
        raise ValueError("sessions cannot be negative.")
    cursor = start
    remaining = sessions
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def load_watchlist_state_from_row(row: dict[str, Any] | None) -> PoliticalWatchlistState | None:
    if not row:
        return None
    ticker = str(row.get("Ticker") or "").strip().upper()
    if not ticker:
        return None
    return PoliticalWatchlistState(
        ticker=ticker,
        structure_classification=str(row.get("Structure Classification") or "").strip().upper(),
        bullish_evidence_score=float(str(row.get("Bullish Evidence Score") or "0") or "0"),
        distribution_evidence_score=float(str(row.get("Distribution Evidence Score") or "0") or "0"),
        breadth_score=float(str(row.get("Breadth Score") or "0") or "0"),
        concentration_score=float(str(row.get("Concentration Score") or "0") or "0"),
        political_conviction=float(str(row.get("Political Conviction") or "0") or "0"),
        entry_quality=float(str(row.get("Entry Quality") or "0") or "0"),
        first_flagged_at=str(row.get("First Flagged At") or "").strip(),
        last_flagged_at=str(row.get("Last Flagged At") or "").strip(),
        watchlist_started_at=str(row.get("Watchlist Started At") or "").strip(),
        watchlist_until=str(row.get("Watchlist Until") or "").strip(),
        watchlist_status=_normalise_status(str(row.get("Watchlist Status") or "")),
        watchlist_priority=int(str(row.get("Watchlist Priority") or "0") or "0"),
        watchlist_retention_type=str(row.get("Watchlist Retention Type") or "").strip().upper(),
        watchlist_reminder_count=int(str(row.get("Watchlist Reminder Count") or "0") or "0"),
        last_detailed_alert_at=str(row.get("Last Detailed Alert At") or "").strip(),
        last_compact_reminder_at=str(row.get("Last Compact Reminder At") or "").strip(),
        previous_entry_category=_normalise_entry_category(row.get("Previous Entry Category") or row.get("Current Entry Category") or ""),
        current_entry_category=_normalise_entry_category(row.get("Current Entry Category") or ""),
        entry_category_changed=str(row.get("Entry Category Changed") or "").strip().upper() == "YES",
        previous_political_classification=str(
            row.get("Previous Political Classification")
            or row.get("Previous Classification")
            or "INSUFFICIENT_EVIDENCE"
        ).strip().upper(),
        current_political_classification=str(
            row.get("Current Political Classification")
            or row.get("Primary Classification")
            or "INSUFFICIENT_EVIDENCE"
        ).strip().upper(),
        political_classification_changed=str(row.get("Political Classification Changed") or "").strip().upper() == "YES",
        last_material_change_at=str(row.get("Last Material Change At") or "").strip(),
        last_material_change_type=str(row.get("Last Material Change Type") or "").strip().upper(),
        last_material_change_reason=str(row.get("Last Material Change Reason") or "").strip(),
        last_detailed_summary_hash=str(row.get("Last Detailed Summary Hash") or row.get("Summary Hash") or "").strip(),
        last_compact_summary_hash=str(row.get("Last Compact Summary Hash") or "").strip(),
        last_trigger_trade_keys=_json_list(row.get("Last Trigger Trade Keys") or row.get("Latest Trigger Trade Keys") or ""),
        current_detailed_summary_hash=str(row.get("Summary Hash") or "").strip(),
    )


def classify_watchlist_retention(
    history: TickerPoliticalHistory,
    config: PoliticalWatchlistConfig,
) -> tuple[str, int]:
    if history.primary_classification == "DISTRIBUTION":
        return "RISK", config.risk_retention_trading_days
    return "STANDARD", config.standard_retention_trading_days


def describe_latest_material_event(history: TickerPoliticalHistory) -> str:
    event = history.new_events[-1] if history.new_events else {}
    if not event:
        release_types = ", ".join(history.release_types) if history.release_types else ""
        return "No new political disclosure" if not release_types else release_types.replace("_", " ").title()
    amount_low = float(event.get("amount_low") or 0.0)
    amount_high = float(event.get("amount_high") or 0.0)
    amount = ""
    if amount_low > 0 or amount_high > 0:
        if amount_low == amount_high:
            amount = f", ${amount_low:,.0f}"
        else:
            amount = f", ${amount_low:,.0f}-${amount_high:,.0f}"
    side = str(event.get("option_side") or "").strip().upper()
    option_prefix = f"{side.lower()} " if side else ""
    actor = str(event.get("owner_relationship") or "").strip().lower()
    actor_prefix = f"{actor} " if actor and actor != "unknown" else ""
    return f"{actor_prefix}{option_prefix}{str(event.get('transaction_type') or 'event').strip().lower()}{amount}".strip()


def primary_risk_label(history: TickerPoliticalHistory) -> str:
    if "put_activity_present" in history.risk_flags:
        return "put activity present"
    if "mixed_directional_history" in history.risk_flags:
        return "mixed directional history"
    primary_window = history.windows.get(90) or history.windows.get(45)
    if primary_window and primary_window.largest_buyer_share_lower_bound >= 0.7:
        return "single-household concentration"
    if "recent_sales_present" in history.risk_flags:
        return "recent sales present"
    if "low_data_confidence" in history.risk_flags:
        return "low data confidence"
    return "None"


def is_watchlist_eligible_initiator(
    history: TickerPoliticalHistory,
    *,
    detailed_material_flag: bool,
    bootstrap_run: bool,
) -> bool:
    if not detailed_material_flag or bootstrap_run:
        return False
    if not any(release_type in MATERIAL_RELEASE_TYPES for release_type in history.release_types):
        return False
    return history.primary_classification != "INSUFFICIENT_EVIDENCE"


def build_compact_summary_hash(
    history: TickerPoliticalHistory,
    *,
    watchlist_day: int,
    watchlist_total_days: int,
    retention_type: str,
) -> str:
    payload = {
        "ticker": history.ticker,
        "classification": history.primary_classification,
        "structure": history.structure_classification,
        "entry_category": history.entry_category,
        "latest_material_event": describe_latest_material_event(history),
        "primary_risk": primary_risk_label(history),
        "watch_day": watchlist_day,
        "watch_total": watchlist_total_days,
        "retention_type": retention_type,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_compact_reminder_due(
    state: PoliticalWatchlistState,
    observed_at: datetime,
    config: PoliticalWatchlistConfig,
) -> bool:
    if state.watchlist_status != "ACTIVE":
        return False
    start_date = _parse_date(state.watchlist_started_at)
    if start_date is None:
        return False
    current_days = count_watchlist_days(start_date, observed_at.date()) + 1
    if current_days < 2:
        return False
    if current_days > state.watchlist_total_days:
        return False
    if not state.last_compact_reminder_at:
        return True
    last_sent = _parse_date(state.last_compact_reminder_at)
    if last_sent is None:
        return True
    return count_watchlist_days(last_sent, observed_at.date()) >= config.compact_reminder_interval_days


def update_watchlist_state(
    previous: PoliticalWatchlistState | None,
    history: TickerPoliticalHistory,
    *,
    observed_at: datetime,
    config: PoliticalWatchlistConfig,
    detailed_material_flag: bool,
    bootstrap_run: bool,
) -> PoliticalWatchlistState:
    previous = previous or PoliticalWatchlistState(ticker=history.ticker)
    observed_iso = observed_at.replace(microsecond=0).isoformat()
    observed_date = observed_at.date()
    retention_type, retention_days = classify_watchlist_retention(history, config)
    started_at = previous.watchlist_started_at
    first_flagged_at = previous.first_flagged_at
    last_flagged_at = previous.last_flagged_at
    status = previous.watchlist_status
    has_new_material_event = is_watchlist_eligible_initiator(
        history,
        detailed_material_flag=detailed_material_flag,
        bootstrap_run=bootstrap_run,
    )
    if has_new_material_event:
        started_at = started_at or observed_iso
        first_flagged_at = first_flagged_at or observed_iso
        last_flagged_at = observed_iso
        status = "ACTIVE" if config.enabled else "SUPPRESSED"
    until_value = previous.watchlist_until
    if started_at:
        until_date = add_watchlist_days(_parse_date(started_at) or observed_date, max(0, retention_days - 1))
        until_value = until_date.isoformat()
        if status == "ACTIVE" and observed_date > until_date:
            status = "EXPIRED"
    if not started_at and previous.watchlist_status == "ACTIVE":
        status = "EXPIRED"
    watchlist_day = count_watchlist_days(_parse_date(started_at) or observed_date, observed_date) + 1 if started_at else 0
    compact_hash = build_compact_summary_hash(
        history,
        watchlist_day=max(1, watchlist_day),
        watchlist_total_days=retention_days,
        retention_type=retention_type,
    )
    reminder_due = config.enabled and config.max_watchlist_items != 0 and is_compact_reminder_due(
        replace(
            previous,
            watchlist_status=status,
            watchlist_started_at=started_at,
            watchlist_total_days=retention_days,
        ),
        observed_at,
        config,
    )
    if has_new_material_event:
        reminder_due = False
    return PoliticalWatchlistState(
        ticker=history.ticker,
        structure_classification=history.structure_classification,
        bullish_evidence_score=history.bullish_evidence_score,
        distribution_evidence_score=history.distribution_evidence_score,
        breadth_score=history.breadth_score,
        concentration_score=history.concentration_score,
        political_conviction=history.political_conviction,
        entry_quality=history.entry_quality,
        first_flagged_at=first_flagged_at,
        last_flagged_at=last_flagged_at,
        watchlist_started_at=started_at,
        watchlist_until=until_value,
        watchlist_status=status,
        watchlist_priority=0,
        watchlist_retention_type=retention_type,
        watchlist_reminder_count=previous.watchlist_reminder_count,
        last_detailed_alert_at=previous.last_detailed_alert_at,
        last_compact_reminder_at=previous.last_compact_reminder_at,
        previous_entry_category=previous.current_entry_category or history.entry_category,
        current_entry_category=history.entry_category,
        entry_category_changed=(previous.current_entry_category or history.entry_category) != history.entry_category,
        previous_political_classification=previous.current_political_classification or history.previous_classification,
        current_political_classification=history.primary_classification,
        political_classification_changed=(previous.current_political_classification or history.previous_classification) != history.primary_classification,
        last_material_change_at=previous.last_material_change_at,
        last_material_change_type=previous.last_material_change_type,
        last_material_change_reason=previous.last_material_change_reason,
        last_detailed_summary_hash=previous.last_detailed_summary_hash,
        last_compact_summary_hash=previous.last_compact_summary_hash,
        last_trigger_trade_keys=history.latest_trigger_trade_keys or previous.last_trigger_trade_keys,
        watchlist_day=watchlist_day,
        watchlist_total_days=retention_days,
        current_detailed_summary_hash=history.summary_hash,
        current_compact_summary_hash=compact_hash,
        latest_material_event=describe_latest_material_event(history),
        primary_risk=primary_risk_label(history),
        eligible_for_watchlist=has_new_material_event or previous.watchlist_status == "ACTIVE",
        reminder_due=reminder_due,
        has_new_material_event=has_new_material_event,
        has_other_new_activity=bool(history.new_events),
    )


def apply_delivery(
    state: PoliticalWatchlistState,
    *,
    delivered_section: str,
    delivered_hash: str,
    sent_at: str,
) -> PoliticalWatchlistState:
    if delivered_section == "NEW_MATERIAL_SIGNALS":
        return replace(
            state,
            last_detailed_alert_at=sent_at,
            last_detailed_summary_hash=delivered_hash,
            watchlist_reminder_count=state.watchlist_reminder_count,
        )
    if delivered_section == "MATERIAL_SIGNAL_UPDATES":
        return replace(
            state,
            last_detailed_alert_at=sent_at,
            last_detailed_summary_hash=delivered_hash,
        )
    if delivered_section == "ACTIVE_POLITICAL_WATCHLIST":
        return replace(
            state,
            last_compact_reminder_at=sent_at,
            last_compact_summary_hash=delivered_hash,
            watchlist_reminder_count=state.watchlist_reminder_count + 1,
        )
    return state
