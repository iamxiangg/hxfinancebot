import yfinance as yf
import pandas as pd
import requests
import os
import time
from datetime import datetime, timezone

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
POSITIONS_CSV = 'positions.csv'
FLAGS_CSV = 'profit_flags.csv'
SALES_LOG_CSV = 'sales_log.csv'
UPDATE_ID_FILE = 'last_update_id.txt'

# Thresholds
DRAWDOWN_BUCKETS = [10, 20, 30, 40]          # percentages
PROFIT_THRESHOLDS = [0.30, 0.50, 1.00]       # decimal

# ---------- TELEGRAM HELPERS ----------
def send_telegram(msg):
    """Send message via bot; fails silently with console log."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        if not resp.ok:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Telegram send error: {e}")

def get_updates(offset=0):
    """Fetch pending updates from Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 10}
    try:
        resp = requests.get(url, params=params, timeout=15)
        return resp.json().get("result", [])
    except:
        return []

# ---------- DATA LOADING ----------
def load_positions():
    return pd.read_csv(POSITIONS_CSV)

def load_flags():
    if not os.path.exists(FLAGS_CSV):
        # columns: Ticker, Threshold (profit or "DD_10","DD_20"...), Fired
        return pd.DataFrame(columns=['Ticker', 'Threshold', 'Fired'])
    return pd.read_csv(FLAGS_CSV)

def save_flags(df):
    df.to_csv(FLAGS_CSV, index=False)

def load_sales_log():
    if not os.path.exists(SALES_LOG_CSV):
        return pd.DataFrame(columns=['Ticker', 'PercentSold', 'GainAtSale', 'Date'])
    return pd.read_csv(SALES_LOG_CSV)

def save_sales_log(df):
    df.to_csv(SALES_LOG_CSV, index=False)

# ---------- CHECK FUNCTIONS (combined for one ticker) ----------
def check_ticker(ticker, entry_price, quantity):
    """Fetch data once and run all checks for a single ticker."""
    stock = yf.Ticker(ticker)

    # --- fetch data ---
    try:
        hist = stock.history(period="1d")
        if hist.empty:
            send_telegram(f"⚠️ *{ticker}*: No data today (delisted or invalid?)")
            return
        current_price = hist['Close'].iloc[-1]
    except Exception as e:
        send_telegram(f"⚠️ *{ticker}*: yfinance error – {str(e)[:100]}")
        return

    # --- Drawdown check ---
    try:
        hist_1y = stock.history(period="1y")
        if hist_1y.empty:
            ath = entry_price
        else:
            ath = max(hist_1y['High'].max(), entry_price)
        drawdown_pct = (ath - current_price) / ath * 100
    except Exception as e:
        drawdown_pct = None
        send_telegram(f"⚠️ *{ticker}*: Cannot compute ATH – {str(e)[:100]}")

    if drawdown_pct is not None:
        # Find which bucket (largest one that is >=)
        crossed_bucket = None
        for bucket in reversed(DRAWDOWN_BUCKETS):
            if drawdown_pct >= bucket:
                crossed_bucket = bucket
                break
        if crossed_bucket is not None:
            # Check if we already alerted for this bucket
            flags = load_flags()
            flag_key = f"DD_{crossed_bucket}"
            already_sent = not flags[(flags['Ticker'] == ticker) & (flags['Threshold'] == flag_key)].empty
            if not already_sent:
                send_telegram(
                    f"🔻 *{ticker}* drawdown {drawdown_pct:.1f}% "
                    f"(ATH ${ath:.2f}, last ${current_price:.2f})"
                )
                # Add flag
                new_row = pd.DataFrame({'Ticker': [ticker], 'Threshold': [flag_key], 'Fired': [True]})
                flags = pd.concat([flags, new_row], ignore_index=True)
                save_flags(flags)

    # --- Profit check ---
    gain_pct = (current_price - entry_price) / entry_price
    flags = load_flags()
    for threshold in PROFIT_THRESHOLDS:
        if gain_pct >= threshold:
            flag_key = f"PR_{threshold*100:.0f}"
            already_sent = not flags[(flags['Ticker'] == ticker) & (flags['Threshold'] == flag_key)].empty
            if not already_sent:
                send_telegram(
                    f"💰 *{ticker}* gained {gain_pct*100:.1f}% "
                    f"(threshold {threshold*100:.0f}%) – consider taking partial profit."
                )
                new_row = pd.DataFrame({'Ticker': [ticker], 'Threshold': [flag_key], 'Fired': [True]})
                flags = pd.concat([flags, new_row], ignore_index=True)
                save_flags(flags)

# ---------- SALE PARSING (unchanged except validation) ----------
def parse_sale_messages():
    offset = 0
    if os.path.exists(UPDATE_ID_FILE):
        with open(UPDATE_ID_FILE) as f:
            offset = int(f.read().strip())

    updates = get_updates(offset)
    sales_log = load_sales_log()
    new_entries = []

    for update in updates:
        update_id = update['update_id']
        offset = update_id + 1
        msg = update.get('message', {}).get('text', '')
        if msg.lower().startswith('sold '):
            try:
                parts = msg.split()
                ticker = parts[1].upper()
                percent_sold = float(parts[2].replace('%', ''))
                gain_at_sale = float(parts[4].replace('%', ''))
                # Basic validation
                if percent_sold <= 0 or gain_at_sale < -100:
                    send_telegram(f"⚠️ Invalid sale message: `{msg}`")
                    continue
                date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                new_entries.append({
                    'Ticker': ticker,
                    'PercentSold': percent_sold,
                    'GainAtSale': gain_at_sale,
                    'Date': date
                })
                send_telegram(f"✅ Logged sale: {ticker} {percent_sold}% at {gain_at_sale:.1f}% gain.")
            except (IndexError, ValueError) as e:
                send_telegram(f"⚠️ Could not parse sale message: {msg} – {str(e)[:100]}")

    if new_entries:
        new_df = pd.DataFrame(new_entries)
        sales_log = pd.concat([sales_log, new_df], ignore_index=True)
        save_sales_log(sales_log)

    # Save offset
    with open(UPDATE_ID_FILE, 'w') as f:
        f.write(str(offset))

# ---------- MAIN ----------
def main():
    positions = load_positions()
    for _, row in positions.iterrows():
        ticker = row['Ticker']
        entry = row['EntryPrice']
        qty = row['Quantity']
        check_ticker(ticker, entry, qty)
        time.sleep(0.5)   # polite delay

    # Parse sales after all checks
    parse_sale_messages()

if __name__ == '__main__':
    main()
