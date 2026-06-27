#!/usr/bin/env python3
"""
gh_btd.py — GitHub Actions BTD Analysis
========================================
Fetches tickers from Google Sheets, runs BTD analysis,
sends Telegram summary. 
Uses GitHub Secrets for GCP credentials.
"""

import os
import sys
import json
import time
import logging
import pandas as pd
import yfinance as yf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from io import StringIO

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SHEET_NAME = "Xiang Stock Analysis"
WORKSHEET_NAME = "Stock Summary USD"
HIST_WORKSHEET_NAME = "Historical_BTD_Metric"

# ─── Helpers ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Send notification via Telegram bot using env vars."""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    try:
        import requests
        if len(message) > 4000:
            message = message[:3997] + "..."
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, data={'chat_id': chat_id, 'text': message}, timeout=10)
        if r.status_code != 200:
            logger.error(f"Telegram API error: {r.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

def get_sg_time() -> str:
    """Return Singapore time formatted."""
    from datetime import timezone, timedelta
    sg_tz = timezone(timedelta(hours=8))
    return datetime.now(sg_tz).strftime("%Y-%m-%d %H:%M:%S %z")

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting BTD Analysis")

    # ── Step 1: Authenticate to Google Sheets using GitHub Secret ──
    creds_json = os.getenv('GCP_SERVICE_ACCOUNT_FILE')
    if not creds_json:
        logger.error("Environment variable GCP_SERVICE_ACCOUNT_FILE is missing")
        send_telegram("❌ BTD Analysis: Secret GCP_SERVICE_ACCOUNT_FILE not found")
        return

    try:
        # Parse the JSON string from the secret directly into a dictionary
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        client = gspread.authorize(creds)
        
        workbook = client.open(SHEET_NAME)
        sheet = workbook.worksheet(WORKSHEET_NAME)
        hist_sheet = workbook.worksheet(HIST_WORKSHEET_NAME)
    except Exception as e:
        logger.error(f"Google Sheets auth failed: {e}")
        send_telegram(f"❌ BTD Analysis: Google Sheets auth failed — {str(e)[:100]}")
        return

    # ── Step 2: Read Tickers & BTD (Column A & E) ──
    try:
        col_a = sheet.col_values(1)
        col_e = sheet.col_values(5)
    except Exception as e:
        logger.error(f"Failed to read sheet: {e}")
        send_telegram(f"❌ BTD Analysis: Failed to read sheet — {str(e)[:100]}")
        return

    ticker_btd_pairs = []
    for i in range(1, min(len(col_a), len(col_e))):
        ticker = col_a[i].strip().upper() if i < len(col_a) else ""
        btd = col_e[i].strip() if i < len(col_e) else ""
        if ticker:
            ticker_btd_pairs.append((ticker, btd))

    if not ticker_btd_pairs:
        logger.warning("No tickers found")
        send_telegram("⚠️ BTD Analysis: No tickers found in sheet")
        return

    tickers = [p[0] for p in ticker_btd_pairs]
    logger.info(f"Found {len(tickers)} tickers")

    # ── Step 3: Archive previous data ──
    logger.info("Archiving previous data state...")
    try:
        all_main_values = sheet.get_all_values()

        existing_history = set()
        try:
            hist_vals = hist_sheet.get_all_values()
            if len(hist_vals) > 1:
                for r in hist_vals[1:]:
                    if len(r) >= 2:
                        existing_history.add((r[0].strip(), r[1].strip().upper()))
        except Exception as e:
            logger.warning(f"Could not read history: {e}")

        hist_rows_to_add = []
        for row in all_main_values[1:]:
            if len(row) >= 37:
                ticker = row[0].strip().upper()
                btd_val = row[4].strip()
                date_val = row[36].strip()
                if ticker and date_val and date_val != "N/A" and (date_val, ticker) not in existing_history:
                    hist_rows_to_add.append([date_val, ticker, btd_val])

        if hist_rows_to_add:
            hist_sheet.append_rows(hist_rows_to_add, value_input_option="USER_ENTERED")
            logger.info(f"Archived {len(hist_rows_to_add)} rows")
        else:
            logger.info("Nothing new to archive")
    except Exception as e:
        logger.warning(f"Archive step failed (non-critical): {e}")

    # ── Step 4: Fetch yfinance data ──
    last_updated_str = datetime.now().strftime("%b %d, %Y")
    records = []
    success_count = 0
    error_count = 0

    for ticker in tickers:
        logger.info(f"  → {ticker}")
        row = {}

        try:
            t = yf.Ticker(ticker)
            info = t.info

            # Earnings date
            earnings_date = "N/A"
            try:
                cal = t.calendar
                if cal is not None and 'Earnings Date' in cal:
                    e_list = cal['Earnings Date']
                    if e_list:
                        earnings_date = e_list[0].strftime("%b %d, %Y")
                else:
                    earnings_date = "No upcoming"
            except:
                earnings_date = "N/A"

            row["Next_Earnings_Date"] = earnings_date
            row["enterpriseValue"] = info.get("enterpriseValue", "")
            row["totalRevenue"] = info.get("totalRevenue", "")
            row["ebitdaMargins"] = info.get("ebitdaMargins", "")
            row["revenueGrowth"] = info.get("revenueGrowth", "")
            row["grossMargins"] = info.get("grossMargins", "")
            row["No. of FTE"] = info.get("fullTimeEmployees", "")
            row["Last_Updated"] = last_updated_str

            success_count += 1
            logger.info(f"  ✓ {ticker} OK")

        except Exception as e:
            error_count += 1
            logger.warning(f"  ✗ {ticker} ERROR: {e}")
            for key in ["Next_Earnings_Date", "enterpriseValue", "totalRevenue",
                        "ebitdaMargins", "revenueGrowth", "grossMargins",
                        "No. of FTE", "Last_Updated"]:
                row[key] = "ERROR"

        records.append(row)
        time.sleep(0.5)

    # ── Step 5: Update Google Sheets ──
    cols = ["Next_Earnings_Date", "enterpriseValue", "totalRevenue", "ebitdaMargins",
            "revenueGrowth", "grossMargins", "No. of FTE", "Last_Updated"]
    df = pd.DataFrame(records)[cols]

    header = ["Next Earnings Date", "Enterprise Value", "Total Revenue",
              "ebitdaMargins", "Revenue Growth", "Gross Margin", "No. of FTE", "Last Updated"]

    try:
        sheet.update(range_name="AD1:AK1", values=[header], value_input_option="USER_ENTERED")
        sheet.update(range_name=f"AD2:AK{len(df) + 1}", values=df.astype(str).values.tolist(),
                     value_input_option="USER_ENTERED")
        logger.info("Sheet updated successfully")
    except Exception as e:
        logger.error(f"Failed to update sheet: {e}")
        send_telegram(f"❌ BTD Analysis: Sheet update failed — {str(e)[:100]}")
        return

    # ── Step 6: Send Telegram Summary ──
    sg_time = get_sg_time()
    total = success_count + error_count

    message = (
        f"📈 BTD Analysis Complete\n"
        f"Updated {total} tickers in '{SHEET_NAME}'.\n"
        f"Time: {sg_time}"
    )

    if error_count > 0:
        message += f"\n⚠️ {error_count} errors (see logs)"

    logger.info(message)
    sent = send_telegram(message)

    if sent:
        logger.info("Telegram notification sent")
    else:
        logger.warning("Telegram notification failed")

    logger.info(f"Done. {success_count} OK, {error_count} errors")

if __name__ == '__main__':
    main()
