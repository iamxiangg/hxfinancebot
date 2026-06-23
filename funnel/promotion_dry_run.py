# VERSION: 2026-06-23-PROMOTION-DRY-RUN-1
#
# Approval-based ticker promotion dry run:
# - reads Pending_New_Tickers;
# - reads Stock Summary USD;
# - identifies explicitly approved, complete, current candidates;
# - rejects duplicate, incomplete or inconsistent approvals;
# - writes CSV and JSON artefacts only;
# - never modifies any Google Sheet.

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)
from funnel.pilot_funnel_writer import PENDING_HEADERS
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.signal_schema import normalise_ticker


logger = logging.getLogger(__name__)

MASTER_SHEET = "Stock Summary USD"
PENDING_SHEET = "Pending_New_Tickers"

REQUIRED_SHEETS = {
    MASTER_SHEET,
    PENDING_SHEET,
}

BASE_EXPORT_HEADERS = [
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
    "Pending Sheet Row",
]

APPROVED_EXPORT_HEADERS = (
    BASE_EXPORT_HEADERS
    + [
        "Master Status",
        "Dry Run Action",
    ]
)

REJECTED_EXPORT_HEADERS = (
    BASE_EXPORT_HEADERS
    + [
        "Master Status",
        "Dry Run Action",
        "Rejection Reason",
    ]
)


def _text(value: Any) -> str:
    """Return a stripped string."""
    if value is None:
        return ""

    return str(value).strip()


def _ticker(value: Any) -> str:
    """Return a normalised ticker or a blank value."""
    raw_value = _text(value)

    if not raw_value:
        return ""

    return normalise_ticker(raw_value)


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


def _preview_count() -> int:
    """Return the number of records to show in the workflow log."""
    raw_value = _text(
        os.getenv(
            "PREVIEW_COUNT",
            "10",
        )
    )

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "PREVIEW_COUNT must be an integer."
        ) from exc

    return max(
        0,
        min(
            value,
            50,
        ),
    )


def _column_letter(column_number: int) -> str:
    """Convert a one-based column number to a Sheets column label."""
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


def _get_sheet_titles(
    service: Any,
    spreadsheet_id: str,
) -> set[str]:
    """Return the worksheet titles in the spreadsheet."""
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        )
        .execute()
    )

    titles: set[str] = set()

    for sheet in response.get(
        "sheets",
        [],
    ):
        title = _text(
            sheet.get(
                "properties",
                {},
            ).get(
                "title"
            )
        )

        if title:
            titles.add(title)

    return titles


def _read_values(
    service: Any,
    spreadsheet_id: str,
    range_name: str,
) -> list[list[Any]]:
    """Read one Google Sheets range."""
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _normalise_header(row: list[Any]) -> list[str]:
    """Normalise a worksheet header and remove trailing blanks."""
    values = [
        _text(value)
        for value in row
    ]

    while values and not values[-1]:
        values.pop()

    return values


def _read_pending_records(
    service: Any,
    spreadsheet_id: str,
) -> list[dict[str, str]]:
    """Read Pending_New_Tickers without modifying it."""
    final_column = _column_letter(
        len(PENDING_HEADERS)
    )

    rows = _read_values(
        service,
        spreadsheet_id,
        (
            f"'{PENDING_SHEET}'!"
            f"A1:{final_column}"
        ),
    )

    if not rows:
        raise RuntimeError(
            f"{PENDING_SHEET} is empty."
        )

    actual_headers = _normalise_header(
        rows[0]
    )

    if actual_headers != PENDING_HEADERS:
        raise RuntimeError(
            f"{PENDING_SHEET} header mismatch. "
            f"Expected {PENDING_HEADERS}; "
            f"found {actual_headers}."
        )

    records: list[dict[str, str]] = []

    for sheet_row, raw_row in enumerate(
        rows[1:],
        start=2,
    ):
        padded = (
            list(raw_row)
            + [""] * len(PENDING_HEADERS)
        )[:len(PENDING_HEADERS)]

        if not any(
            _text(value)
            for value in padded
        ):
            continue

        record = {
            header: _text(
                padded[index]
            )
            for index, header
            in enumerate(PENDING_HEADERS)
        }

        record["Ticker"] = _ticker(
            record.get("Ticker")
        )

        record["_Sheet Row"] = str(
            sheet_row
        )

        records.append(record)

    return records


def _extract_master_ticker(
    record: dict[str, Any],
) -> str:
    """Extract a ticker from a master-sheet reader record."""
    for key in (
        "ticker",
        "Ticker",
        "symbol",
        "Symbol",
    ):
        value = _ticker(
            record.get(key)
        )

        if value:
            return value

    return ""


