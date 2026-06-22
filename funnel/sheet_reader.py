# NEW — Funnel Pilot Step 2: Read-only Stock Summary ticker reader

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Xiang Stock Analysis")
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Stock Summary USD")

# Optional. If supplied, this avoids searching Google Drive by workbook name.
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

GCP_CREDENTIALS_ENV = "GCP_SERVICE_ACCOUNT_FILE"

READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _load_service_account_info() -> dict[str, Any]:
    """
    Load the Google service-account credentials.

    Supports either:
    1. The complete service-account JSON stored directly in the
       GCP_SERVICE_ACCOUNT_FILE environment variable; or
    2. A local path to a service-account JSON file.

    No Google Sheets data are modified.
    """
    raw_value = os.getenv(GCP_CREDENTIALS_ENV, "").strip()

    if not raw_value:
        raise RuntimeError(
            f"Missing environment variable: {GCP_CREDENTIALS_ENV}"
        )

    # Existing GitHub Actions setup: the environment variable contains JSON.
    if raw_value.startswith("{"):
        try:
            credentials_info = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{GCP_CREDENTIALS_ENV} contains invalid JSON."
            ) from exc

    # Optional local-development setup: environment variable contains a path.
    else:
        credentials_path = Path(raw_value).expanduser()

        if not credentials_path.is_file():
            raise RuntimeError(
                f"{GCP_CREDENTIALS_ENV} is neither valid JSON nor an "
                f"existing file path: {credentials_path}"
            )

        try:
            credentials_info = json.loads(
                credentials_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Unable to read service-account file: {credentials_path}"
            ) from exc

    required_fields = {
        "type",
        "project_id",
        "private_key",
        "client_email",
        "token_uri",
    }

    missing_fields = required_fields.difference(credentials_info)

    if missing_fields:
        raise RuntimeError(
            "Service-account credentials are missing required fields: "
            + ", ".join(sorted(missing_fields))
        )

    return credentials_info


def _get_credentials() -> Credentials:
    """Create read-only Google credentials."""
    credentials_info = _load_service_account_info()

    return Credentials.from_service_account_info(
        credentials_info,
        scopes=READ_ONLY_SCOPES,
    )


# ---------------------------------------------------------------------------
# Workbook discovery
# ---------------------------------------------------------------------------

def _find_spreadsheet_id(credentials: Credentials) -> str:
    """
    Return the spreadsheet ID.

    GOOGLE_SHEET_ID is used when available. Otherwise, the function searches
    Google Drive for the exact workbook name.
    """
    if SPREADSHEET_ID:
        logger.info("Using spreadsheet ID from GOOGLE_SHEET_ID.")
        return SPREADSHEET_ID

    drive_service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    # Escape apostrophes for the Google Drive search query.
    escaped_name = SHEET_NAME.replace("\\", "\\\\").replace("'", "\\'")

    query = (
        f"name = '{escaped_name}' "
        "and mimeType = 'application/vnd.google-apps.spreadsheet' "
        "and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            fields="files(id,name,modifiedTime)",
            pageSize=10,
        )
        .execute()
    )

    files = response.get("files", [])

    if not files:
        raise RuntimeError(
            f"Google spreadsheet '{SHEET_NAME}' was not found. "
            "Confirm that the service-account email has access to it."
        )

    if len(files) > 1:
        matching_files = ", ".join(
            f"{file.get('name')} [{file.get('id')}]"
            for file in files
        )

        raise RuntimeError(
            f"More than one spreadsheet named '{SHEET_NAME}' was found: "
            f"{matching_files}. Set GOOGLE_SHEET_ID to the correct ID."
        )

    spreadsheet_id = files[0]["id"]

    logger.info(
        "Located spreadsheet '%s' with ID %s.",
        files[0].get("name"),
        spreadsheet_id,
    )

    return spreadsheet_id


# ---------------------------------------------------------------------------
# Ticker reading
# ---------------------------------------------------------------------------

def _normalise_ticker(value: str) -> str:
    """
    Apply only safe master-sheet normalisation.

    Dots are not changed to hyphens here because column A remains the
    authoritative ticker representation in Google Sheets.
    """
    return str(value).strip().upper()


def get_stock_summary_ticker_records() -> list[dict[str, Any]]:
    """
    Read ticker records from column A of Stock Summary USD.

    Returns:
        [
            {"ticker": "AAPL", "sheet_row": 2},
            {"ticker": "MSFT", "sheet_row": 3},
        ]

    This function performs no writes.
    """
    credentials = _get_credentials()
    spreadsheet_id = _find_spreadsheet_id(credentials)

    sheets_service = build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    range_name = f"'{WORKSHEET_NAME}'!A2:A"

    response = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            majorDimension="ROWS",
        )
        .execute()
    )

    rows = response.get("values", [])

    records: list[dict[str, Any]] = []
    first_row_by_ticker: dict[str, int] = {}
    duplicate_rows: dict[str, list[int]] = {}

    # A2 is worksheet row 2.
    for sheet_row, row in enumerate(rows, start=2):
        raw_ticker = row[0] if row else ""
        ticker = _normalise_ticker(raw_ticker)

        if not ticker:
            continue

        if ticker in first_row_by_ticker:
            duplicate_rows.setdefault(
                ticker,
                [first_row_by_ticker[ticker]],
            ).append(sheet_row)
            continue

        first_row_by_ticker[ticker] = sheet_row

        records.append(
            {
                "ticker": ticker,
                "sheet_row": sheet_row,
            }
        )

    if duplicate_rows:
        for ticker, affected_rows in duplicate_rows.items():
            logger.warning(
                "Duplicate ticker %s found in worksheet rows: %s. "
                "Only the first occurrence will be returned.",
                ticker,
                ", ".join(str(row) for row in affected_rows),
            )

    logger.info(
        "Loaded %d unique monitored tickers from '%s'.",
        len(records),
        WORKSHEET_NAME,
    )

    return records


def get_stock_summary_tickers() -> list[str]:
    """
    Return the unique monitored tickers from column A.

    Example:
        ["AAPL", "AMD", "AMZN", "MSFT"]
    """
    records = get_stock_summary_ticker_records()

    return [record["ticker"] for record in records]


def get_stock_summary_ticker_rows() -> dict[str, int]:
    """
    Return a mapping between each ticker and its physical worksheet row.

    Example:
        {
            "AAPL": 2,
            "MSFT": 3,
        }

    The row mapping will be useful later when the pilot needs to match scanner
    signals to the correct stock without writing anything yet.
    """
    records = get_stock_summary_ticker_records()

    return {
        record["ticker"]: record["sheet_row"]
        for record in records
    }


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------

def main() -> None:
    """Run a read-only connection and ticker-list test."""
    try:
        records = get_stock_summary_ticker_records()
    except Exception as exc:
        logger.exception("Sheet-reader test failed: %s", exc)
        raise SystemExit(1) from exc

    tickers = [record["ticker"] for record in records]

    print()
    print("FUNNEL PILOT — STOCK SUMMARY READER")
    print("-----------------------------------")
    print(f"Workbook:       {SHEET_NAME}")
    print(f"Worksheet:      {WORKSHEET_NAME}")
    print(f"Tickers loaded: {len(tickers)}")
    print("Writes made:    None")

    if tickers:
        preview_count = min(10, len(tickers))

        print()
        print(f"First {preview_count} tickers:")

        for record in records[:preview_count]:
            print(
                f"  Row {record['sheet_row']}: "
                f"{record['ticker']}"
            )
    else:
        print()
        print("No tickers were found in column A.")

    print()


if __name__ == "__main__":
    main()