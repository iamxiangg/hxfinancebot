# VERSION: 2026-06-22-PILOT-FUNNEL-WRITER-1
#
# Controlled pilot write stage:
# - reads Stock Summary USD;
# - reruns the Congress adapter;
# - writes only to Funnel_Pilot and Pending_New_Tickers;
# - preserves manual-review fields;
# - retains historical rows with Current Run = NO;
# - never writes to Stock Summary USD or Scanner_Signal_Log_Pilot;
# - verifies both worksheets after writing.

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from funnel.candidate_ingestor import (
    classify_signals,
    get_pending_new_ticker_records,
)
from funnel.congress_adapter import run_congress_adapter
from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)
from funnel.sheet_reader import (
    get_stock_summary_ticker_records,
)


logger = logging.getLogger(__name__)

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

FUNNEL_SHEET = "Funnel_Pilot"
PENDING_SHEET = "Pending_New_Tickers"

ALLOWED_WRITE_SHEETS = {
    FUNNEL_SHEET,
    PENDING_SHEET,
}

PROTECTED_SHEETS = {
    "Stock Summary USD",
    "Scanner_Signal_Log_Pilot",
}


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


CLASSIFICATION_RANK = {
    "actionable": 5,
    "wait": 4,
    "risk": 3,
    "near_miss": 2,
    "other": 1,
}


def _require_confirmation() -> None:
    """Require explicit confirmation before writing pilot funnel data."""
    confirmation = str(
        os.getenv(
            "CONFIRM_PILOT_FUNNEL_WRITE",
            "",
        )
    ).strip().upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Pilot funnel writing was not confirmed. "
            "Set CONFIRM_PILOT_FUNNEL_WRITE=YES."
        )


def _float_environment(
    name: str,
    default: float,
) -> float:
    """Read a finite float from an environment variable."""
    raw_value = str(
        os.getenv(
            name,
            str(default),
        )
    ).strip()

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be numeric. "
            f"Received: {raw_value!r}."
        ) from exc

    if not math.isfinite(value):
        raise ValueError(
            f"{name} must be finite."
        )

    return value


def _assert_write_target(
    sheet_name: str,
) -> None:
    """Permit writes only to the two approved pilot worksheets."""
    if sheet_name in PROTECTED_SHEETS:
        raise RuntimeError(
            f"Writing to protected worksheet "
            f"{sheet_name!r} is prohibited."
        )

    if sheet_name not in ALLOWED_WRITE_SHEETS:
        raise RuntimeError(
            f"Writing to worksheet "
            f"{sheet_name!r} is prohibited."
        )


def _column_letter(
    column_number: int,
) -> str:
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


def _cell_text(
    value: Any,
) -> str:
    """Return a stripped worksheet-compatible string."""
    if value is None:
        return ""

    return str(value).strip()


def _normalise_ticker(
    value: Any,
) -> str:
    """Return a normalised ticker."""
    return _cell_text(
        value
    ).upper()


def _number_or_blank(
    value: Any,
) -> float | int | str:
    """Return a finite number or a blank worksheet value."""
    if value is None or value == "":
        return ""

    try:
        number = float(value)
    except (
        TypeError,
        ValueError,
    ):
        return ""

    if not math.isfinite(number):
        return ""

    if number.is_integer():
        return int(number)

    return number


def _get_sheet_titles(
    service: Any,
    spreadsheet_id: str,
) -> set[str]:
    """Return all worksheet titles in the spreadsheet."""
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
        title = _cell_text(
            sheet.get(
                "properties",
                {},
            ).get(
                "title",
                "",
            )
        )

        if title:
            titles.add(
                title
            )

    return titles


def _read_values(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    range_suffix: str,
) -> list[list[Any]]:
    """Read values from one worksheet range."""
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"{range_suffix}"
            ),
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _normalise_header(
    row: list[Any],
) -> list[str]:
    """Normalise a worksheet header and remove trailing blanks."""
    values = [
        _cell_text(value)
        for value in row
    ]

    while values and not values[-1]:
        values.pop()

    return values


