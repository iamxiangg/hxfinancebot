from __future__ import annotations

import hashlib
import logging
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import (
    BOT_STATE_HEADERS,
    BOT_STATE_SHEET,
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    DECISION_LOG_HEADERS,
    DECISION_LOG_SHEET,
    MASTERLIST_SHEET,
    utc_now_iso,
)
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.sheet_table import append_records, cell_text, column_letter, read_table, upsert_records
from funnel.telegram_review import answer_callback, get_updates, parse_callback_data


logger = logging.getLogger(__name__)


def _state_value(records: list[dict[str, str]], key: str, default: str = "") -> str:
    for record in records:
        if record.get("Key") == key:
            return str(record.get("Value") or "")
    return default


def _set_state(service, spreadsheet_id: str, key: str, value: str) -> None:
    upsert_records(
        service,
        spreadsheet_id,
        BOT_STATE_SHEET,
        BOT_STATE_HEADERS,
        "Key",
        [{"Key": key, "Value": value, "Updated At": utc_now_iso()}],
    )


def _candidate_index(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(record.get("Candidate ID") or "").strip(): record
        for record in records
        if str(record.get("Candidate ID") or "").strip()
    }


def _find_header_index(headers: list[str], names: set[str]) -> int | None:
    normalized = {name.lower() for name in names}
    for index, header in enumerate(headers):
        if header.strip().lower() in normalized:
            return index
    return None


def _read_master_headers(service, spreadsheet_id: str) -> list[str]:
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{MASTERLIST_SHEET}'!1:1")
        .execute()
    )
    return [cell_text(value) for value in response.get("values", [[]])[0]]


