# VERSION: 2026-06-23-AUTO-PROMOTION-RUNNER-1
#
# Automatic batch-promotion runner:
# - reads reviewed and approved rows from Pending_New_Tickers;
# - supports DRY_RUN and APPLY modes;
# - requires explicit confirmation before APPLY mode;
# - limits the number of promotions per run;
# - sorts candidates deterministically by pending-sheet row;
# - calls the tested funnel.promotion_apply module once per ticker;
# - revalidates each ticker immediately before insertion;
# - stops the batch if one promotion fails;
# - verifies all successful promotions after completion;
# - writes consolidated plan, result and receipt artefacts.

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
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
    PENDING_SHEET,
    _build_master_index,
    _evaluate_promotions,
    _read_pending_records,
    _text,
    _ticker,
)
from funnel.sheet_reader import (
    get_stock_summary_ticker_records,
)


SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

VALID_MODES = {
    "DRY_RUN",
    "APPLY",
}

DEFAULT_PROMOTION_LIMIT = 3
MAX_PROMOTION_LIMIT = 10

PLAN_HEADERS = [
    "Sequence",
    "Ticker",
    "Pending Sheet Row",
    "Classification",
    "Score",
    "Entry Quality",
    "Review Priority",
    "Opportunity Stage",
    "Signal ID",
    "Current Run",
    "Validation Status",
    "Add to Stock Summary USD?",
    "Planned Action",
]

REJECTION_HEADERS = [
    "Ticker",
    "Pending Sheet Row",
    "Classification",
    "Score",
    "Current Run",
    "Validation Status",
    "Add to Stock Summary USD?",
    "Rejection Reason",
]

RESULT_HEADERS = [
    "Sequence",
    "Ticker",
    "Pending Sheet Row",
    "Status",
    "Master Row Added",
    "Pending Row Updated",
    "Added Date",
    "Stock Summary Tickers After",
    "Receipt Path",
    "Error",
]


class AutoPromotionError(RuntimeError):
    """Raised when automatic promotion cannot safely continue."""


def _output_directory() -> Path:
    """Return and create the automatic-promotion artefact directory."""
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


def _mode() -> str:
    """Return the requested automatic-promotion mode."""
    mode = _text(
        os.getenv(
            "AUTO_PROMOTION_MODE",
            "DRY_RUN",
        )
    ).upper()

    if mode not in VALID_MODES:
        raise ValueError(
            "AUTO_PROMOTION_MODE must be DRY_RUN or APPLY."
        )

    return mode


def _promotion_limit() -> int:
    """Return the permitted number of promotions for this run."""
    raw_value = _text(
        os.getenv(
            "AUTO_PROMOTION_LIMIT",
            str(DEFAULT_PROMOTION_LIMIT),
        )
    )

    try:
        value = int(
            raw_value
        )
    except ValueError as exc:
        raise ValueError(
            "AUTO_PROMOTION_LIMIT must be an integer."
        ) from exc

    if value < 1:
        raise ValueError(
            "AUTO_PROMOTION_LIMIT must be at least 1."
        )

    if value > MAX_PROMOTION_LIMIT:
        raise ValueError(
            "AUTO_PROMOTION_LIMIT cannot exceed "
            f"{MAX_PROMOTION_LIMIT}."
        )

    return value


def _require_apply_confirmation(
    mode: str,
) -> None:
    """Require explicit confirmation before automatic master writes."""
    if mode != "APPLY":
        return

    confirmation = _text(
        os.getenv(
            "CONFIRM_AUTO_PROMOTION",
            "",
        )
    ).upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Automatic promotion APPLY mode was not confirmed. "
            "Set CONFIRM_AUTO_PROMOTION=YES."
        )


def _write_json(
    path: Path,
    payload: Any,
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


def _read_json(
    path: Path,
) -> dict[str, Any]:
    """Read and validate one JSON-object artefact."""
    if not path.exists():
        raise RuntimeError(
            f"Expected artefact was not created: {path}"
        )

    payload = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        payload,
        dict,
    ):
        raise RuntimeError(
            f"Expected a JSON object in {path}."
        )

    return payload


