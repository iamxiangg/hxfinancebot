# NEW — Funnel Pilot Step 2: read-only Stock Summary reader

from __future__ import annotations

import logging
import os
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id

WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Stock Summary USD").strip()
EXPECTED_HEADERS = ["Ticker", "Google Ticker", "Stock Name"]

logger = logging.getLogger(__name__)


def normalise_ticker(value: Any) -> str:
    """Normalise only whitespace and case; preserve the sheet's ticker syntax."""
    if value is None:
        return ""
    return str(value).strip().upper()


def get_stock_summary_ticker_records(service=None) -> list[dict[str, Any]]:
    """
    Read columns A:C from Stock Summary USD.

    Returns one record per unique non-blank ticker and preserves the physical
    worksheet row. This function performs no writes.
    """
    service = service or get_sheets_service(readonly=True)
    spreadsheet_id = get_spreadsheet_id()
    range_name = f"'{WORKSHEET_NAME}'!A1:C"

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            majorDimension="ROWS",
        )
        .execute()
    )
    rows = response.get("values", [])
    if not rows:
        raise RuntimeError(f"No data found in {WORKSHEET_NAME}!A:C")

    header = [str(value).strip() for value in rows[0]]
    padded_header = header + [""] * (len(EXPECTED_HEADERS) - len(header))
    actual = padded_header[: len(EXPECTED_HEADERS)]
    if [item.casefold() for item in actual] != [
        item.casefold() for item in EXPECTED_HEADERS
    ]:
        raise RuntimeError(
            f"Unexpected headers in {WORKSHEET_NAME}!A1:C1. "
            f"Expected {EXPECTED_HEADERS}, found {actual}"
        )

    records: list[dict[str, Any]] = []
    first_row_by_ticker: dict[str, int] = {}
    duplicate_rows: dict[str, list[int]] = {}

    for sheet_row, row in enumerate(rows[1:], start=2):
        padded = list(row) + [""] * (3 - len(row))
        ticker = normalise_ticker(padded[0])
        if not ticker:
            continue

        if ticker in first_row_by_ticker:
            duplicate_rows.setdefault(
                ticker, [first_row_by_ticker[ticker]]
            ).append(sheet_row)
            continue

        first_row_by_ticker[ticker] = sheet_row
        records.append(
            {
                "ticker": ticker,
                "google_ticker": str(padded[1]).strip(),
                "stock_name": str(padded[2]).strip(),
                "sheet_row": sheet_row,
            }
        )

    for ticker, affected_rows in sorted(duplicate_rows.items()):
        logger.warning(
            "Duplicate ticker %s found in rows %s; only the first is returned",
            ticker,
            ", ".join(str(row) for row in affected_rows),
        )

    logger.info(
        "Loaded %d unique monitored tickers from %s",
        len(records),
        WORKSHEET_NAME,
    )
    return records


def get_stock_summary_tickers(service=None) -> list[str]:
    """Return only the unique ticker symbols from column A."""
    return [
        record["ticker"]
        for record in get_stock_summary_ticker_records(service=service)
    ]


def get_stock_summary_ticker_rows(service=None) -> dict[str, int]:
    """Return ticker-to-worksheet-row mapping."""
    return {
        record["ticker"]: record["sheet_row"]
        for record in get_stock_summary_ticker_records(service=service)
    }


def print_reader_summary(records: list[dict[str, Any]], preview_count: int = 10) -> None:
    """Print a concise, read-only verification report."""
    preview_count = max(1, min(int(preview_count), 50))
    print("\nFUNNEL PILOT — STOCK SUMMARY READER")
    print("=" * 43)
    print(f"Worksheet:      {WORKSHEET_NAME}")
    print(f"Tickers loaded: {len(records)}")
    print("Writes made:    None")
    print(f"\nFirst {min(preview_count, len(records))} ticker records:")
    for record in records[:preview_count]:
        print(
            f"  Row {record['sheet_row']}: {record['ticker']} | "
            f"{record['google_ticker']} | {record['stock_name']}"
        )
    print("\nREAD-ONLY TEST COMPLETED SUCCESSFULLY\n")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    preview_count = int(os.getenv("PREVIEW_COUNT", "10"))
    try:
        records = get_stock_summary_ticker_records()
        print_reader_summary(records, preview_count)
    except Exception as exc:
        logger.exception("Sheet-reader test failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
