#!/usr/bin/env python3
"""
btd_analysis.py — GitHub Actions BTD Analysis
==============================================

Fetches tickers from column A of the Google Sheets worksheet,
runs yfinance analysis, updates the worksheet, archives historical
BTD data, and sends a Telegram summary.

Ticker discovery is driven exclusively by column A.
Missing or blank values in other columns do not exclude a ticker.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import gspread
import pandas as pd
import yfinance as yf
from oauth2client.service_account import ServiceAccountCredentials


# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

SHEET_NAME = "Xiang Stock Analysis"
WORKSHEET_NAME = "Stock Summary USD"
HIST_WORKSHEET_NAME = "Historical_BTD_Metric"

OUTPUT_START_COLUMN = "AD"
OUTPUT_END_COLUMN = "AK"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """
    Send a Telegram notification using environment variables.

    Required environment variables:
    - TELEGRAM_BOT_TOKEN
    - TELEGRAM_CHAT_ID
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured"
        )
        return False

    try:
        import requests

        if len(message) > 4000:
            message = message[:3997] + "..."

        url = f"https://api.telegram.org/bot{token}/sendMessage"

        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
            },
            timeout=10,
        )

        if response.status_code != 200:
            logger.error(
                "Telegram API error: status=%s response=%s",
                response.status_code,
                response.text,
            )
            return False

        return True

    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def get_sg_time() -> str:
    """Return the current Singapore date and time."""
    sg_timezone = timezone(timedelta(hours=8))

    return datetime.now(sg_timezone).strftime(
        "%Y-%m-%d %H:%M:%S %z"
    )


def get_sg_date_display() -> str:
    """Return the current Singapore date for the Last Updated column."""
    sg_timezone = timezone(timedelta(hours=8))

    return datetime.now(sg_timezone).strftime("%b %d, %Y")


