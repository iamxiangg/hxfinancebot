# VERSION: 2026-06-22-PILOT-SHEET-SETUP-LEGACY-MIGRATION-3
# Creates and verifies pilot worksheets.
# Known legacy headers are migrated only when no data rows exist.

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)


logger = logging.getLogger(__name__)


PROTECTED_PRODUCTION_SHEET = "Stock Summary USD"

PENDING_SHEET = "Pending_New_Tickers"
SIGNAL_LOG_SHEET = "Scanner_Signal_Log_Pilot"
FUNNEL_SHEET = "Funnel_Pilot"


PENDING_HEADERS = [
    "Ticker",
    "Stock Name",
    "Google Ticker",
    "Scanner",
    "Classification",
    "Score",
    "Entry Quality",
    "Estimated Capital Mid",
    "Buyers",
    "Cluster Buyers",
    "Flow",
    "Names",
    "Review Priority",
    "Opportunity Stage",
    "Discovery Reason",
    "First Seen",
    "Last Seen",
    "Valid Until",
    "Signal ID",
    "Current Run",
    "Validation Status",
    "Add to Stock Summary USD?",
    "Added Date",
    "Reviewer Notes",
]


SIGNAL_HEADERS = [
    "Signal ID",
    "Ticker",
    "Scanner",
    "Classification",
    "Score",
    "Observed At",
    "Valid Until",
    "Active",
    "Flow",
    "Names",
    "Entry Quality",
    "Estimated Capital Mid",
    "Buyers",
    "Cluster Buyers",
    "Details JSON",
]


FUNNEL_HEADERS = [
    "Ticker",
    "Stock Name",
    "Google Ticker",
    "Already in Stock Summary USD?",
    "Stock Summary Row",
    "Candidate Status",
    "Pending New Ticker?",
    "Review Route",
    "Review Priority",
    "Scanner",
    "Latest Classification",
    "Latest Score",
    "Entry Quality",
    "Estimated Capital Mid",
    "Buyers",
    "Cluster Buyers",
    "Flow",
    "Names",
    "Opportunity Stage",
    "Discovery Reason",
    "Signal Count",
    "Latest Signal Date",
    "Valid Until",
    "Signal ID",
    "Current Run",
    "Manual Decision",
    "Notes",
]


PILOT_SCHEMAS = {
    PENDING_SHEET: PENDING_HEADERS,
    SIGNAL_LOG_SHEET: SIGNAL_HEADERS,
    FUNNEL_SHEET: FUNNEL_HEADERS,
}


# Earlier schemas generated during the initial pilot.
# They can be replaced automatically only when the worksheet contains
# no data rows below row 1.
LEGACY_SCHEMAS = {
    PENDING_SHEET: [
        [
            "Ticker",
            "Stock Name",
            "Google Ticker",
            "Discovery Source",
            "Discovery Reason",
            "Date Discovered",
            "Validation Status",
            "Add to Stock Summary USD?",
            "Added Date",
        ],
    ],
    SIGNAL_LOG_SHEET: [
        [
            "Signal ID",
            "Ticker",
            "Scanner",
            "Classification",
            "Score",
            "Observed At",
            "Valid Until",
            "Details",
            "Active",
        ],
        [
            "Signal ID",
            "Ticker",
            "Scanner",
            "Classification",
            "Score",
            "Observed At",
            "Valid Until",
            "Details JSON",
            "Active",
        ],
    ],
    FUNNEL_SHEET: [
        [
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
        ],
    ],
}


ALLOWED_WRITE_SHEETS = set(PILOT_SCHEMAS)


def _require_confirmation() -> None:
    """Require explicit confirmation before creating or changing headers."""
    confirmation = str(
        os.getenv(
            "CONFIRM_PILOT_SHEET_SETUP",
            "",
        )
    ).strip().upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Pilot sheet setup was not confirmed. "
            "Set CONFIRM_PILOT_SHEET_SETUP=YES."
        )


def _assert_allowed_sheet(
    sheet_name: str,
) -> None:
    """Block every write outside the approved pilot worksheets."""
    if sheet_name == PROTECTED_PRODUCTION_SHEET:
        raise RuntimeError(
            "Writing to Stock Summary USD is prohibited."
        )

    if sheet_name not in ALLOWED_WRITE_SHEETS:
        raise RuntimeError(
            f"Writing to worksheet {sheet_name!r} is prohibited."
        )


def _column_letter(
    column_number: int,
) -> str:
    """Convert a one-based column number to a Sheets column letter."""
    if column_number < 1:
        raise ValueError(
            "column_number must be at least 1."
        )

    result = ""
    remaining = column_number

    while remaining:
        remaining, remainder = divmod(
            remaining - 1,
            26,
        )

        result = (
            chr(65 + remainder)
            + result
        )

    return result


