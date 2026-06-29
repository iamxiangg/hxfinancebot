"""
Sheet Beautifier — one-shot formatting for all production Google Sheets.

Applies:
  - Bold, wrapped headers with frozen row 1
  - Alternating row colours (zebra stripes) from row 2
  - Auto-resized column widths
  - Number formatting (currency, percentage, date, integer) per column type
  - Colour-coded sheet tabs

Usage:
  python -m funnel.sheet_beautifier              # all production sheets
  python -m funnel.sheet_beautifier --dry-run    # preview without applying
  BEAUTIFY_SHEETS="Stock Summary USD,BTD_Candidates" python -m funnel.sheet_beautifier
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import (
    BOT_STATE_HEADERS,
    BOT_STATE_SHEET,
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    CONGRESS_LEDGER_HEADERS,
    CONGRESS_LEDGER_SHEET,
    DECISION_LOG_HEADERS,
    DECISION_LOG_SHEET,
    FEROLDI_AI_DRAFT_HEADERS,
    FEROLDI_AI_DRAFTS_SHEET,
    INSIDER_LEDGER_HEADERS,
    INSIDER_LEDGER_SHEET,
    MASTERLIST_SHEET,
    MANUAL_SEED_HEADERS,
    MANUAL_SEED_SHEET,
    SIGNAL_LOG_HEADERS,
    SIGNAL_LOG_SHEET,
)
from funnel.sheet_table import column_letter as _column_letter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
TAB_COLOURS: dict[str, dict[str, float]] = {
    MASTERLIST_SHEET: {"red": 0.051, "green": 0.502, "blue": 0.263},
    SIGNAL_LOG_SHEET: {"red": 0.102, "green": 0.451, "blue": 0.910},
    BTD_CANDIDATES_SHEET: {"red": 0.976, "green": 0.671, "blue": 0.0},
    REVIEW_REQUESTS_SHEET: {"red": 0.890, "green": 0.455, "blue": 0.0},
    FEROLDI_AI_DRAFTS_SHEET: {"red": 0.576, "green": 0.204, "blue": 0.894},
    BOT_STATE_SHEET: {"red": 0.373, "green": 0.380, "blue": 0.408},
    DECISION_LOG_SHEET: {"red": 0.071, "green": 0.620, "blue": 0.686},
    CONGRESS_LEDGER_SHEET: {"red": 0.851, "green": 0.188, "blue": 0.145},
    INSIDER_LEDGER_SHEET: {"red": 0.773, "green": 0.133, "blue": 0.122},
    MANUAL_SEED_SHEET: {"red": 0.482, "green": 0.545, "blue": 0.227},
}

ZEBRA_FIRST: dict[str, float] = {"red": 1.0, "green": 1.0, "blue": 1.0}
ZEBRA_SECOND: dict[str, float] = {"red": 0.953, "green": 0.957, "blue": 0.965}

# ---------------------------------------------------------------------------
# Production sheets — (name, headers) tuples
# ---------------------------------------------------------------------------
REVIEW_REQUESTS_SHEET = "Review_Requests"

PRODUCTION_SHEETS: list[tuple[str, list[str]]] = [
    (MASTERLIST_SHEET, []),
    (SIGNAL_LOG_SHEET, SIGNAL_LOG_HEADERS),
    (BTD_CANDIDATES_SHEET, BTD_CANDIDATE_HEADERS),
    (REVIEW_REQUESTS_SHEET, []),
    (FEROLDI_AI_DRAFTS_SHEET, FEROLDI_AI_DRAFT_HEADERS),
    (BOT_STATE_SHEET, BOT_STATE_HEADERS),
    (DECISION_LOG_SHEET, DECISION_LOG_HEADERS),
    (CONGRESS_LEDGER_SHEET, CONGRESS_LEDGER_HEADERS),
    (INSIDER_LEDGER_SHEET, INSIDER_LEDGER_HEADERS),
    (MANUAL_SEED_SHEET, MANUAL_SEED_HEADERS),
]

# ---------------------------------------------------------------------------
# Number format classification
# ---------------------------------------------------------------------------

# Keywords that suggest currency formatting
_CURRENCY_KEYWORDS = (
    "amount", "capital", "value", "price", "cost", "revenue", "ebitda",
    "ev", "market cap", "purchase", "strike", "transaction value",
    "penalty", "bonus", "salary", "compensation",
)

# Headers that suggest billions-scale currency
_BILLIONS_KEYWORDS = ("ev (b)", "revenue ttm (b)")

# Headers that suggest percentage formatting
_PERCENT_KEYWORDS = (
    "%", "margin", "growth", "ratio", "rate", "coverage",
    "yield", "confidence", "weight", "return",
)

# Headers that suggest date formatting
_DATE_KEYWORDS = (
    "date", " at", " seen", "updated", "created", "observed",
    "filed", "expiry", "decided", "expiration", "maturity",
    "ingested", "notified", "timestamp",
)

# Headers that suggest integer formatting
_INTEGER_KEYWORDS = (
    "count", "number", "days", "buyers",
    "members", "employees", "row", "span", "message id",
    "update id", "telegram message id",
)

# Headers that don't need number formatting (text/boolean)
_TEXT_KEYWORDS = (
    "?", "name", "source", "status", "class", "route",
    "decision", "reason", "notes", "summary", "case",
    "case study", "json", "details", "display", "type",
    "ticker", "symbol", "role", "chamber", "party",
    "state", "text", "description", "comment", "title",
    "security", "nature", "document", "url", "hash",
    "fingerprint", "side", "code", "flag", "owner",
    "identity", "bioguide", "committee", "agency",
    "sector", "industry", "exposure", "error",
    "confirmation", "priority", "stage", "route",
    "candidate", "review", "by", "actor", "field",
    "relationship", "office", "level", "flow",
    "input", "cik", "accession", "id", "key",
    "signal", "trade", "record", "scan",
)


def _header_key(header: str) -> str:
    return header.strip().lower()


def _is_currency(header: str) -> bool:
    key = _header_key(header)
    if any(kw in key for kw in _BILLIONS_KEYWORDS):
        return True
    return any(kw in key for kw in _CURRENCY_KEYWORDS) and not any(
        kw in key for kw in _PERCENT_KEYWORDS
    )


def _is_billions(header: str) -> bool:
    return any(kw in _header_key(header) for kw in _BILLIONS_KEYWORDS)


def _is_percentage(header: str) -> bool:
    key = _header_key(header)
    if "%" in key:
        return True
    return any(kw in key for kw in _PERCENT_KEYWORDS) and not _is_currency(header)


def _is_date(header: str) -> bool:
    return any(kw in _header_key(header) for kw in _DATE_KEYWORDS)


def _is_integer(header: str) -> bool:
    key = _header_key(header)
    if any(kw in key for kw in _INTEGER_KEYWORDS):
        return True
    return False


def _is_text(header: str) -> bool:
    key = _header_key(header)
    return any(kw in key for kw in _TEXT_KEYWORDS)


def classify_headers(headers: list[str]) -> dict[str, list[int]]:
    """Return a dict mapping format types to zero-based column indices."""
    result: dict[str, list[int]] = {
        "currency": [],
        "currency_billions": [],
        "percentage": [],
        "date": [],
        "integer": [],
        "text": [],
    }
    for idx, header in enumerate(headers):
        if _is_text(header):
            result["text"].append(idx)
        elif _is_billions(header):
            result["currency_billions"].append(idx)
        elif _is_currency(header):
            result["currency"].append(idx)
        elif _is_percentage(header):
            result["percentage"].append(idx)
        elif _is_date(header):
            result["date"].append(idx)
        elif _is_integer(header):
            result["integer"].append(idx)
        else:
            result["text"].append(idx)
    return result


# ---------------------------------------------------------------------------
# Sheet metadata helpers
# ---------------------------------------------------------------------------
def _sheet_metadata(service, spreadsheet_id: str) -> dict[str, dict[str, Any]]:
    response = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    meta: dict[str, dict[str, Any]] = {}
    for sheet in response.get("sheets", []):
        props = sheet.get("properties", {})
        title = str(props.get("title", "")).strip()
        if title:
            meta[title] = props
    return meta


# ---------------------------------------------------------------------------
# Formatting operations
# ---------------------------------------------------------------------------
def apply_header_format(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
) -> None:
    """Bold, wrap, and freeze the header row."""
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": max(column_count, 1),
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 10},
                        "wrapStrategy": "WRAP",
                        "backgroundColor": {
                            "red": 0.957,
                            "green": 0.961,
                            "blue": 0.969,
                        },
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": (
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.fontSize,"
                    "userEnteredFormat.wrapStrategy,"
                    "userEnteredFormat.backgroundColor,"
                    "userEnteredFormat.horizontalAlignment"
                ),
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def apply_zebra_stripes(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
    row_count: int,
) -> None:
    """Add alternating row colours from row 2 onwards."""
    if row_count < 2:
        return
    requests = [
        {
            "addBanding": {
                "bandedRange": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": max(row_count, 2),
                        "startColumnIndex": 0,
                        "endColumnIndex": max(column_count, 1),
                    },
                    "rowProperties": {
                        "firstBandColor": ZEBRA_FIRST,
                        "secondBandColor": ZEBRA_SECOND,
                        "headerColor": ZEBRA_SECOND,
                    },
                }
            }
        }
    ]
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
    except Exception as exc:
        logger.warning("Zebra stripes skipped for sheet %d: %s", sheet_id, exc)


def apply_column_widths(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
) -> None:
    """Auto-resize columns to fit content."""
    requests = [
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": max(column_count, 1),
                }
            }
        }
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def apply_number_formats(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
    header_classes: dict[str, list[int]],
) -> None:
    """Apply per-column number formats based on header classification."""
    format_map = {
        "currency": "$#,##0.00",
        "currency_billions": "$#,##0.00",
        "percentage": "0.00%",
        "date": "yyyy-mm-dd",
        "integer": "#,##0",
        "text": "@",
    }

    requests: list[dict[str, Any]] = []
    for fmt_key, col_indices in header_classes.items():
        pattern = format_map.get(fmt_key, "@")
        if fmt_key == "text":
            continue  # default is already text
        for col_idx in col_indices:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": pattern,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()


def apply_tab_colour(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    colour: dict[str, float],
) -> None:
    """Set the sheet tab colour."""
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "tabColor": colour,
                },
                "fields": "tabColor",
            }
        }
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


# ---------------------------------------------------------------------------
# Missing sheet columns — reading headers without known schema
# ---------------------------------------------------------------------------
def _read_existing_headers(
    service, spreadsheet_id: str, sheet_name: str, max_cols: int = 100
) -> list[str]:
    end_col = _column_letter(max_cols)
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1:{end_col}1",
            )
            .execute()
        )
        raw = response.get("values", [[]])[0]
        return [str(v or "").strip() for v in raw]
    except Exception:
        return []


def _read_row_count(
    metadata: dict[str, dict[str, Any]], sheet_name: str, fallback: int = 2000
) -> int:
    """Return the effective row count from pre-fetched sheet metadata."""
    try:
        props = metadata.get(sheet_name)
        if props:
            grid = props.get("gridProperties", {})
            return max(int(grid.get("rowCount", fallback)), 2)
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Dry-run receipt
# ---------------------------------------------------------------------------
@dataclass
class BeautifyReceipt:
    sheets_processed: list[str] = field(default_factory=list)
    sheets_skipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def beautify_sheets(
    *,
    dry_run: bool = False,
    sheet_filter: list[str] | None = None,
) -> BeautifyReceipt:
    """Apply all formatting to production sheets."""
    receipt = BeautifyReceipt(dry_run=dry_run)

    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    metadata = _sheet_metadata(service, spreadsheet_id)

    filter_set = {s.strip() for s in sheet_filter} if sheet_filter else None

    for sheet_name, known_headers in PRODUCTION_SHEETS:
        if filter_set and sheet_name not in filter_set:
            receipt.sheets_skipped.append(sheet_name)
            continue

        props = metadata.get(sheet_name)
        if props is None:
            logger.info("Sheet %r not found — skipping.", sheet_name)
            receipt.sheets_skipped.append(sheet_name)
            continue

        sheet_id = int(props.get("sheetId", 0))

        # Determine headers
        headers = list(known_headers)
        if not headers:
            headers = _read_existing_headers(service, spreadsheet_id, sheet_name)
        if not headers:
            logger.warning("No headers found for %r — skipping formatting.", sheet_name)
            receipt.sheets_skipped.append(sheet_name)
            continue

        column_count = len(headers)
        row_count = _read_row_count(metadata, sheet_name)
        header_classes = classify_headers(headers)
        tab_colour = TAB_COLOURS.get(sheet_name, {"red": 0.8, "green": 0.8, "blue": 0.8})

        logger.info(
            "Beautifying %r (%d cols × %d rows) …",
            sheet_name,
            column_count,
            row_count,
        )
        logger.info("  Currency: %d  %%: %d  Date: %d  Int: %d  Text: %d",
            len(header_classes["currency"]) + len(header_classes["currency_billions"]),
            len(header_classes["percentage"]),
            len(header_classes["date"]),
            len(header_classes["integer"]),
            len(header_classes["text"]),
        )

        if dry_run:
            receipt.sheets_processed.append(sheet_name)
            continue

        try:
            apply_header_format(service, spreadsheet_id, sheet_id, column_count)
            apply_zebra_stripes(service, spreadsheet_id, sheet_id, column_count, row_count)
            apply_column_widths(service, spreadsheet_id, sheet_id, column_count)
            apply_number_formats(service, spreadsheet_id, sheet_id, column_count, header_classes)
            apply_tab_colour(service, spreadsheet_id, sheet_id, tab_colour)
            receipt.sheets_processed.append(sheet_name)
        except Exception as exc:
            logger.error("Failed to beautify %r: %s", sheet_name, exc)
            receipt.errors.append({"sheet": sheet_name, "error": str(exc)})

    return receipt


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    dry_run = "--dry-run" in sys.argv
    sheet_filter_str = os.getenv("BEAUTIFY_SHEETS", "").strip()
    sheet_filter = (
        [s.strip() for s in sheet_filter_str.split(",") if s.strip()]
        if sheet_filter_str
        else None
    )

    if dry_run:
        print("DRY RUN — no writes will be performed.\n")

    receipt = beautify_sheets(dry_run=dry_run, sheet_filter=sheet_filter)

    print()
    print("SHEET BEAUTIFIER")
    print("=" * 50)
    print(f"Mode:         {'DRY RUN' if dry_run else 'APPLIED'}")
    print(f"Processed:    {len(receipt.sheets_processed)}")
    print(f"Skipped:      {len(receipt.sheets_skipped)}")
    print(f"Errors:       {len(receipt.errors)}")
    if receipt.sheets_processed:
        print(f"Sheets:       {', '.join(receipt.sheets_processed)}")
    if receipt.errors:
        for err in receipt.errors:
            print(f"  ! {err['sheet']}: {err['error']}")

    # Write receipt
    output_dir = Path(os.getenv("FUNNEL_OUTPUT_DIR", "funnel_output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / "sheet_beautifier_receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "dry_run": receipt.dry_run,
                "sheets_processed": receipt.sheets_processed,
                "sheets_skipped": receipt.sheets_skipped,
                "errors": receipt.errors,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"\nReceipt:      {receipt_path}")
    print("SHEET BEAUTIFIER COMPLETED")


if __name__ == "__main__":
    main()