def _build_master_index(
    ticker_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Create a unique ticker index for Stock Summary USD."""
    index: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()

    for record in ticker_records:
        ticker = _extract_master_ticker(
            record
        )

        if not ticker:
            continue

        if ticker in index:
            duplicates.add(ticker)
            continue

        index[ticker] = record

    if duplicates:
        raise RuntimeError(
            "Stock Summary USD contains duplicate tickers: "
            + ", ".join(
                sorted(duplicates)
            )
        )

    return index


def _safe_csv_value(value: Any) -> str:
    """Reduce spreadsheet-formula injection risk in generated CSV files."""
    text = _text(value)

    if text.startswith(
        (
            "=",
            "+",
            "-",
            "@",
        )
    ):
        return "'" + text

    return text


def _write_csv(
    path: Path,
    headers: list[str],
    records: list[dict[str, Any]],
) -> None:
    """Write a deterministic UTF-8 CSV file."""
    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=headers,
            extrasaction="ignore",
        )

        writer.writeheader()

        for record in records:
            writer.writerow(
                {
                    header: _safe_csv_value(
                        record.get(
                            header,
                            "",
                        )
                    )
                    for header in headers
                }
            )


def _sha256(path: Path) -> str:
    """Return the SHA-256 hash of a file."""
    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        for chunk in iter(
            lambda: file_handle.read(
                1024 * 1024
            ),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def _write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:
    """Write formatted UTF-8 JSON."""
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )


def _base_export_record(
    record: dict[str, str],
) -> dict[str, Any]:
    """Map one pending-sheet row into the export schema."""
    output = {
        header: record.get(
            header,
            "",
        )
        for header in BASE_EXPORT_HEADERS
        if header != "Pending Sheet Row"
    }

    output["Ticker"] = _ticker(
        record.get("Ticker")
    )

    output["Pending Sheet Row"] = (
        record.get(
            "_Sheet Row",
            "",
        )
    )

    return output

def _evaluate_promotions(
    pending_records: list[dict[str, str]],
    master_index: dict[str, dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
]:
    """
    Evaluate explicit promotion requests.

    Only rows with Add to Stock Summary USD? = YES are considered
    promotion requests. HOLD, REVIEW and blank values remain untouched.
    """
    ticker_counts = Counter(
        _ticker(
            record.get("Ticker")
        )
        for record in pending_records
        if _ticker(
            record.get("Ticker")
        )
    )

    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    approval_request_count = 0

    for record in pending_records:
        approval_flag = _text(
            record.get(
                "Add to Stock Summary USD?"
            )
        ).upper()

        if approval_flag != "YES":
            continue

        approval_request_count += 1

        ticker = _ticker(
            record.get("Ticker")
        )

        validation_status = _text(
            record.get(
                "Validation Status"
            )
        ).upper()

        current_run = _text(
            record.get(
                "Current Run"
            )
        ).upper()

        stock_name = _text(
            record.get(
                "Stock Name"
            )
        )

        google_ticker = _text(
            record.get(
                "Google Ticker"
            )
        )

        signal_id = _text(
            record.get(
                "Signal ID"
            )
        )

        added_date = _text(
            record.get(
                "Added Date"
            )
        )

        reasons: list[str] = []

        if not ticker:
            reasons.append(
                "MISSING_TICKER"
            )

        if ticker and ticker_counts[ticker] > 1:
            reasons.append(
                "DUPLICATE_TICKER_IN_PENDING_SHEET"
            )

        if validation_status != "REVIEWED":
            reasons.append(
                "VALIDATION_STATUS_NOT_REVIEWED"
            )

        if current_run != "YES":
            reasons.append(
                "NOT_IN_CURRENT_RUN"
            )

        if not stock_name:
            reasons.append(
                "MISSING_STOCK_NAME"
            )

        if not google_ticker:
            reasons.append(
                "MISSING_GOOGLE_TICKER"
            )

        if not signal_id:
            reasons.append(
                "MISSING_SIGNAL_ID"
            )

        already_in_master = bool(
            ticker
            and ticker in master_index
        )

        if already_in_master:
            reasons.append(
                "TICKER_ALREADY_IN_STOCK_SUMMARY_USD"
            )

        if added_date and not already_in_master:
            reasons.append(
                "ADDED_DATE_PRESENT_BUT_TICKER_NOT_IN_MASTER"
            )

        export_record = _base_export_record(
            record
        )

        export_record["Master Status"] = (
            "ALREADY_PRESENT"
            if already_in_master
            else "NOT_PRESENT"
        )

        if reasons:
            export_record["Dry Run Action"] = (
                "REJECTED"
            )

            export_record["Rejection Reason"] = (
                " | ".join(reasons)
            )

            rejected.append(
                export_record
            )

        else:
            export_record["Dry Run Action"] = (
                "ELIGIBLE_FOR_PROMOTION"
            )

            approved.append(
                export_record
            )

    approved.sort(
        key=lambda item: _ticker(
            item.get("Ticker")
        )
    )

    rejected.sort(
        key=lambda item: (
            _ticker(
                item.get("Ticker")
            ),
            _text(
                item.get(
                    "Pending Sheet Row"
                )
            ),
        )
    )

    return (
        approved,
        rejected,
        approval_request_count,
    )


def main() -> None:
    """Run the approval-based promotion dry run."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
    )

    output_directory = (
        _output_directory()
    )

    preview_count = (
        _preview_count()
    )

    service = get_sheets_service(
        readonly=True
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    sheet_titles = _get_sheet_titles(
        service,
        spreadsheet_id,
    )

    missing_sheets = sorted(
        REQUIRED_SHEETS.difference(
            sheet_titles
        )
    )

    if missing_sheets:
        raise RuntimeError(
            "Required worksheets are missing: "
            + ", ".join(
                missing_sheets
            )
        )

    logger.info(
        "Reading Pending_New_Tickers."
    )

    pending_records = (
        _read_pending_records(
            service,
            spreadsheet_id,
        )
    )

    logger.info(
        "Reading Stock Summary USD."
    )

    ticker_records = (
        get_stock_summary_ticker_records()
    )

    master_index = (
        _build_master_index(
            ticker_records
        )
    )

    (
        approved_records,
        rejected_records,
        approval_request_count,
    ) = _evaluate_promotions(
        pending_records,
        master_index,
    )

    approved_path = (
        output_directory
        / "approved_ticker_promotions.csv"
    )

    rejected_path = (
        output_directory
        / "rejected_ticker_promotions.csv"
    )

    receipt_path = (
        output_directory
        / "promotion_dry_run_receipt.json"
    )

    _write_csv(
        approved_path,
        APPROVED_EXPORT_HEADERS,
        approved_records,
    )

    _write_csv(
        rejected_path,
        REJECTED_EXPORT_HEADERS,
        rejected_records,
    )

    receipt = {
        "status": "PASSED",
        "mode": "PROMOTION_DRY_RUN",
        "pending_rows_read": len(
            pending_records
        ),
        "stock_summary_tickers_read": len(
            master_index
        ),
        "approval_requests_found": (
            approval_request_count
        ),
        "eligible_promotions": len(
            approved_records
        ),
        "rejected_promotions": len(
            rejected_records
        ),
        "approved_artifact": {
            "path": str(
                approved_path
            ),
            "sha256": _sha256(
                approved_path
            ),
        },
        "rejected_artifact": {
            "path": str(
                rejected_path
            ),
            "sha256": _sha256(
                rejected_path
            ),
        },
        "google_sheets_written": [],
        "stock_summary_usd_written": False,
        "pending_new_tickers_written": False,
        "telegram_messages_sent": False,
    }

    _write_json(
        receipt_path,
        receipt,
    )

    print()
    print(
        "FUNNEL PILOT — PROMOTION DRY RUN"
    )
    print(
        "=" * 38
    )
    print(
        "Pending rows read:           "
        f"{len(pending_records)}"
    )
    print(
        "Stock Summary tickers read:  "
        f"{len(master_index)}"
    )
    print(
        "Approval requests found:     "
        f"{approval_request_count}"
    )
    print(
        "Eligible promotions:         "
        f"{len(approved_records)}"
    )
    print(
        "Rejected promotions:         "
        f"{len(rejected_records)}"
    )
    print(
        "Stock Summary USD writes:    None"
    )
    print(
        "Pending ticker writes:       None"
    )
    print(
        "Telegram messages:           None"
    )

    if approved_records and preview_count:
        print()
        print(
            "ELIGIBLE PROMOTION PREVIEW"
        )

        for record in approved_records[
            :preview_count
        ]:
            print(
                "  "
                f"{record['Ticker']} | "
                f"{record['Stock Name']} | "
                f"{record['Google Ticker']} | "
                f"row {record['Pending Sheet Row']}"
            )

    if rejected_records and preview_count:
        print()
        print(
            "REJECTED PROMOTION PREVIEW"
        )

        for record in rejected_records[
            :preview_count
        ]:
            ticker = (
                record.get("Ticker")
                or "<blank ticker>"
            )

            print(
                "  "
                f"{ticker} | "
                f"row {record['Pending Sheet Row']} | "
                f"{record['Rejection Reason']}"
            )

    print()
    print(
        "Approved CSV:                "
        f"{approved_path}"
    )
    print(
        "Rejected CSV:                "
        f"{rejected_path}"
    )
    print(
        "Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "PROMOTION DRY RUN COMPLETED "
        "SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()