def _safe_csv_value(
    value: Any,
) -> str:
    """Reduce spreadsheet-formula injection risk in CSV artefacts."""
    text = _text(
        value
    )

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
    """Write a deterministic UTF-8 CSV artefact."""
    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file_handle:
        writer = csv.DictWriter(
            file_handle,
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


def _pending_row_number(
    record: dict[str, Any],
) -> int:
    """Return a sortable pending-sheet row number."""
    raw_value = _text(
        record.get(
            "Pending Sheet Row"
        )
    )

    try:
        return int(
            raw_value
        )
    except ValueError:
        return 10**9


def _safe_path_component(
    value: str,
) -> str:
    """Return a filesystem-safe ticker component."""
    cleaned = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    ).strip(
        "._"
    )

    return (
        cleaned
        or "unknown_ticker"
    )


def _current_snapshot() -> dict[str, Any]:
    """Read pending approvals and the current master ticker universe."""
    service = get_sheets_service(
        readonly=True
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

    (
        eligible_records,
        rejected_records,
        approval_request_count,
    ) = _evaluate_promotions(
        pending_records,
        master_index,
    )

    eligible_records.sort(
        key=lambda record: (
            _pending_row_number(
                record
            ),
            _ticker(
                record.get(
                    "Ticker"
                )
            ),
        )
    )

    rejected_records.sort(
        key=lambda record: (
            _pending_row_number(
                record
            ),
            _ticker(
                record.get(
                    "Ticker"
                )
            ),
        )
    )

    return {
        "pending_records": pending_records,
        "master_index": master_index,
        "eligible_records": eligible_records,
        "rejected_records": rejected_records,
        "approval_request_count": (
            approval_request_count
        ),
    }


def _plan_record(
    sequence: int,
    record: dict[str, Any],
    planned_action: str,
) -> dict[str, Any]:
    """Map an eligible record into the batch-plan schema."""
    return {
        "Sequence": sequence,
        "Ticker": _ticker(
            record.get(
                "Ticker"
            )
        ),
        "Pending Sheet Row": record.get(
            "Pending Sheet Row",
            "",
        ),
        "Classification": record.get(
            "Classification",
            "",
        ),
        "Score": record.get(
            "Score",
            "",
        ),
        "Entry Quality": record.get(
            "Entry Quality",
            "",
        ),
        "Review Priority": record.get(
            "Review Priority",
            "",
        ),
        "Opportunity Stage": record.get(
            "Opportunity Stage",
            "",
        ),
        "Signal ID": record.get(
            "Signal ID",
            "",
        ),
        "Current Run": record.get(
            "Current Run",
            "",
        ),
        "Validation Status": record.get(
            "Validation Status",
            "",
        ),
        "Add to Stock Summary USD?": record.get(
            "Add to Stock Summary USD?",
            "",
        ),
        "Planned Action": planned_action,
    }


def _rejection_record(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Map one rejected approval request into the export schema."""
    return {
        "Ticker": _ticker(
            record.get(
                "Ticker"
            )
        ),
        "Pending Sheet Row": record.get(
            "Pending Sheet Row",
            "",
        ),
        "Classification": record.get(
            "Classification",
            "",
        ),
        "Score": record.get(
            "Score",
            "",
        ),
        "Current Run": record.get(
            "Current Run",
            "",
        ),
        "Validation Status": record.get(
            "Validation Status",
            "",
        ),
        "Add to Stock Summary USD?": record.get(
            "Add to Stock Summary USD?",
            "",
        ),
        "Rejection Reason": record.get(
            "Rejection Reason",
            "",
        ),
    }

def _run_one_promotion(
    sequence: int,
    record: dict[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    """
    Run the tested single-ticker promotion module in a child process.

    The child receives its own artefact directory so each promotion keeps
    a separate backup and receipt.
    """
    ticker = _ticker(
        record.get(
            "Ticker"
        )
    )

    if not ticker:
        raise AutoPromotionError(
            "An eligible promotion record has no ticker."
        )

    pending_row = _text(
        record.get(
            "Pending Sheet Row"
        )
    )

    item_directory = (
        output_directory
        / "auto_promotion_items"
        / (
            f"{sequence:02d}_"
            f"{_safe_path_component(ticker)}"
        )
    )

    item_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    child_environment = os.environ.copy()

    child_environment[
        "PROMOTION_TICKER"
    ] = ticker

    child_environment[
        "CONFIRM_MASTER_PROMOTION"
    ] = "YES"

    child_environment[
        "FUNNEL_OUTPUT_DIR"
    ] = str(
        item_directory
    )

    child_environment[
        "PYTHONUNBUFFERED"
    ] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "funnel.promotion_apply",
        ],
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    child_output = (
        completed.stdout
        or ""
    )

    if child_output:
        print(
            child_output,
            end=(
                ""
                if child_output.endswith("\n")
                else "\n"
            ),
        )

    receipt_path = (
        item_directory
        / "promotion_apply_receipt.json"
    )

    receipt: dict[str, Any] = {}

    if receipt_path.exists():
        try:
            receipt = _read_json(
                receipt_path
            )
        except Exception as exc:
            receipt = {
                "status": "INVALID_RECEIPT",
                "error": repr(
                    exc
                ),
            }

    result = {
        "Sequence": sequence,
        "Ticker": ticker,
        "Pending Sheet Row": pending_row,
        "Status": receipt.get(
            "status",
            (
                "FAILED"
                if completed.returncode
                else "MISSING_RECEIPT"
            ),
        ),
        "Master Row Added": receipt.get(
            "master_row_added",
            "",
        ),
        "Pending Row Updated": receipt.get(
            "pending_row_updated",
            "",
        ),
        "Added Date": receipt.get(
            "added_date",
            "",
        ),
        "Stock Summary Tickers After": (
            receipt.get(
                "stock_summary_tickers_after",
                "",
            )
        ),
        "Receipt Path": str(
            receipt_path
        ),
        "Error": receipt.get(
            "error",
            "",
        ),
        "return_code": completed.returncode,
        "receipt": receipt,
        "child_output": child_output,
    }

    if completed.returncode != 0:
        error_text = (
            _text(
                result.get(
                    "Error"
                )
            )
            or (
                child_output[-1500:]
                if child_output
                else "No child-process output."
            )
        )

        result[
            "Error"
        ] = error_text

        raise AutoPromotionError(
            f"Automatic promotion failed for {ticker}: "
            f"{error_text}"
        )

    if receipt.get(
        "status"
    ) != "PASSED":
        raise AutoPromotionError(
            f"Automatic promotion for {ticker} did not produce "
            "a PASSED receipt."
        )

    if _ticker(
        receipt.get(
            "ticker"
        )
    ) != ticker:
        raise AutoPromotionError(
            f"Promotion receipt ticker mismatch for {ticker}."
        )

    return result


def _verify_successful_promotions(
    promoted_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Verify successful promotions against both Google Sheets."""
    if not promoted_results:
        return []

    service = get_sheets_service(
        readonly=True
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

    pending_by_ticker: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for record in pending_records:
        ticker = _ticker(
            record.get(
                "Ticker"
            )
        )

        if not ticker:
            continue

        pending_by_ticker.setdefault(
            ticker,
            [],
        ).append(
            record
        )

    verification: list[
        dict[str, Any]
    ] = []

    for result in promoted_results:
        ticker = _ticker(
            result.get(
                "Ticker"
            )
        )

        errors: list[
            str
        ] = []

        if ticker not in master_index:
            errors.append(
                "TICKER_NOT_FOUND_IN_STOCK_SUMMARY_USD"
            )

        matching_pending = (
            pending_by_ticker.get(
                ticker,
                [],
            )
        )

        if len(
            matching_pending
        ) != 1:
            errors.append(
                "PENDING_ROW_COUNT_NOT_ONE"
            )

            pending_record: dict[
                str,
                Any,
            ] = {}

        else:
            pending_record = (
                matching_pending[0]
            )

            if _text(
                pending_record.get(
                    "Add to Stock Summary USD?"
                )
            ).upper() != "ADDED":
                errors.append(
                    "PENDING_STATUS_NOT_ADDED"
                )

            if not _text(
                pending_record.get(
                    "Added Date"
                )
            ):
                errors.append(
                    "PENDING_ADDED_DATE_BLANK"
                )

        verification.append(
            {
                "ticker": ticker,
                "master_present": (
                    ticker in master_index
                ),
                "pending_matches": len(
                    matching_pending
                ),
                "pending_status": _text(
                    pending_record.get(
                        "Add to Stock Summary USD?"
                    )
                ),
                "added_date": _text(
                    pending_record.get(
                        "Added Date"
                    )
                ),
                "status": (
                    "PASSED"
                    if not errors
                    else "FAILED"
                ),
                "errors": errors,
            }
        )

    failed = [
        item
        for item in verification
        if item[
            "status"
        ]
        != "PASSED"
    ]

    if failed:
        raise AutoPromotionError(
            "Post-batch promotion verification failed: "
            + json.dumps(
                failed,
                ensure_ascii=False,
                default=str,
            )
        )

    return verification


def _result_export_record(
    result: dict[str, Any],
) -> dict[str, Any]:
    """Map an internal promotion result into the result CSV schema."""
    return {
        header: result.get(
            header,
            "",
        )
        for header in RESULT_HEADERS
    }


def _print_plan_preview(
    selected_records: list[dict[str, Any]],
    deferred_records: list[dict[str, Any]],
    rejected_records: list[dict[str, Any]],
) -> None:
    """Print a concise batch-plan preview."""
    if selected_records:
        print()
        print(
            "SELECTED FOR THIS RUN"
        )

        for sequence, record in enumerate(
            selected_records,
            start=1,
        ):
            print(
                "  "
                f"{sequence}. "
                f"${_ticker(record.get('Ticker'))} "
                f"| pending row "
                f"{record.get('Pending Sheet Row', '')}"
            )

    if deferred_records:
        print()
        print(
            "DEFERRED BY BATCH LIMIT"
        )

        for record in deferred_records:
            print(
                "  "
                f"${_ticker(record.get('Ticker'))} "
                f"| pending row "
                f"{record.get('Pending Sheet Row', '')}"
            )

    if rejected_records:
        print()
        print(
            "REJECTED APPROVAL REQUESTS"
        )

        for record in rejected_records:
            print(
                "  "
                f"${_ticker(record.get('Ticker')) or '?'} "
                f"| pending row "
                f"{record.get('Pending Sheet Row', '')} "
                f"| {record.get('Rejection Reason', '')}"
            )

def main() -> None:
    """Run one automatic promotion dry run or controlled batch apply."""
    mode = _mode()

    promotion_limit = (
        _promotion_limit()
    )

    _require_apply_confirmation(
        mode
    )

    timestamp = datetime.now(
        SINGAPORE_TZ
    )

    run_id = timestamp.strftime(
        "%Y%m%dT%H%M%S%z"
    )

    output_directory = (
        _output_directory()
    )

    snapshot = (
        _current_snapshot()
    )

    eligible_records = snapshot[
        "eligible_records"
    ]

    rejected_records = snapshot[
        "rejected_records"
    ]

    selected_records = (
        eligible_records[
            :promotion_limit
        ]
    )

    deferred_records = (
        eligible_records[
            promotion_limit:
        ]
    )

    plan_records: list[
        dict[str, Any]
    ] = []

    for sequence, record in enumerate(
        selected_records,
        start=1,
    ):
        plan_records.append(
            _plan_record(
                sequence,
                record,
                (
                    "PROMOTE"
                    if mode == "APPLY"
                    else "DRY_RUN_ELIGIBLE"
                ),
            )
        )

    for offset, record in enumerate(
        deferred_records,
        start=len(
            selected_records
        ) + 1,
    ):
        plan_records.append(
            _plan_record(
                offset,
                record,
                "DEFERRED_BY_BATCH_LIMIT",
            )
        )

    rejection_exports = [
        _rejection_record(
            record
        )
        for record in rejected_records
    ]

    plan_path = (
        output_directory
        / "auto_promotion_plan.csv"
    )

    rejection_path = (
        output_directory
        / "auto_promotion_rejections.csv"
    )

    result_path = (
        output_directory
        / "auto_promotion_results.csv"
    )

    receipt_path = (
        output_directory
        / "auto_promotion_receipt.json"
    )

    _write_csv(
        plan_path,
        PLAN_HEADERS,
        plan_records,
    )

    _write_csv(
        rejection_path,
        REJECTION_HEADERS,
        rejection_exports,
    )

    print()
    print(
        "HX FUNNEL — AUTOMATIC PROMOTION"
    )
    print(
        "=" * 39
    )
    print(
        "Mode:                        "
        f"{mode}"
    )
    print(
        "Batch limit:                 "
        f"{promotion_limit}"
    )
    print(
        "Pending rows read:           "
        f"{len(snapshot['pending_records'])}"
    )
    print(
        "Master tickers before:       "
        f"{len(snapshot['master_index'])}"
    )
    print(
        "Approval requests found:     "
        f"{snapshot['approval_request_count']}"
    )
    print(
        "Eligible promotions:         "
        f"{len(eligible_records)}"
    )
    print(
        "Selected this run:           "
        f"{len(selected_records)}"
    )
    print(
        "Deferred by limit:           "
        f"{len(deferred_records)}"
    )
    print(
        "Rejected requests:           "
        f"{len(rejected_records)}"
    )

    _print_plan_preview(
        selected_records,
        deferred_records,
        rejected_records,
    )

    if mode == "DRY_RUN":
        _write_csv(
            result_path,
            RESULT_HEADERS,
            [],
        )

        receipt = {
            "status": "PASSED",
            "mode": "AUTO_PROMOTION_DRY_RUN",
            "run_id": run_id,
            "timestamp": (
                timestamp.isoformat()
            ),
            "promotion_limit": (
                promotion_limit
            ),
            "pending_rows_read": len(
                snapshot[
                    "pending_records"
                ]
            ),
            "master_tickers_before": len(
                snapshot[
                    "master_index"
                ]
            ),
            "approval_requests_found": (
                snapshot[
                    "approval_request_count"
                ]
            ),
            "eligible_promotions": len(
                eligible_records
            ),
            "selected_promotions": len(
                selected_records
            ),
            "deferred_promotions": len(
                deferred_records
            ),
            "rejected_promotions": len(
                rejected_records
            ),
            "selected_tickers": [
                _ticker(
                    record.get(
                        "Ticker"
                    )
                )
                for record in selected_records
            ],
            "deferred_tickers": [
                _ticker(
                    record.get(
                        "Ticker"
                    )
                )
                for record in deferred_records
            ],
            "google_sheets_written": [],
            "stock_summary_usd_written": False,
            "pending_new_tickers_written": False,
            "plan_path": str(
                plan_path
            ),
            "rejection_path": str(
                rejection_path
            ),
            "result_path": str(
                result_path
            ),
        }

        _write_json(
            receipt_path,
            receipt,
        )

        print()
        print(
            "Stock Summary USD writes:    None"
        )
        print(
            "Pending ticker writes:       None"
        )
        print(
            "Plan:                        "
            f"{plan_path}"
        )
        print(
            "Receipt:                     "
            f"{receipt_path}"
        )
        print(
            "AUTO PROMOTION DRY RUN "
            "COMPLETED SUCCESSFULLY"
        )
        print()

        return

    successful_results: list[
        dict[str, Any]
    ] = []

    failed_result: dict[
        str,
        Any,
    ] | None = None

    for sequence, record in enumerate(
        selected_records,
        start=1,
    ):
        ticker = _ticker(
            record.get(
                "Ticker"
            )
        )

        print()
        print(
            f"AUTO PROMOTION {sequence}/"
            f"{len(selected_records)}: "
            f"{ticker}"
        )
        print(
            "-" * 45
        )

        try:
            result = _run_one_promotion(
                sequence,
                record,
                output_directory,
            )

            successful_results.append(
                result
            )

        except Exception as exc:
            failed_result = {
                "Sequence": sequence,
                "Ticker": ticker,
                "Pending Sheet Row": (
                    record.get(
                        "Pending Sheet Row",
                        "",
                    )
                ),
                "Status": "FAILED",
                "Master Row Added": "",
                "Pending Row Updated": "",
                "Added Date": "",
                "Stock Summary Tickers After": "",
                "Receipt Path": "",
                "Error": repr(
                    exc
                ),
            }

            break

    result_exports = [
        _result_export_record(
            result
        )
        for result in successful_results
    ]

    if failed_result:
        result_exports.append(
            _result_export_record(
                failed_result
            )
        )

    _write_csv(
        result_path,
        RESULT_HEADERS,
        result_exports,
    )

    if failed_result:
        failure_receipt = {
            "status": "FAILED",
            "mode": "AUTO_PROMOTION_APPLY",
            "run_id": run_id,
            "timestamp": (
                timestamp.isoformat()
            ),
            "promotion_limit": (
                promotion_limit
            ),
            "selected_promotions": len(
                selected_records
            ),
            "successful_promotions": len(
                successful_results
            ),
            "failed_promotion": (
                failed_result
            ),
            "remaining_selected_not_attempted": [
                _ticker(
                    record.get(
                        "Ticker"
                    )
                )
                for record in selected_records[
                    len(
                        successful_results
                    ) + 1:
                ]
            ],
            "successful_results": (
                successful_results
            ),
            "plan_path": str(
                plan_path
            ),
            "rejection_path": str(
                rejection_path
            ),
            "result_path": str(
                result_path
            ),
            "stock_summary_usd_may_have_changed": bool(
                successful_results
            ),
            "pending_new_tickers_may_have_changed": bool(
                successful_results
            ),
        }

        _write_json(
            receipt_path,
            failure_receipt,
        )

        raise AutoPromotionError(
            "Automatic promotion batch stopped after a failure. "
            f"{len(successful_results)} ticker(s) were promoted "
            "successfully before the failure. Review "
            f"{receipt_path}."
        )

    verification = (
        _verify_successful_promotions(
            successful_results
        )
    )

    final_master_records = (
        get_stock_summary_ticker_records()
    )

    final_master_index = (
        _build_master_index(
            final_master_records
        )
    )

    success_receipt = {
        "status": "PASSED",
        "mode": "AUTO_PROMOTION_APPLY",
        "run_id": run_id,
        "timestamp": (
            timestamp.isoformat()
        ),
        "promotion_limit": (
            promotion_limit
        ),
        "approval_requests_found": (
            snapshot[
                "approval_request_count"
            ]
        ),
        "eligible_promotions": len(
            eligible_records
        ),
        "selected_promotions": len(
            selected_records
        ),
        "successful_promotions": len(
            successful_results
        ),
        "deferred_promotions": len(
            deferred_records
        ),
        "rejected_promotions": len(
            rejected_records
        ),
        "promoted_tickers": [
            _ticker(
                result.get(
                    "Ticker"
                )
            )
            for result in successful_results
        ],
        "deferred_tickers": [
            _ticker(
                record.get(
                    "Ticker"
                )
            )
            for record in deferred_records
        ],
        "master_tickers_before": len(
            snapshot[
                "master_index"
            ]
        ),
        "master_tickers_after": len(
            final_master_index
        ),
        "verification": verification,
        "successful_results": (
            successful_results
        ),
        "plan_path": str(
            plan_path
        ),
        "rejection_path": str(
            rejection_path
        ),
        "result_path": str(
            result_path
        ),
        "stock_summary_usd_written": bool(
            successful_results
        ),
        "pending_new_tickers_written": bool(
            successful_results
        ),
    }

    _write_json(
        receipt_path,
        success_receipt,
    )

    print()
    print(
        "AUTOMATIC PROMOTION BATCH RESULT"
    )
    print(
        "=" * 39
    )
    print(
        "Promoted successfully:       "
        f"{len(successful_results)}"
    )
    print(
        "Deferred by limit:           "
        f"{len(deferred_records)}"
    )
    print(
        "Rejected requests:           "
        f"{len(rejected_records)}"
    )
    print(
        "Master tickers before:       "
        f"{len(snapshot['master_index'])}"
    )
    print(
        "Master tickers after:        "
        f"{len(final_master_index)}"
    )

    if successful_results:
        print()
        print(
            "PROMOTED TICKERS"
        )

        for result in successful_results:
            print(
                "  "
                f"${result['Ticker']} "
                f"→ master row "
                f"{result['Master Row Added']}"
            )

    print()
    print(
        "Plan:                        "
        f"{plan_path}"
    )
    print(
        "Results:                     "
        f"{result_path}"
    )
    print(
        "Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "AUTOMATIC PROMOTION BATCH "
        "COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()