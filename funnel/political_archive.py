from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import (
    BOT_STATE_HEADERS,
    BOT_STATE_SHEET,
    POLITICAL_DIGEST_LOG_HEADERS,
    POLITICAL_DIGEST_LOG_SHEET,
    POLITICAL_DIGEST_SNAPSHOT_HEADERS,
    POLITICAL_DIGEST_SNAPSHOT_SHEET,
    POLITICAL_REVIEW_OVERRIDES_HEADERS,
    POLITICAL_REVIEW_OVERRIDES_SHEET,
    POLITICAL_TICKER_SUMMARY_HEADERS,
    POLITICAL_TICKER_SUMMARY_SHEET,
    POLITICAL_TRADES_RAW_HEADERS,
    POLITICAL_TRADES_RAW_SHEET,
)
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import append_records, read_table, upsert_records
from scanners.congress.models import DigestDeliverySnapshot, PoliticalArchiveStats, PoliticalWatchlistState, TickerPoliticalHistory


BOOTSTRAP_STATE_KEY = "political_archive_bootstrapped_payload_sha"
LAST_PAYLOAD_HASH_KEY = "political_archive_last_payload_sha"
LAST_RECORD_COUNT_KEY = "political_archive_last_record_count"


@dataclass
class PoliticalArchiveState:
    context: Any | None
    raw_rows: dict[str, dict[str, Any]]
    summary_rows: dict[str, dict[str, Any]]
    digest_rows: list[dict[str, Any]]
    snapshot_rows: dict[str, dict[str, Any]]
    review_override_rows: dict[str, dict[str, Any]]
    bot_state: dict[str, str]


@dataclass
class RawArchiveUpdateResult:
    rows_to_upsert: list[dict[str, Any]]
    new_rows: int
    amended_rows: int
    idempotent_rows: int
    seen_updates: int
    deactivated_rows: int
    removed_events: list[dict[str, Any]]


def _state_directory() -> Path:
    path = Path(os.getenv("CONGRESS_STATE_DIR", "funnel_output/congress_state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sheet_backend_enabled() -> bool:
    backend = str(os.getenv("POLITICAL_ARCHIVE_BACKEND", "auto")).strip().lower()
    if backend == "local":
        return False
    return bool(os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip() and os.getenv("GOOGLE_SHEET_ID", "").strip())


def _raw_path() -> Path:
    return _state_directory() / "political_trades_raw.json"


def _summary_path() -> Path:
    return _state_directory() / "political_ticker_summary.json"


def _digest_log_path() -> Path:
    return _state_directory() / "political_digest_log.json"


def _digest_snapshot_path() -> Path:
    return _state_directory() / "political_digest_snapshot.json"


def _review_overrides_path() -> Path:
    return _state_directory() / "political_review_overrides.json"


def _bot_state_path() -> Path:
    return _state_directory() / "bot_state.json"


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload


def _save_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _bot_state_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("Key") or "").strip(): str(row.get("Value") or "").strip()
        for row in rows
        if str(row.get("Key") or "").strip()
    }


