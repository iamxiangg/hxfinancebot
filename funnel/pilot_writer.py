# NEW — Funnel Pilot Steps 6 to 9: write only to pilot worksheets

from __future__ import annotations

import logging
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.signal_schema import Signal

logger = logging.getLogger(__name__)

PENDING_SHEET = "Pending_New_Tickers"
SIGNAL_LOG_SHEET = "Scanner_Signal_Log_Pilot"
FUNNEL_SHEET = "Funnel_Pilot"
PROTECTED_PRODUCTION_SHEET = "Stock Summary USD"

PENDING_HEADERS = [
    "Ticker",
    "Stock Name",
    "Google Ticker",
    "Discovery Source",
    "Discovery Reason",
    "Date Discovered",
    "Validation Status",
    "Add to Stock Summary USD?",
    "Added Date",
]

SIGNAL_HEADERS = [
    "Signal ID",
    "Ticker",
    "Scanner",
    "Classification",
    "Score",
    "Observed At",
    "Valid Until",
    "Details JSON",
    "Active",
]

FUNNEL_HEADERS = [
    "Ticker",
    "Stock Name",
    "Already in Stock Summary USD?",
    "Discovery Source",
    "Latest Classification",
    "Latest Score",
    "Latest Signal Date",
    "Valid Until",
    "Signal Count",
    "Opportunity Stage",
    "Manual Decision",
    "Notes",
]


def _sheet_names(service, spreadsheet_id: str) -> set[str]:
    metadata = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    return {
        sheet["properties"]["title"]
        for sheet in metadata.get("sheets", [])
    }


def ensure_pilot_sheets(service, spreadsheet_id: str) -> None:
    existing = _sheet_names(service, spreadsheet_id)
    required = {PENDING_SHEET, SIGNAL_LOG_SHEET, FUNNEL_SHEET}
    missing = sorted(required.difference(existing))
    if not missing:
        return

    requests = [
        {"addSheet": {"properties": {"title": title}}}
        for title in missing
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()
    logger.info("Created pilot worksheets: %s", ", ".join(missing))


def _read_table(service, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A:Z",
            majorDimension="ROWS",
        )
        .execute()
    )
    return response.get("values", [])


def _rows_to_dicts(rows: list[list[Any]], headers: list[str]) -> list[dict[str, Any]]:
    if not rows:
        return []
    actual = [str(value).strip() for value in rows[0]]
    if actual[: len(headers)] != headers:
        raise RuntimeError(
            f"Pilot sheet headers do not match. Expected {headers}, found {actual}"
        )
    output: list[dict[str, Any]] = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        output.append(dict(zip(headers, padded[: len(headers)])))
    return output


def _write_table(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    records: list[dict[str, Any]],
) -> None:
    if sheet_name == PROTECTED_PRODUCTION_SHEET:
        raise RuntimeError("Pilot writer is forbidden from writing to Stock Summary USD")

    values = [headers] + [
        [record.get(header, "") for header in headers]
        for record in records
    ]
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:Z",
        body={},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def _merge_pending(existing: list[dict[str, Any]], comparison: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {
        str(row.get("Ticker", "")).strip().upper(): dict(row)
        for row in existing
        if str(row.get("Ticker", "")).strip()
    }
    for item in comparison:
        if item["candidate_status"] != "NEW_CANDIDATE":
            continue
        ticker = item["ticker"]
        current = by_ticker.get(ticker, {})
        by_ticker[ticker] = {
            "Ticker": ticker,
            "Stock Name": current.get("Stock Name", ""),
            "Google Ticker": current.get("Google Ticker", ""),
            "Discovery Source": item["primary_scanner"],
            "Discovery Reason": item["discovery_reason"],
            "Date Discovered": item["latest_signal_date"],
            "Validation Status": current.get("Validation Status", "PENDING_REVIEW"),
            "Add to Stock Summary USD?": current.get(
                "Add to Stock Summary USD?", "REVIEW"
            ),
            "Added Date": current.get("Added Date", ""),
        }
    return [by_ticker[ticker] for ticker in sorted(by_ticker)]


def _merge_signal_log(existing: list[dict[str, Any]], signals: list[Signal]) -> list[dict[str, Any]]:
    by_id = {
        str(row.get("Signal ID", "")).strip(): dict(row)
        for row in existing
        if str(row.get("Signal ID", "")).strip()
    }
    for signal in signals:
        by_id[signal.signal_id] = {
            "Signal ID": signal.signal_id,
            "Ticker": signal.ticker,
            "Scanner": signal.scanner,
            "Classification": signal.classification,
            "Score": "" if signal.score is None else round(signal.score, 2),
            "Observed At": signal.observed_at,
            "Valid Until": signal.valid_until or "",
            "Details JSON": signal.details_json(),
            "Active": "YES",
        }
    return sorted(
        by_id.values(),
        key=lambda row: (str(row.get("Observed At", "")), str(row.get("Ticker", ""))),
        reverse=True,
    )


def _merge_funnel(existing: list[dict[str, Any]], comparison: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {
        str(row.get("Ticker", "")).strip().upper(): dict(row)
        for row in existing
        if str(row.get("Ticker", "")).strip()
    }
    for item in comparison:
        ticker = item["ticker"]
        current = by_ticker.get(ticker, {})
        by_ticker[ticker] = {
            "Ticker": ticker,
            "Stock Name": item.get("stock_name", "") or current.get("Stock Name", ""),
            "Already in Stock Summary USD?": (
                "YES" if item["already_in_stock_summary"] else "NO"
            ),
            "Discovery Source": item["primary_scanner"],
            "Latest Classification": item["primary_classification"],
            "Latest Score": (
                "" if item["primary_score"] is None else round(item["primary_score"], 2)
            ),
            "Latest Signal Date": item["latest_signal_date"],
            "Valid Until": item["valid_until"],
            "Signal Count": item["signal_count"],
            "Opportunity Stage": item["opportunity_stage"],
            "Manual Decision": current.get("Manual Decision", ""),
            "Notes": current.get("Notes", ""),
        }
    return sorted(
        by_ticker.values(),
        key=lambda row: (
            str(row.get("Opportunity Stage", "")),
            float(row.get("Latest Score") or 0),
            str(row.get("Ticker", "")),
        ),
        reverse=True,
    )


def write_pilot_results(signals: list[Signal], comparison: list[dict[str, Any]]) -> None:
    """Create/update only the three pilot worksheets."""
    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_pilot_sheets(service, spreadsheet_id)

    pending_existing = _rows_to_dicts(
        _read_table(service, spreadsheet_id, PENDING_SHEET),
        PENDING_HEADERS,
    )
    signal_existing = _rows_to_dicts(
        _read_table(service, spreadsheet_id, SIGNAL_LOG_SHEET),
        SIGNAL_HEADERS,
    )
    funnel_existing = _rows_to_dicts(
        _read_table(service, spreadsheet_id, FUNNEL_SHEET),
        FUNNEL_HEADERS,
    )

    _write_table(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        PENDING_HEADERS,
        _merge_pending(pending_existing, comparison),
    )
    _write_table(
        service,
        spreadsheet_id,
        SIGNAL_LOG_SHEET,
        SIGNAL_HEADERS,
        _merge_signal_log(signal_existing, signals),
    )
    _write_table(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        FUNNEL_HEADERS,
        _merge_funnel(funnel_existing, comparison),
    )
    logger.info("Pilot worksheets updated; Stock Summary USD was not modified")
