# VERSION: 2026-06-22-PILOT-SHEET-SETUP-GENERIC-MIGRATION-4
#
# Funnel Pilot worksheet setup.
#
# Safety rules:
# - writes are permitted only to the three pilot worksheets;
# - Stock Summary USD is always protected;
# - current headers are accepted without modification;
# - any earlier header may be migrated only when no data rows exist;
# - worksheets containing data under an unexpected header are not changed;
# - all worksheets are checked before any header migration begins.

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


ALLOWED_WRITE_SHEETS = set(
    PILOT_SCHEMAS
)


MINIMUM_ROW_COUNT = 2000
MINIMUM_COLUMN_COUNT = 40


def _require_confirmation() -> None:
    """
    Require an explicit workflow confirmation before any worksheet changes.
    """
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
    """
    Block every write outside the three approved pilot worksheets.
    """
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
    """
    Convert a one-based column number into a Google Sheets column label.
    """
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
    """
    Convert row cells into stripped strings and remove trailing blanks.
    """
    values = [
        str(value).strip()
        for value in row
    ]

    while (
        values
        and values[-1] == ""
    ):
        values.pop()

    return values


def _contains_data_rows(
    rows: list[list[Any]],
) -> bool:
    """
    Return True when any non-empty cell exists below the header row.
    """
    for row in rows[1:]:
        for value in row:
            if str(value).strip():
                return True

    return False


def _get_sheet_metadata(
    service: Any,
    spreadsheet_id: str,
) -> dict[str, dict[str, Any]]:
    """
    Retrieve worksheet metadata keyed by worksheet title.
    """
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
            metadata[
                title
            ] = properties

    return metadata


def _create_missing_pilot_sheets(
    service: Any,
    spreadsheet_id: str,
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """
    Create only missing approved pilot worksheets.
    """
    missing_sheets = sorted(
        ALLOWED_WRITE_SHEETS.difference(
            metadata.keys()
        )
    )

    if not missing_sheets:
        return []

    requests: list[
        dict[str, Any]
    ] = []

    for sheet_name in missing_sheets:
        _assert_allowed_sheet(
            sheet_name
        )

        required_columns = max(
            MINIMUM_COLUMN_COUNT,
            len(
                PILOT_SCHEMAS[
                    sheet_name
                ]
            ),
        )

        requests.append(
            {
                "addSheet": {
                    "properties": {
                        "title": sheet_name,
                        "gridProperties": {
                            "rowCount": (
                                MINIMUM_ROW_COUNT
                            ),
                            "columnCount": (
                                required_columns
                            ),
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

    return missing_sheets


def _ensure_grid_capacity(
    service: Any,
    spreadsheet_id: str,
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """
    Ensure every pilot worksheet has adequate rows and columns.
    """
    requests: list[
        dict[str, Any]
    ] = []

    expanded_sheets: list[
        str
    ] = []

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
                f"Missing metadata for worksheet {sheet_name!r}."
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
            MINIMUM_ROW_COUNT,
        )

        target_columns = max(
            current_columns,
            MINIMUM_COLUMN_COUNT,
            len(
                PILOT_SCHEMAS[
                    sheet_name
                ]
            ),
        )

        if (
            target_rows == current_rows
            and target_columns
            == current_columns
        ):
            continue

        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": (
                            properties[
                                "sheetId"
                            ]
                        ),
                        "gridProperties": {
                            "rowCount": (
                                target_rows
                            ),
                            "columnCount": (
                                target_columns
                            ),
                        },
                    },
                    "fields": (
                        "gridProperties.rowCount,"
                        "gridProperties.columnCount"
                    ),
                }
            }
        )

        expanded_sheets.append(
            sheet_name
        )

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": requests
            },
        ).execute()

    return expanded_sheets


def _read_sheet_rows(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> list[list[Any]]:
    """
    Read the relevant area of one approved pilot worksheet.

    The read extent is derived from the canonical header length for the
    sheet (``len(PILOT_SCHEMAS[sheet_name])``) so that trimming trailing
    empty grid columns never causes the read to silently drop fields.
    ``MINIMUM_COLUMN_COUNT`` is reserved for grid CREATION only.
    """
    _assert_allowed_sheet(
        sheet_name
    )

    final_column = _column_letter(
        len(
            PILOT_SCHEMAS[
                sheet_name
            ]
        )
    )

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"A:{final_column}"
            ),
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _determine_header_action(
    sheet_name: str,
    rows: list[list[Any]],
    expected_headers: list[str],
) -> str:
    """
    Determine the required header action without modifying the worksheet.

    Possible results:
    - HEADER_CREATED
    - HEADER_ALREADY_VALID
    - LEGACY_HEADER_MIGRATED

    Any unexpected header containing data below it causes a safe failure.
    """
    if not rows:
        return "HEADER_CREATED"

    actual_header = _normalise_row(
        rows[0]
    )

    if not actual_header:
        if _contains_data_rows(
            rows
        ):
            raise RuntimeError(
                f"{sheet_name} has data rows but no header. "
                "No changes were performed."
            )

        return "HEADER_CREATED"

    if actual_header == expected_headers:
        return "HEADER_ALREADY_VALID"

    if _contains_data_rows(
        rows
    ):
        raise RuntimeError(
            f"{sheet_name} contains an earlier or unexpected header "
            "and also contains data rows. "
            f"Expected {expected_headers}; "
            f"found {actual_header}. "
            "No headers were migrated."
        )

    # Any header-only structure in an approved pilot worksheet may be
    # migrated because there are no records beneath it.
    return "LEGACY_HEADER_MIGRATED"