def _verify_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    expected_headers: list[str],
) -> None:
    """Verify an exact approved pilot worksheet header."""
    _assert_write_target(
        sheet_name
    )

    final_column = _column_letter(
        len(expected_headers)
    )

    rows = _read_values(
        service,
        spreadsheet_id,
        sheet_name,
        f"A1:{final_column}1",
    )

    if not rows:
        raise RuntimeError(
            f"{sheet_name} has no header. "
            "Run pilot-sheet-setup first."
        )

    actual_headers = _normalise_header(
        rows[0]
    )

    if actual_headers != expected_headers:
        raise RuntimeError(
            f"{sheet_name} header mismatch. "
            f"Expected {expected_headers}; "
            f"found {actual_headers}. "
            "No data rows were written."
        )


def _read_table(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> tuple[
    list[dict[str, str]],
    int,
]:
    """
    Read a pilot worksheet into dictionaries.

    Returns the records and the number of existing data rows.
    """
    _assert_write_target(
        sheet_name
    )

    final_column = _column_letter(
        len(headers)
    )

    rows = _read_values(
        service,
        spreadsheet_id,
        sheet_name,
        f"A1:{final_column}",
    )

    if not rows:
        raise RuntimeError(
            f"{sheet_name} is empty."
        )

    actual_headers = _normalise_header(
        rows[0]
    )

    if actual_headers != headers:
        raise RuntimeError(
            f"{sheet_name} header changed before reading."
        )

    records: list[
        dict[str, str]
    ] = []

    for row_number, raw_row in enumerate(
        rows[1:],
        start=2,
    ):
        padded = list(
            raw_row
        ) + [""] * (
            len(headers)
            - len(raw_row)
        )

        padded = padded[
            :len(headers)
        ]

        if not any(
            _cell_text(value)
            for value in padded
        ):
            continue

        record = {
            header: _cell_text(
                padded[index]
            )
            for index, header
            in enumerate(headers)
        }

        ticker = _normalise_ticker(
            record.get(
                "Ticker"
            )
        )

        if not ticker:
            raise RuntimeError(
                f"{sheet_name} row {row_number} "
                "contains data but has no ticker."
            )

        record[
            "Ticker"
        ] = ticker

        records.append(
            record
        )

    return records, len(
        rows[1:]
    )


def _index_existing_rows(
    records: list[dict[str, str]],
    sheet_name: str,
) -> dict[str, dict[str, str]]:
    """Index existing worksheet rows and reject duplicate tickers."""
    output: dict[
        str,
        dict[str, str],
    ] = {}

    for record in records:
        ticker = _normalise_ticker(
            record.get(
                "Ticker"
            )
        )

        if ticker in output:
            raise RuntimeError(
                f"{sheet_name} contains duplicate ticker "
                f"{ticker!r}. No rows were written."
            )

        output[
            ticker
        ] = record

    return output


def _funnel_current_record(
    comparison: dict[str, Any],
    existing: dict[str, str] | None,
) -> dict[str, Any]:
    """Build one current Funnel_Pilot record."""
    return {
        "Ticker": comparison.get(
            "ticker",
            "",
        ),
        "Stock Name": comparison.get(
            "stock_name",
            "",
        ),
        "Google Ticker": comparison.get(
            "google_ticker",
            "",
        ),
        "Already in Stock Summary USD?": (
            comparison.get(
                "already_in_stock_summary",
                "",
            )
        ),
        "Stock Summary Row": comparison.get(
            "stock_summary_row",
            "",
        ),
        "Candidate Status": comparison.get(
            "candidate_status",
            "",
        ),
        "Pending New Ticker?": comparison.get(
            "pending_new_ticker",
            "",
        ),
        "Review Route": comparison.get(
            "review_route",
            "",
        ),
        "Review Priority": comparison.get(
            "review_priority",
            "",
        ),
        "Scanner": comparison.get(
            "scanner",
            "",
        ),
        "Latest Classification": comparison.get(
            "classification",
            "",
        ),
        "Latest Score": _number_or_blank(
            comparison.get(
                "score"
            )
        ),
        "Entry Quality": _number_or_blank(
            comparison.get(
                "entry_quality"
            )
        ),
        "Estimated Capital Mid": _number_or_blank(
            comparison.get(
                "estimated_capital_mid"
            )
        ),
        "Buyers": _number_or_blank(
            comparison.get(
                "buyers"
            )
        ),
        "Cluster Buyers": _number_or_blank(
            comparison.get(
                "cluster_buyers"
            )
        ),
        "Flow": comparison.get(
            "flow",
            "",
        ),
        "Names": comparison.get(
            "names",
            "",
        ),
        "Opportunity Stage": comparison.get(
            "opportunity_stage",
            "",
        ),
        "Discovery Reason": comparison.get(
            "discovery_reason",
            "",
        ),
        "Signal Count": _number_or_blank(
            comparison.get(
                "signal_count"
            )
        ),
        "Latest Signal Date": comparison.get(
            "observed_at",
            "",
        ),
        "Valid Until": comparison.get(
            "valid_until",
            "",
        ),
        "Signal ID": comparison.get(
            "signal_id",
            "",
        ),
        "Current Run": "YES",
        "Manual Decision": (
            existing.get(
                "Manual Decision",
                "",
            )
            if existing
            else ""
        ),
        "Notes": (
            existing.get(
                "Notes",
                "",
            )
            if existing
            else ""
        ),
    }


def _pending_current_record(
    pending: dict[str, Any],
    existing: dict[str, str] | None,
) -> dict[str, Any]:
    """Build one current Pending_New_Tickers record."""
    observed_at = _cell_text(
        pending.get(
            "observed_at"
        )
    )

    first_seen = (
        _cell_text(
            existing.get(
                "First Seen"
            )
        )
        if existing
        else ""
    )

    if not first_seen:
        first_seen = observed_at

    return {
        "Ticker": pending.get(
            "ticker",
            "",
        ),
        "Stock Name": pending.get(
            "stock_name",
            "",
        ),
        "Google Ticker": pending.get(
            "google_ticker",
            "",
        ),
        "Scanner": pending.get(
            "scanner",
            "",
        ),
        "Classification": pending.get(
            "classification",
            "",
        ),
        "Score": _number_or_blank(
            pending.get(
                "score"
            )
        ),
        "Entry Quality": _number_or_blank(
            pending.get(
                "entry_quality"
            )
        ),
        "Estimated Capital Mid": _number_or_blank(
            pending.get(
                "estimated_capital_mid"
            )
        ),
        "Buyers": _number_or_blank(
            pending.get(
                "buyers"
            )
        ),
        "Cluster Buyers": _number_or_blank(
            pending.get(
                "cluster_buyers"
            )
        ),
        "Flow": pending.get(
            "flow",
            "",
        ),
        "Names": pending.get(
            "names",
            "",
        ),
        "Review Priority": pending.get(
            "review_priority",
            "",
        ),
        "Opportunity Stage": pending.get(
            "opportunity_stage",
            "",
        ),
        "Discovery Reason": pending.get(
            "discovery_reason",
            "",
        ),
        "First Seen": first_seen,
        "Last Seen": observed_at,
        "Valid Until": pending.get(
            "valid_until",
            "",
        ),
        "Signal ID": pending.get(
            "signal_id",
            "",
        ),
        "Current Run": "YES",
        "Validation Status": (
            existing.get(
                "Validation Status",
                "",
            )
            if existing
            else "PENDING_REVIEW"
        ),
        "Add to Stock Summary USD?": (
            existing.get(
                "Add to Stock Summary USD?",
                "",
            )
            if existing
            else "REVIEW"
        ),
        "Added Date": (
            existing.get(
                "Added Date",
                "",
            )
            if existing
            else ""
        ),
        "Reviewer Notes": (
            existing.get(
                "Reviewer Notes",
                "",
            )
            if existing
            else ""
        ),
    }


def _stale_record(
    existing: dict[str, str],
    headers: list[str],
) -> dict[str, Any]:
    """Retain a historical record and mark it as absent from the current run."""
    record = {
        header: existing.get(
            header,
            "",
        )
        for header in headers
    }

    record[
        "Current Run"
    ] = "NO"

    return record


def _merge_funnel_records(
    comparison: list[dict[str, Any]],
    existing_records: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Merge current funnel output with historical pilot rows."""
    existing_by_ticker = (
        _index_existing_rows(
            existing_records,
            FUNNEL_SHEET,
        )
    )

    current_tickers: set[
        str
    ] = set()

    current_records: list[
        dict[str, Any]
    ] = []

    for comparison_record in comparison:
        ticker = _normalise_ticker(
            comparison_record.get(
                "ticker"
            )
        )

        if not ticker:
            raise RuntimeError(
                "A comparison record has no ticker."
            )

        if ticker in current_tickers:
            raise RuntimeError(
                f"Duplicate current funnel ticker: {ticker}"
            )

        current_tickers.add(
            ticker
        )

        current_records.append(
            _funnel_current_record(
                comparison_record,
                existing_by_ticker.get(
                    ticker
                ),
            )
        )

    stale_records = [
        _stale_record(
            existing,
            FUNNEL_HEADERS,
        )
        for ticker, existing
        in existing_by_ticker.items()
        if ticker not in current_tickers
    ]

    stale_records.sort(
        key=lambda record: _normalise_ticker(
            record.get(
                "Ticker"
            )
        )
    )

    return (
        current_records
        + stale_records
    )


def _merge_pending_records(
    pending: list[dict[str, Any]],
    existing_records: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Merge current pending candidates with historical review rows."""
    existing_by_ticker = (
        _index_existing_rows(
            existing_records,
            PENDING_SHEET,
        )
    )

    current_tickers: set[
        str
    ] = set()

    current_records: list[
        dict[str, Any]
    ] = []

    for pending_record in pending:
        ticker = _normalise_ticker(
            pending_record.get(
                "ticker"
            )
        )

        if not ticker:
            raise RuntimeError(
                "A pending candidate has no ticker."
            )

        if ticker in current_tickers:
            raise RuntimeError(
                f"Duplicate pending ticker: {ticker}"
            )

        current_tickers.add(
            ticker
        )

        current_records.append(
            _pending_current_record(
                pending_record,
                existing_by_ticker.get(
                    ticker
                ),
            )
        )

    current_records.sort(
        key=lambda record: (
            CLASSIFICATION_RANK.get(
                _cell_text(
                    record.get(
                        "Classification"
                    )
                ).lower(),
                0,
            ),
            float(
                record.get(
                    "Score"
                )
                or 0
            ),
            _normalise_ticker(
                record.get(
                    "Ticker"
                )
            ),
        ),
        reverse=True,
    )

    stale_records = [
        _stale_record(
            existing,
            PENDING_HEADERS,
        )
        for ticker, existing
        in existing_by_ticker.items()
        if ticker not in current_tickers
    ]

    stale_records.sort(
        key=lambda record: _normalise_ticker(
            record.get(
                "Ticker"
            )
        )
    )

    return (
        current_records
        + stale_records
    )


def _records_to_rows(
    records: list[dict[str, Any]],
    headers: list[str],
) -> list[list[Any]]:
    """Convert dictionaries into the exact worksheet column order."""
    rows: list[
        list[Any]
    ] = []

    for record in records:
        ticker = _normalise_ticker(
            record.get(
                "Ticker"
            )
        )

        if not ticker:
            raise RuntimeError(
                "Attempted to create a worksheet row without a ticker."
            )

        record[
            "Ticker"
        ] = ticker

        rows.append(
            [
                record.get(
                    header,
                    "",
                )
                for header in headers
            ]
        )

    return rows


def _write_new_rows(
    service: Any,
    spreadsheet_id: str,
    funnel_rows: list[list[Any]],
    pending_rows: list[list[Any]],
) -> None:
    """
    Write new rows before clearing any obsolete trailing rows.

    This reduces the risk of losing existing manual-review data if the
    initial write request fails.
    """
    data: list[
        dict[str, Any]
    ] = []

    if funnel_rows:
        _assert_write_target(
            FUNNEL_SHEET
        )

        data.append(
            {
                "range": (
                    f"'{FUNNEL_SHEET}'!A2"
                ),
                "majorDimension": "ROWS",
                "values": funnel_rows,
            }
        )

    if pending_rows:
        _assert_write_target(
            PENDING_SHEET
        )

        data.append(
            {
                "range": (
                    f"'{PENDING_SHEET}'!A2"
                ),
                "majorDimension": "ROWS",
                "values": pending_rows,
            }
        )

    if not data:
        return

    (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": data,
            },
        )
        .execute()
    )


