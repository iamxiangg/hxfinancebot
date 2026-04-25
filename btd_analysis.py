import os
import time
import json
import pandas as pd
import yfinance as yf
import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz

# -------------------------------------------------
# 1. Google Sheets Setup (GitHub Action Compatible)
# -------------------------------------------------
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Create a temporary file for the credentials from the Environment Variable
gcp_json_str = os.environ.get("GCP_JSON")
with open('creds.json', 'w') as f:
    f.write(gcp_json_str)

creds = ServiceAccountCredentials.from_json_keyfile_name('creds.json', SCOPE)
client = gspread.authorize(creds)
workbook = client.open("Xiang Stock Analysis")
sheet = workbook.worksheet("Stock Summary USD")
hist_sheet = workbook.worksheet("Historical_BTD_Metric")

# Telegram Setup
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

# -------------------------------------------------
# 2. Timing Logic
# -------------------------------------------------
sg_tz = pytz.timezone("Asia/Singapore")
now_sg_dt = datetime.now(sg_tz)
last_updated_str = now_sg_dt.strftime("%b %d, %Y")
now_sg_str = now_sg_dt.strftime("%Y-%m-%d %H:%M:%S %Z")

print(f"Script started at: {now_sg_str}", flush=True)

# -------------------------------------------------
# 3. Read Tickers & Archive (Your existing logic)
# -------------------------------------------------
def get_tickers_and_btd():
    col_a = sheet.col_values(1)
    col_e = sheet.col_values(5)
    pairs = []
    for i in range(1, min(len(col_a), len(col_e))):
        ticker = col_a[i].strip().upper() if i < len(col_a) else ""
        btd = col_e[i].strip() if i < len(col_e) else ""
        if ticker:
            pairs.append((ticker, btd))
    return pairs

ticker_btd_pairs = get_tickers_and_btd()
if not ticker_btd_pairs:
    print("No tickers found. Exiting.", flush=True)
    raise SystemExit

tickers = [p[0] for p in ticker_btd_pairs]

# --- ARCHIVING LOGIC ---
print("Archiving previous data state...", flush=True)
all_main_values = sheet.get_all_values()
existing_history = set()
try:
    hist_vals = hist_sheet.get_all_values()
    if len(hist_vals) > 1:
        for r in hist_vals[1:]:
            if len(r) >= 2: existing_history.add((r[0].strip(), r[1].strip().upper()))
except: pass

hist_rows_to_add = []
for row in all_main_values[1:]:
    if len(row) >= 37:
        ticker, btd_val, date_val = row[0].strip().upper(), row[4].strip(), row[36].strip()
        if ticker and date_val and date_val != "N/A" and (date_val, ticker) not in existing_history:
            hist_rows_to_add.append([date_val, ticker, btd_val])

if hist_rows_to_add:
    hist_sheet.append_rows(hist_rows_to_add, value_input_option="USER_ENTERED")

# -------------------------------------------------
# 4. Fetch New Data (YFinance)
# -------------------------------------------------
records = []
for ticker in tickers:
    print(f"Fetching: {ticker}", flush=True)
    row = {}
    try:
        t = yf.Ticker(ticker)
        info = t.info
        
        # Earnings date logic
        earnings_date = "N/A"
        try:
            cal = t.calendar
            if cal is not None and 'Earnings Date' in cal:
                earnings_date = cal['Earnings Date'][0].strftime("%b %d, %Y")
        except: pass

        row.update({
            "Next_Earnings_Date": earnings_date,
            "enterpriseValue": info.get("enterpriseValue", ""),
            "totalRevenue": info.get("totalRevenue", ""),
            "ebitdaMargins": info.get("ebitdaMargins", ""),
            "revenueGrowth": info.get("revenueGrowth", ""),
            "grossMargins": info.get("grossMargins", ""),
            "No. of FTE": info.get("fullTimeEmployees", ""),
            "Last_Updated": last_updated_str
        })
    except Exception as e:
        print(f"Error {ticker}: {e}")
        for key in ["Next_Earnings_Date", "enterpriseValue", "totalRevenue", "ebitdaMargins", "revenueGrowth", "grossMargins", "No. of FTE", "Last_Updated"]:
            row[key] = "ERROR"
    
    records.append(row)
    time.sleep(0.7)

# -------------------------------------------------
# 5. Update Google Sheet
# -------------------------------------------------
cols = ["Next_Earnings_Date", "enterpriseValue", "totalRevenue", "ebitdaMargins", "revenueGrowth", "grossMargins", "No. of FTE", "Last_Updated"]
df = pd.DataFrame(records)[cols]
header = ["Next Earnings Date", "Enterprise Value", "Total Revenue", "ebitdaMargins", "Revenue Growth", "Gross Margin", "No. of FTE", "Last Updated"]

sheet.update(range_name="AD1:AK1", values=[header], value_input_option="USER_ENTERED")
sheet.update(range_name=f"AD2:AK{len(df) + 1}", values=df.astype(str).values.tolist(), value_input_option="USER_ENTERED")

# --- Telegram Final Alert ---
summary = f"📈 **BTD Analysis Complete**\nUpdated {len(tickers)} tickers in 'Xiang Stock Analysis'.\nTime: {now_sg_str}"
send_telegram(summary)

# Cleanup
if os.path.exists("creds.json"):
    os.remove("creds.json")

print(f"SUCCESS! Sheet updated at {now_sg_str}", flush=True)