def _normalise_row(
    row: list[Any],
) -> list[str]:
    """Convert cells to stripped strings and remove trailing blanks."""
    values = [
        str(value).strip()
        for value in row
    ]

    while values and values[-1] == "":
        values.pop()

    return values


def _contains_data_rows(
    rows: list[list[Any]],
) -> bool:
    """Return True when a non-empty cell exists below the header."""
    for row in rows[1:]:
        for value in row:
            if str(value).strip():
                return True

    return False


def _get_sheet_metadata(
    service: Any,
    spreadsheet_id: str,
) -> dict[str, dict[str, Any]]:
    """Return worksheet metadata keyed by worksheet title."""
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields=(
                "sheets.properties.sheetId,"
                "sheets.properties.title,"
                "sheets.properties.gridProperties"
            ),
        )
        .execute()
    )

    metadata: dict[
        str,
        dict[str, Any],
    ] = {}

    for sheet in response.get(
        "sheets",
        [],
    ):
        properties = sheet.get(
            "properties",
            {},
        )

        title = str(
            properties.get("title")
            or ""
        ).strip()

        if title:
            metadata[title] = properties

    return metadata


def _create_missing_pilot_sheets(
    service: Any,
    spreadsheet_id: str,
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """Create only missing approved pilot worksheets."""
    missing = sorted(
        ALLOWED_WRITE_SHEETS.difference(
            metadata.keys()
        )
    )

    if not missing:
        return []

    requests: list[
        dict[str, Any]
    ] = []

    for sheet_name in missing:
        _assert_allowed_sheet(
            sheet_name
        )

        requests.append(
            {
                "addSheet": {
                    "properties": {
                        "title": sheet_name,
                        "gridProperties": {
                            "rowCount": 2000,
                            "columnCount": 40,
                        },
                    }
                }
            }
        )

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": requests
        },
    ).execute()

    return missing


def _ensure_grid_capacity(
    service: Any,
    spreadsheet_id: str,
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """Ensure pilot worksheets have at least 2,000 rows and 40 columns."""
    requests: list[
        dict[str, Any]
    ] = []

    expanded: list[str] = []

    for sheet_name in sorted(
        ALLOWED_WRITE_SHEETS
    ):
        _assert_allowed_sheet(
            sheet_name
        )

        properties = metadata.get(
            sheet_name
        )

        if properties is None:
            raise RuntimeError(
                f"Missing metadata for {sheet_name!r}."
            )

        grid = properties.get(
            "gridProperties",
            {},
        )

        current_rows = int(
            grid.get("rowCount")
            or 0
        )

        current_columns = int(
            grid.get("columnCount")
            or 0
        )

        target_rows = max(
            current_rows,
            2000,
        )

        target_columns = max(
            current_columns,
            40,
            len(PILOT_SCHEMAS[sheet_name]),
        )

        if (
            target_rows == current_rows
            and target_columns == current_columns
        ):
            continue

        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": properties[
                            "sheetId"
                        ],
                        "gridProperties": {
                            "rowCount": target_rows,
                            "columnCount": target_columns,
                        },
                    },
                    "fields": (
                        "gridProperties.rowCount,"
                        "gridProperties.columnCount"
                    ),
                }
            }
        )

        expanded.append(
            sheet_name
        )

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": requests
            },
        ).execute()

    return expanded


def _read_sheet_rows(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> list[list[Any]]:
    """Read the used cells of an approved pilot worksheet."""
    _assert_allowed_sheet(
        sheet_name
    )

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A:AZ",
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _write_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> None:
    """Write one approved pilot worksheet header."""
    _assert_allowed_sheet(
        sheet_name
    )

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="RAW",
        body={
            "values": [
                headers
            ]
        },
    ).execute()


