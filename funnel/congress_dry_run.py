# VERSION: 2026-06-22-CONGRESS-DRY-RUN-INTEGRITY-2
# Funnel Pilot: read-only Congress comparison with strict artefact validation

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

from funnel.candidate_ingestor import (
    PENDING_ELIGIBLE_CLASSIFICATIONS,
    classify_signals,
    get_pending_new_ticker_records,
)
from funnel.congress_adapter import run_congress_adapter
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.signal_schema import Signal


logger = logging.getLogger(__name__)


COMPARISON_COLUMNS = [
    "ticker",
    "already_in_stock_summary",
    "stock_summary_row",
    "google_ticker",
    "stock_name",
    "candidate_status",
    "pending_new_ticker",
    "review_route",
    "review_priority",
    "scanner",
    "classification",
    "score",
    "entry_quality",
    "estimated_capital_mid",
    "buyers",
    "cluster_buyers",
    "flow",
    "names",
    "opportunity_stage",
    "discovery_reason",
    "signal_count",
    "observed_at",
    "valid_until",
    "signal_id",
]


PENDING_COLUMNS = [
    "ticker",
    "stock_name",
    "google_ticker",
    "scanner",
    "classification",
    "score",
    "entry_quality",
    "estimated_capital_mid",
    "buyers",
    "cluster_buyers",
    "flow",
    "names",
    "review_priority",
    "opportunity_stage",
    "discovery_reason",
    "observed_at",
    "valid_until",
    "signal_id",
]


def _float_environment(
    name: str,
    default: float,
) -> float:
    """Read a float from an environment variable."""
    raw_value = str(
        os.getenv(
            name,
            str(default),
        )
    ).strip()

    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be numeric. "
            f"Received: {raw_value}"
        ) from exc


def _output_directory() -> Path:
    """Return and create the workflow artefact directory."""
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

    return output_dir


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


def _stringify(
    value: Any,
) -> str:
    """Match the text representation produced by csv.DictWriter."""
    return (
        ""
        if value is None
        else str(value)
    )


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    """Write a CSV with a fixed header and column order."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="raise",
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    column: row.get(
                        column,
                        "",
                    )
                    for column in columns
                }
            )


def _read_csv_strict(
    path: Path,
    expected_columns: list[str],
) -> list[dict[str, str]]:
    """
    Read a CSV and reject missing, extra or reordered columns.
    """
    with path.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        reader = csv.DictReader(
            handle
        )

        if reader.fieldnames != expected_columns:
            raise RuntimeError(
                f"{path.name} header mismatch. "
                f"Expected {expected_columns}; "
                f"found {reader.fieldnames}."
            )

        rows: list[
            dict[str, str]
        ] = []

        for line_number, row in enumerate(
            reader,
            start=2,
        ):
            if None in row:
                raise RuntimeError(
                    f"{path.name} line "
                    f"{line_number} contains "
                    "extra values."
                )

            missing_values = [
                column
                for column in expected_columns
                if row.get(column) is None
            ]

            if missing_values:
                raise RuntimeError(
                    f"{path.name} line "
                    f"{line_number} is missing "
                    "columns: "
                    + ", ".join(
                        missing_values
                    )
                )

            rows.append(
                {
                    column: str(
                        row.get(column)
                        or ""
                    )
                    for column
                    in expected_columns
                }
            )

    return rows


def _pending_export_rows(
    pending: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the reduced manual-review export."""
    return [
        {
            column: row.get(
                column,
                "",
            )
            for column in PENDING_COLUMNS
        }
        for row in pending
    ]


def _require_text(
    row: dict[str, Any],
    field: str,
    context: str,
) -> str:
    """Return a required non-blank field."""
    value = str(
        row.get(field)
        or ""
    ).strip()

    if not value:
        raise RuntimeError(
            f"{context} has a blank "
            f"required field: {field}."
        )

    return value


