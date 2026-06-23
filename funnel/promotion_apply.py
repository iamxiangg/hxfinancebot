# VERSION: 2026-06-23-CONTROLLED-PROMOTION-APPLY-2
#
# Controlled approved-ticker promotion:
# - revalidates the Pending_New_Tickers approval;
# - promotes only the explicitly requested ticker;
# - dynamically discovers the ticker column in Stock Summary USD;
# - automatically expands worksheet row capacity when required;
# - inserts one row and copies formulas, formatting and validation;
# - writes only the ticker into the new master row;
# - marks the pending record as ADDED;
# - verifies both worksheets;
# - rolls back the inserted data if verification fails.

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)
from funnel.promotion_dry_run import (
    MASTER_SHEET,
    PENDING_HEADERS,
    PENDING_SHEET,
    _build_master_index,
    _column_letter,
    _evaluate_promotions,
    _read_pending_records,
    _text,
    _ticker,
    _write_json,
)
from funnel.sheet_reader import get_stock_summary_ticker_records


logger = logging.getLogger(__name__)

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

HEADER_SCAN_ROWS = 30
MAX_MASTER_COLUMN = "ZZ"

TICKER_HEADER_ALIASES = {
    "ticker",
    "stock ticker",
    "ticker symbol",
    "stock symbol",
}


def _output_directory() -> Path:
    """Return and create the workflow artefact directory."""
    output_directory = Path(
        os.getenv(
            "FUNNEL_OUTPUT_DIR",
            "funnel_output",
        )
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return output_directory


def _require_inputs() -> str:
    """Require explicit confirmation and an exact ticker."""
    confirmation = _text(
        os.getenv(
            "CONFIRM_MASTER_PROMOTION",
            "",
        )
    ).upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Master promotion was not confirmed. "
            "Set CONFIRM_MASTER_PROMOTION=YES."
        )

    ticker = _ticker(
        os.getenv(
            "PROMOTION_TICKER",
            "",
        )
    )

    if not ticker:
        raise RuntimeError(
            "PROMOTION_TICKER is required."
        )

    return ticker


def _read_values(
    service: Any,
    spreadsheet_id: str,
    range_name: str,
    *,
    formulas: bool = False,
) -> list[list[Any]]:
    """Read values or formulas from a Google Sheets range."""
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            majorDimension="ROWS",
            valueRenderOption=(
                "FORMULA"
                if formulas
                else "FORMATTED_VALUE"
            ),
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _sheet_metadata(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> dict[str, int]:
    """Return the sheet ID and grid dimensions."""
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields=(
                "sheets(properties("
                "sheetId,title,gridProperties("
                "rowCount,columnCount)))"
            ),
        )
        .execute()
    )

    for sheet in response.get(
        "sheets",
        [],
    ):
        properties = sheet.get(
            "properties",
            {},
        )

        if properties.get(
            "title"
        ) != sheet_name:
            continue

        grid = properties.get(
            "gridProperties",
            {},
        )

        return {
            "sheet_id": int(
                properties["sheetId"]
            ),
            "row_count": int(
                grid.get(
                    "rowCount",
                    0,
                )
            ),
            "column_count": int(
                grid.get(
                    "columnCount",
                    0,
                )
            ),
        }

    raise RuntimeError(
        f"Worksheet {sheet_name!r} was not found."
    )


def _normalised_header(
    value: Any,
) -> str:
    """Return a normalised possible header value."""
    return " ".join(
        _text(
            value
        ).lower().split()
    )


def _row_value(
    rows: list[list[Any]],
    row_number: int,
    column_number: int,
) -> str:
    """Read a one-based row and column safely."""
    row_index = row_number - 1
    column_index = column_number - 1

    if (
        row_index < 0
        or row_index >= len(rows)
    ):
        return ""

    row = rows[row_index]

    if (
        column_index < 0
        or column_index >= len(row)
    ):
        return ""

    return _text(
        row[column_index]
    )