def load_political_archive_state() -> PoliticalArchiveState:
    if _sheet_backend_enabled():
        try:
            service = get_sheets_service(readonly=False)
            spreadsheet_id = get_spreadsheet_id()
            ensure_review_sheets(service, spreadsheet_id)
            return PoliticalArchiveState(
                context={"service": service, "spreadsheet_id": spreadsheet_id},
                raw_rows={
                    str(row.get("Trade Key") or "").strip(): row
                    for row in read_table(service, spreadsheet_id, POLITICAL_TRADES_RAW_SHEET, POLITICAL_TRADES_RAW_HEADERS)
                    if str(row.get("Trade Key") or "").strip()
                },
                summary_rows={
                    str(row.get("Ticker") or "").strip().upper(): row
                    for row in read_table(service, spreadsheet_id, POLITICAL_TICKER_SUMMARY_SHEET, POLITICAL_TICKER_SUMMARY_HEADERS)
                    if str(row.get("Ticker") or "").strip()
                },
                digest_rows=read_table(service, spreadsheet_id, POLITICAL_DIGEST_LOG_SHEET, POLITICAL_DIGEST_LOG_HEADERS),
                snapshot_rows={
                    str(row.get("Digest ID") or "").strip(): row
                    for row in read_table(service, spreadsheet_id, POLITICAL_DIGEST_SNAPSHOT_SHEET, POLITICAL_DIGEST_SNAPSHOT_HEADERS)
                    if str(row.get("Digest ID") or "").strip()
                },
                review_override_rows={
                    str(row.get("Trade Key") or "").strip(): row
                    for row in read_table(service, spreadsheet_id, POLITICAL_REVIEW_OVERRIDES_SHEET, POLITICAL_REVIEW_OVERRIDES_HEADERS)
                    if str(row.get("Trade Key") or "").strip()
                },
                bot_state=_bot_state_map(read_table(service, spreadsheet_id, BOT_STATE_SHEET, BOT_STATE_HEADERS)),
            )
        except Exception:
            pass
    return PoliticalArchiveState(
        context=None,
        raw_rows={
            str(row.get("Trade Key") or "").strip(): row
            for row in _load_json(_raw_path(), default=[])
            if isinstance(row, dict) and str(row.get("Trade Key") or "").strip()
        },
        summary_rows={
            str(row.get("Ticker") or "").strip().upper(): row
            for row in _load_json(_summary_path(), default=[])
            if isinstance(row, dict) and str(row.get("Ticker") or "").strip()
        },
        digest_rows=[row for row in _load_json(_digest_log_path(), default=[]) if isinstance(row, dict)],
        snapshot_rows={
            str(row.get("Digest ID") or "").strip(): row
            for row in _load_json(_digest_snapshot_path(), default=[])
            if isinstance(row, dict) and str(row.get("Digest ID") or "").strip()
        },
        review_override_rows={
            str(row.get("Trade Key") or "").strip(): row
            for row in _load_json(_review_overrides_path(), default=[])
            if isinstance(row, dict) and str(row.get("Trade Key") or "").strip()
        },
        bot_state={str(key): str(value) for key, value in _load_json(_bot_state_path(), default={}).items()},
    )


def active_review_overrides(state: PoliticalArchiveState) -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}
    for trade_key, row in state.review_override_rows.items():
        if str(row.get("Active") or "").strip().upper() not in {"YES", "Y", "TRUE", "1"}:
            continue
        decision = str(row.get("Review Decision") or "").strip().upper()
        if decision not in {"RESOLVE", "EXCLUDE", "DEFER"}:
            continue
        overrides[trade_key] = dict(row)
    return overrides


def _text_bool(value: bool) -> str:
    return "YES" if value else "NO"


def _number(value: Any) -> float | int | str:
    return value if value not in (None, "") else ""


def _amount_range_label(low: float, high: float) -> str:
    if low <= 0 and high <= 0:
        return ""
    if low == high:
        return f"${low:,.0f}"
    return f"${low:,.0f}-${high:,.0f}"


def _row_version(row: dict[str, Any]) -> int:
    try:
        return int(str(row.get("Record Version") or "1"))
    except ValueError:
        return 1


