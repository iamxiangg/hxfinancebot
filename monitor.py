import yfinance as yf
import pandas as pd
import requests
import os
import time
from datetime import datetime, timezone

# ---------- CONFIG ----------
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
POSITIONS_CSV = 'positions.csv'
FLAGS_CSV = 'profit_flags.csv'
SALES_LOG_CSV = 'sales_log.csv'
UPDATE_ID_FILE = 'last_update_id.txt'

# Thresholds
DRAWDOWN_BUCKETS = [10, 20, 30, 40]          # percentages
PROFIT_THRESHOLDS = [0.30, 0.50, 1.00]       # decimal (kept for possible future use)

# Profit-taking plan: (gain threshold, % of position to sell)
PROFIT_PLAN = [
    (0.30, 0.10),   # sell 10% at +30%
    (0.60, 0.15),   # sell 15% at +60%
    (1.00, 0.25),   # sell 25% at +100%
]
# After the last tranche, remainder rides with trailing stop.

# ---------- TELEGRAM HELPERS ----------
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        if not resp.ok:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Telegram send error: {e}")

def get_updates(offset=0):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 10}
    try:
        resp = requests.get(url, params=params, timeout=15)
        return resp.json().get("result", [])
    except:
        return []

# ---------- DATA LOADING ----------
def load_positions():
    df = pd.read_csv(POSITIONS_CSV)
    print(f"DEBUG: loaded {len(df)} positions from {POSITIONS_CSV}")
    return df

def load_flags():
    if not os.path.exists(FLAGS_CSV):
        print(f"DEBUG: {FLAGS_CSV} not found, creating empty.")
        return pd.DataFrame(columns=['Ticker', 'Threshold', 'Fired'])
    df = pd.read_csv(FLAGS_CSV)
    print(f"DEBUG: loaded {len(df)} flag rows")
    return df

def save_flags(df):
    df.to_csv(FLAGS_CSV, index=False)
    print(f"DEBUG: saved {len(df)} flags to {FLAGS_CSV}")

def load_sales_log():
    if not os.path.exists(SALES_LOG_CSV):
        print(f"DEBUG: {SALES_LOG_CSV} not found, creating empty.")
        return pd.DataFrame(columns=['Ticker', 'PercentSold', 'GainAtSale', 'Date'])
    df = pd.read_csv(SALES_LOG_CSV)
    print(f"DEBUG: loaded {len(df)} sales log rows")
    return df

def save_sales_log(df):
    df.to_csv(SALES_LOG_CSV, index=False)
    print(f"DEBUG: saved {len(df)} sales to {SALES_LOG_CSV}")

# ---------- HELPERS FOR PROFIT PLAN ----------
def get_cumulative_sold(ticker, sales_df):
    ticker_sales = sales_df[sales_df['Ticker'] == ticker]
    if ticker_sales.empty:
        return 0.0
    return ticker_sales['PercentSold'].sum() / 100.0

def get_next_target(ticker, sales_df, current_gain=None):
    cum_sold = get_cumulative_sold(ticker, sales_df)
    total_planned = 0.0
    for threshold, pct in PROFIT_PLAN:
        total_planned += pct
        if cum_sold < total_planned:
            already_in_this_tranche = cum_sold - (total_planned - pct)
            if already_in_this_tranche < pct:
                remaining_pct = pct - already_in_this_tranche
                if current_gain is not None and current_gain >= threshold:
                    return f"Sell {remaining_pct*100:.0f}% (to complete the {pct*100:.0f}% tranche at +{threshold*100:.0f}% gain)"
                else:
                    return f"Next: sell {remaining_pct*100:.0f}% at +{threshold*100:.0f}% gain"
            else:
                continue
    return None