def _padded_row(
    rows: list[list[Any]],
    row_number: int,
    width: int,
) -> list[Any]:
    """Return one row padded to an exact width."""
    if (
        row_number < 1
        or row_number > len(rows)
    ):
        return [""] * width

    row = list(
        rows[row_number - 1]
    )

    return (
        row
        + [""] * width
    )[:width]


def _row_has_content(
    rows: list[list[Any]],
    row_number: int,
) -> bool:
    """Return whether a row contains any value or formula."""
    if (
        row_number < 1
        or row_number > len(rows)
    ):
        return False

    return any(
        _text(value)
        for value in rows[row_number - 1]
    )


def _discover_master_layout(
    displayed_rows: list[list[Any]],
    formula_rows: list[list[Any]],
    row_capacity: int,
) -> dict[str, Any]:
    """Discover the ticker table and its next available row."""
    header_matches: list[
        tuple[int, int, str]
    ] = []

    scan_limit = min(
        HEADER_SCAN_ROWS,
        len(displayed_rows),
    )

    for row_number in range(
        1,
        scan_limit + 1,
    ):
        row = displayed_rows[
            row_number - 1
        ]

        for column_number, value in enumerate(
            row,
            start=1,
        ):
            header = _normalised_header(
                value
            )

            if header in TICKER_HEADER_ALIASES:
                header_matches.append(
                    (
                        row_number,
                        column_number,
                        _text(value),
                    )
                )

    if not header_matches:
        raise RuntimeError(
            "Could not identify the ticker header in "
            "Stock Summary USD."
        )

    if len(header_matches) != 1:
        raise RuntimeError(
            "Multiple possible ticker headers were found: "
            + repr(header_matches)
        )

    (
        header_row,
        ticker_column,
        ticker_header,
    ) = header_matches[0]

    total_rows = max(
        len(displayed_rows),
        len(formula_rows),
    )

    ticker_rows = [
        row_number
        for row_number in range(
            header_row + 1,
            total_rows + 1,
        )
        if _row_value(
            displayed_rows,
            row_number,
            ticker_column,
        )
    ]

    if not ticker_rows:
        raise RuntimeError(
            "No existing master tickers were found below "
            f"the {ticker_header!r} header."
        )

    first_data_row = min(
        ticker_rows
    )

    last_data_row = max(
        ticker_rows
    )

    missing_ticker_rows = [
        row_number
        for row_number in range(
            first_data_row,
            last_data_row + 1,
        )
        if not _row_value(
            displayed_rows,
            row_number,
            ticker_column,
        )
    ]

    if missing_ticker_rows:
        raise RuntimeError(
            "Stock Summary USD is not a contiguous ticker table. "
            "Blank ticker rows were found within the data area: "
            + ", ".join(
                str(row)
                for row in missing_ticker_rows
            )
        )

    content_below = [
        row_number
        for row_number in range(
            last_data_row + 1,
            total_rows + 1,
        )
        if (
            _row_has_content(
                displayed_rows,
                row_number,
            )
            or _row_has_content(
                formula_rows,
                row_number,
            )
        )
    ]

    if content_below:
        raise RuntimeError(
            "Stock Summary USD contains content below the "
            "last ticker row. Automatic insertion was stopped. "
            f"First affected row: {content_below[0]}."
        )

    target_row = (
        last_data_row
        + 1
    )

    capacity_rows_required = max(
        0,
        target_row - row_capacity,
    )

    used_column_count = max(
        [
            ticker_column,
            *[
                len(row)
                for row in displayed_rows
            ],
            *[
                len(row)
                for row in formula_rows
            ],
        ]
    )

    return {
        "header_row": header_row,
        "ticker_header": ticker_header,
        "ticker_column": ticker_column,
        "first_data_row": first_data_row,
        "source_row": last_data_row,
        "target_row": target_row,
        "used_column_count": used_column_count,
        "row_capacity_before": row_capacity,
        "capacity_rows_required": capacity_rows_required,
    }


