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
    REVIEW_REQUESTS_HEADERS,
    REVIEW_REQUESTS_SHEET,
    REVIEW_STATE_APPROVED_PENDING_PROMOTION,
    REVIEW_STATE_ARCHIVED,
    REVIEW_STATE_REJECTED,
    REVIEW_STATE_SENT,
    utc_now_iso,
)
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.sheet_table import append_records, cell_text, column_letter, read_table, upsert_records
from funnel.telegram_review import (
    _validate_callback_auth,
    answer_callback,
    candidate_is_eligible_for_review,
    edit_message_keyboard,
    get_updates,
    is_legacy_callback,
    is_review_expired,
    parse_callback_data,
    send_telegram_text,
    verify_snapshot,
)


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


def _review_index(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(record.get("Review ID") or "").strip(): record
        for record in records
        if str(record.get("Review ID") or "").strip()
    }


def _find_header_index(headers: list[str], names: set[str]) -> int | None:
    normalized = {name.lower() for name in names}
    for index, header in enumerate(headers):
        if header.strip().lower() in normalized:
            return index
    return None


def _set_if_header(row: list[Any], headers: list[str], names: set[str], value: Any) -> None:
    if value in ("", None):
        return
    index = _find_header_index(headers, names)
    if index is not None:
        row[index] = value


def _read_master_headers(service, spreadsheet_id: str) -> list[str]:
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{MASTERLIST_SHEET}'!1:1")
        .execute()
    )
    return [cell_text(value) for value in response.get("values", [[]])[0]]


def promote_candidate_to_master(service, spreadsheet_id: str, candidate: dict[str, Any]) -> str:
    """Controlled promotion — writes to master list. Only call from promotion service."""
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

    _set_if_header(row, headers, {"btd score"}, candidate.get("BTD Score"))
    _set_if_header(row, headers, {"btd ratio"}, candidate.get("BTD Ratio"))
    _set_if_header(row, headers, {"ev (b)", "enterprise value (b)"}, candidate.get("EV (B)"))
    _set_if_header(row, headers, {"revenue ttm (b)", "total revenue (b)"}, candidate.get("Revenue TTM (B)"))
    _set_if_header(row, headers, {"gross margin %"}, candidate.get("Gross Margin %"))
    _set_if_header(row, headers, {"revenue growth %"}, candidate.get("Revenue Growth %"))
    _set_if_header(row, headers, {"btd formula"}, candidate.get("BTD Formula"))
    _set_if_header(row, headers, {"btd summary"}, candidate.get("BTD Summary"))
    _set_if_header(row, headers, {"btd last updated"}, candidate.get("BTD Last Updated"))
    _set_if_header(row, headers, {"signal source", "source"}, candidate.get("Source"))
    _set_if_header(row, headers, {"discovery reason", "review notes"}, candidate.get("Discovery Reason"))
    _set_if_header(row, headers, {"funnel score"}, candidate.get("Funnel Score"))

    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{MASTERLIST_SHEET}'!A1:{column_letter(len(headers))}",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return "ADDED"


def _decision_id(candidate_id: str, action: str, update_id: str) -> str:
    digest = hashlib.sha1(f"{candidate_id}|{action}|{update_id}".encode("utf-8")).hexdigest()[:16]
    return f"decision-{digest}"


def _review_decision_id(review_id: str, action: str, update_id: str) -> str:
    digest = hashlib.sha1(f"{review_id}|{action}|{update_id}".encode("utf-8")).hexdigest()[:16]
    return f"decision-{digest}"


def _confirmation_text(candidate: dict[str, Any], action: str, result: str) -> str:
    ticker = str(candidate.get("Ticker") or "").strip().upper() or "candidate"
    action_text = action.strip().upper()
    return f"{action_text} ${ticker}: {result}"


