# VERSION: 2026-06-22-PILOT-SIGNAL-LOG-WRITER-1
#
# Controlled write stage:
# - writes only to Scanner_Signal_Log_Pilot;
# - never writes to Stock Summary USD;
# - verifies the exact worksheet header before writing;
# - replaces only the data rows beneath the header;
# - reads the result back and validates all written signal IDs.

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from funnel.congress_adapter import run_congress_adapter
from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)
from funnel.signal_schema import Signal


logger = logging.getLogger(__name__)


SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

TARGET_SHEET = "Scanner_Signal_Log_Pilot"

PROTECTED_SHEETS = {
    "Stock Summary USD",
    "Pending_New_Tickers",
    "Funnel_Pilot",
}


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


CLASSIFICATION_RANK = {
    "actionable": 5,
    "wait": 4,
    "risk": 3,
    "near_miss": 2,
    "other": 1,
}


def _require_confirmation() -> None:
    """
    Require explicit confirmation before writing signal data.
    """
    confirmation = str(
        os.getenv(
            "CONFIRM_PILOT_SIGNAL_LOG_WRITE",
            "",
        )
    ).strip().upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Pilot signal-log writing was not confirmed. "
            "Set CONFIRM_PILOT_SIGNAL_LOG_WRITE=YES."
        )


def _float_environment(
    name: str,
    default: float,
) -> float:
    """
    Read a finite float from an environment variable.
    """
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
    """
    Enforce the single permitted write destination.
    """
    if sheet_name in PROTECTED_SHEETS:
        raise RuntimeError(
            f"Writing to protected worksheet "
            f"{sheet_name!r} is prohibited."
        )

    if sheet_name != TARGET_SHEET:
        raise RuntimeError(
            f"This stage may write only to "
            f"{TARGET_SHEET!r}. "
            f"Received {sheet_name!r}."
        )


def _normalise_row(
    row: list[Any],
) -> list[str]:
    """
    Convert a worksheet row to stripped strings and remove trailing blanks.
    """
    values = [
        str(value).strip()
        for value in row
    ]

    while values and values[-1] == "":
        values.pop()

    return values


def _get_sheet_titles(
    service: Any,
    spreadsheet_id: str,
) -> set[str]:
    """
    Retrieve all worksheet titles.
    """
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        )
        .execute()
    )

    return {
        str(
            sheet.get(
                "properties",
                {},
            ).get(
                "title",
                "",
            )
        ).strip()
        for sheet in response.get(
            "sheets",
            [],
        )
        if str(
            sheet.get(
                "properties",
                {},
            ).get(
                "title",
                "",
            )
        ).strip()
    }


def _read_values(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    cell_range: str,
) -> list[list[Any]]:
    """
    Read rows from an approved worksheet range.
    """
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!"
                f"{cell_range}"
            ),
            majorDimension="ROWS",
        )
        .execute()
    )

    return response.get(
        "values",
        [],
    )


def _verify_header(
    service: Any,
    spreadsheet_id: str,
) -> None:
    """
    Verify that the target worksheet uses the exact approved schema.
    """
    _assert_write_target(
        TARGET_SHEET
    )

    rows = _read_values(
        service,
        spreadsheet_id,
        TARGET_SHEET,
        "A1:O1",
    )

    if not rows:
        raise RuntimeError(
            f"{TARGET_SHEET} has no header. "
            "Run pilot-sheet-setup first."
        )

    actual_header = _normalise_row(
        rows[0]
    )

    if actual_header != SIGNAL_HEADERS:
        raise RuntimeError(
            f"{TARGET_SHEET} header mismatch. "
            f"Expected {SIGNAL_HEADERS}; "
            f"found {actual_header}. "
            "No rows were cleared or written."
        )


def _parse_datetime(
    value: str,
) -> datetime:
    """
    Parse a timezone-aware ISO datetime.
    """
    text = str(
        value or ""
    ).strip()

    if not text:
        raise ValueError(
            "Datetime value cannot be blank."
        )

    parsed = datetime.fromisoformat(
        text.replace(
            "Z",
            "+00:00",
        )
    )

    if parsed.tzinfo is None:
        raise ValueError(
            f"Datetime must contain timezone information: {text}"
        )

    return parsed


