# VERSION: 2026-06-23-PRODUCTION-RUNNER-1
#
# Production-facing wrapper around the tested consolidated pilot engine.
# Existing Python module names and worksheet names remain unchanged.

from __future__ import annotations

import json
import os
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


def _text(value: Any) -> str:
    """Return a stripped string."""
    return (
        ""
        if value is None
        else str(value).strip()
    )


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


def _output_directory() -> Path:
    """Return and create the artefact directory."""
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


def main() -> None:
    """Run the production refresh and optional Telegram notification."""
    _require_confirmation()

    telegram_mode = (
        _telegram_mode()
    )

    output_directory = (
        _output_directory()
    )

    # Reuse the exact consolidated engine already validated in pilot.
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

    service = get_sheets_service(
        readonly=True
    )

    spreadsheet_id = (
        get_spreadsheet_id()
    )

    new_funnel, _ = (
        funnel_writer._read_table(
            service,
            spreadsheet_id,
            funnel_writer.FUNNEL_SHEET,
            funnel_writer.FUNNEL_HEADERS,
        )
    )

    new_pending, _ = (
        funnel_writer._read_table(
            service,
            spreadsheet_id,
            funnel_writer.PENDING_SHEET,
            funnel_writer.PENDING_HEADERS,
        )
    )

    changes = analyse_funnel_changes(
        old_funnel,
        new_funnel,
        old_pending,
        new_pending,
    )

    notification_text = ""

    if telegram_mode == "OFF":
        notification: dict[
            str,
            Any,
        ] = {
            "status": "SKIPPED_DISABLED",
            "message_count": 0,
            "attempts_total": 0,
        }

    elif (
        telegram_mode
        == "CHANGES_ONLY"
        and changes[
            "material_change_count"
        ]
        == 0
    ):
        notification = {
            "status": (
                "SKIPPED_NO_MATERIAL_CHANGE"
            ),
            "message_count": 0,
            "attempts_total": 0,
        }

    else:
        notification_text = (
            build_funnel_message(
                full_receipt,
                changes,
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
                "error": str(exc),
            }

            print(
                "::warning::Telegram "
                f"notification failed: {exc}"
            )

    production_receipt = {
        "status": "PASSED",
        "mode": "PRODUCTION_REFRESH",
        "run_id": full_receipt.get(
            "run_id"
        ),
        "tested_engine": (
            "funnel.pilot_full_runner"
        ),
        "sheets_written": (
            full_receipt.get(
                "written_sheets",
                [],
            )
        ),
        "stock_summary_usd_written": False,
        "full_run_receipt": str(
            full_receipt_path
        ),
        "prewrite_backup": str(
            backup_path
        ),
        "telegram_mode": telegram_mode,
        "telegram": notification,
        "changes": changes,
        "notification_text": (
            notification_text
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
        "HX FUNNEL — PRODUCTION REFRESH"
    )
    print(
        "=" * 38
    )
    print(
        "Current signals:             "
        f"{changes['current_signal_count']}"
    )
    print(
        "Material changes:            "
        f"{changes['material_change_count']}"
    )
    print(
        "Current pending:             "
        f"{changes['current_pending_count']}"
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
        "Stock Summary USD writes:    None"
    )
    print(
        "Receipt:                     "
        f"{receipt_path}"
    )
    print(
        "PRODUCTION REFRESH COMPLETED "
        "SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()