def _process_legacy_callback(
    parsed,
    callback: dict[str, Any],
    candidates: dict[str, dict[str, str]],
    update_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Safely reject legacy hxv2 callbacks with an expired-review-card message."""
    user = callback.get("from") or {}
    actor = str(user.get("username") or user.get("id") or "telegram")

    candidate = candidates.get(parsed.candidate_id)
    ticker = str(candidate.get("Ticker") or "") if candidate else ""
    now = utc_now_iso()

    log_row = {
        "Decision ID": _decision_id(parsed.candidate_id, parsed.action, update_id),
        "Candidate ID": parsed.candidate_id,
        "Ticker": ticker,
        "Action": f"LEGACY_{parsed.action.upper()}",
        "Actor": actor,
        "Telegram Update ID": update_id,
        "Decision At": now,
        "Result": "Legacy callback rejected — review card expired",
        "Details": "hxv2 callback format retired; resend with hx3",
    }
    return None, log_row, "Review card has expired. Please request a new review card."


def _process_v3_callback(
    parsed,
    callback: dict[str, Any],
    auth,
    candidates: dict[str, dict[str, str]],
    review_requests: dict[str, dict[str, str]],
    service,
    spreadsheet_id: str,
    update_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """Process a valid hx3 callback through the review-request state machine."""
    now = utc_now_iso()
    review_id = parsed.review_id
    review = review_requests.get(review_id)
    user = callback.get("from") or {}
    actor_username = str(user.get("username") or "")
    actor_user_id = str(auth.user_id)
    actor = actor_username or actor_user_id

    callback_id = str(callback.get("id") or "")

    # --- Gate checks ---

    # 1. Review exists?
    if not review:
        generic_row = {
            "Decision ID": _review_decision_id(review_id, parsed.action, update_id),
            "Candidate ID": "",
            "Ticker": "",
            "Action": parsed.action.upper(),
            "Actor": actor,
            "Telegram Update ID": update_id,
            "Decision At": now,
            "Result": "Review not found",
            "Details": f"review_id={review_id}",
        }
        answer_callback(callback_id, "Review request was not found.", token=None)
        return None, generic_row, "Review request was not found."

    # 2. Review is in SENT state?
    current_state = str(review.get("State") or "").strip()
    if current_state != REVIEW_STATE_SENT:
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                f"Review not actionable (state={current_state})"), \
               "This review card is not actionable."

    # 3. Message ID matches?
    stored_msg_id = str(review.get("Telegram Message ID") or "").strip()
    if stored_msg_id and str(auth.message_id) != stored_msg_id:
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                f"Message ID mismatch: expected {stored_msg_id}, got {auth.message_id}"), \
               "Review card mismatch."

    # 4. Review not expired?
    expires_at = str(review.get("Expires At") or "")
    if is_review_expired(expires_at):
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                "Review has expired"), \
               "Review card has expired."

    # 5. Look up candidate
    candidate_id = str(review.get("Candidate ID") or "").strip()
    candidate = candidates.get(candidate_id)
    if not candidate:
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                "Candidate not found"), \
               "Candidate was not found."

    # 6. Verify snapshot
    expected_hash = str(review.get("Candidate Snapshot Hash") or "").strip()
    if not verify_snapshot(candidate, expected_hash):
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                "Candidate snapshot mismatch — STALE_REVIEW"), \
               "Candidate data has changed. Please request a new review card."

    # 7. Check candidate eligibility
    eligible, reason = candidate_is_eligible_for_review(candidate)
    if not eligible:
        return None, _audit_row(review, parsed.action, actor, update_id, now,
                                f"Candidate ineligible: {reason}"), \
               f"Candidate is not eligible for review: {reason}"

    # --- Execute decision ---
    updated_candidate = dict(candidate)
    updated_review = dict(review)
    action = parsed.action.lower()

    if action == "approve":
        updated_review["State"] = REVIEW_STATE_APPROVED_PENDING_PROMOTION
        updated_review["Decision"] = "APPROVE"
        result = "Approval recorded. Promotion will be processed by the controlled promotion worker."
        # Candidate stays NOTIFIED; promotion worker will handle master-list write
        updated_candidate["Decision"] = "APPROVE"
        updated_candidate["Decision At"] = now
        updated_candidate["Decision By"] = actor
    elif action == "reject":
        updated_review["State"] = REVIEW_STATE_REJECTED
        updated_review["Decision"] = "REJECT"
        result = "Candidate rejected."
        updated_candidate["Status"] = "REJECTED"
        updated_candidate["Decision"] = "REJECT"
        updated_candidate["Decision At"] = now
        updated_candidate["Decision By"] = actor
        updated_candidate["Active?"] = "NO"
    elif action == "archive":
        updated_review["State"] = REVIEW_STATE_ARCHIVED
        updated_review["Decision"] = "ARCHIVE"
        result = "Candidate archived."
        updated_candidate["Status"] = "ARCHIVED"
        updated_candidate["Decision"] = "ARCHIVE"
        updated_candidate["Decision At"] = now
        updated_candidate["Decision By"] = actor
        updated_candidate["Active?"] = "NO"

    updated_review["Decision At"] = now
    updated_review["Decision By User ID"] = actor_user_id
    updated_review["Decision By Username"] = actor_username
    updated_review["Telegram Update ID"] = update_id
    updated_review["Updated At"] = now

    # Attempt keyboard removal (non-fatal)
    try:
        edit_message_keyboard(auth.chat_id, auth.message_id, token=None)
    except Exception:
        logger.warning("Failed to remove keyboard for review %s; ignoring", review_id)

    log_row = {
        "Decision ID": _review_decision_id(review_id, action, update_id),
        "Candidate ID": candidate_id,
        "Ticker": str(candidate.get("Ticker") or ""),
        "Action": action.upper(),
        "Actor": actor,
        "Telegram Update ID": update_id,
        "Decision At": now,
        "Result": result,
        "Details": f"review_id={review_id}",
    }

    return (updated_candidate, updated_review), log_row, result


def _audit_row(review: dict[str, str], action: str, actor: str, update_id: str, now: str, reason: str) -> dict[str, Any]:
    return {
        "Decision ID": _review_decision_id(str(review.get("Review ID") or ""), action, update_id),
        "Candidate ID": str(review.get("Candidate ID") or ""),
        "Ticker": str(review.get("Ticker") or ""),
        "Action": action.upper(),
        "Actor": actor,
        "Telegram Update ID": update_id,
        "Decision At": now,
        "Result": reason,
        "Details": "",
    }


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

    review_records = read_table(
        service,
        spreadsheet_id,
        REVIEW_REQUESTS_SHEET,
        REVIEW_REQUESTS_HEADERS,
    )
    review_map = _review_index(review_records)

    changed_candidates: list[dict[str, Any]] = []
    changed_reviews: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    max_update_id = last_update_id

    for update in updates:
        update_id = int(update.get("update_id", 0))
        max_update_id = max(max_update_id, update_id)
        callback = update.get("callback_query") or {}
        parsed = parse_callback_data(str(callback.get("data") or ""))
        if not parsed:
            continue

        callback_id = str(callback.get("id") or "")
        user = callback.get("from") or {}
        actor = str(user.get("username") or user.get("id") or "telegram")

        # --- Authorization (Workstream A5) ---
        auth = _validate_callback_auth(callback)
        if auth is None:
            answer_callback(callback_id, "Unauthorised action.")
            decision_rows.append(
                {
                    "Decision ID": _decision_id(parsed.candidate_id or parsed.review_id, parsed.action, str(update_id)),
                    "Candidate ID": parsed.candidate_id,
                    "Ticker": "",
                    "Action": f"UNAUTHORISED_{parsed.action.upper()}",
                    "Actor": actor,
                    "Telegram Update ID": str(update_id),
                    "Decision At": utc_now_iso(),
                    "Result": "Callback authorisation failed",
                    "Details": "",
                }
            )
            continue

        # --- Legacy hxv2 callback → safe rejection ---
        if is_legacy_callback(str(callback.get("data") or "")):
            _, log_row, msg = _process_legacy_callback(parsed, callback, candidates, str(update_id))
            decision_rows.append(log_row)
            answer_callback(callback_id, msg)
            continue

        # --- hx3 callback processing ---
        if not parsed.review_id:
            answer_callback(callback_id, "Invalid review card.")
            continue

        try:
            result_tuple = _process_v3_callback(
                parsed, callback, auth, candidates, review_map,
                service, spreadsheet_id, str(update_id),
            )
            (cand_and_review), log_row, msg = result_tuple
            decision_rows.append(log_row)
            answer_callback(callback_id, msg)

            if cand_and_review:
                updated_candidate, updated_review = cand_and_review
                changed_candidates.append(updated_candidate)
                changed_reviews.append(updated_review)
                candidates[str(updated_candidate.get("Candidate ID") or "")] = updated_candidate
                review_map[parsed.review_id] = updated_review
                try:
                    send_telegram_text(_confirmation_text(updated_candidate, parsed.action, msg))
                except Exception as exc:
                    logger.warning("Confirmation text failed for %s: %r", parsed.review_id, exc)
        except Exception as exc:
            message = f"Action failed: {exc!r}"[:180]
            logger.exception("Failed to process review action for %s", parsed.review_id)
            decision_rows.append(
                {
                    "Decision ID": _review_decision_id(parsed.review_id, parsed.action, str(update_id)),
                    "Candidate ID": "",
                    "Ticker": "",
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
    if changed_reviews:
        upsert_records(
            service,
            spreadsheet_id,
            REVIEW_REQUESTS_SHEET,
            REVIEW_REQUESTS_HEADERS,
            "Review ID",
            changed_reviews,
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
