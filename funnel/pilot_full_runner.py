# VERSION: 2026-06-23-PILOT-FULL-RUNNER-1
#
# Consolidated pilot run:
# - runs the Congress adapter once;
# - refreshes Scanner_Signal_Log_Pilot;
# - refreshes Funnel_Pilot and Pending_New_Tickers;
# - preserves manual-review fields through the existing tested merge logic;
# - validates cross-sheet consistency;
# - writes a pre-write backup and attempts rollback if a write-stage error occurs;
# - never writes to Stock Summary USD.

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from funnel import pilot_funnel_writer as funnel_writer
from funnel import pilot_signal_log_writer as signal_writer
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

SIGNAL_SHEET = signal_writer.TARGET_SHEET
FUNNEL_SHEET = funnel_writer.FUNNEL_SHEET
PENDING_SHEET = funnel_writer.PENDING_SHEET
PRODUCTION_SHEET = "Stock Summary USD"

ALLOWED_WRITE_SHEETS = {
    SIGNAL_SHEET,
    FUNNEL_SHEET,
    PENDING_SHEET,
}

REQUIRED_SHEETS = (
    ALLOWED_WRITE_SHEETS
    | {
        PRODUCTION_SHEET,
    }
)


class FullRunWriteError(RuntimeError):
    """Raised when a consolidated write or rollback fails."""


def _require_confirmation() -> None:
    """
    Require explicit confirmation before any consolidated pilot write.
    """
    confirmation = str(
        os.getenv(
            "CONFIRM_PILOT_FULL_RUN",
            "",
        )
    ).strip().upper()

    if confirmation != "YES":
        raise RuntimeError(
            "The consolidated pilot run was not confirmed. "
            "Set CONFIRM_PILOT_FULL_RUN=YES."
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
        value = float(
            raw_value
        )
    except ValueError as exc:
        raise ValueError(
            f"{name} must be numeric. "
            f"Received: {raw_value!r}."
        ) from exc

    if not math.isfinite(
        value
    ):
        raise ValueError(
            f"{name} must be finite."
        )

    return value


def _output_directory() -> Path:
    """
    Return and create the workflow artefact directory.
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

    return output_directory


def _assert_write_target(
    sheet_name: str,
) -> None:
    """
    Permit writes only to the three pilot worksheets.
    """
    if sheet_name == PRODUCTION_SHEET:
        raise RuntimeError(
            "Writing to Stock Summary USD is prohibited."
        )

    if sheet_name not in ALLOWED_WRITE_SHEETS:
        raise RuntimeError(
            f"Writing to worksheet "
            f"{sheet_name!r} is prohibited."
        )


def _column_letter(
    column_number: int,
) -> str:
    """
    Convert a one-based column number to a Google Sheets label.
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


def _read_signal_rows(
    service: Any,
    spreadsheet_id: str,
) -> list[list[Any]]:
    """
    Read existing signal-log data rows, excluding the header.
    """
    rows = signal_writer._read_values(
        service,
        spreadsheet_id,
        SIGNAL_SHEET,
        "A2:O",
    )

    return [
        list(row)
        for row in rows
        if any(
            str(value).strip()
            for value in row
        )
    ]


def _records_to_backup_rows(
    records: list[dict[str, str]],
    headers: list[str],
) -> list[list[Any]]:
    """
    Convert existing worksheet records into exact column-order rows.
    """
    return [
        [
            record.get(
                header,
                "",
            )
            for header in headers
        ]
        for record in records
    ]


def _write_json(
    path: Path,
    payload: Any,
) -> None:
    """
    Write formatted UTF-8 JSON.
    """
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


def _write_prewrite_backup(
    output_directory: Path,
    run_id: str,
    signal_rows: list[list[Any]],
    funnel_rows: list[list[Any]],
    pending_rows: list[list[Any]],
) -> Path:
    """
    Persist the three pilot tables before any data-row write.
    """
    backup_path = (
        output_directory
        / "pilot_full_run_prewrite_backup.json"
    )

    _write_json(
        backup_path,
        {
            "run_id": run_id,
            "status": "PREWRITE_BACKUP",
            "production_sheet_included": False,
            "worksheets": {
                SIGNAL_SHEET: {
                    "headers": (
                        signal_writer.SIGNAL_HEADERS
                    ),
                    "data_rows": signal_rows,
                },
                FUNNEL_SHEET: {
                    "headers": (
                        funnel_writer.FUNNEL_HEADERS
                    ),
                    "data_rows": funnel_rows,
                },
                PENDING_SHEET: {
                    "headers": (
                        funnel_writer.PENDING_HEADERS
                    ),
                    "data_rows": pending_rows,
                },
            },
        },
    )

    return backup_path


def _replace_sheet_data(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    rows: list[list[Any]],
) -> None:
    """
    Replace only rows 2 onward of one approved pilot worksheet.
    """
    _assert_write_target(
        sheet_name
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
                f"A2:{final_column}"
            ),
            body={},
        )
        .execute()
    )

    if not rows:
        return

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=(
                f"'{sheet_name}'!A2"
            ),
            valueInputOption="RAW",
            body={
                "values": rows
            },
        )
        .execute()
    )