def _raw_row_from_record(record, *, now_iso: str, payload_hash: str, first_seen_at: str | None = None, existing_row: dict[str, Any] | None = None, materially_amended: bool = False, active_in_latest_payload: bool = True) -> dict[str, Any]:
    version = 1 if existing_row is None else _row_version(existing_row) + (1 if materially_amended else 0)
    first_seen = first_seen_at or (existing_row or {}).get("First Seen At") or now_iso
    last_changed = now_iso if existing_row is None or materially_amended else str((existing_row or {}).get("Last Changed At") or now_iso)
    previous_notified_at = str((existing_row or {}).get("First Successfully Notified At") or "").strip()
    last_notified_at = str((existing_row or {}).get("Last Successfully Notified At") or "").strip()
    return {
        "Trade Key": record.trade_key,
        "Source Trade ID": record.source_trade_id,
        "Fingerprint": record.fingerprint,
        "Source Payload Hash": payload_hash,
        "Source ID": record.source_id,
        "Filing ID": record.filing_id,
        "Filer ID": record.filer_id,
        "Filer Name": record.filer_name,
        "Canonical Household ID": record.filer_id or record.filer_name,
        "Branch": record.branch,
        "Chamber": record.chamber,
        "Party": record.party,
        "State": record.state,
        "Agency": record.agency,
        "Level": record.level,
        "Office": record.office,
        "Owner": record.owner,
        "Owner Relationship": record.owner_relationship,
        "Ticker": record.ticker,
        "Yahoo Ticker": record.yf_ticker,
        "Asset Name": record.asset_name,
        "Security Description": record.description or record.asset_name,
        "Asset Type": record.asset_type,
        "Asset Class": record.asset_class,
        "Asset Intent Class": record.asset_intent_class,
        "Transaction Type": record.transaction_type,
        "Action": record.action,
        "Option Side": record.option_side,
        "Strike": _number(record.strike),
        "Expiry": record.expiry,
        "Amount Low": _number(record.amount_range_low),
        "Amount Mid Estimate": _number(record.amount_range_mid),
        "Amount High": _number(record.amount_range_high),
        "Amount Range Label": _amount_range_label(float(record.amount_range_low or 0.0), float(record.amount_range_high or 0.0)),
        "Transaction Date": record.transaction_date,
        "Filing Date": record.filing_date,
        "Notification Date": record.filing_date,
        "Ingested At": record.ingested_at,
        "Transaction Age": _number(record.transaction_age),
        "Filing Age": _number(record.filing_age),
        "Days To File": _number(record.days_to_file),
        "Late Filing Status": record.late_filing_status,
        "Company Specific": _text_bool(bool(record.company_specific)),
        "Bullish": _text_bool(bool(record.bullish)),
        "Bearish": _text_bool(bool(record.bearish)),
        "Broad Market": _text_bool(bool(record.broad_market)),
        "Discretionary Weight": _number(record.discretionary_weight),
        "Intentionality Score": _number(record.intentionality_score),
        "Broad Outcome": record.broad_outcome,
        "Reason": record.reason,
        "Proposed Resolution": record.proposed_resolution,
        "Manual Review Required": _text_bool(bool(record.manual_review_required)),
        "Comments": record.comments,
        "Description": record.description,
        "Filing Type": record.filing_type,
        "Document URL": record.doc_url,
        "Trigger Type": record.trigger_type,
        "First Seen At": first_seen,
        "Last Seen At": now_iso,
        "Notification Status": "AMENDED" if materially_amended else str((existing_row or {}).get("Notification Status") or "VALIDATED"),
        "First Successfully Notified At": previous_notified_at,
        "Last Successfully Notified At": last_notified_at,
        "Digest Delivery Status": str((existing_row or {}).get("Digest Delivery Status") or ""),
        "Amends Trade Key": str((existing_row or {}).get("Amends Trade Key") or ""),
        "Parser Version": "political-digest-v3",
        "Last Changed At": last_changed,
        "Record Version": version,
        "Is Materially Amended": _text_bool(materially_amended),
        "Last Seen Payload Hash": payload_hash,
        "Active In Latest Payload": _text_bool(active_in_latest_payload),
    }