def _is_active(
    signal: Signal,
    now: datetime,
) -> str:
    """
    Return YES when the signal has not expired.
    """
    if not signal.valid_until:
        return "YES"

    valid_until = _parse_datetime(
        signal.valid_until
    )

    return (
        "YES"
        if valid_until >= now
        else "NO"
    )


def _names_text(
    signal: Signal,
) -> str:
    """
    Convert names into a stable worksheet string.
    """
    names = signal.details.get(
        "names"
    )

    if isinstance(
        names,
        (list, tuple, set),
    ):
        return ", ".join(
            str(name).strip()
            for name in names
            if str(name).strip()
        )

    return str(
        names or ""
    ).strip()


def _number_or_blank(
    value: Any,
) -> float | int | str:
    """
    Return a finite number or a blank worksheet value.
    """
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


def _signal_to_row(
    signal: Signal,
    now: datetime,
) -> list[Any]:
    """
    Convert one Signal into the approved worksheet column order.
    """
    details = signal.details

    return [
        signal.signal_id,
        signal.ticker,
        signal.scanner,
        signal.classification,
        _number_or_blank(
            signal.score
        ),
        signal.observed_at,
        signal.valid_until or "",
        _is_active(
            signal,
            now,
        ),
        str(
            details.get("flow")
            or ""
        ).strip(),
        _names_text(
            signal
        ),
        _number_or_blank(
            details.get(
                "entry_quality"
            )
        ),
        _number_or_blank(
            details.get(
                "estimated_capital_mid"
            )
        ),
        _number_or_blank(
            details.get(
                "buyers"
            )
        ),
        _number_or_blank(
            details.get(
                "cluster_buyers"
            )
        ),
        json.dumps(
            details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            default=str,
        ),
    ]


def _sort_signals(
    signals: list[Signal],
) -> list[Signal]:
    """
    Sort signals by classification priority, score and ticker.
    """
    return sorted(
        signals,
        key=lambda signal: (
            CLASSIFICATION_RANK.get(
                signal.classification,
                0,
            ),
            signal.score
            if signal.score is not None
            else -1.0,
            signal.ticker,
        ),
        reverse=True,
    )


def _validate_source_signals(
    signals: list[Signal],
) -> None:
    """
    Reject duplicate or internally inconsistent source signals.
    """
    signal_ids: set[str] = set()

    for position, signal in enumerate(
        signals,
        start=1,
    ):
        context = (
            f"signal {position}"
        )

        if not signal.signal_id:
            raise RuntimeError(
                f"{context} has no signal ID."
            )

        expected_prefix = (
            f"{signal.scanner}-"
            f"{signal.ticker}-"
        )

        if not signal.signal_id.startswith(
            expected_prefix
        ):
            raise RuntimeError(
                f"{context} signal ID "
                f"{signal.signal_id!r} does not "
                f"match {expected_prefix!r}."
            )

        if signal.signal_id in signal_ids:
            raise RuntimeError(
                "Duplicate signal ID detected: "
                f"{signal.signal_id}"
            )

        signal_ids.add(
            signal.signal_id
        )


def _clear_existing_data(
    service: Any,
    spreadsheet_id: str,
) -> None:
    """
    Clear only data rows beneath the verified header.
    """
    _assert_write_target(
        TARGET_SHEET
    )

    (
        service.spreadsheets()
        .values()
        .clear(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{TARGET_SHEET}'!"
                "A2:O"
            ),
            body={},
        )
        .execute()
    )


def _write_rows(
    service: Any,
    spreadsheet_id: str,
    rows: list[list[Any]],
) -> None:
    """
    Write all current signal rows in one request.
    """
    if not rows:
        return

    _assert_write_target(
        TARGET_SHEET
    )

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{TARGET_SHEET}'!A2"
            ),
            valueInputOption="RAW",
            body={
                "values": rows
            },
        )
        .execute()
    )