def _attempt_rollback(
    service: Any,
    spreadsheet_id: str,
    signal_rows: list[list[Any]],
    funnel_rows: list[list[Any]],
    pending_rows: list[list[Any]],
) -> dict[str, Any]:
    """
    Attempt to restore all three pilot tables from the pre-write backup.
    """
    result: dict[str, Any] = {
        "attempted": True,
        "status": "PASSED",
        "restored_sheets": [],
        "errors": [],
    }

    rollback_plan = [
        (
            SIGNAL_SHEET,
            signal_writer.SIGNAL_HEADERS,
            signal_rows,
        ),
        (
            FUNNEL_SHEET,
            funnel_writer.FUNNEL_HEADERS,
            funnel_rows,
        ),
        (
            PENDING_SHEET,
            funnel_writer.PENDING_HEADERS,
            pending_rows,
        ),
    ]

    for (
        sheet_name,
        headers,
        rows,
    ) in rollback_plan:
        try:
            _replace_sheet_data(
                service,
                spreadsheet_id,
                sheet_name,
                headers,
                rows,
            )

            result[
                "restored_sheets"
            ].append(
                sheet_name
            )

        except Exception as exc:
            result[
                "status"
            ] = "FAILED"

            result[
                "errors"
            ].append(
                {
                    "sheet": sheet_name,
                    "error": repr(exc),
                }
            )

    return result


