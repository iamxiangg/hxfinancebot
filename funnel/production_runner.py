# VERSION: 2026-06-23-PRODUCTION-RUNNER-WITH-AUTO-PROMOTION-2
#
# Production sequence:
# 1. Run the tested consolidated Congress funnel refresh.
# 2. Verify and compare the updated funnel sheets.
# 3. Run automatic promotion after the sheet refresh.
# 4. Verify promoted rows through auto_promotion_runner.
# 5. Send one consolidated Telegram message.
#
# Stock Summary USD is written only by the tested promotion modules.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from funnel import pilot_full_runner as full_runner
from funnel import pilot_funnel_writer as funnel_writer
from funnel.google_client import (
    get_sheets_service,
    get_spreadsheet_id,
)
from funnel.telegram_notifier import (
    TelegramNotificationError,
    analyse_funnel_changes,
    build_funnel_message,
    send_telegram_text,
)


TELEGRAM_MODES = {
    "CHANGES_ONLY",
    "TEST",
    "OFF",
}

AUTO_PROMOTION_MODES = {
    "APPLY",
    "DRY_RUN",
    "OFF",
}


def _text(value: Any) -> str:
    """Return a stripped string."""
    return (
        ""
        if value is None
        else str(value).strip()
    )


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """Return an integer or a safe default."""
    try:
        return int(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return default


def _require_confirmation() -> None:
    """Require explicit confirmation for a production refresh."""
    confirmation = _text(
        os.getenv(
            "CONFIRM_PRODUCTION_RUN"
        )
    ).upper()

    if confirmation != "YES":
        raise RuntimeError(
            "Production refresh was not confirmed. "
            "Set CONFIRM_PRODUCTION_RUN=YES."
        )


def _telegram_mode() -> str:
    """Return the selected Telegram behaviour."""
    mode = _text(
        os.getenv(
            "TELEGRAM_MODE",
            "CHANGES_ONLY",
        )
    ).upper()

    if mode not in TELEGRAM_MODES:
        raise ValueError(
            "TELEGRAM_MODE must be "
            "CHANGES_ONLY, TEST or OFF."
        )

    return mode


def _auto_promotion_mode() -> str:
    """Return the selected automatic-promotion behaviour."""
    mode = _text(
        os.getenv(
            "AUTO_PROMOTION_MODE",
            "OFF",
        )
    ).upper()

    if mode not in AUTO_PROMOTION_MODES:
        raise ValueError(
            "AUTO_PROMOTION_MODE must be "
            "APPLY, DRY_RUN or OFF."
        )

    return mode


def _auto_promotion_limit() -> int:
    """Return the maximum number of promotions for this run."""
    raw_value = _text(
        os.getenv(
            "AUTO_PROMOTION_LIMIT",
            "3",
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

    if value < 1 or value > 10:
        raise ValueError(
            "AUTO_PROMOTION_LIMIT must be between 1 and 10."
        )

    return value


def _output_directory() -> Path:
    """Return and create the output directory."""
    directory = Path(
        os.getenv(
            "FUNNEL_OUTPUT_DIR",
            "funnel_output",
        )
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def _read_json(
    path: Path,
) -> dict[str, Any]:
    """Read and validate one JSON object."""
    if not path.exists():
        raise RuntimeError(
            "Required artefact was not created: "
            f"{path}"
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
            f"Invalid JSON object in {path}."
        )

    return payload


def _write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:
    """Write formatted JSON."""
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


def _backup_records(
    backup: dict[str, Any],
    sheet_name: str,
    expected_headers: list[str],
) -> list[dict[str, str]]:
    """Convert backed-up worksheet rows into dictionaries."""
    worksheets = backup.get(
        "worksheets"
    )

    sheet = (
        worksheets.get(
            sheet_name
        )
        if isinstance(
            worksheets,
            dict,
        )
        else None
    )

    if not isinstance(
        sheet,
        dict,
    ):
        raise RuntimeError(
            "Pre-write backup has no "
            f"{sheet_name!r} worksheet."
        )

    if (
        sheet.get(
            "headers"
        )
        != expected_headers
    ):
        raise RuntimeError(
            "Pre-write backup header mismatch "
            f"for {sheet_name}."
        )

    records: list[
        dict[str, str]
    ] = []

    for raw_row in sheet.get(
        "data_rows",
        [],
    ):
        if not isinstance(
            raw_row,
            list,
        ):
            continue

        padded = (
            raw_row
            + [""] * len(
                expected_headers
            )
        )[
            :len(
                expected_headers
            )
        ]

        if not any(
            _text(
                value
            )
            for value in padded
        ):
            continue

        records.append(
            {
                header: _text(
                    padded[index]
                )
                for index, header
                in enumerate(
                    expected_headers
                )
            }
        )

    return records


def _read_current_tables() -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
]:
    """Read the current funnel and pending tables."""
    service = get_sheets_service(
        readonly=True
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    funnel_records, _ = (
        funnel_writer._read_table(
            service,
            spreadsheet_id,
            funnel_writer.FUNNEL_SHEET,
            funnel_writer.FUNNEL_HEADERS,
        )
    )

    pending_records, _ = (
        funnel_writer._read_table(
            service,
            spreadsheet_id,
            funnel_writer.PENDING_SHEET,
            funnel_writer.PENDING_HEADERS,
        )
    )

    return (
        funnel_records,
        pending_records,
    )


def _count_outstanding_current_pending(
    records: list[dict[str, Any]],
) -> int:
    """
    Count current pending rows that have not already been promoted.

    Rows marked ADDED remain as history but are not outstanding review work.
    """
    count = 0

    for record in records:
        if (
            _text(
                record.get(
                    "Current Run"
                )
            ).upper()
            != "YES"
        ):
            continue

        if (
            _text(
                record.get(
                    "Add to Stock Summary USD?"
                )
            ).upper()
            == "ADDED"
        ):
            continue

        count += 1

    return count


def _run_auto_promotion(
    output_directory: Path,
    mode: str,
    promotion_limit: int,
) -> tuple[
    dict[str, Any],
    int,
    str,
]:
    """Run automatic promotion after the funnel refresh."""
    if mode == "OFF":
        receipt = {
            "status": "SKIPPED_DISABLED",
            "mode": "AUTO_PROMOTION_OFF",
            "promotion_limit": promotion_limit,
            "eligible_promotions": 0,
            "selected_promotions": 0,
            "successful_promotions": 0,
            "deferred_promotions": 0,
            "rejected_promotions": 0,
            "promoted_tickers": [],
            "stock_summary_usd_written": False,
            "pending_new_tickers_written": False,
        }

        return (
            receipt,
            0,
            "",
        )

    child_environment = (
        os.environ.copy()
    )

    child_environment[
        "AUTO_PROMOTION_MODE"
    ] = mode

    child_environment[
        "AUTO_PROMOTION_LIMIT"
    ] = str(
        promotion_limit
    )

    child_environment[
        "CONFIRM_AUTO_PROMOTION"
    ] = (
        "YES"
        if mode == "APPLY"
        else "NO"
    )

    child_environment[
        "FUNNEL_OUTPUT_DIR"
    ] = str(
        output_directory
    )

    child_environment[
        "PYTHONUNBUFFERED"
    ] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "funnel.auto_promotion_runner",
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
        output_directory
        / "auto_promotion_receipt.json"
    )

    if receipt_path.exists():
        try:
            receipt = _read_json(
                receipt_path
            )
        except Exception as exc:
            receipt = {
                "status": "FAILED",
                "mode": (
                    f"AUTO_PROMOTION_{mode}"
                ),
                "error": (
                    "Automatic-promotion receipt "
                    f"could not be read: {exc!r}"
                ),
            }
    else:
        receipt = {
            "status": "FAILED",
            "mode": (
                f"AUTO_PROMOTION_{mode}"
            ),
            "error": (
                "Automatic-promotion process did not "
                "create its receipt."
            ),
        }

    return (
        receipt,
        completed.returncode,
        child_output,
    )


def _promotion_is_material(
    receipt: dict[str, Any],
) -> bool:
    """Return whether promotion activity warrants a Telegram update."""
    if (
        _text(
            receipt.get(
                "status"
            )
        ).upper()
        == "FAILED"
    ):
        return True

    return any(
        (
            _safe_int(
                receipt.get(
                    "successful_promotions"
                )
            )
            > 0,
            _safe_int(
                receipt.get(
                    "selected_promotions"
                )
            )
            > 0,
            _safe_int(
                receipt.get(
                    "rejected_promotions"
                )
            )
            > 0,
        )
    )


def _promotion_summary_lines(
    receipt: dict[str, Any],
) -> list[str]:
    """Build the automatic-promotion portion of the Telegram message."""
    mode = _text(
        receipt.get(
            "mode"
        )
    ).upper()

    status = _text(
        receipt.get(
            "status"
        )
    ).upper()

    lines = [
        "",
        "⚙️ AUTO PROMOTION",
    ]

    if mode == "AUTO_PROMOTION_OFF":
        lines.append(
            "Mode: OFF"
        )

        return lines

    if mode == "AUTO_PROMOTION_DRY_RUN":
        lines.extend(
            [
                "Mode: DRY RUN",
                (
                    "Eligible: "
                    f"{_safe_int(receipt.get('eligible_promotions'))}"
                ),
                (
                    "Selected: "
                    f"{_safe_int(receipt.get('selected_promotions'))}"
                ),
                (
                    "Rejected: "
                    f"{_safe_int(receipt.get('rejected_promotions'))}"
                ),
                "Master writes: None",
            ]
        )

        selected_tickers = (
            receipt.get(
                "selected_tickers",
                [],
            )
        )

        if isinstance(
            selected_tickers,
            list,
        ) and selected_tickers:
            lines.append(
                "Eligible this run: "
                + ", ".join(
                    f"${_text(ticker).upper()}"
                    for ticker
                    in selected_tickers
                    if _text(
                        ticker
                    )
                )
            )

        return lines

    successful = _safe_int(
        receipt.get(
            "successful_promotions"
        )
    )

    deferred = _safe_int(
        receipt.get(
            "deferred_promotions"
        )
    )

    rejected = _safe_int(
        receipt.get(
            "rejected_promotions"
        )
    )

    lines.extend(
        [
            "Mode: APPLY",
            f"Status: {status or 'UNKNOWN'}",
            f"Promoted: {successful}",
            f"Deferred: {deferred}",
            f"Rejected: {rejected}",
        ]
    )

    successful_results = (
        receipt.get(
            "successful_results",
            [],
        )
    )

    if isinstance(
        successful_results,
        list,
    ):
        for result in successful_results[
            :8
        ]:
            if not isinstance(
                result,
                dict,
            ):
                continue

            ticker = _text(
                result.get(
                    "Ticker"
                )
            ).upper()

            master_row = _text(
                result.get(
                    "Master Row Added"
                )
            )

            if ticker:
                line = (
                    f"${ticker} → Stock Summary USD"
                )

                if master_row:
                    line += (
                        f" row {master_row}"
                    )

                lines.append(
                    line
                )

    promoted_tickers = (
        receipt.get(
            "promoted_tickers",
            [],
        )
    )

    if (
        successful > 0
        and not successful_results
        and isinstance(
            promoted_tickers,
            list,
        )
    ):
        lines.append(
            "Promoted tickers: "
            + ", ".join(
                f"${_text(ticker).upper()}"
                for ticker
                in promoted_tickers
                if _text(
                    ticker
                )
            )
        )

    if status == "FAILED":
        failed_promotion = (
            receipt.get(
                "failed_promotion"
            )
        )

        if isinstance(
            failed_promotion,
            dict,
        ):
            failed_ticker = _text(
                failed_promotion.get(
                    "Ticker"
                )
            ).upper()

            error = _text(
                failed_promotion.get(
                    "Error"
                )
            )

            if failed_ticker:
                lines.append(
                    f"Failed ticker: ${failed_ticker}"
                )

            if error:
                lines.append(
                    "Error: "
                    + error[:600]
                )

        else:
            error = _text(
                receipt.get(
                    "error"
                )
            )

            if error:
                lines.append(
                    "Error: "
                    + error[:600]
                )

    master_before = receipt.get(
        "master_tickers_before"
    )

    master_after = receipt.get(
        "master_tickers_after"
    )

    if (
        master_before is not None
        and master_after is not None
    ):
        lines.append(
            "Master tickers: "
            f"{master_before} → {master_after}"
        )

    return lines


def _build_consolidated_message(
    full_receipt: dict[str, Any],
    changes: dict[str, Any],
    promotion_receipt: dict[str, Any],
    *,
    test_mode: bool,
) -> str:
    """Build one scanner-and-promotion Telegram message."""
    message_changes = dict(
        changes
    )

    # These rows were assessed before automatic promotion.
    # The final promotion section reports their actual outcome.
    message_changes[
        "promotion_ready"
    ] = []

    scanner_message = (
        build_funnel_message(
            full_receipt,
            message_changes,
            test_mode=test_mode,
        )
    )

    promotion_lines = (
        _promotion_summary_lines(
            promotion_receipt
        )
    )

    return (
        scanner_message
        + "\n"
        + "\n".join(
            promotion_lines
        )
    ).strip()

def main() -> None:
    """Run the complete production refresh and promotion sequence."""
    _require_confirmation()

    telegram_mode = (
        _telegram_mode()
    )

    auto_promotion_mode = (
        _auto_promotion_mode()
    )

    promotion_limit = (
        _auto_promotion_limit()
    )

    output_directory = (
        _output_directory()
    )

    print()
    print(
        "HX FUNNEL — PRODUCTION SEQUENCE"
    )
    print(
        "=" * 40
    )
    print(
        "Telegram mode:               "
        f"{telegram_mode}"
    )
    print(
        "Auto-promotion mode:         "
        f"{auto_promotion_mode}"
    )
    print(
        "Auto-promotion limit:        "
        f"{promotion_limit}"
    )
    print()

    # Reuse the exact consolidated scanner and writer already tested.
    os.environ[
        "CONFIRM_PILOT_FULL_RUN"
    ] = "YES"

    full_runner.main()

    full_receipt_path = (
        output_directory
        / "pilot_full_run_receipt.json"
    )

    backup_path = (
        output_directory
        / "pilot_full_run_prewrite_backup.json"
    )

    full_receipt = _read_json(
        full_receipt_path
    )

    backup = _read_json(
        backup_path
    )

    old_funnel = _backup_records(
        backup,
        funnel_writer.FUNNEL_SHEET,
        funnel_writer.FUNNEL_HEADERS,
    )

    old_pending = _backup_records(
        backup,
        funnel_writer.PENDING_SHEET,
        funnel_writer.PENDING_HEADERS,
    )

    (
        refreshed_funnel,
        refreshed_pending,
    ) = _read_current_tables()

    changes = analyse_funnel_changes(
        old_funnel,
        refreshed_funnel,
        old_pending,
        refreshed_pending,
    )

    print()
    print(
        "RUNNING POST-SCAN AUTOMATIC PROMOTION"
    )
    print(
        "=" * 40
    )

    (
        promotion_receipt,
        promotion_return_code,
        promotion_output,
    ) = _run_auto_promotion(
        output_directory,
        auto_promotion_mode,
        promotion_limit,
    )

    # Read the pending sheet again because successful promotions mark rows ADDED.
    (
        final_funnel,
        final_pending,
    ) = _read_current_tables()

    changes[
        "current_signal_count"
    ] = sum(
        1
        for record in final_funnel
        if (
            _text(
                record.get(
                    "Current Run"
                )
            ).upper()
            == "YES"
        )
    )

    changes[
        "current_pending_count"
    ] = (
        _count_outstanding_current_pending(
            final_pending
        )
    )

    scanner_material = (
        _safe_int(
            changes.get(
                "material_change_count"
            )
        )
        > 0
    )

    promotion_material = (
        _promotion_is_material(
            promotion_receipt
        )
    )

    notification_text = ""

    should_send = (
        telegram_mode == "TEST"
        or (
            telegram_mode
            == "CHANGES_ONLY"
            and (
                scanner_material
                or promotion_material
            )
        )
    )

    if telegram_mode == "OFF":
        notification: dict[
            str,
            Any,
        ] = {
            "status": "SKIPPED_DISABLED",
            "message_count": 0,
            "attempts_total": 0,
        }

    elif not should_send:
        notification = {
            "status": (
                "SKIPPED_NO_MATERIAL_CHANGE"
            ),
            "message_count": 0,
            "attempts_total": 0,
        }

    else:
        notification_text = (
            _build_consolidated_message(
                full_receipt,
                changes,
                promotion_receipt,
                test_mode=(
                    telegram_mode
                    == "TEST"
                ),
            )
        )

        try:
            notification = (
                send_telegram_text(
                    notification_text
                )
            )

        except TelegramNotificationError as exc:
            notification = {
                "status": "FAILED_NON_FATAL",
                "message_count": 0,
                "attempts_total": 0,
                "error": str(
                    exc
                ),
            }

            print(
                "::warning::Telegram "
                f"notification failed: {exc}"
            )

    promotion_failed = (
        promotion_return_code != 0
        or _text(
            promotion_receipt.get(
                "status"
            )
        ).upper()
        == "FAILED"
    )

    overall_status = (
        "FAILED"
        if promotion_failed
        else "PASSED"
    )

    production_receipt = {
        "status": overall_status,
        "mode": "PRODUCTION_REFRESH_WITH_AUTO_PROMOTION",
        "run_id": full_receipt.get(
            "run_id"
        ),
        "tested_scanner_engine": (
            "funnel.pilot_full_runner"
        ),
        "tested_promotion_engine": (
            "funnel.auto_promotion_runner"
        ),
        "sheets_written_by_scanner": (
            full_receipt.get(
                "written_sheets",
                [],
            )
        ),
        "stock_summary_usd_written_by_scanner": False,
        "auto_promotion_mode": (
            auto_promotion_mode
        ),
        "auto_promotion_limit": (
            promotion_limit
        ),
        "auto_promotion_return_code": (
            promotion_return_code
        ),
        "auto_promotion_receipt": (
            promotion_receipt
        ),
        "auto_promotion_output": (
            promotion_output
        ),
        "full_run_receipt": str(
            full_receipt_path
        ),
        "prewrite_backup": str(
            backup_path
        ),
        "telegram_mode": telegram_mode,
        "telegram": notification,
        "scanner_changes": changes,
        "notification_text": (
            notification_text
        ),
        "final_current_signals": (
            changes[
                "current_signal_count"
            ]
        ),
        "final_outstanding_pending": (
            changes[
                "current_pending_count"
            ]
        ),
    }

    receipt_path = (
        output_directory
        / "production_run_receipt.json"
    )

    _write_json(
        receipt_path,
        production_receipt,
    )

    print()
    print(
        "HX FUNNEL — FINAL PRODUCTION RESULT"
    )
    print(
        "=" * 42
    )
    print(
        "Overall status:              "
        f"{overall_status}"
    )
    print(
        "Current signals:             "
        f"{changes['current_signal_count']}"
    )
    print(
        "Scanner material changes:    "
        f"{changes['material_change_count']}"
    )
    print(
        "Outstanding pending:         "
        f"{changes['current_pending_count']}"
    )
    print(
        "Auto-promotion mode:         "
        f"{auto_promotion_mode}"
    )
    print(
        "Promoted successfully:       "
        f"{_safe_int(promotion_receipt.get('successful_promotions'))}"
    )
    print(
        "Telegram mode:               "
        f"{telegram_mode}"
    )
    print(
        "Telegram status:             "
        f"{notification['status']}"
    )
    print(
        "Production receipt:          "
        f"{receipt_path}"
    )

    if promotion_failed:
        print(
            "PRODUCTION SCAN COMPLETED, "
            "BUT AUTOMATIC PROMOTION FAILED"
        )
        print()

        raise RuntimeError(
            "Automatic promotion failed after the scanner "
            "successfully refreshed the funnel sheets. "
            f"Review {receipt_path}."
        )

    print(
        "PRODUCTION SCAN, PROMOTION AND "
        "NOTIFICATION COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()