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
PROFIT_THRESHOLDS = [0.30, 0.50, 1.00]       # decimal

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
    return pd.read_csv(POSITIONS_CSV)

def load_flags():
    if not os.path.exists(FLAGS_CSV):
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

# ---------- HELPERS FOR PROFIT PLAN ----------
def get_cumulative_sold(ticker, sales_df):
    """Return total % sold for ticker (decimal, e.g. 0.10 = 10%)."""
    ticker_sales = sales_df[sales_df['Ticker'] == ticker]
    if ticker_sales.empty:
        return 0.0
    return ticker_sales['PercentSold'].sum() / 100.0   # convert % to decimal

def get_next_target(ticker, sales_df, current_gain=None):
    """
    Return a string describing the next profit-taking target.
    If all tranches are completed, return None.
    """
    cum_sold = get_cumulative_sold(ticker, sales_df)
    total_planned = 0.0
    for threshold, pct in PROFIT_PLAN:
        total_planned += pct
        # If we haven't sold enough to cover this tranche, this is the next target
        if cum_sold < total_planned:
            # Already sold part of this tranche? Suggest remaining.
            already_in_this_tranche = cum_sold - (total_planned - pct)
            if already_in_this_tranche < pct:
                remaining_pct = pct - already_in_this_tranche
                # If we are exactly at this threshold or above, suggest selling the remaining
                if current_gain is not None and current_gain >= threshold:
                    return f"Sell {remaining_pct*100:.0f}% (to complete the {pct*100:.0f}% tranche at +{threshold*100:.0f}% gain)"
                else:
                    return f"Next: sell {remaining_pct*100:.0f}% at +{threshold*100:.0f}% gain"
            else:
                # This tranche is fully sold, move to next
                continue
    # All tranches completed
    return None

# ---------- CHECK TICKER ----------
def check_ticker(ticker, entry_price, quantity):
    stock = yf.Ticker(ticker)
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

    # --- Profit check with plan suggestions ---
    gain_pct = (current_price - entry_price) / entry_price
    sales_log = load_sales_log()   # needed for cumulative sold
    cum_sold = get_cumulative_sold(ticker, sales_log)

    flags = load_flags()
    for threshold, pct in PROFIT_PLAN:
        if gain_pct >= threshold:
            flag_key = f"PR_{threshold*100:.0f}"
            already_sent = not flags[(flags['Ticker'] == ticker) & (flags['Threshold'] == flag_key)].empty
            if not already_sent:
                # Check if this tranche is already fully sold
                total_planned_before = sum(p for t,p in PROFIT_PLAN if t < threshold)
                if cum_sold < total_planned_before + pct:
                    # Suggest selling the remaining
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
                        # Already sold full tranche; still send generic alert?
                        send_telegram(f"💰 *{ticker}* gained {gain_pct*100:.1f}% (threshold +{threshold*100:.0f}%)")
                else:
                    # Tranche already completed, just generic alert
                    send_telegram(f"💰 *{ticker}* gained {gain_pct*100:.1f}% (threshold +{threshold*100:.0f}%)")
                # Add flag
                new_row = pd.DataFrame({'Ticker': [ticker], 'Threshold': [flag_key], 'Fired': [True]})
                flags = pd.concat([flags, new_row], ignore_index=True)
                save_flags(flags)
        else:
            # If the gain hasn't crossed this threshold, no need to flag
            pass

# ---------- SALE PARSING ----------
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
                sales_log_update = load_sales_log()   # reload with new data
                # We haven't saved yet, so add the new entry temporarily
                temp_sales = pd.concat([sales_log, pd.DataFrame(new_entries)], ignore_index=True)
                next_target = get_next_target(ticker, temp_sales, current_gain=gain_at_sale)
                if next_target:
                    send_telegram(f"📝 *{ticker}*: {next_target}")
                else:
                    send_telegram(f"✅ *{ticker}*: All profit-taking tranches completed – use trailing stop now.")
            except (IndexError, ValueError) as e:
                send_telegram(f"⚠️ Could not parse sale message: {msg} – {str(e)[:100]}")

    if new_entries:
        new_df = pd.DataFrame(new_entries)
        sales_log = pd.concat([sales_log, new_df], ignore_index=True)
        save_sales_log(sales_log)

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
        time.sleep(0.5)

    parse_sale_messages()

if __name__ == '__main__':
    main()