def prepare_raw_archive_upserts(
    records: list[Any],
    *,
    existing_rows: dict[str, dict[str, Any]],
    observed_at: str | date | datetime,
    payload_hash: str,
) -> RawArchiveUpdateResult:
    now_dt = observed_at if isinstance(observed_at, datetime) else datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")) if isinstance(observed_at, str) else datetime.combine(observed_at, datetime.min.time(), tzinfo=UTC)
    now_iso = now_dt.isoformat()
    deduped = {}
    for record in records:
        deduped[record.trade_key] = record
    rows_to_upsert: list[dict[str, Any]] = []
    new_rows = amended_rows = idempotent_rows = seen_updates = 0
    current_keys = set(deduped)
    removed_events: list[dict[str, Any]] = []

    for trade_key, record in sorted(deduped.items()):
        existing_row = existing_rows.get(trade_key)
        if existing_row is None:
            rows_to_upsert.append(_raw_row_from_record(record, now_iso=now_iso, payload_hash=payload_hash))
            new_rows += 1
            continue
        same_fingerprint = str(existing_row.get("Fingerprint") or "").strip() == record.fingerprint
        materially_amended = not same_fingerprint
        rows_to_upsert.append(
            _raw_row_from_record(
                record,
                now_iso=now_iso,
                payload_hash=payload_hash,
                first_seen_at=str(existing_row.get("First Seen At") or "").strip() or now_iso,
                existing_row=existing_row,
                materially_amended=materially_amended,
            )
        )
        if materially_amended:
            amended_rows += 1
        else:
            idempotent_rows += 1
        seen_updates += 1

    deactivated_rows = 0
    for trade_key, existing_row in sorted(existing_rows.items()):
        if trade_key in current_keys:
            continue
        if str(existing_row.get("Active In Latest Payload") or "").strip().upper() != "YES":
            continue
        updated = dict(existing_row)
        updated["Active In Latest Payload"] = "NO"
        updated["Notification Status"] = "REMOVED"
        rows_to_upsert.append(updated)
        deactivated_rows += 1
        removed_events.append(
            {
                "trade_key": trade_key,
                "ticker": str(existing_row.get("Ticker") or "").strip().upper(),
                "release_type": "DATA_CORRECTION",
                "event_type": "REMOVED_FROM_PAYLOAD",
            }
        )

    return RawArchiveUpdateResult(
        rows_to_upsert=rows_to_upsert,
        new_rows=new_rows,
        amended_rows=amended_rows,
        idempotent_rows=idempotent_rows,
        seen_updates=seen_updates,
        deactivated_rows=deactivated_rows,
        removed_events=removed_events,
    )