def _positive_int(
    value: Any,
    field: str,
    context: str,
) -> int:
    """Parse a positive integer field."""
    try:
        parsed = int(value)
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError(
            f"{context} has a "
            f"non-integer {field}: "
            f"{value!r}."
        ) from exc

    if parsed < 1:
        raise RuntimeError(
            f"{context} must have "
            f"{field} of at least 1."
        )

    return parsed


def validate_artifact_records(
    signals: list[Signal],
    comparison: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> None:
    """
    Validate the in-memory relationship between signals and output rows.

    The workflow stops before upload when any ticker, signal ID,
    routing decision or required field is inconsistent.
    """
    signal_ids = [
        signal.signal_id
        for signal in signals
    ]

    if len(signal_ids) != len(
        set(signal_ids)
    ):
        duplicates = sorted(
            signal_id
            for signal_id, count
            in Counter(
                signal_ids
            ).items()
            if count > 1
        )

        raise RuntimeError(
            "Duplicate signal IDs "
            "detected: "
            + ", ".join(
                duplicates
            )
        )

    signals_by_id = {
        signal.signal_id: signal
        for signal in signals
    }

    comparison_tickers = [
        str(
            row.get("ticker")
            or ""
        ).strip().upper()
        for row in comparison
    ]

    if len(comparison_tickers) != len(
        set(comparison_tickers)
    ):
        duplicates = sorted(
            ticker
            for ticker, count
            in Counter(
                comparison_tickers
            ).items()
            if count > 1
        )

        raise RuntimeError(
            "Duplicate comparison "
            "tickers detected: "
            + ", ".join(
                duplicates
            )
        )

    comparison_by_ticker: dict[
        str,
        dict[str, Any],
    ] = {}

    for row_number, row in enumerate(
        comparison,
        start=1,
    ):
        context = (
            f"comparison row {row_number}"
        )

        missing_keys = [
            column
            for column in COMPARISON_COLUMNS
            if column not in row
        ]

        if missing_keys:
            raise RuntimeError(
                f"{context} is missing "
                "keys: "
                + ", ".join(
                    missing_keys
                )
            )

        ticker = _require_text(
            row,
            "ticker",
            context,
        ).upper()

        scanner = _require_text(
            row,
            "scanner",
            context,
        ).lower()

        classification = _require_text(
            row,
            "classification",
            context,
        ).lower()

        signal_id = _require_text(
            row,
            "signal_id",
            context,
        )

        observed_at = _require_text(
            row,
            "observed_at",
            context,
        )

        _positive_int(
            row.get(
                "signal_count"
            ),
            "signal_count",
            context,
        )

        expected_prefix = (
            f"{scanner}-{ticker}-"
        )

        if not signal_id.startswith(
            expected_prefix
        ):
            raise RuntimeError(
                f"{context} signal ID "
                f"{signal_id!r} does not "
                "start with "
                f"{expected_prefix!r}."
            )

        source_signal = (
            signals_by_id.get(
                signal_id
            )
        )

        if source_signal is None:
            raise RuntimeError(
                f"{context} refers to "
                "unknown signal ID "
                f"{signal_id!r}."
            )

        if source_signal.ticker != ticker:
            raise RuntimeError(
                f"{context} ticker "
                f"{ticker!r} does not "
                "match signal ticker "
                f"{source_signal.ticker!r}."
            )

        if source_signal.scanner != scanner:
            raise RuntimeError(
                f"{context} scanner "
                f"{scanner!r} does not "
                "match signal scanner "
                f"{source_signal.scanner!r}."
            )

        if (
            source_signal.classification
            != classification
        ):
            raise RuntimeError(
                f"{context} classification "
                f"{classification!r} does "
                "not match signal "
                "classification "
                f"{source_signal.classification!r}."
            )

        if (
            source_signal.observed_at
            != observed_at
        ):
            raise RuntimeError(
                f"{context} observed_at "
                "does not match its "
                "source signal."
            )

        already = _require_text(
            row,
            "already_in_stock_summary",
            context,
        ).upper()

        pending_flag = _require_text(
            row,
            "pending_new_ticker",
            context,
        ).upper()

        status = _require_text(
            row,
            "candidate_status",
            context,
        )

        route = _require_text(
            row,
            "review_route",
            context,
        )

        if already not in {
            "YES",
            "NO",
        }:
            raise RuntimeError(
                f"{context} has invalid "
                "already_in_stock_summary: "
                f"{already}."
            )

        if pending_flag not in {
            "YES",
            "NO",
        }:
            raise RuntimeError(
                f"{context} has invalid "
                "pending_new_ticker: "
                f"{pending_flag}."
            )

        existing = (
            already == "YES"
        )

        should_be_pending = (
            not existing
            and classification
            in PENDING_ELIGIBLE_CLASSIFICATIONS
        )

        expected_status = (
            "EXISTING_MONITORED_TICKER"
            if existing
            else "NEW_SIGNAL_TICKER"
        )

        expected_pending = (
            "YES"
            if should_be_pending
            else "NO"
        )

        expected_route = (
            "EXISTING_FUNNEL"
            if existing
            else (
                "PENDING_NEW_TICKERS"
                if should_be_pending
                else "SIGNAL_LOG_ONLY"
            )
        )

        if status != expected_status:
            raise RuntimeError(
                f"{context} status is "
                f"{status!r}; expected "
                f"{expected_status!r}."
            )

        if pending_flag != expected_pending:
            raise RuntimeError(
                f"{context} pending flag "
                f"is {pending_flag!r}; "
                "expected "
                f"{expected_pending!r}."
            )

        if route != expected_route:
            raise RuntimeError(
                f"{context} route is "
                f"{route!r}; expected "
                f"{expected_route!r}."
            )

        if (
            existing
            and not str(
                row.get(
                    "stock_summary_row"
                )
                or ""
            ).strip()
        ):
            raise RuntimeError(
                f"{context} is an "
                "existing ticker without "
                "a sheet row."
            )

        comparison_by_ticker[
            ticker
        ] = row

    pending_tickers = [
        str(
            row.get("ticker")
            or ""
        ).strip().upper()
        for row in pending
    ]

    if len(pending_tickers) != len(
        set(pending_tickers)
    ):
        duplicates = sorted(
            ticker
            for ticker, count
            in Counter(
                pending_tickers
            ).items()
            if count > 1
        )

        raise RuntimeError(
            "Duplicate pending tickers "
            "detected: "
            + ", ".join(
                duplicates
            )
        )

    expected_pending_tickers = {
        ticker
        for ticker, row
        in comparison_by_ticker.items()
        if row.get(
            "pending_new_ticker"
        )
        == "YES"
    }

    if (
        set(pending_tickers)
        != expected_pending_tickers
    ):
        raise RuntimeError(
            "Pending ticker set does "
            "not match comparison routing. "
            f"Expected "
            f"{sorted(expected_pending_tickers)}; "
            f"found "
            f"{sorted(set(pending_tickers))}."
        )

    for row_number, row in enumerate(
        pending,
        start=1,
    ):
        context = (
            f"pending row {row_number}"
        )

        ticker = _require_text(
            row,
            "ticker",
            context,
        ).upper()

        signal_id = _require_text(
            row,
            "signal_id",
            context,
        )

        _require_text(
            row,
            "observed_at",
            context,
        )

        _require_text(
            row,
            "valid_until",
            context,
        )

        comparison_row = (
            comparison_by_ticker[
                ticker
            ]
        )

        if (
            signal_id
            != comparison_row.get(
                "signal_id"
            )
        ):
            raise RuntimeError(
                f"{context} signal ID "
                "does not match the "
                "comparison row for "
                f"{ticker}."
            )

        if (
            row.get(
                "classification"
            )
            not in
            PENDING_ELIGIBLE_CLASSIFICATIONS
        ):
            raise RuntimeError(
                f"{context} has "
                "ineligible classification "
                f"{row.get('classification')!r}."
            )


def _validate_serialised_csv(
    path: Path,
    expected_rows: list[dict[str, Any]],
    columns: list[str],
    key_fields: tuple[str, ...],
) -> None:
    """
    Read a written CSV back and compare every exported value.
    """
    actual_rows = _read_csv_strict(
        path,
        columns,
    )

    if len(actual_rows) != len(
        expected_rows
    ):
        raise RuntimeError(
            f"{path.name} contains "
            f"{len(actual_rows)} rows; "
            f"expected "
            f"{len(expected_rows)}."
        )

    def make_key(
        row: dict[str, Any],
    ) -> tuple[str, ...]:
        return tuple(
            _stringify(
                row.get(
                    field,
                    "",
                )
            )
            for field in key_fields
        )

    expected_by_key = {
        make_key(row): {
            column: _stringify(
                row.get(
                    column,
                    "",
                )
            )
            for column in columns
        }
        for row in expected_rows
    }

    actual_by_key = {
        make_key(row): row
        for row in actual_rows
    }

    if (
        set(actual_by_key)
        != set(expected_by_key)
    ):
        raise RuntimeError(
            f"{path.name} key set "
            "changed during "
            "serialisation. "
            f"Expected "
            f"{sorted(expected_by_key)}; "
            f"found "
            f"{sorted(actual_by_key)}."
        )

    for key, expected in (
        expected_by_key.items()
    ):
        actual = actual_by_key[
            key
        ]

        for column in columns:
            if (
                actual[column]
                != expected[column]
            ):
                raise RuntimeError(
                    f"{path.name} row "
                    f"{key} column "
                    f"{column!r} changed "
                    "during serialisation. "
                    f"Expected "
                    f"{expected[column]!r}; "
                    f"found "
                    f"{actual[column]!r}."
                )


def _sha256(
    path: Path,
) -> str:
    """Return a file SHA-256 checksum."""
    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as handle:
        for block in iter(
            lambda: handle.read(
                65536
            ),
            b"",
        ):
            digest.update(
                block
            )

    return digest.hexdigest()


def _write_integrity_manifest(
    output_dir: Path,
    counts: dict[str, int],
    file_names: list[str],
) -> None:
    """Write checksums and counts after all validations pass."""
    files: dict[
        str,
        dict[str, Any],
    ] = {}

    for file_name in file_names:
        path = (
            output_dir
            / file_name
        )

        files[file_name] = {
            "bytes": (
                path.stat().st_size
            ),
            "sha256": _sha256(
                path
            ),
        }

    _write_json(
        output_dir
        / "artifact_integrity.json",
        {
            "status": "PASSED",
            "counts": counts,
            "files": files,
        },
    )


def _print_preview(
    title: str,
    rows: list[dict[str, Any]],
    maximum: int = 10,
) -> None:
    """Print a compact workflow preview."""
    print()
    print(title)
    print(
        "-" * len(title)
    )

    if not rows:
        print("None")
        return

    for row in rows[
        :maximum
    ]:
        ticker = row.get(
            "ticker",
            "",
        )

        classification = row.get(
            "classification",
            "",
        )

        score = float(
            row.get("score")
            or 0.0
        )

        route = row.get(
            "review_route",
            "",
        )

        print(
            f"{ticker:<8} | "
            f"{classification:<10} | "
            f"{score:>6.1f} | "
            f"{route}"
        )


def main() -> None:
    """Run the complete read-only Congress funnel comparison."""
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
    )

    minimum_conviction = (
        _float_environment(
            "MIN_CONVICTION",
            15.0,
        )
    )

    output_dir = (
        _output_directory()
    )

    logger.info(
        "Loading Stock Summary USD "
        "ticker universe."
    )

    ticker_records = (
        get_stock_summary_ticker_records()
    )

    logger.info(
        "Loaded %d unique monitored "
        "tickers from Stock Summary USD",
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

    validate_artifact_records(
        signals=signals,
        comparison=comparison,
        pending=pending,
    )

    signal_payload = [
        signal.to_dict()
        for signal in signals
    ]

    pending_export = (
        _pending_export_rows(
            pending
        )
    )

    stock_path = (
        output_dir
        / "stock_summary_tickers.json"
    )

    signals_path = (
        output_dir
        / "congress_signals.json"
    )

    comparison_path = (
        output_dir
        / "candidate_comparison.csv"
    )

    pending_path = (
        output_dir
        / "pending_new_tickers.csv"
    )

    _write_json(
        stock_path,
        ticker_records,
    )

    _write_json(
        signals_path,
        signal_payload,
    )

    _write_csv(
        comparison_path,
        comparison,
        COMPARISON_COLUMNS,
    )

    _write_csv(
        pending_path,
        pending_export,
        PENDING_COLUMNS,
    )

    _validate_serialised_csv(
        path=comparison_path,
        expected_rows=comparison,
        columns=COMPARISON_COLUMNS,
        key_fields=(
            "ticker",
            "signal_id",
        ),
    )

    _validate_serialised_csv(
        path=pending_path,
        expected_rows=pending_export,
        columns=PENDING_COLUMNS,
        key_fields=(
            "ticker",
            "signal_id",
        ),
    )

    existing_count = sum(
        row[
            "already_in_stock_summary"
        ]
        == "YES"
        for row in comparison
    )

    absent_count = sum(
        row[
            "already_in_stock_summary"
        ]
        == "NO"
        for row in comparison
    )

    signal_log_only_count = sum(
        row[
            "review_route"
        ]
        == "SIGNAL_LOG_ONLY"
        for row in comparison
    )

    classification_counts = Counter(
        row[
            "classification"
        ]
        for row in comparison
    )

    counts = {
        "stock_summary_tickers": (
            len(ticker_records)
        ),
        "congress_tickers_analysed": (
            analysed_count
        ),
        "congress_signals_retained": (
            len(signals)
        ),
        "comparison_rows": (
            len(comparison)
        ),
        "existing_monitored_tickers": (
            existing_count
        ),
        "absent_signal_tickers": (
            absent_count
        ),
        "pending_manual_review": (
            len(pending)
        ),
        "signal_log_only_tickers": (
            signal_log_only_count
        ),
    }

    _write_integrity_manifest(
        output_dir=output_dir,
        counts=counts,
        file_names=[
            stock_path.name,
            signals_path.name,
            comparison_path.name,
            pending_path.name,
        ],
    )

    print()
    print(
        "FUNNEL PILOT — CONGRESS DRY RUN"
    )
    print(
        "=" * 39
    )
    print(
        "Stock Summary tickers:       "
        f"{len(ticker_records)}"
    )
    print(
        "Congress tickers analysed:   "
        f"{analysed_count}"
    )
    print(
        "Congress signals retained:   "
        f"{len(signals)}"
    )
    print(
        "Existing monitored tickers:  "
        f"{existing_count}"
    )
    print(
        "Absent signal tickers:       "
        f"{absent_count}"
    )
    print(
        "Pending manual review:       "
        f"{len(pending)}"
    )
    print(
        "Signal-log-only tickers:     "
        f"{signal_log_only_count}"
    )
    print(
        "Minimum conviction:          "
        f"{minimum_conviction:.1f}"
    )

    print()
    print(
        "Classification counts:"
    )

    if classification_counts:
        for classification in sorted(
            classification_counts
        ):
            print(
                f"  {classification:<12} "
                f"{classification_counts[classification]}"
            )
    else:
        print(
            "  None"
        )

    _print_preview(
        "PENDING NEW TICKER REVIEW",
        pending,
    )

    risk_log_rows = [
        row
        for row in comparison
        if row[
            "review_route"
        ]
        == "SIGNAL_LOG_ONLY"
    ]

    _print_preview(
        "SIGNAL LOG ONLY",
        risk_log_rows,
    )

    print()
    print(
        "Files created:"
    )
    print(
        "  stock_summary_tickers.json"
    )
    print(
        "  congress_signals.json"
    )
    print(
        "  candidate_comparison.csv"
    )
    print(
        "  pending_new_tickers.csv"
    )
    print(
        "  artifact_integrity.json"
    )

    print()
    print(
        "ARTIFACT INTEGRITY CHECK PASSED"
    )
    print(
        "Google Sheets writes:        None"
    )
    print(
        "Telegram messages:           None"
    )
    print()
    print(
        "CONGRESS DRY RUN COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()