def _verify_written_rows(
    service: Any,
    spreadsheet_id: str,
    expected_rows: list[list[Any]],
) -> None:
    """
    Read the target worksheet back and verify row count and signal IDs.
    """
    actual_rows = _read_values(
        service,
        spreadsheet_id,
        TARGET_SHEET,
        "A2:O",
    )

    if len(actual_rows) != len(
        expected_rows
    ):
        raise RuntimeError(
            f"Post-write verification failed: "
            f"expected {len(expected_rows)} rows, "
            f"found {len(actual_rows)}."
        )

    expected_ids = [
        str(row[0]).strip()
        for row in expected_rows
    ]

    actual_ids = [
        str(
            row[0]
            if row
            else ""
        ).strip()
        for row in actual_rows
    ]

    if actual_ids != expected_ids:
        raise RuntimeError(
            "Post-write verification failed: "
            "signal ID order or contents differ."
        )

    for row_number, row in enumerate(
        actual_rows,
        start=2,
    ):
        if len(row) < len(
            SIGNAL_HEADERS
        ):
            padded = list(row) + [
                ""
            ] * (
                len(SIGNAL_HEADERS)
                - len(row)
            )
        else:
            padded = row[
                :len(SIGNAL_HEADERS)
            ]

        signal_id = str(
            padded[0]
        ).strip()

        ticker = str(
            padded[1]
        ).strip().upper()

        scanner = str(
            padded[2]
        ).strip().lower()

        if not signal_id.startswith(
            f"{scanner}-{ticker}-"
        ):
            raise RuntimeError(
                "Post-write verification failed "
                f"at worksheet row {row_number}: "
                f"signal ID {signal_id!r} does not "
                "match scanner and ticker."
            )


def _write_receipt(
    payload: dict[str, Any],
) -> Path:
    """
    Write an auditable workflow receipt.
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
        / "pilot_signal_log_write_receipt.json"
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
    """
    Replace the pilot signal-log snapshot with current Congress signals.
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

    minimum_conviction = (
        _float_environment(
            "MIN_CONVICTION",
            15.0,
        )
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

    if TARGET_SHEET not in sheet_titles:
        raise RuntimeError(
            f"Required worksheet "
            f"{TARGET_SHEET!r} was not found. "
            "Run pilot-sheet-setup first."
        )

    for protected_sheet in PROTECTED_SHEETS:
        if protected_sheet not in sheet_titles:
            raise RuntimeError(
                f"Expected protected worksheet "
                f"{protected_sheet!r} was not found."
            )

    _verify_header(
        service,
        spreadsheet_id,
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

    sorted_signals = _sort_signals(
        signals
    )

    _validate_source_signals(
        sorted_signals
    )

    now = datetime.now(
        SINGAPORE_TZ
    )

    rows = [
        _signal_to_row(
            signal,
            now,
        )
        for signal in sorted_signals
    ]

    # The target header has already been verified.
    # Only rows 2 onward are cleared.
    _clear_existing_data(
        service,
        spreadsheet_id,
    )

    _write_rows(
        service,
        spreadsheet_id,
        rows,
    )

    _verify_written_rows(
        service,
        spreadsheet_id,
        rows,
    )

    active_count = sum(
        1
        for row in rows
        if row[7] == "YES"
    )

    inactive_count = (
        len(rows)
        - active_count
    )

    receipt = {
        "status": "PASSED",
        "target_sheet": TARGET_SHEET,
        "rows_written": len(rows),
        "active_signals": active_count,
        "inactive_signals": inactive_count,
        "congress_tickers_analysed": (
            analysed_count
        ),
        "minimum_conviction": (
            minimum_conviction
        ),
        "write_timestamp": (
            now.isoformat()
        ),
        "protected_sheets_written": [],
        "stock_summary_usd_written": False,
    }

    receipt_path = _write_receipt(
        receipt
    )

    print()
    print(
        "FUNNEL PILOT — SIGNAL LOG WRITE"
    )
    print(
        "=" * 39
    )
    print(
        f"Congress tickers analysed:   "
        f"{analysed_count}"
    )
    print(
        f"Signal rows written:         "
        f"{len(rows)}"
    )
    print(
        f"Active signals:              "
        f"{active_count}"
    )
    print(
        f"Inactive signals:            "
        f"{inactive_count}"
    )
    print(
        f"Target worksheet:            "
        f"{TARGET_SHEET}"
    )
    print(
        "Pending_New_Tickers writes:  None"
    )
    print(
        "Funnel_Pilot writes:         None"
    )
    print(
        "Stock Summary USD writes:    None"
    )
    print(
        f"Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "SIGNAL LOG READ-BACK VERIFICATION PASSED"
    )
    print(
        "PILOT SIGNAL LOG WRITE COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()