def _history_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _sheet_cell_text(value: str, *, limit: int = 49_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def summary_row_from_history(
    history: TickerPoliticalHistory,
    *,
    updated_at: str,
    watchlist_state: PoliticalWatchlistState | None = None,
) -> dict[str, Any]:
    w45 = history.windows[45]
    w90 = history.windows[90]
    w365 = history.windows[365]
    state = watchlist_state or PoliticalWatchlistState(ticker=history.ticker)
    return {
        "Ticker": history.ticker,
        "Primary Classification": history.primary_classification,
        "Aggregate Direction": history.aggregate_direction,
        "Structure Classification": history.structure_classification,
        "Latest Disclosure Direction": history.latest_disclosure_direction,
        "Directional Agreement": history.directional_agreement,
        "Previous Classification": history.previous_classification,
        "Classification Changed": _text_bool(history.classification_changed),
        "Event Severity": history.event_severity,
        "Ticker State Severity": history.ticker_state_severity,
        "Material Effect Category": history.material_effect_category,
        "Material Effect Percent": round(history.material_effect_percent, 4),
        "Pre-Event 90D Purchase Low": history.pre_event_purchase_low_90d,
        "Pre-Event 90D Sale Low": history.pre_event_sale_low_90d,
        "Post-Event 90D Purchase Low": history.post_event_purchase_low_90d,
        "Post-Event 90D Sale Low": history.post_event_sale_low_90d,
        "Bullish Evidence Score": round(history.bullish_evidence_score, 2),
        "Distribution Evidence Score": round(history.distribution_evidence_score, 2),
        "Breadth Score": round(history.breadth_score, 2),
        "Concentration Score": round(history.concentration_score, 2),
        "Inference Confidence": history.inference_confidence,
        "Data Confidence": history.data_confidence,
        "45D Purchases": w45.purchase_count,
        "45D Partial Sales": w45.partial_sale_count,
        "45D Full Sales": w45.full_sale_count,
        "45D Unique Buyers": w45.unique_buyer_count,
        "45D Unique Sellers": w45.unique_seller_count,
        "45D Stock Purchase Low": w45.stock_purchase_low,
        "45D Stock Purchase Mid Estimate": w45.stock_purchase_mid_estimate,
        "45D Stock Purchase High": w45.stock_purchase_high,
        "45D Call Purchase Low": w45.call_purchase_low,
        "45D Call Purchase Mid Estimate": w45.call_purchase_mid_estimate,
        "45D Call Purchase High": w45.call_purchase_high,
        "45D Put Purchase Low": w45.put_purchase_low,
        "45D Put Purchase Mid Estimate": w45.put_purchase_mid_estimate,
        "45D Put Purchase High": w45.put_purchase_high,
        "45D Sale Low": w45.sale_low,
        "45D Sale Mid Estimate": w45.sale_mid_estimate,
        "45D Sale High": w45.sale_high,
        "90D Purchases": w90.purchase_count,
        "90D Partial Sales": w90.partial_sale_count,
        "90D Full Sales": w90.full_sale_count,
        "90D Unique Buyers": w90.unique_buyer_count,
        "90D Unique Sellers": w90.unique_seller_count,
        "90D Stock Purchase Low": w90.stock_purchase_low,
        "90D Stock Purchase Mid Estimate": w90.stock_purchase_mid_estimate,
        "90D Stock Purchase High": w90.stock_purchase_high,
        "90D Call Purchase Low": w90.call_purchase_low,
        "90D Call Purchase Mid Estimate": w90.call_purchase_mid_estimate,
        "90D Call Purchase High": w90.call_purchase_high,
        "90D Put Purchase Low": w90.put_purchase_low,
        "90D Put Purchase Mid Estimate": w90.put_purchase_mid_estimate,
        "90D Put Purchase High": w90.put_purchase_high,
        "90D Sale Low": w90.sale_low,
        "90D Sale Mid Estimate": w90.sale_mid_estimate,
        "90D Sale High": w90.sale_high,
        "365D Purchases": w365.purchase_count,
        "365D Partial Sales": w365.partial_sale_count,
        "365D Full Sales": w365.full_sale_count,
        "365D Unique Buyers": w365.unique_buyer_count,
        "365D Unique Sellers": w365.unique_seller_count,
        "Largest Bullish Trade Low": w365.largest_bullish_trade_low,
        "Largest Bullish Trade High": w365.largest_bullish_trade_high,
        "Largest Buyer Share Lower Bound": round(w365.largest_buyer_share_lower_bound, 6),
        "Largest Buyer Share Midpoint Estimate": round(w365.largest_buyer_share_midpoint_estimate, 6),
        "Repeat Buyer Count": w365.repeat_buyer_count,
        "Possible Exit Count": w365.full_sale_count,
        "Latest Transaction Date": history.latest_transaction_date,
        "Latest Filing Date": history.latest_filing_date,
        "Latest Trigger Type": history.latest_trigger_type,
        "Latest Trigger Trade Keys": _history_json(list(history.latest_trigger_trade_keys)),
        "Latest Flag Reasons": _history_json(history.flag_reasons),
        "Political Conviction": round(history.political_conviction, 2),
        "Entry Quality": round(history.entry_quality, 2),
        "Summary Hash": history.summary_hash,
        "First Flagged At": state.first_flagged_at,
        "Last Flagged At": state.last_flagged_at,
        "Watchlist Started At": state.watchlist_started_at,
        "Watchlist Until": state.watchlist_until,
        "Watchlist Status": state.watchlist_status,
        "Watchlist Priority": state.watchlist_priority,
        "Watchlist Retention Type": state.watchlist_retention_type,
        "Watchlist Reminder Count": state.watchlist_reminder_count,
        "Last Detailed Alert At": state.last_detailed_alert_at,
        "Last Compact Reminder At": state.last_compact_reminder_at,
        "Previous Entry Category": state.previous_entry_category,
        "Current Entry Category": state.current_entry_category or history.entry_category,
        "Entry Category Changed": _text_bool(state.entry_category_changed),
        "Previous Political Classification": state.previous_political_classification or history.previous_classification,
        "Current Political Classification": state.current_political_classification or history.primary_classification,
        "Political Classification Changed": _text_bool(state.political_classification_changed),
        "Last Material Change At": state.last_material_change_at,
        "Last Material Change Type": state.last_material_change_type,
        "Last Material Change Reason": state.last_material_change_reason,
        "Last Detailed Summary Hash": state.last_detailed_summary_hash,
        "Last Compact Summary Hash": state.last_compact_summary_hash,
        "Last Trigger Trade Keys": _history_json(list(state.last_trigger_trade_keys or history.latest_trigger_trade_keys)),
        "Last Updated": updated_at,
    }


def persist_raw_archive_updates(state: PoliticalArchiveState, update: RawArchiveUpdateResult) -> None:
    if not update.rows_to_upsert:
        return
    if state.context is None:
        merged = dict(state.raw_rows)
        for row in update.rows_to_upsert:
            merged[str(row.get("Trade Key") or "").strip()] = row
        _save_json(_raw_path(), list(merged.values()))
        state.raw_rows = merged
        return
    upsert_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        POLITICAL_TRADES_RAW_SHEET,
        POLITICAL_TRADES_RAW_HEADERS,
        "Trade Key",
        update.rows_to_upsert,
    )


