# VERSION: 2026-06-22-PILOT-SHEET-SETUP-LEGACY-MIGRATION-2
# Funnel Pilot: create, verify and safely migrate pilot worksheet headers

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


# Known header from the earlier pilot-tab design.
#
# This may be migrated automatically only when there are no data rows
# beneath the header.
LEGACY_PENDING_HEADERS = [
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
    Require explicit workflow confirmation before any worksheet creation
    or header migration.
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
            "column_number must be at least 1."
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
    Prevent every write outside the three approved pilot worksheets.
    """
    if sheet_name == PROTECTED_PRODUCTION_SHEET:
        raise RuntimeError(
            "Pilot setup is forbidden from writing "
            "to Stock Summary USD."
        )

    if sheet_name not in ALLOWED_WRITE_SHEETS:
        raise RuntimeError(
            "Pilot setup is forbidden from writing "
            f"to {sheet_name!r}."
        )


def _normalise_cells(
    values: list[Any],
) -> list[str]:
    """
    Convert a worksheet row into stripped strings.
    """
    return [
        str(value).strip()
        for value in values
    ]


def _trim_trailing_blanks(
    values: list[str],
) -> list[str]:
    """
    Remove trailing blank cells while retaining internal blank cells.
    """
    trimmed = list(values)

    while (
        trimmed
        and trimmed[-1] == ""
    ):
        trimmed.pop()

    return trimmed


def _contains_data_rows(
    rows: list[list[Any]],
) -> bool:
    """
    Return True when at least one non-empty cell exists below row 1.
    """
    for row in rows[1:]:
        for value in row:
            if str(value).strip():
                return True

    return False


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
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """
    Create only approved pilot worksheets that do not already exist.
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
                            "columnCount": required_columns,
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
    metadata: dict[str, dict[str, Any]],
) -> list[str]:
    """
    Ensure every pilot worksheet has enough rows and columns.
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
    service,
    spreadsheet_id: str,
    sheet_name: str,
    column_count: int,
) -> list[list[Any]]:
    """
    Read the worksheet area needed for schema verification.
    """
    _assert_allowed_sheet(
        sheet_name
    )

    last_column = _column_letter(
        column_count
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


def _write_header(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> None:
    """
    Write one approved pilot worksheet header.
    """
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


def _is_known_legacy_header(
    sheet_name: str,
    actual_headers: list[str],
) -> bool:
    """
    Identify an explicitly supported earlier pilot schema.
    """
    if sheet_name != PENDING_SHEET:
        return False

    return (
        actual_headers
        == LEGACY_PENDING_HEADERS
    )


def _set_or_verify_header(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    expected_headers: list[str],
) -> str:
    """
    Create, verify or safely migrate a pilot worksheet header.

    Rules:
    - an empty worksheet receives the current header;
    - the current header is accepted unchanged;
    - a known legacy header is migrated only when no data rows exist;
    - every other existing structure causes a safe failure.
    """
    read_column_count = max(
        len(expected_headers),
        len(LEGACY_PENDING_HEADERS),
    )

    rows = _read_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        read_column_count,
    )

    if not rows:
        _write_header(
            service,
            spreadsheet_id,
            sheet_name,
            expected_headers,
        )

        return "HEADER_CREATED"

    actual_headers = _trim_trailing_blanks(
        _normalise_cells(
            rows[0]
        )
    )

    if actual_headers == expected_headers:
        return "HEADER_ALREADY_VALID"

    if _is_known_legacy_header(
        sheet_name,
        actual_headers,
    ):
        if _contains_data_rows(
            rows
        ):
            raise RuntimeError(
                f"{sheet_name} uses the recognised legacy header, "
                "but contains data rows. No migration was performed. "
                "Back up or migrate those records before rerunning."
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
        f"Expected {expected_headers}; "
        f"found {actual_headers}. "
        "No data were cleared or replaced."
    )


def _verify_headers(
    service,
    spreadsheet_id: str,
) -> None:
    """
    Verify all three pilot worksheet headers after setup.
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
                "Post-setup verification failed: "
                f"{sheet_name} is empty."
            )

        actual_headers = _trim_trailing_blanks(
            _normalise_cells(
                rows[0]
            )
        )

        if actual_headers != expected_headers:
            raise RuntimeError(
                "Post-setup verification failed for "
                f"{sheet_name}. "
                f"Expected {expected_headers}; "
                f"found {actual_headers}."
            )


def _write_receipt(
    receipt: dict[str, Any],
) -> Path:
    """
    Write an auditable setup receipt to the workflow artefact directory.
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
    Create, migrate and verify pilot worksheet structures.
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

    created = _create_missing_pilot_sheets(
        service,
        spreadsheet_id,
        metadata,
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
            "Unable to create pilot worksheets: "
            + ", ".join(
                unresolved
            )
        )

    expanded = _expand_existing_pilot_sheets(
        service,
        spreadsheet_id,
        metadata,
    )

    header_results: dict[
        str,
        str,
    ] = {}

    for (
        sheet_name,
        headers,
    ) in PILOT_SCHEMAS.items():
        header_results[
            sheet_name
        ] = _set_or_verify_header(
            service,
            spreadsheet_id,
            sheet_name,
            headers,
        )

    _verify_headers(
        service,
        spreadsheet_id,
    )

    migrated = sorted(
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
        "created_sheets": created,
        "expanded_sheets": expanded,
        "migrated_legacy_headers": migrated,
        "header_results": header_results,
        "allowed_write_sheets": sorted(
            ALLOWED_WRITE_SHEETS
        ),
        "protected_sheet": PROTECTED_PRODUCTION_SHEET,
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
        f"Migrated legacy headers:     "
        f"{len(migrated)}"
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