def _build_header_plan(
    service: Any,
    spreadsheet_id: str,
) -> dict[str, str]:
    """
    Preflight all pilot worksheets before changing any headers.
    """
    plan: dict[
        str,
        str,
    ] = {}

    for (
        sheet_name,
        expected_headers,
    ) in PILOT_SCHEMAS.items():
        rows = _read_sheet_rows(
            service,
            spreadsheet_id,
            sheet_name,
        )

        plan[
            sheet_name
        ] = _determine_header_action(
            sheet_name,
            rows,
            expected_headers,
        )

    return plan


def _clear_header_row(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    """
    Clear only row 1 of an approved pilot worksheet.

    The clear extent is derived from the canonical header length for the
    sheet (``len(PILOT_SCHEMAS[sheet_name])``) so that the operation
    stays aligned with the live schema even after a grid-trimming pass.
    """
    _assert_allowed_sheet(
        sheet_name
    )

    final_column = _column_letter(
        len(
            PILOT_SCHEMAS[
                sheet_name
            ]
        )
    )

    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"A1:{final_column}1"
            ),
            body={},
        )
        .execute()
    )


def _write_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> None:
    """
    Write one current pilot worksheet header.
    """
    _assert_allowed_sheet(
        sheet_name
    )

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!A1"
            ),
            valueInputOption="RAW",
            body={
                "values": [
                    headers
                ]
            },
        )
        .execute()
    )


def _apply_header_plan(
    service: Any,
    spreadsheet_id: str,
    plan: dict[str, str],
) -> None:
    """
    Apply the already validated header plan.
    """
    for (
        sheet_name,
        action,
    ) in plan.items():
        if action == "HEADER_ALREADY_VALID":
            continue

        _clear_header_row(
            service,
            spreadsheet_id,
            sheet_name,
        )

        _write_header(
            service,
            spreadsheet_id,
            sheet_name,
            PILOT_SCHEMAS[
                sheet_name
            ],
        )


def _format_header(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    header_count: int,
) -> None:
    """
    Bold and wrap the header, and freeze row 1.
    """
    (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": (
                                    sheet_id
                                ),
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": (
                                    header_count
                                ),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {
                                        "bold": True
                                    },
                                    "wrapStrategy": (
                                        "WRAP"
                                    ),
                                }
                            },
                            "fields": (
                                "userEnteredFormat."
                                "textFormat.bold,"
                                "userEnteredFormat."
                                "wrapStrategy"
                            ),
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": (
                                    sheet_id
                                ),
                                "gridProperties": {
                                    "frozenRowCount": 1
                                },
                            },
                            "fields": (
                                "gridProperties."
                                "frozenRowCount"
                            ),
                        }
                    },
                ]
            },
        )
        .execute()
    )


def _verify_headers(
    service: Any,
    spreadsheet_id: str,
) -> None:
    """
    Confirm that every pilot worksheet now has the exact current header.
    """
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
                "Post-setup verification failed: "
                f"{sheet_name} is empty."
            )

        actual_header = _normalise_row(
            rows[0]
        )

        if actual_header != expected_headers:
            raise RuntimeError(
                "Post-setup verification failed for "
                f"{sheet_name}. "
                f"Expected {expected_headers}; "
                f"found {actual_header}."
            )


def _write_receipt(
    receipt: dict[str, Any],
) -> Path:
    """
    Write an auditable setup receipt for the workflow artifact.
    """
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

    receipt_path = (
        output_directory
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
    """
    Create, migrate and verify the three pilot worksheet headers.
    """
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
            "Required worksheet "
            f"{PROTECTED_PRODUCTION_SHEET!r} "
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

    # Preflight every worksheet before changing any header.
    header_plan = _build_header_plan(
        service,
        spreadsheet_id,
    )

    _apply_header_plan(
        service,
        spreadsheet_id,
        header_plan,
    )

    for (
        sheet_name,
        headers,
    ) in PILOT_SCHEMAS.items():
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
            len(headers),
        )

    _verify_headers(
        service,
        spreadsheet_id,
    )

    migrated_sheets = sorted(
        sheet_name
        for (
            sheet_name,
            action,
        ) in header_plan.items()
        if action
        == "LEGACY_HEADER_MIGRATED"
    )

    receipt = {
        "status": "PASSED",
        "created_sheets": (
            created_sheets
        ),
        "expanded_sheets": (
            expanded_sheets
        ),
        "migrated_legacy_headers": (
            migrated_sheets
        ),
        "header_results": (
            header_plan
        ),
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
            f"{header_plan[sheet_name]}"
        )

    print()
    print(
        "Created worksheets:          "
        f"{len(created_sheets)}"
    )
    print(
        "Migrated legacy headers:     "
        f"{len(migrated_sheets)}"
    )
    print(
        "Data rows written:           0"
    )
    print(
        "Stock Summary USD writes:    None"
    )
    print(
        "Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "PILOT SHEET HEADER "
        "VERIFICATION PASSED"
    )
    print(
        "PILOT SHEET SETUP "
        "COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()