def persist_summary_rows(state: PoliticalArchiveState, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    merged = dict(state.summary_rows)
    for row in rows:
        merged[str(row.get("Ticker") or "").strip().upper()] = row
    if state.context is None:
        _save_json(_summary_path(), list(merged.values()))
        state.summary_rows = merged
        return
    upsert_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        POLITICAL_TICKER_SUMMARY_SHEET,
        POLITICAL_TICKER_SUMMARY_HEADERS,
        "Ticker",
        rows,
    )
    state.summary_rows = merged


def persist_digest_rows(state: PoliticalArchiveState, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    if state.context is None:
        merged = list(state.digest_rows) + rows
        _save_json(_digest_log_path(), merged)
        state.digest_rows = merged
        return
    append_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        POLITICAL_DIGEST_LOG_SHEET,
        POLITICAL_DIGEST_LOG_HEADERS,
        rows,
    )
    state.digest_rows = list(state.digest_rows) + rows


def snapshot_row_from_model(snapshot: DigestDeliverySnapshot) -> dict[str, Any]:
    return {
        "Digest ID": snapshot.digest_id,
        "Digest Date": snapshot.digest_date,
        "Run ID": snapshot.run_id,
        "Digest Status": snapshot.digest_status,
        "Source Health": snapshot.source_health,
        "Payload Hash": snapshot.payload_hash,
        "Payload Refreshed": _text_bool(snapshot.payload_refreshed),
        "Fetched Records": snapshot.fetched_records,
        "New Records": snapshot.new_records,
        "Amendments": snapshot.amendments,
        "Review Required Count": snapshot.review_required_count,
        "Included Trade Keys": _history_json(list(snapshot.included_trade_keys)),
        "Excluded Trade Keys": _history_json(list(snapshot.excluded_trade_keys)),
        "Ticker Summaries JSON": _sheet_cell_text(snapshot.ticker_summaries_json),
        "Threshold Settings JSON": _sheet_cell_text(snapshot.threshold_settings_json),
        "Rule Version": snapshot.rule_version,
        "Template Version": snapshot.template_version,
        "Code Commit": snapshot.code_commit,
        "Message Hash": snapshot.message_hash,
        "Telegram Message IDs": _history_json(list(snapshot.telegram_message_ids)),
        "Chunk Count": snapshot.chunk_count,
        "Successful Chunks": snapshot.successful_chunks,
        "Failed Chunks": snapshot.failed_chunks,
        "Attempt Count": snapshot.attempt_count,
        "Last Delivery Error": snapshot.last_delivery_error,
        "Rendered Digest": _sheet_cell_text(snapshot.rendered_digest),
        "Delivered At": snapshot.delivered_at,
        "Created At": snapshot.created_at,
        "Updated At": snapshot.updated_at,
    }