def _clear_obsolete_rows(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    old_row_count: int,
    new_row_count: int,
) -> None:
    """Clear only rows left behind after a shorter replacement."""
    if new_row_count >= old_row_count:
        return

    _assert_write_target(
        sheet_name
    )

    first_clear_row = (
        new_row_count
        + 2
    )

    final_old_row = (
        old_row_count
        + 1
    )

    final_column = _column_letter(
        len(headers)
    )

    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"A{first_clear_row}:"
                f"{final_column}{final_old_row}"
            ),
            body={},
        )
        .execute()
    )


def _verify_written_table(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    expected_records: list[dict[str, Any]],
) -> None:
    """Read a written table back and verify identifiers and manual fields."""
    actual_records, _ = _read_table(
        service,
        spreadsheet_id,
        sheet_name,
        headers,
    )

    if len(actual_records) != len(
        expected_records
    ):
        raise RuntimeError(
            f"{sheet_name} read-back count mismatch. "
            f"Expected {len(expected_records)} rows; "
            f"found {len(actual_records)}."
        )

    expected_tickers = [
        _normalise_ticker(
            record.get(
                "Ticker"
            )
        )
        for record in expected_records
    ]

    actual_tickers = [
        _normalise_ticker(
            record.get(
                "Ticker"
            )
        )
        for record in actual_records
    ]

    if actual_tickers != expected_tickers:
        raise RuntimeError(
            f"{sheet_name} ticker order or contents "
            "changed during writing."
        )

    manual_fields = (
        [
            "Manual Decision",
            "Notes",
        ]
        if sheet_name == FUNNEL_SHEET
        else [
            "First Seen",
            "Validation Status",
            "Add to Stock Summary USD?",
            "Added Date",
            "Reviewer Notes",
        ]
    )

    for index, (
        expected,
        actual,
    ) in enumerate(
        zip(
            expected_records,
            actual_records,
        ),
        start=2,
    ):
        expected_current = _cell_text(
            expected.get(
                "Current Run"
            )
        )

        actual_current = _cell_text(
            actual.get(
                "Current Run"
            )
        )

        if actual_current != expected_current:
            raise RuntimeError(
                f"{sheet_name} row {index} "
                "has an incorrect Current Run value."
            )

        expected_signal_id = _cell_text(
            expected.get(
                "Signal ID"
            )
        )

        actual_signal_id = _cell_text(
            actual.get(
                "Signal ID"
            )
        )

        if actual_signal_id != expected_signal_id:
            raise RuntimeError(
                f"{sheet_name} row {index} "
                "has an incorrect Signal ID."
            )

        for field in manual_fields:
            if _cell_text(
                actual.get(
                    field
                )
            ) != _cell_text(
                expected.get(
                    field
                )
            ):
                raise RuntimeError(
                    f"{sheet_name} row {index} "
                    f"did not preserve {field!r}."
                )


