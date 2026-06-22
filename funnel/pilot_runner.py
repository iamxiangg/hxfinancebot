# NEW — Funnel Pilot orchestrator: reader, dry-run and pilot-write modes

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

from funnel.candidate_ingestor import classify_signals
from funnel.congress_adapter import get_congress_signals
from funnel.pilot_writer import write_pilot_results
from funnel.sheet_reader import (
    get_stock_summary_ticker_records,
    print_reader_summary,
)

OUTPUT_DIR = Path("pilot_output")
logger = logging.getLogger(__name__)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _save_outputs(records, signals, comparison) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(OUTPUT_DIR / "stock_summary_tickers.json", records)
    _write_json(
        OUTPUT_DIR / "congress_signals.json",
        [signal.to_dict() for signal in signals],
    )
    _write_csv(OUTPUT_DIR / "candidate_comparison.csv", comparison)


def _print_comparison_summary(comparison: list[dict[str, Any]]) -> None:
    existing = [
        row for row in comparison if row["already_in_stock_summary"]
    ]
    new = [
        row for row in comparison if not row["already_in_stock_summary"]
    ]
    print("\nFUNNEL PILOT — CONGRESS COMPARISON")
    print("=" * 39)
    print(f"Signalled tickers:            {len(comparison)}")
    print(f"Existing monitored tickers:  {len(existing)}")
    print(f"New candidate tickers:       {len(new)}")
    print("\nNew candidates:")
    for row in new[:20]:
        print(
            f"  {row['ticker']}: {row['primary_classification']} | "
            f"score {float(row['primary_score'] or 0):.0f} | "
            f"{row['opportunity_stage']}"
        )
    if not new:
        print("  None")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HX Finance funnel pilot")
    parser.add_argument(
        "--mode",
        choices=("reader", "dry-run", "pilot-write"),
        default="reader",
    )
    parser.add_argument("--preview-count", type=int, default=10)
    parser.add_argument("--min-conviction", type=float, default=15.0)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = parse_args()
    try:
        records = get_stock_summary_ticker_records()
        print_reader_summary(records, args.preview_count)

        if args.mode == "reader":
            return

        signals = get_congress_signals(args.min_conviction)
        comparison = classify_signals(signals, records)
        _save_outputs(records, signals, comparison)
        _print_comparison_summary(comparison)

        if args.mode == "pilot-write":
            write_pilot_results(signals, comparison)
            print(
                "Pilot worksheets updated: Pending_New_Tickers, "
                "Scanner_Signal_Log_Pilot and Funnel_Pilot."
            )
            print("Stock Summary USD was not modified.\n")
        else:
            print("Dry run completed. No Google Sheets writes were made.\n")
    except Exception as exc:
        logger.exception("Funnel pilot failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