def normalise_ticker(raw_ticker) -> str:
    """Normalise a ticker obtained from Google Sheets."""
    if raw_ticker is None:
        return ""

    return str(raw_ticker).strip().upper()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting BTD Analysis")

    # ── Step 1: Authenticate to Google Sheets ────────────────────────────────

    credentials_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")

    if not credentials_json:
        logger.error(
            "Environment variable GCP_SERVICE_ACCOUNT_FILE is missing"
        )
        send_telegram(
            "❌ BTD Analysis: Secret GCP_SERVICE_ACCOUNT_FILE not found"
        )
        return

    try:
        credentials_dict = json.loads(credentials_json)

        credentials = (
            ServiceAccountCredentials.from_json_keyfile_dict(
                credentials_dict,
                SCOPE,
            )
        )

        client = gspread.authorize(credentials)

        workbook = client.open(SHEET_NAME)
        sheet = workbook.worksheet(WORKSHEET_NAME)
        hist_sheet = workbook.worksheet(HIST_WORKSHEET_NAME)

        logger.info(
            "Connected to workbook '%s', worksheet '%s'",
            SHEET_NAME,
            WORKSHEET_NAME,
        )

    except Exception as exc:
        logger.error("Google Sheets authentication failed: %s", exc)

        send_telegram(
            "❌ BTD Analysis: Google Sheets authentication failed — "
            f"{str(exc)[:100]}"
        )
        return

    # ── Step 2: Read tickers from column A only ──────────────────────────────
    #
    # Column A is the sole source of truth for ticker discovery.
    #
    # The previous implementation used:
    #
    #     min(len(col_a), len(col_e))
    #
    # This caused valid tickers in column A to be excluded whenever column E
    # contained fewer populated rows.
    #
    # This implementation processes every non-blank ticker returned from
    # column A, regardless of whether other columns contain data.

    try:
        col_a = sheet.col_values(1)

    except Exception as exc:
        logger.error("Failed to read ticker column A: %s", exc)

        send_telegram(
            "❌ BTD Analysis: Failed to read column A — "
            f"{str(exc)[:100]}"
        )
        return

    tickers = []
    blank_ticker_rows = []

    # Index 0 is assumed to be the header row.
    # start=2 records the actual Google Sheets row number.
    for sheet_row, raw_ticker in enumerate(col_a[1:], start=2):
        ticker = normalise_ticker(raw_ticker)

        if not ticker:
            blank_ticker_rows.append(sheet_row)
            continue

        tickers.append(ticker)

    logger.info(
        "Column A returned %d rows below the header",
        max(len(col_a) - 1, 0),
    )

    logger.info(
        "Found %d non-blank tickers in column A",
        len(tickers),
    )

    if blank_ticker_rows:
        logger.warning(
            "Ignored %d blank ticker rows in column A: %s",
            len(blank_ticker_rows),
            blank_ticker_rows,
        )

    if not tickers:
        logger.warning("No tickers found in column A")

        send_telegram(
            "⚠️ BTD Analysis: No tickers found in column A"
        )
        return

    # Report duplicate tickers without removing them.
    #
    # Duplicates are retained because each occurrence may represent a
    # separate worksheet row. Removing duplicates would cause output rows
    # to become misaligned with the worksheet.

    ticker_counts = {}

    for ticker in tickers:
        ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1

    duplicate_tickers = {
        ticker: count
        for ticker, count in ticker_counts.items()
        if count > 1
    }

    if duplicate_tickers:
        logger.warning(
            "Duplicate tickers detected and retained: %s",
            duplicate_tickers,
        )

    # ── Step 3: Archive previous data ────────────────────────────────────────

    logger.info("Archiving previous data state")

    try:
        all_main_values = sheet.get_all_values()

        existing_history = set()

        try:
            historical_values = hist_sheet.get_all_values()

            if len(historical_values) > 1:
                for historical_row in historical_values[1:]:
                    if len(historical_row) >= 2:
                        historical_date = historical_row[0].strip()
                        historical_ticker = (
                            historical_row[1].strip().upper()
                        )

                        existing_history.add(
                            (historical_date, historical_ticker)
                        )

        except Exception as exc:
            logger.warning(
                "Could not read historical worksheet: %s",
                exc,
            )

        historical_rows_to_add = []

        for row in all_main_values[1:]:
            # Column indexes:
            # A  = 0
            # E  = 4
            # AK = 36
            if len(row) < 37:
                continue

            ticker = row[0].strip().upper()
            btd_value = row[4].strip()
            date_value = row[36].strip()

            history_key = (date_value, ticker)

            if (
                ticker
                and date_value
                and date_value != "N/A"
                and history_key not in existing_history
            ):
                historical_rows_to_add.append(
                    [
                        date_value,
                        ticker,
                        btd_value,
                    ]
                )

                # Prevent duplicate additions during the same execution.
                existing_history.add(history_key)

        if historical_rows_to_add:
            hist_sheet.append_rows(
                historical_rows_to_add,
                value_input_option="USER_ENTERED",
            )

            logger.info(
                "Archived %d historical rows",
                len(historical_rows_to_add),
            )

        else:
            logger.info("Nothing new to archive")

    except Exception as exc:
        # Archiving is treated as non-critical so that the market-data update
        # can continue even when the historical worksheet has a problem.
        logger.warning(
            "Archive step failed but processing will continue: %s",
            exc,
        )

    # ── Step 4: Fetch yfinance data ──────────────────────────────────────────

    last_updated_string = get_sg_date_display()

    records = []
    success_count = 0
    error_count = 0
    failed_tickers = []

    for ticker_number, ticker in enumerate(tickers, start=1):
        logger.info(
            "Processing %d/%d: %s",
            ticker_number,
            len(tickers),
            ticker,
        )

        row = {}

        try:
            ticker_object = yf.Ticker(ticker)
            info = ticker_object.info or {}

            # Earnings date
            earnings_date = "N/A"

            try:
                calendar = ticker_object.calendar

                if (
                    calendar is not None
                    and "Earnings Date" in calendar
                ):
                    earnings_date_values = calendar["Earnings Date"]

                    if earnings_date_values:
                        first_earnings_date = earnings_date_values[0]

                        if hasattr(first_earnings_date, "strftime"):
                            earnings_date = (
                                first_earnings_date.strftime(
                                    "%b %d, %Y"
                                )
                            )
                        else:
                            earnings_date = str(first_earnings_date)

                    else:
                        earnings_date = "No upcoming"

                else:
                    earnings_date = "No upcoming"

            except Exception as earnings_exc:
                logger.warning(
                    "Could not obtain earnings date for %s: %s",
                    ticker,
                    earnings_exc,
                )
                earnings_date = "N/A"

            row["Next_Earnings_Date"] = earnings_date
            row["enterpriseValue"] = info.get("enterpriseValue", "")
            row["totalRevenue"] = info.get("totalRevenue", "")
            row["ebitdaMargins"] = info.get("ebitdaMargins", "")
            row["revenueGrowth"] = info.get("revenueGrowth", "")
            row["grossMargins"] = info.get("grossMargins", "")
            row["No. of FTE"] = info.get("fullTimeEmployees", "")
            row["Last_Updated"] = last_updated_string

            success_count += 1

            logger.info("%s processed successfully", ticker)

        except Exception as exc:
            error_count += 1
            failed_tickers.append(ticker)

            logger.warning(
                "%s failed: %s",
                ticker,
                exc,
            )

            for key in [
                "Next_Earnings_Date",
                "enterpriseValue",
                "totalRevenue",
                "ebitdaMargins",
                "revenueGrowth",
                "grossMargins",
                "No. of FTE",
                "Last_Updated",
            ]:
                row[key] = "ERROR"

        records.append(row)

        # Reduce the likelihood of Yahoo Finance throttling.
        time.sleep(0.5)

    # ── Step 5: Update Google Sheets ─────────────────────────────────────────

    output_columns = [
        "Next_Earnings_Date",
        "enterpriseValue",
        "totalRevenue",
        "ebitdaMargins",
        "revenueGrowth",
        "grossMargins",
        "No. of FTE",
        "Last_Updated",
    ]

    output_dataframe = pd.DataFrame(records)[output_columns]

    output_header = [
        "Next Earnings Date",
        "Enterprise Value",
        "Total Revenue",
        "ebitdaMargins",
        "Revenue Growth",
        "Gross Margin",
        "No. of FTE",
        "Last Updated",
    ]

    try:
        sheet.update(
            range_name=(
                f"{OUTPUT_START_COLUMN}1:"
                f"{OUTPUT_END_COLUMN}1"
            ),
            values=[output_header],
            value_input_option="USER_ENTERED",
        )

        sheet.update(
            range_name=(
                f"{OUTPUT_START_COLUMN}2:"
                f"{OUTPUT_END_COLUMN}{len(output_dataframe) + 1}"
            ),
            values=output_dataframe.astype(str).values.tolist(),
            value_input_option="USER_ENTERED",
        )

        logger.info(
            "Google Sheet updated successfully for %d ticker rows",
            len(output_dataframe),
        )

    except Exception as exc:
        logger.error("Failed to update Google Sheet: %s", exc)

        send_telegram(
            "❌ BTD Analysis: Sheet update failed — "
            f"{str(exc)[:100]}"
        )
        return

    # ── Step 6: Send Telegram summary ────────────────────────────────────────

    singapore_time = get_sg_time()
    total_processed = success_count + error_count

    message = (
        "📈 BTD Analysis Complete\n"
        f"Detected {len(tickers)} tickers from column A.\n"
        f"Updated {total_processed} tickers in '{SHEET_NAME}'.\n"
        f"✅ {success_count} successful\n"
        f"Time: {singapore_time}"
    )

    if error_count > 0:
        failed_ticker_display = ", ".join(failed_tickers)

        message += (
            f"\n⚠️ {error_count} errors"
            f"\nFailed tickers: {failed_ticker_display}"
        )

    if duplicate_tickers:
        duplicate_display = ", ".join(
            f"{ticker} ×{count}"
            for ticker, count in duplicate_tickers.items()
        )

        message += (
            f"\nℹ️ Duplicate rows retained: {duplicate_display}"
        )

    logger.info(message)

    telegram_sent = send_telegram(message)

    if telegram_sent:
        logger.info("Telegram notification sent")
    else:
        logger.warning("Telegram notification failed")

    logger.info(
        "Done. %d OK, %d errors, %d total column-A tickers",
        success_count,
        error_count,
        len(tickers),
    )


if __name__ == "__main__":
    main()
