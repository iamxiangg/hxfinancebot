# VERSION: 2026-06-22-CONGRESS-DRY-RUN-ROUTING-1
# Funnel Pilot: read-only Congress comparison and candidate routing

from __future__ import annotations

import csv
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

from funnel.candidate_ingestor import (
    classify_signals,
    get_pending_new_ticker_records,
)
from funnel.congress_adapter import (
    run_congress_adapter,
)
from funnel.sheet_reader import (
    get_stock_summary_ticker_records,
)


logger = logging.getLogger(
    __name__
)


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
    """Read a finite float from an environment variable."""
    raw_value = str(
        os.getenv(
            name,
            str(default),
        )
    ).strip()

    try:
        return float(
            raw_value
        )
    except ValueError as exc:
        raise ValueError(
            f"{name} must be numeric. "
            f"Received: {raw_value}"
        ) from exc


def _output_directory() -> Path:
    """Return and create the workflow artifact directory."""
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


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    """Write a CSV with stable columns even when no records qualify."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
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


def _print_preview(
    title: str,
    rows: list[dict[str, Any]],
    maximum: int = 10,
) -> None:
    """Print a compact workflow preview."""
    print()
    print(
        title
    )
    print(
        "-" * len(title)
    )

    if not rows:
        print(
            "None"
        )
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
        "Loading Stock Summary USD ticker universe."
    )

    ticker_records = (
        get_stock_summary_ticker_records()
    )

    logger.info(
        "Loaded %d unique monitored tickers "
        "from Stock Summary USD",
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

    signal_payload = [
        signal.to_dict()
        for signal in signals
    ]

    _write_json(
        output_dir
        / "stock_summary_tickers.json",
        ticker_records,
    )

    _write_json(
        output_dir
        / "congress_signals.json",
        signal_payload,
    )

    _write_csv(
        output_dir
        / "candidate_comparison.csv",
        comparison,
        COMPARISON_COLUMNS,
    )

    _write_csv(
        output_dir
        / "pending_new_tickers.csv",
        _pending_export_rows(
            pending
        ),
        PENDING_COLUMNS,
    )

    existing_count = sum(
        1
        for row in comparison
        if row[
            "already_in_stock_summary"
        ]
        == "YES"
    )

    absent_count = sum(
        1
        for row in comparison
        if row[
            "already_in_stock_summary"
        ]
        == "NO"
    )

    signal_log_only_count = sum(
        1
        for row in comparison
        if row[
            "review_route"
        ]
        == "SIGNAL_LOG_ONLY"
    )

    classification_counts = Counter(
        row["classification"]
        for row in comparison
    )

    print()
    print(
        "FUNNEL PILOT — CONGRESS DRY RUN"
    )
    print(
        "=" * 39
    )
    print(
        f"Stock Summary tickers:       "
        f"{len(ticker_records)}"
    )
    print(
        f"Congress tickers analysed:   "
        f"{analysed_count}"
    )
    print(
        f"Congress signals retained:   "
        f"{len(signals)}"
    )
    print(
        f"Existing monitored tickers:  "
        f"{existing_count}"
    )
    print(
        f"Absent signal tickers:       "
        f"{absent_count}"
    )
    print(
        f"Pending manual review:       "
        f"{len(pending)}"
    )
    print(
        f"Signal-log-only tickers:     "
        f"{signal_log_only_count}"
    )
    print(
        f"Minimum conviction:          "
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
        if row["review_route"]
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

    print()
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