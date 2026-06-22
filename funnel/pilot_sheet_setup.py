# VERSION: 2026-06-22-PILOT-SHEET-SETUP-1
# Funnel Pilot: create and verify pilot worksheet headers only

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


def _require_confirmation() -> None:
    """
    Require explicit workflow confirmation before any worksheet creation.
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


def _column_letter(
    column_number: int,
) -> str:
    """
    Convert a 1-based column number into a Google Sheets column label.
    """
    if column_number < 1:
        raise ValueError(
            "column_number must be at least 1"
        )

    output = ""
    remaining = column_number

    while remaining:
        remaining, remainder = divmod(
            remaining - 1,
            26,
        )

        output = (
            chr(65 + remainder)
            + output
        )

    return output


def _assert_allowed_sheet(
    sheet_name: str,
) -> None:
    """
    Prevent every write outside the three explicitly approved pilot tabs.
    """
    if (
        sheet_name
        == PROTECTED_PRODUCTION_SHEET
    ):
        raise RuntimeError(
            "Pilot setup is forbidden from writing "
            "to Stock Summary USD."
        )

    if (
        sheet_name
        not in ALLOWED_WRITE_SHEETS
    ):
        raise RuntimeError(
            "Pilot setup is forbidden from writing "
            f"to {sheet_name!r}."
        )


def _get_sheet_metadata(
    service,
    spreadsheet_id: str,
) -> dict[str, dict[str, Any]]:
    """
    Retrieve worksheet titles, IDs and grid sizes.
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
    service,
    spreadsheet_id: str,
    metadata: dict[
        str,
        dict[str, Any],
    ],
) -> list[str]:
    """
    Create only the approved pilot worksheets that do not already exist.
    """
    missing = sorted(
        ALLOWED_WRITE_SHEETS.difference(
            metadata
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

        required_columns = max(
            40,
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
                            "rowCount": 2000,
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

    return missing


def _expand_existing_pilot_sheets(
    service,
    spreadsheet_id: str,
    metadata: dict[
        str,
        dict[str, Any],
    ],
) -> list[str]:
    """
    Ensure each pilot sheet has enough rows and columns for later stages.
    """
    requests: list[
        dict[str, Any]
    ] = []

    expanded: list[str] = []

    for (
        sheet_name,
        headers,
    ) in PILOT_SCHEMAS.items():
        _assert_allowed_sheet(
            sheet_name
        )

        properties = metadata.get(
            sheet_name
        )

        if not properties:
            raise RuntimeError(
                "Missing metadata for pilot "
                f"worksheet {sheet_name!r}."
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
            len(headers),
        )

        if (
            target_rows
            == current_rows
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
    service,
    spreadsheet_id: str,
    sheet_name: str,
    header_count: int,
) -> list[list[Any]]:
    """
    Read the area required for the pilot worksheet schema.
    """
    _assert_allowed_sheet(
        sheet_name
    )

    last_column = _column_letter(
        header_count
    )

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"A:{last_column}"
            ),
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _set_or_verify_header(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    expected_headers: list[str],
) -> str:
    """
    Write the header only when the worksheet is completely empty.

    Existing valid headers and data are left unchanged. Unexpected headers
    cause the workflow to stop without clearing the worksheet.
    """
    rows = _read_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        len(expected_headers),
    )

    if not rows:
        _assert_allowed_sheet(
            sheet_name
        )

        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!A1"
            ),
            valueInputOption="RAW",
            body={
                "values": [
                    expected_headers
                ]
            },
        ).execute()

        return "HEADER_CREATED"

    actual_headers = [
        str(value).strip()
        for value in rows[0]
    ]

    actual_headers += [
        ""
    ] * (
        len(expected_headers)
        - len(actual_headers)
    )

    actual_headers = actual_headers[
        :len(expected_headers)
    ]

    if (
        actual_headers
        != expected_headers
    ):
        raise RuntimeError(
            f"{sheet_name} already contains "
            "an unexpected header. "
            f"Expected {expected_headers}; "
            f"found {actual_headers}. "
            "No data were cleared or replaced."
        )

    return "HEADER_ALREADY_VALID"


def _verify_headers(
    service,
    spreadsheet_id: str,
) -> None:
    """
    Verify all three header rows after setup.
    """
    for (
        sheet_name,
        expected_headers,
    ) in PILOT_SCHEMAS.items():
        rows = _read_sheet_rows(
            service,
            spreadsheet_id,
            sheet_name,
            len(expected_headers),
        )

        if not rows:
            raise RuntimeError(
                "Post-setup verification "
                f"failed: {sheet_name} is empty."
            )

        actual_headers = [
            str(value).strip()
            for value in rows[0]
        ]

        actual_headers += [
            ""
        ] * (
            len(expected_headers)
            - len(actual_headers)
        )

        actual_headers = (
            actual_headers[
                :len(expected_headers)
            ]
        )

        if (
            actual_headers
            != expected_headers
        ):
            raise RuntimeError(
                "Post-setup verification "
                f"failed for {sheet_name}."
            )


def _write_receipt(
    receipt: dict[str, Any],
) -> Path:
    """
    Write an auditable setup receipt to the workflow artifact directory.
    """
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

    path = (
        output_dir
        / "pilot_sheet_setup_receipt.json"
    )

    path.write_text(
        json.dumps(
            receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return path


def main() -> None:
    """
    Create and verify the pilot worksheet structures.
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

    created = (
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

    unresolved = sorted(
        ALLOWED_WRITE_SHEETS.difference(
            metadata
        )
    )

    if unresolved:
        raise RuntimeError(
            "Unable to create pilot "
            "worksheets: "
            + ", ".join(
                unresolved
            )
        )

    expanded = (
        _expand_existing_pilot_sheets(
            service,
            spreadsheet_id,
            metadata,
        )
    )

    header_results = {
        sheet_name: (
            _set_or_verify_header(
                service,
                spreadsheet_id,
                sheet_name,
                headers,
            )
        )
        for (
            sheet_name,
            headers,
        ) in PILOT_SCHEMAS.items()
    }

    _verify_headers(
        service,
        spreadsheet_id,
    )

    receipt = {
        "status": "PASSED",
        "created_sheets": created,
        "expanded_sheets": expanded,
        "header_results": (
            header_results
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

    receipt_path = (
        _write_receipt(
            receipt
        )
    )

    print()
    print(
        "FUNNEL PILOT — PILOT SHEET SETUP"
    )
    print(
        "=" * 39
    )
    print(
        "Pilot worksheets:"
    )

    for sheet_name in sorted(
        PILOT_SCHEMAS
    ):
        print(
            f"  {sheet_name}: "
            f"{header_results[sheet_name]}"
        )

    print()
    print(
        f"Created worksheets:          "
        f"{len(created)}"
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