# ---------- CHECK TICKER ----------
def check_ticker(ticker, entry_price, quantity):
    print(f"DEBUG: checking {ticker} (entry ${entry_price:.2f}, qty {quantity})")
    stock = yf.Ticker(ticker)
    try:
        hist = stock.history(period="1d")
        if hist.empty:
            send_telegram(f"⚠️ *{ticker}*: No data today (delisted or invalid?)")
            return
        current_price = hist['Close'].iloc[-1]
        print(f"DEBUG: {ticker} current price ${current_price:.2f}")
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
        print(f"DEBUG: {ticker} drawdown {drawdown_pct:.1f}%")
    except Exception as e:
        drawdown_pct = None
        send_telegram(f"⚠️ *{ticker}*: Cannot compute ATH – {str(e)[:100]}")

    if drawdown_pct is not None:
        crossed_bucket = None
        for bucket in reversed(DRAWDOWN_BUCKETS):
            if drawdown_pct >= bucket:
                crossed_bucket = bucket
                break
        if crossed_bucket is not None:
            flags = load_flags()
            flag_key = f"DD_{crossed_bucket}"
            already_sent = not flags[(flags['Ticker'] == ticker) & (flags['Threshold'] == flag_key)].empty
            if not already_sent:
                send_telegram(
                    f"🔻 *{ticker}* drawdown {drawdown_pct:.1f}% "
                    f"(ATH ${ath:.2f}, last ${current_price:.2f})"
                )
                new_row = pd.DataFrame({'Ticker': [ticker], 'Threshold': [flag_key], 'Fired': [True]})
                flags = pd.concat([flags, new_row], ignore_index=True)
                save_flags(flags)
                print(f"DEBUG: drawdown flag sent for {ticker} at {crossed_bucket}%")
        else:
            print(f"DEBUG: {ticker} no drawdown bucket crossed")

    # --- Profit check with plan suggestions ---
    gain_pct = (current_price - entry_price) / entry_price
    print(f"DEBUG: {ticker} gain {gain_pct*100:.1f}%")
    sales_log = load_sales_log()
    cum_sold = get_cumulative_sold(ticker, sales_log)
    print(f"DEBUG: {ticker} cumulative sold {cum_sold*100:.1f}%")

    flags = load_flags()
    for threshold, pct in PROFIT_PLAN:
        if gain_pct >= threshold:
            flag_key = f"PR_{threshold*100:.0f}"
            already_sent = not flags[(flags['Ticker'] == ticker) & (flags['Threshold'] == flag_key)].empty
            if not already_sent:
                total_planned_before = sum(p for t,p in PROFIT_PLAN if t < threshold)
                if cum_sold < total_planned_before + pct:
                    already_in_tranche = max(0, cum_sold - total_planned_before)
                    remaining = pct - already_in_tranche
                    if remaining > 0.01:
                        send_telegram(
                            f"💰 *{ticker}* gained {gain_pct*100:.1f}% "
                            f"(threshold +{threshold*100:.0f}% reached)\n"
                            f"👉 Sell {remaining*100:.0f}% of your position "
                            f"(={pct*100:.0f}% tranche, already sold {already_in_tranche*100:.0f}%)"
                        )
                    else:
                        send_telegram(f"💰 *{ticker}* gained {gain_pct*100:.1f}% (threshold +{threshold*100:.0f}%)")
                else:
                    send_telegram(f"💰 *{ticker}* gained {gain_pct*100:.1f}% (threshold +{threshold*100:.0f}%)")
                new_row = pd.DataFrame({'Ticker': [ticker], 'Threshold': [flag_key], 'Fired': [True]})
                flags = pd.concat([flags, new_row], ignore_index=True)
                save_flags(flags)
                print(f"DEBUG: profit flag sent for {ticker} at {threshold*100:.0f}%")
        else:
            print(f"DEBUG: {ticker} gain below {threshold*100:.0f}% – no action")

    time.sleep(0.5)

# ---------- SALE PARSING ----------
def parse_sale_messages():
    print("DEBUG: parsing sale messages")
    offset = 0
    if os.path.exists(UPDATE_ID_FILE):
        with open(UPDATE_ID_FILE) as f:
            offset = int(f.read().strip())
        print(f"DEBUG: last update ID = {offset}")

    updates = get_updates(offset)
    print(f"DEBUG: received {len(updates)} updates")
    sales_log = load_sales_log()
    new_entries = []

    for update in updates:
        update_id = update['update_id']
        offset = update_id + 1
        msg = update.get('message', {}).get('text', '')
        print(f"DEBUG: update {update_id}: '{msg[:50]}'")
        if msg.lower().startswith('sold '):
            try:
                parts = msg.split()
                ticker = parts[1].upper()
                percent_sold = float(parts[2].replace('%', ''))
                gain_at_sale = float(parts[4].replace('%', ''))
                print(f"DEBUG: parsed sale: {ticker} {percent_sold}% at {gain_at_sale}%")
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
                send_telegram(
                    f"✅ Logged sale: {ticker} {percent_sold}% at {gain_at_sale:.1f}% gain."
                )
                # After logging, suggest next target
                temp_sales = pd.concat([sales_log, pd.DataFrame(new_entries)], ignore_index=True)
                next_target = get_next_target(ticker, temp_sales, current_gain=gain_at_sale)
                if next_target:
                    send_telegram(f"📝 *{ticker}*: {next_target}")
                else:
                    send_telegram(f"✅ *{ticker}*: All profit-taking tranches completed – use trailing stop now.")
            except (IndexError, ValueError) as e:
                send_telegram(f"⚠️ Could not parse sale message: {msg} – {str(e)[:100]}")
        else:
            print(f"DEBUG: update {update_id} ignored (not 'sold' command)")

    if new_entries:
        new_df = pd.DataFrame(new_entries)
        sales_log = pd.concat([sales_log, new_df], ignore_index=True)
        save_sales_log(sales_log)
        print(f"DEBUG: saved {len(new_entries)} new sale(s)")

    with open(UPDATE_ID_FILE, 'w') as f:
        f.write(str(offset))
    print("DEBUG: finished parsing sale messages")

# ---------- MAIN ----------
def main():
    print("DEBUG: script started")
    
    # Test Telegram connectivity (optional – comment out after first run)
    # send_telegram("Test message from monitor.py – script is running.")
    
    positions = load_positions()
    print(f"DEBUG: positions shape = {positions.shape}")
    if positions.empty:
        print("WARNING: positions.csv is empty or has no data rows.")
        return
    
    for _, row in positions.iterrows():
        ticker = row['Ticker']
        entry = row['EntryPrice']
        qty = row['Quantity']
        print(f"DEBUG: processing {ticker}...")
        check_ticker(ticker, entry, qty)

    print("DEBUG: finished checking all tickers, now parsing incoming messages")
    parse_sale_messages()
    print("DEBUG: script finished successfully")

if __name__ == '__main__':
    main()