def _ensure_master_row_capacity(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    current_row_count: int,
    target_row: int,
) -> dict[str, int]:
    """
    Append blank worksheet rows when the target row is outside the grid.

    Rows are appended only when required. The subsequent insert operation
    will then create the promoted data row inside the expanded grid.
    """
    required_rows = max(
        0,
        target_row - current_row_count,
    )

    if required_rows == 0:
        return {
            "row_capacity_before": current_row_count,
            "rows_appended": 0,
            "row_capacity_after_expansion": current_row_count,
        }

    (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "appendDimension": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "length": required_rows,
                        }
                    }
                ]
            },
        )
        .execute()
    )

    refreshed_metadata = _sheet_metadata(
        service,
        spreadsheet_id,
        MASTER_SHEET,
    )

    refreshed_row_count = int(
        refreshed_metadata[
            "row_count"
        ]
    )

    if refreshed_row_count < target_row:
        raise RuntimeError(
            "Stock Summary USD row-capacity expansion failed. "
            f"Target row: {target_row}; "
            f"capacity after expansion: {refreshed_row_count}."
        )

    return {
        "row_capacity_before": current_row_count,
        "rows_appended": required_rows,
        "row_capacity_after_expansion": refreshed_row_count,
    }


def _find_eligible_record(
    requested_ticker: str,
    pending_records: list[dict[str, str]],
    master_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the eligible approval matching the requested ticker."""
    (
        approved,
        rejected,
        _,
    ) = _evaluate_promotions(
        pending_records,
        master_index,
    )

    matches = [
        record
        for record in approved
        if _ticker(
            record.get("Ticker")
        )
        == requested_ticker
    ]

    if len(matches) == 1:
        return matches[0]

    rejected_matches = [
        record
        for record in rejected
        if _ticker(
            record.get("Ticker")
        )
        == requested_ticker
    ]

    if rejected_matches:
        reasons = sorted(
            {
                _text(
                    record.get(
                        "Rejection Reason"
                    )
                )
                for record in rejected_matches
            }
        )

        raise RuntimeError(
            f"{requested_ticker} is not eligible for promotion: "
            + " | ".join(reasons)
        )

    raise RuntimeError(
        f"{requested_ticker} is not an active, reviewed "
        "promotion request."
    )


def _insert_and_copy_master_row(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    source_row: int,
    target_row: int,
    used_column_count: int,
) -> None:
    """Insert one row and copy formula-supporting properties."""
    source_start = (
        source_row
        - 1
    )

    target_start = (
        target_row
        - 1
    )

    source_range = {
        "sheetId": sheet_id,
        "startRowIndex": source_start,
        "endRowIndex": source_start + 1,
        "startColumnIndex": 0,
        "endColumnIndex": used_column_count,
    }

    destination_range = {
        "sheetId": sheet_id,
        "startRowIndex": target_start,
        "endRowIndex": target_start + 1,
        "startColumnIndex": 0,
        "endColumnIndex": used_column_count,
    }

    requests = [
        {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": target_start,
                    "endIndex": target_start + 1,
                },
                "inheritFromBefore": False,
            }
        },
        {
            "copyPaste": {
                "source": source_range,
                "destination": destination_range,
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL",
            }
        },
        {
            "copyPaste": {
                "source": source_range,
                "destination": destination_range,
                "pasteType": "PASTE_FORMULA",
                "pasteOrientation": "NORMAL",
            }
        },
        {
            "copyPaste": {
                "source": source_range,
                "destination": destination_range,
                "pasteType": "PASTE_DATA_VALIDATION",
                "pasteOrientation": "NORMAL",
            }
        },
    ]

    (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": requests
            },
        )
        .execute()
    )


def _delete_master_row(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    target_row: int,
) -> None:
    """Delete the inserted master row during rollback."""
    start_index = (
        target_row
        - 1
    )

    (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": start_index,
                                "endIndex": start_index + 1,
                            }
                        }
                    }
                ]
            },
        )
        .execute()
    )


def _write_master_ticker(
    service: Any,
    spreadsheet_id: str,
    ticker_column: int,
    target_row: int,
    ticker: str,
) -> None:
    """Write only the approved ticker into the new master row."""
    ticker_column_letter = _column_letter(
        ticker_column
    )

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{MASTER_SHEET}'!"
                f"{ticker_column_letter}{target_row}"
            ),
            valueInputOption="RAW",
            body={
                "values": [
                    [ticker]
                ]
            },
        )
        .execute()
    )

def _read_pending_row(
    service: Any,
    spreadsheet_id: str,
    pending_row: int,
) -> list[Any]:
    """Read and pad one complete pending-ticker row."""
    final_column = _column_letter(
        len(PENDING_HEADERS)
    )

    rows = _read_values(
        service,
        spreadsheet_id,
        (
            f"'{PENDING_SHEET}'!"
            f"A{pending_row}:"
            f"{final_column}{pending_row}"
        ),
    )

    if not rows:
        return [""] * len(
            PENDING_HEADERS
        )

    return (
        list(rows[0])
        + [""] * len(PENDING_HEADERS)
    )[:len(PENDING_HEADERS)]


def _restore_pending_row(
    service: Any,
    spreadsheet_id: str,
    pending_row: int,
    original_values: list[Any],
) -> None:
    """Restore the complete pending row during rollback."""
    final_column = _column_letter(
        len(PENDING_HEADERS)
    )

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{PENDING_SHEET}'!"
                f"A{pending_row}:"
                f"{final_column}{pending_row}"
            ),
            valueInputOption="RAW",
            body={
                "values": [
                    original_values
                ]
            },
        )
        .execute()
    )


def _update_pending_record(
    service: Any,
    spreadsheet_id: str,
    pending_row: int,
    added_date: str,
) -> None:
    """Mark the approved pending record as added."""
    approval_column = (
        PENDING_HEADERS.index(
            "Add to Stock Summary USD?"
        )
        + 1
    )

    added_date_column = (
        PENDING_HEADERS.index(
            "Added Date"
        )
        + 1
    )

    approval_cell = (
        f"'{PENDING_SHEET}'!"
        f"{_column_letter(approval_column)}"
        f"{pending_row}"
    )

    added_date_cell = (
        f"'{PENDING_SHEET}'!"
        f"{_column_letter(added_date_column)}"
        f"{pending_row}"
    )

    (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {
                        "range": approval_cell,
                        "values": [
                            ["ADDED"]
                        ],
                    },
                    {
                        "range": added_date_cell,
                        "values": [
                            [added_date]
                        ],
                    },
                ],
            },
        )
        .execute()
    )


def _verify_master_row(
    service: Any,
    spreadsheet_id: str,
    ticker: str,
    layout: dict[str, Any],
) -> dict[str, int]:
    """Verify the inserted ticker and copied formulas."""
    ticker_column = int(
        layout["ticker_column"]
    )

    source_row = int(
        layout["source_row"]
    )

    target_row = int(
        layout["target_row"]
    )

    used_column_count = int(
        layout["used_column_count"]
    )

    ticker_column_letter = _column_letter(
        ticker_column
    )

    ticker_rows = _read_values(
        service,
        spreadsheet_id,
        (
            f"'{MASTER_SHEET}'!"
            f"{ticker_column_letter}{target_row}"
        ),
    )

    actual_ticker = (
        _ticker(
            ticker_rows[0][0]
        )
        if ticker_rows
        and ticker_rows[0]
        else ""
    )

    if actual_ticker != ticker:
        raise RuntimeError(
            "Master ticker read-back failed. "
            f"Expected {ticker}; found {actual_ticker!r}."
        )

    final_column = _column_letter(
        used_column_count
    )

    source_formulas = _read_values(
        service,
        spreadsheet_id,
        (
            f"'{MASTER_SHEET}'!"
            f"A{source_row}:"
            f"{final_column}{source_row}"
        ),
        formulas=True,
    )

    target_formulas = _read_values(
        service,
        spreadsheet_id,
        (
            f"'{MASTER_SHEET}'!"
            f"A{target_row}:"
            f"{final_column}{target_row}"
        ),
        formulas=True,
    )

    source_formula_row = (
        list(source_formulas[0])
        if source_formulas
        else []
    )

    target_formula_row = (
        list(target_formulas[0])
        if target_formulas
        else []
    )

    source_formula_row = (
        source_formula_row
        + [""] * used_column_count
    )[:used_column_count]

    target_formula_row = (
        target_formula_row
        + [""] * used_column_count
    )[:used_column_count]

    missing_formula_columns: list[int] = []
    copied_formula_count = 0

    for column_number, source_value in enumerate(
        source_formula_row,
        start=1,
    ):
        if not _text(
            source_value
        ).startswith("="):
            continue

        copied_formula_count += 1

        target_value = _text(
            target_formula_row[
                column_number - 1
            ]
        )

        if not target_value.startswith("="):
            missing_formula_columns.append(
                column_number
            )

    if missing_formula_columns:
        raise RuntimeError(
            "Formula copying failed in master columns: "
            + ", ".join(
                _column_letter(column)
                for column in missing_formula_columns
            )
        )

    refreshed_records = (
        get_stock_summary_ticker_records()
    )

    refreshed_index = (
        _build_master_index(
            refreshed_records
        )
    )

    if ticker not in refreshed_index:
        raise RuntimeError(
            f"{ticker} was not found by the master ticker reader "
            "after insertion."
        )

    return {
        "master_ticker_count": len(
            refreshed_index
        ),
        "copied_formula_count": (
            copied_formula_count
        ),
    }


def _verify_pending_record(
    service: Any,
    spreadsheet_id: str,
    pending_row: int,
    ticker: str,
    added_date: str,
) -> None:
    """Verify the pending approval status after promotion."""
    values = _read_pending_row(
        service,
        spreadsheet_id,
        pending_row,
    )

    record = {
        header: _text(
            values[index]
        )
        for index, header in enumerate(
            PENDING_HEADERS
        )
    }

    if _ticker(
        record.get("Ticker")
    ) != ticker:
        raise RuntimeError(
            "Pending ticker changed during promotion verification."
        )

    if _text(
        record.get(
            "Add to Stock Summary USD?"
        )
    ).upper() != "ADDED":
        raise RuntimeError(
            "Pending promotion status was not updated to ADDED."
        )

    if _text(
        record.get(
            "Added Date"
        )
    ) != added_date:
        raise RuntimeError(
            "Pending Added Date read-back failed."
        )


def _write_receipt(
    output_directory: Path,
    payload: dict[str, Any],
) -> Path:
    """Write the controlled-promotion receipt."""
    receipt_path = (
        output_directory
        / "promotion_apply_receipt.json"
    )

    _write_json(
        receipt_path,
        payload,
    )

    return receipt_path


def main() -> None:
    """Apply one explicitly approved ticker promotion."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
    )

    requested_ticker = (
        _require_inputs()
    )

    output_directory = (
        _output_directory()
    )

    timestamp = datetime.now(
        SINGAPORE_TZ
    )

    added_date = (
        timestamp.date().isoformat()
    )

    run_id = timestamp.strftime(
        "%Y%m%dT%H%M%S%z"
    )

    service = get_sheets_service(
        readonly=False
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    pending_records = (
        _read_pending_records(
            service,
            spreadsheet_id,
        )
    )

    master_records = (
        get_stock_summary_ticker_records()
    )

    master_index = (
        _build_master_index(
            master_records
        )
    )

    eligible_record = (
        _find_eligible_record(
            requested_ticker,
            pending_records,
            master_index,
        )
    )

    pending_row = int(
        eligible_record[
            "Pending Sheet Row"
        ]
    )

    master_metadata = (
        _sheet_metadata(
            service,
            spreadsheet_id,
            MASTER_SHEET,
        )
    )

    master_range = (
        f"'{MASTER_SHEET}'!"
        f"A1:{MAX_MASTER_COLUMN}"
    )

    displayed_rows = _read_values(
        service,
        spreadsheet_id,
        master_range,
    )

    formula_rows = _read_values(
        service,
        spreadsheet_id,
        master_range,
        formulas=True,
    )

    layout = _discover_master_layout(
        displayed_rows,
        formula_rows,
        master_metadata[
            "row_count"
        ],
    )

    source_row_values = _padded_row(
        displayed_rows,
        int(layout["source_row"]),
        int(layout["used_column_count"]),
    )

    source_row_formulas = _padded_row(
        formula_rows,
        int(layout["source_row"]),
        int(layout["used_column_count"]),
    )

    original_pending_values = (
        _read_pending_row(
            service,
            spreadsheet_id,
            pending_row,
        )
    )

    backup_path = (
        output_directory
        / "promotion_apply_prewrite_backup.json"
    )

    _write_json(
        backup_path,
        {
            "status": "PREWRITE_BACKUP",
            "run_id": run_id,
            "ticker": requested_ticker,
            "pending_row": pending_row,
            "pending_row_values": (
                original_pending_values
            ),
            "master_layout": layout,
            "master_source_row_values": (
                source_row_values
            ),
            "master_source_row_formulas": (
                source_row_formulas
            ),
        },
    )

    inserted_master_row = False
    pending_row_changed = False

    capacity_result = {
        "row_capacity_before": int(
            master_metadata["row_count"]
        ),
        "rows_appended": 0,
        "row_capacity_after_expansion": int(
            master_metadata["row_count"]
        ),
    }

    rollback = {
        "attempted": False,
        "master_row_deleted": False,
        "pending_row_restored": False,
        "capacity_rows_appended": 0,
        "capacity_rows_retained": 0,
        "errors": [],
    }

    try:
        capacity_result = (
            _ensure_master_row_capacity(
                service,
                spreadsheet_id,
                int(
                    master_metadata[
                        "sheet_id"
                    ]
                ),
                int(
                    master_metadata[
                        "row_count"
                    ]
                ),
                int(
                    layout[
                        "target_row"
                    ]
                ),
            )
        )

        rollback[
            "capacity_rows_appended"
        ] = capacity_result[
            "rows_appended"
        ]

        _insert_and_copy_master_row(
            service,
            spreadsheet_id,
            int(
                master_metadata[
                    "sheet_id"
                ]
            ),
            int(layout["source_row"]),
            int(layout["target_row"]),
            int(layout["used_column_count"]),
        )

        inserted_master_row = True

        _write_master_ticker(
            service,
            spreadsheet_id,
            int(layout["ticker_column"]),
            int(layout["target_row"]),
            requested_ticker,
        )

        _update_pending_record(
            service,
            spreadsheet_id,
            pending_row,
            added_date,
        )

        pending_row_changed = True

        time.sleep(2)

        verification = _verify_master_row(
            service,
            spreadsheet_id,
            requested_ticker,
            layout,
        )

        _verify_pending_record(
            service,
            spreadsheet_id,
            pending_row,
            requested_ticker,
            added_date,
        )

    except Exception as exc:
        rollback["attempted"] = bool(
            inserted_master_row
            or pending_row_changed
            or capacity_result[
                "rows_appended"
            ]
        )

        if pending_row_changed:
            try:
                _restore_pending_row(
                    service,
                    spreadsheet_id,
                    pending_row,
                    original_pending_values,
                )

                rollback[
                    "pending_row_restored"
                ] = True

            except Exception as rollback_exc:
                rollback["errors"].append(
                    {
                        "operation": "restore_pending_row",
                        "error": repr(
                            rollback_exc
                        ),
                    }
                )

        if inserted_master_row:
            try:
                _delete_master_row(
                    service,
                    spreadsheet_id,
                    int(
                        master_metadata[
                            "sheet_id"
                        ]
                    ),
                    int(layout["target_row"]),
                )

                rollback[
                    "master_row_deleted"
                ] = True

            except Exception as rollback_exc:
                rollback["errors"].append(
                    {
                        "operation": "delete_master_row",
                        "error": repr(
                            rollback_exc
                        ),
                    }
                )

        # Any rows appended solely to expand grid capacity remain blank.
        # Leaving blank capacity is safer than deleting dimensions after
        # a failed transaction because a concurrent user edit could exist.
        rollback[
            "capacity_rows_retained"
        ] = capacity_result[
            "rows_appended"
        ]

        failure_receipt = {
            "status": "FAILED",
            "run_id": run_id,
            "ticker": requested_ticker,
            "error": repr(exc),
            "capacity": capacity_result,
            "rollback": rollback,
            "prewrite_backup": str(
                backup_path
            ),
        }

        _write_receipt(
            output_directory,
            failure_receipt,
        )

        if rollback["errors"]:
            raise RuntimeError(
                "Promotion failed and rollback was not fully "
                "successful. Review the workflow artefacts."
            ) from exc

        raise RuntimeError(
            "Promotion failed. The inserted master data and "
            "pending record were restored. Any appended grid "
            "capacity remains blank."
        ) from exc

    final_master_metadata = (
        _sheet_metadata(
            service,
            spreadsheet_id,
            MASTER_SHEET,
        )
    )

    receipt = {
        "status": "PASSED",
        "mode": "CONTROLLED_PROMOTION_APPLY",
        "run_id": run_id,
        "ticker": requested_ticker,
        "master_sheet": MASTER_SHEET,
        "master_row_added": int(
            layout["target_row"]
        ),
        "ticker_column": int(
            layout["ticker_column"]
        ),
        "ticker_header": layout[
            "ticker_header"
        ],
        "row_capacity_before": (
            capacity_result[
                "row_capacity_before"
            ]
        ),
        "capacity_rows_appended": (
            capacity_result[
                "rows_appended"
            ]
        ),
        "row_capacity_after_expansion": (
            capacity_result[
                "row_capacity_after_expansion"
            ]
        ),
        "final_grid_row_count": int(
            final_master_metadata[
                "row_count"
            ]
        ),
        "pending_sheet": PENDING_SHEET,
        "pending_row_updated": pending_row,
        "pending_status": "ADDED",
        "added_date": added_date,
        "stock_summary_tickers_after": (
            verification[
                "master_ticker_count"
            ]
        ),
        "formulas_copied": (
            verification[
                "copied_formula_count"
            ]
        ),
        "stock_name_required": False,
        "google_ticker_required": False,
        "prewrite_backup": str(
            backup_path
        ),
        "rollback_required": False,
    }

    receipt_path = _write_receipt(
        output_directory,
        receipt,
    )

    print()
    print(
        "FUNNEL PILOT — CONTROLLED PROMOTION"
    )
    print(
        "=" * 41
    )
    print(
        "Promoted ticker:             "
        f"{requested_ticker}"
    )
    print(
        "Master row added:            "
        f"{layout['target_row']}"
    )
    print(
        "Master ticker column:        "
        f"{_column_letter(layout['ticker_column'])}"
    )
    print(
        "Row capacity before:         "
        f"{capacity_result['row_capacity_before']}"
    )
    print(
        "Capacity rows appended:      "
        f"{capacity_result['rows_appended']}"
    )
    print(
        "Final grid row count:        "
        f"{final_master_metadata['row_count']}"
    )
    print(
        "Formulas copied:             "
        f"{verification['copied_formula_count']}"
    )
    print(
        "Pending row updated:         "
        f"{pending_row}"
    )
    print(
        "Pending status:              ADDED"
    )
    print(
        "Added date:                  "
        f"{added_date}"
    )
    print(
        "Stock Summary tickers after: "
        f"{verification['master_ticker_count']}"
    )
    print(
        "MASTER AND PENDING READ-BACK "
        "VERIFICATION PASSED"
    )
    print(
        "Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "CONTROLLED PROMOTION COMPLETED "
        "SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()