def promote_candidate_to_master(service, spreadsheet_id: str, candidate: dict[str, Any]) -> str:
    ticker = str(candidate.get("Ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("Candidate ticker is blank")

    master_records = get_stock_summary_ticker_records(service=service)
    existing_tickers = {
        str(record.get("ticker") or "").strip().upper()
        for record in master_records
    }
    if ticker in existing_tickers:
        return "ALREADY_EXISTS"

    headers = _read_master_headers(service, spreadsheet_id)
    if not headers:
        headers = ["Ticker", "Stock Name", "Google Ticker"]

    row = ["" for _ in headers]
    ticker_index = _find_header_index(headers, {"ticker", "symbol", "stock ticker"})
    name_index = _find_header_index(headers, {"stock name", "company name", "name"})
    google_index = _find_header_index(headers, {"google ticker", "google symbol"})

    if ticker_index is None:
        ticker_index = 0
    row[ticker_index] = ticker

    if name_index is not None:
        row[name_index] = candidate.get("Company Name", "")

    if google_index is not None:
        row[google_index] = candidate.get("Google Ticker") or ticker

    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{MASTERLIST_SHEET}'!A1:{column_letter(len(headers))}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return "ADDED"


def _decision_id(candidate_id: str, action: str, update_id: str) -> str:
    digest = hashlib.sha1(f"{candidate_id}|{action}|{update_id}".encode("utf-8")).hexdigest()[:16]
    return f"decision-{digest}"


def apply_action(
    service,
    spreadsheet_id: str,
    candidate: dict[str, Any],
    action: str,
    *,
    actor: str,
    update_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    now = utc_now_iso()
    updated = dict(candidate)
    action = action.lower()

    result = ""
    if action == "approve":
        promotion = promote_candidate_to_master(service, spreadsheet_id, candidate)
        if promotion == "ADDED":
            updated["Status"] = "APPROVED_ADDED"
            result = "Ticker added to Stock Summary USD"
        else:
            updated["Status"] = "APPROVED_ALREADY_EXISTS"
            result = "Ticker already existed in Stock Summary USD"
    elif action == "reject":
        updated["Status"] = "REJECTED"
        result = "Candidate rejected"
    elif action == "archive":
        updated["Status"] = "ARCHIVED"
        result = "Candidate archived"
    else:
        raise ValueError(f"Unsupported action: {action}")

    updated["Decision"] = action.upper()
    updated["Decision At"] = now
    updated["Decision By"] = actor

    log_row = {
        "Decision ID": _decision_id(str(candidate.get("Candidate ID", "")), action, update_id),
        "Candidate ID": candidate.get("Candidate ID", ""),
        "Ticker": candidate.get("Ticker", ""),
        "Action": action.upper(),
        "Actor": actor,
        "Telegram Update ID": update_id,
        "Decision At": now,
        "Result": result,
        "Details": "",
    }
    return updated, log_row, result


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_review_sheets(service, spreadsheet_id)

    state_records = read_table(service, spreadsheet_id, BOT_STATE_SHEET, BOT_STATE_HEADERS)
    last_update_id = int(_state_value(state_records, "telegram_last_update_id", "0") or "0")
    updates = get_updates(last_update_id + 1)
    if not updates:
        logger.info("No Telegram review actions found.")
        return

    candidate_records = read_table(
        service,
        spreadsheet_id,
        BTD_CANDIDATES_SHEET,
        BTD_CANDIDATE_HEADERS,
    )
    candidates = _candidate_index(candidate_records)

    changed_candidates: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    max_update_id = last_update_id

    for update in updates:
        update_id = int(update.get("update_id", 0))
        max_update_id = max(max_update_id, update_id)
        callback = update.get("callback_query") or {}
        parsed = parse_callback_data(str(callback.get("data") or ""))
        if not parsed:
            continue

        candidate = candidates.get(parsed.candidate_id)
        callback_id = str(callback.get("id") or "")
        user = callback.get("from") or {}
        actor = str(user.get("username") or user.get("id") or "telegram")

        if not candidate:
            answer_callback(callback_id, "Candidate was not found.")
            decision_rows.append(
                {
                    "Decision ID": _decision_id(parsed.candidate_id, parsed.action, str(update_id)),
                    "Candidate ID": parsed.candidate_id,
                    "Ticker": "",
                    "Action": parsed.action.upper(),
                    "Actor": actor,
                    "Telegram Update ID": str(update_id),
                    "Decision At": utc_now_iso(),
                    "Result": "Candidate was not found",
                    "Details": "",
                }
            )
            continue

        try:
            updated, log_row, result = apply_action(
                service,
                spreadsheet_id,
                candidate,
                parsed.action,
                actor=actor,
                update_id=str(update_id),
            )
            changed_candidates.append(updated)
            decision_rows.append(log_row)
            candidates[parsed.candidate_id] = updated
            answer_callback(callback_id, result)
        except Exception as exc:
            message = f"Action failed: {exc!r}"[:180]
            logger.exception("Failed to process Telegram action for %s", parsed.candidate_id)
            decision_rows.append(
                {
                    "Decision ID": _decision_id(parsed.candidate_id, parsed.action, str(update_id)),
                    "Candidate ID": parsed.candidate_id,
                    "Ticker": str(candidate.get("Ticker") or ""),
                    "Action": parsed.action.upper(),
                    "Actor": actor,
                    "Telegram Update ID": str(update_id),
                    "Decision At": utc_now_iso(),
                    "Result": "FAILED",
                    "Details": message,
                }
            )
            answer_callback(callback_id, message)

    upsert_records(
        service,
        spreadsheet_id,
        BTD_CANDIDATES_SHEET,
        BTD_CANDIDATE_HEADERS,
        "Candidate ID",
        changed_candidates,
    )
    append_records(
        service,
        spreadsheet_id,
        DECISION_LOG_SHEET,
        DECISION_LOG_HEADERS,
        decision_rows,
    )
    _set_state(service, spreadsheet_id, "telegram_last_update_id", str(max_update_id))
    logger.info("Processed %d Telegram review actions.", len(decision_rows))


if __name__ == "__main__":
    run()