def persist_digest_snapshot(state: PoliticalArchiveState, snapshot: DigestDeliverySnapshot) -> None:
    row = snapshot_row_from_model(snapshot)
    if state.context is None:
        merged = dict(state.snapshot_rows)
        merged[snapshot.digest_id] = row
        _save_json(_digest_snapshot_path(), list(merged.values()))
        state.snapshot_rows = merged
        return
    upsert_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        POLITICAL_DIGEST_SNAPSHOT_SHEET,
        POLITICAL_DIGEST_SNAPSHOT_HEADERS,
        "Digest ID",
        [row],
    )
    state.snapshot_rows[snapshot.digest_id] = row


def update_raw_notification_status(
    state: PoliticalArchiveState,
    *,
    trade_keys: list[str],
    notification_status: str,
    notified_at: str,
    digest_delivery_status: str,
) -> None:
    rows: list[dict[str, Any]] = []
    for trade_key in sorted({key for key in trade_keys if key}):
        current = state.raw_rows.get(trade_key)
        if current is None:
            continue
        updated = dict(current)
        updated["Notification Status"] = notification_status
        updated["Digest Delivery Status"] = digest_delivery_status
        if notified_at:
            updated["Last Successfully Notified At"] = notified_at
        if notified_at and not str(updated.get("First Successfully Notified At") or "").strip() and notification_status == "NOTIFIED":
            updated["First Successfully Notified At"] = notified_at
        rows.append(updated)
        state.raw_rows[trade_key] = updated
    if not rows:
        return
    if state.context is None:
        _save_json(_raw_path(), list(state.raw_rows.values()))
        return
    upsert_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        POLITICAL_TRADES_RAW_SHEET,
        POLITICAL_TRADES_RAW_HEADERS,
        "Trade Key",
        rows,
    )


def get_bootstrap_marker(state: PoliticalArchiveState) -> str:
    return str(state.bot_state.get(BOOTSTRAP_STATE_KEY, "")).strip()


def get_bot_state_value(state: PoliticalArchiveState, key: str) -> str:
    return str(state.bot_state.get(key, "")).strip()


def set_bot_state_value(state: PoliticalArchiveState, key: str, value: str) -> None:
    updated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    state.bot_state[key] = value
    if state.context is None:
        _save_json(_bot_state_path(), state.bot_state)
        return
    upsert_records(
        state.context["service"],
        state.context["spreadsheet_id"],
        BOT_STATE_SHEET,
        BOT_STATE_HEADERS,
        "Key",
        [{"Key": key, "Value": value, "Updated At": updated_at}],
    )


def set_bootstrap_marker(state: PoliticalArchiveState, payload_hash: str) -> None:
    set_bot_state_value(state, BOOTSTRAP_STATE_KEY, payload_hash)


def build_archive_stats(update: RawArchiveUpdateResult, *, summary_written: int, digest_logged: int, bootstrap_completed: bool) -> PoliticalArchiveStats:
    return PoliticalArchiveStats(
        raw_inserted=update.new_rows,
        raw_amended=update.amended_rows,
        raw_idempotent=update.idempotent_rows,
        raw_deactivated=update.deactivated_rows,
        raw_seen_updates=update.seen_updates,
        summary_written=summary_written,
        digest_logged=digest_logged,
        bootstrap_completed=bootstrap_completed,
    )
