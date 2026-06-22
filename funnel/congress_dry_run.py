# NEW — Funnel Pilot Step 4: Congress versus Google Sheets dry run

from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from typing import Any

from funnel.congress_adapter import run_congress_adapter
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.signal_schema import ScannerSignal


OUTPUT_DIRECTORY = Path(
    os.getenv(
        "FUNNEL_OUTPUT_DIR",
        "funnel_output",
    )
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


def _get_min_conviction() -> float:
    """Read the near-miss conviction threshold."""
    raw_value = os.getenv(
        "MIN_CONVICTION",
        "15",
    ).strip()

    try:
        return float(raw_value)
    except ValueError:
        logger.warning(
            "Invalid MIN_CONVICTION '%s'. Using 15.",
            raw_value,
        )
        return 15.0


def _classification_priority(
    classification: str,
) -> int:
    """Rank Congress classifications for presentation."""
    priority = {
        "actionable": 4,
        "wait": 3,
        "risk": 2,
        "near_miss": 1,
    }

    return priority.get(
        classification,
        0,
    )


def _opportunity_stage(
    classification: str,
) -> str:
    """
    Assign a pilot organisational stage.

    These are not trade instructions.
    """
    mapping = {
        "actionable": "SHORTLISTED",
        "wait": "ENTRY_WATCH",
        "risk": "RESEARCH_RISK",
        "near_miss": "RESEARCH",
    }

    return mapping.get(
        classification,
        "MONITORING",
    )


def _build_comparison_records(
    signals: list[ScannerSignal],
    stock_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare Congress signals with Stock Summary USD column A."""
    stock_record_map = {
        record["ticker"]: record
        for record in stock_records
    }

    comparison_records: list[dict[str, Any]] = []

    for signal in signals:
        existing_record = stock_record_map.get(
            signal.ticker
        )

        already_monitored = (
            existing_record is not None
        )

        comparison_records.append(
            {
                "ticker": signal.ticker,
                "already_in_stock_summary": (
                    "YES"
                    if already_monitored
                    else "NO"
                ),
                "stock_summary_row": (
                    existing_record["sheet_row"]
                    if existing_record
                    else ""
                ),
                "google_ticker": (
                    existing_record["google_ticker"]
                    if existing_record
                    else ""
                ),
                "stock_name": (
                    existing_record["stock_name"]
                    if existing_record
                    else ""
                ),
                "candidate_status": (
                    "EXISTING_MONITORED_TICKER"
                    if already_monitored
                    else "NEW_CANDIDATE"
                ),
                "scanner": signal.scanner,
                "classification": (
                    signal.classification
                ),
                "score": signal.score,
                "opportunity_stage": (
                    _opportunity_stage(
                        signal.classification
                    )
                ),
                "observed_at": signal.observed_at,
                "valid_until": (
                    signal.valid_until or ""
                ),
                "signal_id": signal.signal_id,
                "entry_quality": signal.details.get(
                    "entry_quality",
                    "",
                ),
                "estimated_capital_mid": (
                    signal.details.get(
                        "estimated_capital_mid",
                        "",
                    )
                ),
                "buyers": signal.details.get(
                    "buyers",
                    "",
                ),
                "cluster_buyers": (
                    signal.details.get(
                        "cluster_buyers",
                        "",
                    )
                ),
                "flow": signal.details.get(
                    "flow",
                    "",
                ),
                "names": ", ".join(
                    signal.details.get(
                        "names",
                        [],
                    )
                ),
            }
        )

    comparison_records.sort(
        key=lambda record: (
            _classification_priority(
                str(record["classification"])
            ),
            float(record["score"] or 0),
            str(record["ticker"]),
        ),
        reverse=True,
    )

    return comparison_records


def _write_json(
    path: Path,
    data: Any,
) -> None:
    """Write formatted JSON."""
    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_comparison_csv(
    path: Path,
    records: list[dict[str, Any]],
) -> None:
    """Write the ticker comparison report."""
    fieldnames = [
        "ticker",
        "already_in_stock_summary",
        "stock_summary_row",
        "google_ticker",
        "stock_name",
        "candidate_status",
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
        "observed_at",
        "valid_until",
        "signal_id",
    ]

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    """Run the complete read-only Congress comparison."""
    min_conviction = _get_min_conviction()

    logger.info(
        "Loading Stock Summary USD ticker universe."
    )

    stock_records = (
        get_stock_summary_ticker_records()
    )

    logger.info(
        "Running Congress adapter with minimum conviction %.1f.",
        min_conviction,
    )

    signals, analysed_count = run_congress_adapter(
        min_conviction=min_conviction
    )

    comparison_records = _build_comparison_records(
        signals=signals,
        stock_records=stock_records,
    )

    existing_records = [
        record
        for record in comparison_records
        if record["already_in_stock_summary"] == "YES"
    ]

    new_candidate_records = [
        record
        for record in comparison_records
        if record["already_in_stock_summary"] == "NO"
    ]

    OUTPUT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    _write_json(
        OUTPUT_DIRECTORY
        / "stock_summary_tickers.json",
        stock_records,
    )

    _write_json(
        OUTPUT_DIRECTORY
        / "congress_signals.json",
        [
            signal.to_dict()
            for signal in signals
        ],
    )

    _write_comparison_csv(
        OUTPUT_DIRECTORY
        / "candidate_comparison.csv",
        comparison_records,
    )

    print()
    print("FUNNEL PILOT — CONGRESS DRY RUN")
    print("=" * 38)
    print(
        f"Stock Summary tickers:       "
        f"{len(stock_records)}"
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
        f"{len(existing_records)}"
    )
    print(
        f"Potential new candidates:    "
        f"{len(new_candidate_records)}"
    )
    print(
        f"Minimum conviction:          "
        f"{min_conviction:.1f}"
    )
    print("Google Sheets writes:        None")
    print("Telegram messages:           None")

    print()
    print("EXISTING MONITORED TICKERS")

    if existing_records:
        for record in existing_records[:15]:
            print(
                f"  {record['ticker']} | "
                f"{record['classification']} | "
                f"C{float(record['score']):.0f} | "
                f"Sheet row {record['stock_summary_row']}"
            )
    else:
        print("  None")

    print()
    print("POTENTIAL NEW CANDIDATES")

    if new_candidate_records:
        for record in new_candidate_records[:20]:
            print(
                f"  {record['ticker']} | "
                f"{record['classification']} | "
                f"C{float(record['score']):.0f} | "
                f"{record['opportunity_stage']}"
            )
    else:
        print("  None")

    print()
    print(
        f"Output directory: "
        f"{OUTPUT_DIRECTORY.resolve()}"
    )
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
    print()
    print(
        "CONGRESS DRY RUN COMPLETED SUCCESSFULLY"
    )
    print()


if __name__ == "__main__":
    main()