def _validate_cross_sheet_consistency(
    signals: list[Any],
    funnel_records: list[dict[str, Any]],
    pending_records: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Validate signal IDs and current-run relationships before writing.
    """
    signal_ids = [
        signal.signal_id
        for signal in signals
    ]

    if len(signal_ids) != len(
        set(signal_ids)
    ):
        raise RuntimeError(
            "Duplicate signal IDs were produced "
            "by the Congress adapter."
        )

    signal_id_set = set(
        signal_ids
    )

    current_funnel = [
        record
        for record in funnel_records
        if str(
            record.get(
                "Current Run",
                "",
            )
        ).strip().upper()
        == "YES"
    ]

    current_pending = [
        record
        for record in pending_records
        if str(
            record.get(
                "Current Run",
                "",
            )
        ).strip().upper()
        == "YES"
    ]

    current_funnel_ids = [
        str(
            record.get(
                "Signal ID",
                "",
            )
        ).strip()
        for record in current_funnel
    ]

    current_pending_ids = [
        str(
            record.get(
                "Signal ID",
                "",
            )
        ).strip()
        for record in current_pending
    ]

    if any(
        not signal_id
        for signal_id
        in current_funnel_ids
    ):
        raise RuntimeError(
            "A current Funnel_Pilot record "
            "has a blank Signal ID."
        )

    if any(
        not signal_id
        for signal_id
        in current_pending_ids
    ):
        raise RuntimeError(
            "A current Pending_New_Tickers record "
            "has a blank Signal ID."
        )

    missing_funnel_ids = sorted(
        set(
            current_funnel_ids
        ).difference(
            signal_id_set
        )
    )

    if missing_funnel_ids:
        raise RuntimeError(
            "Current Funnel_Pilot records refer "
            "to signal IDs absent from the "
            "signal-log snapshot: "
            + ", ".join(
                missing_funnel_ids
            )
        )

    current_funnel_id_set = set(
        current_funnel_ids
    )

    missing_pending_ids = sorted(
        set(
            current_pending_ids
        ).difference(
            current_funnel_id_set
        )
    )

    if missing_pending_ids:
        raise RuntimeError(
            "Current Pending_New_Tickers records "
            "refer to signal IDs absent from "
            "Funnel_Pilot: "
            + ", ".join(
                missing_pending_ids
            )
        )

    current_funnel_tickers = [
        str(
            record.get(
                "Ticker",
                "",
            )
        ).strip().upper()
        for record in current_funnel
    ]

    current_pending_tickers = [
        str(
            record.get(
                "Ticker",
                "",
            )
        ).strip().upper()
        for record in current_pending
    ]

    if len(
        current_funnel_tickers
    ) != len(
        set(
            current_funnel_tickers
        )
    ):
        raise RuntimeError(
            "Current Funnel_Pilot contains "
            "duplicate tickers."
        )

    if len(
        current_pending_tickers
    ) != len(
        set(
            current_pending_tickers
        )
    ):
        raise RuntimeError(
            "Current Pending_New_Tickers contains "
            "duplicate tickers."
        )

    return {
        "signal_log_current_rows": len(
            signals
        ),
        "funnel_current_rows": len(
            current_funnel
        ),
        "pending_current_rows": len(
            current_pending
        ),
    }


def _write_receipt(
    output_directory: Path,
    payload: dict[str, Any],
) -> Path:
    """
    Write the consolidated run receipt.
    """
    receipt_path = (
        output_directory
        / "pilot_full_run_receipt.json"
    )

    _write_json(
        receipt_path,
        payload,
    )

    return receipt_path


def main() -> None:
    """
    Run one consistent Congress snapshot across all three pilot tabs.
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

    run_timestamp = datetime.now(
        SINGAPORE_TZ
    )

    run_id = run_timestamp.strftime(
        "%Y%m%dT%H%M%S%z"
    )

    output_directory = (
        _output_directory()
    )

    service = get_sheets_service(
        readonly=False
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    sheet_titles = (
        funnel_writer._get_sheet_titles(
            service,
            spreadsheet_id,
        )
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

    # Preflight all three pilot targets before changing data.
    signal_writer._verify_header(
        service,
        spreadsheet_id,
    )

    funnel_writer._verify_header(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        funnel_writer.FUNNEL_HEADERS,
    )

    funnel_writer._verify_header(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        funnel_writer.PENDING_HEADERS,
    )

    existing_signal_rows = (
        _read_signal_rows(
            service,
            spreadsheet_id,
        )
    )

    (
        existing_funnel_records,
        old_funnel_count,
    ) = funnel_writer._read_table(
        service,
        spreadsheet_id,
        FUNNEL_SHEET,
        funnel_writer.FUNNEL_HEADERS,
    )

    (
        existing_pending_records,
        old_pending_count,
    ) = funnel_writer._read_table(
        service,
        spreadsheet_id,
        PENDING_SHEET,
        funnel_writer.PENDING_HEADERS,
    )

    existing_funnel_rows = (
        _records_to_backup_rows(
            existing_funnel_records,
            funnel_writer.FUNNEL_HEADERS,
        )
    )

    existing_pending_rows = (
        _records_to_backup_rows(
            existing_pending_records,
            funnel_writer.PENDING_HEADERS,
        )
    )

    backup_path = (
        _write_prewrite_backup(
            output_directory,
            run_id,
            existing_signal_rows,
            existing_funnel_rows,
            existing_pending_rows,
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
        "Running one Congress snapshot "
        "with minimum conviction %.1f.",
        minimum_conviction,
    )

    signals, analysed_count = (
        run_congress_adapter(
            min_conviction=minimum_conviction
        )
    )

    sorted_signals = (
        signal_writer._sort_signals(
            signals
        )
    )

    signal_writer._validate_source_signals(
        sorted_signals
    )

    signal_rows = [
        signal_writer._signal_to_row(
            signal,
            run_timestamp,
        )
        for signal in sorted_signals
    ]

    comparison = classify_signals(
        signals=sorted_signals,
        ticker_records=ticker_records,
    )

    pending = (
        get_pending_new_ticker_records(
            comparison
        )
    )

    funnel_records = (
        funnel_writer._merge_funnel_records(
            comparison,
            existing_funnel_records,
        )
    )

    pending_records = (
        funnel_writer._merge_pending_records(
            pending,
            existing_pending_records,
        )
    )

    funnel_rows = (
        funnel_writer._records_to_rows(
            funnel_records,
            funnel_writer.FUNNEL_HEADERS,
        )
    )

    pending_rows = (
        funnel_writer._records_to_rows(
            pending_records,
            funnel_writer.PENDING_HEADERS,
        )
    )

    consistency_counts = (
        _validate_cross_sheet_consistency(
            sorted_signals,
            funnel_records,
            pending_records,
        )
    )

    write_started = False

    rollback_result: dict[str, Any] = {
        "attempted": False,
        "status": "NOT_REQUIRED",
        "restored_sheets": [],
        "errors": [],
    }

    try:
        write_started = True

        # Replace the signal-log snapshot below its header.
        signal_writer._clear_existing_data(
            service,
            spreadsheet_id,
        )

        signal_writer._write_rows(
            service,
            spreadsheet_id,
            signal_rows,
        )

        # Write the funnel and pending tables using the tested merge logic.
        funnel_writer._write_new_rows(
            service,
            spreadsheet_id,
            funnel_rows,
            pending_rows,
        )

        funnel_writer._clear_obsolete_rows(
            service,
            spreadsheet_id,
            FUNNEL_SHEET,
            funnel_writer.FUNNEL_HEADERS,
            old_funnel_count,
            len(funnel_rows),
        )

        funnel_writer._clear_obsolete_rows(
            service,
            spreadsheet_id,
            PENDING_SHEET,
            funnel_writer.PENDING_HEADERS,
            old_pending_count,
            len(pending_rows),
        )

        # Read all three worksheets back.
        signal_writer._verify_written_rows(
            service,
            spreadsheet_id,
            signal_rows,
        )

        funnel_writer._verify_written_table(
            service,
            spreadsheet_id,
            FUNNEL_SHEET,
            funnel_writer.FUNNEL_HEADERS,
            funnel_records,
        )

        funnel_writer._verify_written_table(
            service,
            spreadsheet_id,
            PENDING_SHEET,
            funnel_writer.PENDING_HEADERS,
            pending_records,
        )

    except Exception as exc:
        if write_started:
            rollback_result = (
                _attempt_rollback(
                    service,
                    spreadsheet_id,
                    existing_signal_rows,
                    existing_funnel_rows,
                    existing_pending_rows,
                )
            )

        failure_receipt = {
            "status": "FAILED",
            "run_id": run_id,
            "write_timestamp": (
                run_timestamp.isoformat()
            ),
            "error": repr(exc),
            "rollback": rollback_result,
            "prewrite_backup": str(
                backup_path
            ),
            "stock_summary_usd_written": False,
        }

   