def _format_header(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    header_count: int,
) -> None:
    """Apply basic formatting and freeze row 1."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": header_count,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "bold": True
                                },
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": (
                            "userEnteredFormat.textFormat.bold,"
                            "userEnteredFormat.wrapStrategy"
                        ),
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "frozenRowCount": 1
                            },
                        },
                        "fields": (
                            "gridProperties.frozenRowCount"
                        ),
                    }
                },
            ]
        },
    ).execute()


def _is_known_legacy_header(
    sheet_name: str,
    actual_header: list[str],
) -> bool:
    """Return True when a header matches a supported legacy schema."""
    return actual_header in LEGACY_SCHEMAS.get(
        sheet_name,
        [],
    )


def _set_or_verify_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    expected_headers: list[str],
) -> str:
    """
    Create, verify or safely migrate a worksheet header.

    A known legacy header is migrated only when there are no data rows.
    """
    rows = _read_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
    )

    if not rows:
        _write_header(
            service,
            spreadsheet_id,
            sheet_name,
            expected_headers,
        )

        return "HEADER_CREATED"

    actual_header = _normalise_row(
        rows[0]
    )

    if actual_header == expected_headers:
        return "HEADER_ALREADY_VALID"

    if _is_known_legacy_header(
        sheet_name,
        actual_header,
    ):
        if _contains_data_rows(
            rows
        ):
            raise RuntimeError(
                f"{sheet_name} contains a recognised legacy header, "
                "but it also contains data rows. No migration was performed."
            )

        _write_header(
            service,
            spreadsheet_id,
            sheet_name,
            expected_headers,
        )

        return "LEGACY_HEADER_MIGRATED"

    raise RuntimeError(
        f"{sheet_name} already contains an unexpected header. "
        f"Expected {expected_headers}; found {actual_header}. "
        "No data were cleared or replaced."
    )


def _verify_headers(
    service: Any,
    spreadsheet_id: str,
) -> None:
    """Confirm all pilot worksheets have the current exact headers."""
    for (
        sheet_name,
        expected_headers,
    ) in PILOT_SCHEMAS.items():
        rows = _read_sheet_rows(
            service,
            spreadsheet_id,
            sheet_name,
        )

        if not rows:
            raise RuntimeError(
                f"Post-setup verification failed: {sheet_name} is empty."
            )

        actual_header = _normalise_row(
            rows[0]
        )

        if actual_header != expected_headers:
            raise RuntimeError(
                f"Post-setup verification failed for {sheet_name}. "
                f"Expected {expected_headers}; found {actual_header}."
            )


def _write_receipt(
    receipt: dict[str, Any],
) -> Path:
    """Write a setup receipt for the GitHub Actions artifact."""
    output_dir = Path(
        os.getenv(
            "FUNNEL_OUTPUT_DIR",
            "funnel_output",
        )
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    receipt_path = (
        output_dir
        / "pilot_sheet_setup_receipt.json"
    )

    receipt_path.write_text(
        json.dumps(
            receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return receipt_path


def main() -> None:
    """Run the controlled header-only pilot worksheet setup."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
    )

    _require_confirmation()

    service = get_sheets_service(
        readonly=False
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    metadata = _get_sheet_metadata(
        service,
        spreadsheet_id,
    )

    if (
        PROTECTED_PRODUCTION_SHEET
        not in metadata
    ):
        raise RuntimeError(
            f"Required worksheet {PROTECTED_PRODUCTION_SHEET!r} "
            "was not found."
        )

    created_sheets = (
        _create_missing_pilot_sheets(
            service,
            spreadsheet_id,
            metadata,
        )
    )

    metadata = _get_sheet_metadata(
        service,
        spreadsheet_id,
    )

    missing_after_creation = sorted(
        ALLOWED_WRITE_SHEETS.difference(
            metadata.keys()
        )
    )

    if missing_after_creation:
        raise RuntimeError(
            "Unable to create pilot worksheets: "
            + ", ".join(
                missing_after_creation
            )
        )

    expanded_sheets = (
        _ensure_grid_capacity(
            service,
            spreadsheet_id,
            metadata,
        )
    )

    header_results: dict[
        str,
        str,
    ] = {}

    for (
        sheet_name,
        expected_headers,
    ) in PILOT_SCHEMAS.items():
        result = _set_or_verify_header(
            service,
            spreadsheet_id,
            sheet_name,
            expected_headers,
        )

        header_results[
            sheet_name
        ] = result

        _format_header(
            service,
            spreadsheet_id,
            int(
                metadata[
                    sheet_name
                ][
                    "sheetId"
                ]
            ),
            len(expected_headers),
        )

    _verify_headers(
        service,
        spreadsheet_id,
    )

    migrated_sheets = sorted(
        sheet_name
        for (
            sheet_name,
            result,
        ) in header_results.items()
        if result
        == "LEGACY_HEADER_MIGRATED"
    )

    receipt = {
        "status": "PASSED",
        "created_sheets": created_sheets,
        "expanded_sheets": expanded_sheets,
        "migrated_legacy_headers": migrated_sheets,
        "header_results": header_results,
        "allowed_write_sheets": sorted(
            ALLOWED_WRITE_SHEETS
        ),
        "protected_sheet": (
            PROTECTED_PRODUCTION_SHEET
        ),
        "protected_sheet_written": False,
        "data_rows_written": 0,
    }

    receipt_path = _write_receipt(
        receipt
    )

    print()
    print(
        "FUNNEL PILOT — PILOT SHEET SETUP"
    )
    print(
        "=" * 39
    )

    for sheet_name in sorted(
        PILOT_SCHEMAS
    ):
        print(
            f"{sheet_name}: "
            f"{header_results[sheet_name]}"
        )

    print()
    print(
        f"Created worksheets:          "
        f"{len(created_sheets)}"
    )
    print(
        f"Migrated legacy headers:     "
        f"{len(migrated_sheets)}"
    )
    print(
        "Data rows written:           0"
    )
    print(
        "Stock Summary USD writes:    None"
    )
    print(
        f"Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "PILOT SHEET HEADER VERIFICATION PASSED"
    )
    print(
        "PILOT SHEET SETUP COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()