def _write_receipt(
    payload: dict[str, Any],
) -> Path:
    """Write an auditable pilot-write receipt."""
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
        / "pilot_funnel_write_receipt.json"
    )

    receipt_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )

    return receipt_path


def main() -> None:
    """Populate Funnel_Pilot and Pending_New_Tickers safely."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
    )

    _require_confirmation()

    minimum_conviction = (
        _float_environment(
            "MIN_CONVICTION",
            15.0,
        )
    )

    run_timestamp = datetime.now(
        SINGAPORE_TZ
    )

    run_id = run_timestamp.strftime(
        "%Y%m%dT%H%M%S%z"
    )

    service = get_sheets_service(
        readonly=False
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    sheet_titles = _get_sheet_titles(
        service,
        spreadsheet_id,
    )

    required_sheets = (
        ALLOWED_WRITE_SHEETS
        | PROTECTED_SHEETS
    )

    missing_sheets = sorted(
        required_sheets.difference(
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

    _verify_header(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        FUNNEL_HEADERS,
    )

    _verify_header(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        PENDING_HEADERS,
    )

    existing_funnel, old_funnel_count = (
        _read_table(
            service,
            spreadsheet_id,
            FUNNEL_SHEET,
            FUNNEL_HEADERS,
        )
    )

    existing_pending, old_pending_count = (
        _read_table(
            service,
            spreadsheet_id,
            PENDING_SHEET,
            PENDING_HEADERS,
        )
    )

    logger.info(
        "Loading Stock Summary USD ticker universe."
    )

    ticker_records = (
        get_stock_summary_ticker_records()
    )

    logger.info(
        "Loaded %d monitored tickers.",
        len(ticker_records),
    )

    logger.info(
        "Running Congress adapter with "
        "minimum conviction %.1f.",
        minimum_conviction,
    )

    signals, analysed_count = (
        run_congress_adapter(
            min_conviction=minimum_conviction
        )
    )

    comparison = classify_signals(
        signals=signals,
        ticker_records=ticker_records,
    )

    pending = (
        get_pending_new_ticker_records(
            comparison
        )
    )

    funnel_records = (
        _merge_funnel_records(
            comparison,
            existing_funnel,
        )
    )

    pending_records = (
        _merge_pending_records(
            pending,
            existing_pending,
        )
    )

    funnel_rows = _records_to_rows(
        funnel_records,
        FUNNEL_HEADERS,
    )

    pending_rows = _records_to_rows(
        pending_records,
        PENDING_HEADERS,
    )

    _write_new_rows(
        service,
        spreadsheet_id,
        funnel_rows,
        pending_rows,
    )

    _clear_obsolete_rows(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        FUNNEL_HEADERS,
        old_funnel_count,
        len(funnel_rows),
    )

    _clear_obsolete_rows(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        PENDING_HEADERS,
        old_pending_count,
        len(pending_rows),
    )

    _verify_written_table(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        FUNNEL_HEADERS,
        funnel_records,
    )

    _verify_written_table(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        PENDING_HEADERS,
        pending_records,
    )

    current_funnel_count = sum(
        1
        for record in funnel_records
        if record.get(
            "Current Run"
        )
        == "YES"
    )

    historical_funnel_count = (
        len(funnel_records)
        - current_funnel_count
    )

    current_pending_count = sum(
        1
        for record in pending_records
        if record.get(
            "Current Run"
        )
        == "YES"
    )

    historical_pending_count = (
        len(pending_records)
        - current_pending_count
    )

    receipt = {
        "status": "PASSED",
        "run_id": run_id,
        "write_timestamp": (
            run_timestamp.isoformat()
        ),
        "minimum_conviction": (
            minimum_conviction
        ),
        "congress_tickers_analysed": (
            analysed_count
        ),
        "signals_retained": len(
            signals
        ),
        "funnel_rows_written": len(
            funnel_records
        ),
        "current_funnel_rows": (
            current_funnel_count
        ),
        "historical_funnel_rows": (
            historical_funnel_count
        ),
        "pending_rows_written": len(
            pending_records
        ),
        "current_pending_rows": (
            current_pending_count
        ),
        "historical_pending_rows": (
            historical_pending_count
        ),
        "manual_fields_preserved": True,
        "written_sheets": sorted(
            ALLOWED_WRITE_SHEETS
        ),
        "protected_sheets_written": [],
        "stock_summary_usd_written": False,
        "scanner_signal_log_written": False,
    }

    receipt_path = _write_receipt(
        receipt
    )

    print()
    print(
        "FUNNEL PILOT — FUNNEL DATA WRITE"
    )
    print(
        "=" * 39
    )
    print(
        f"Congress tickers analysed:   "
        f"{analysed_count}"
    )
    print(
        f"Signals retained:            "
        f"{len(signals)}"
    )
    print(
        f"Current funnel rows:         "
        f"{current_funnel_count}"
    )
    print(
        f"Historical funnel rows:      "
        f"{historical_funnel_count}"
    )
    print(
        f"Current pending rows:        "
        f"{current_pending_count}"
    )
    print(
        f"Historical pending rows:     "
        f"{historical_pending_count}"
    )
    print(
        "Manual fields preserved:    YES"
    )
    print(
        "Scanner signal-log writes:  None"
    )
    print(
        "Stock Summary USD writes:   None"
    )
    print(
        f"Receipt:                    "
        f"{receipt_path}"
    )
    print(
        "FUNNEL AND PENDING READ-BACK "
        "VERIFICATION PASSED"
    )
    print(
        "PILOT FUNNEL WRITE "
        "COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()