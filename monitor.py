import yfinance as yf
import pandas as pd
import requests
import os
import time
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
POSITIONS_CSV = 'positions.csv'
SALES_LOG_CSV = 'sales_log.csv'
UPDATE_ID_FILE = 'last_update_id.txt'

# ---------- THRESHOLDS ----------
DRAWDOWN_BUCKETS = [20, 30, 40]          # % drop from ATH
# Sell plan for drawdown: (drawdown%, fraction of position to sell_AT_this_level)
# Cumulative sell fractions: 20%->1/3, 30%->2/3, 40%->1.0
DOWNSIDE_PLAN = [
    (0.20, 1/3),
    (0.30, 1/3),
    (0.40, 1/3),   # last tranche – total 100%
]

PROFIT_PLAN = [
    (0.30, 0.10),
    (0.60, 0.15),
    (1.00, 0.25),
]

# ---------- TELEGRAM ----------
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.ok:
            print(f"DEBUG: Telegram message sent: {msg[:50]}")
        else:
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

# ---------- DATA ----------
def load_positions():
    df = pd.read_csv(POSITIONS_CSV)
    print(f"DEBUG: loaded {len(df)} positions")
    return df

def load_sales_log():
    if not os.path.exists(SALES_LOG_CSV):
        return pd.DataFrame(columns=['Ticker', 'PercentSold', 'GainAtSale', 'Date'])
    return pd.read_csv(SALES_LOG_CSV)

def save_sales_log(df):
    df.to_csv(SALES_LOG_CSV, index=False)
    print(f"DEBUG: saved {len(df)} sales")

# ---------- HELPERS ----------
def get_cumulative_sold(ticker, sales_df):
    ticker_sales = sales_df[sales_df['Ticker'] == ticker]
    if ticker_sales.empty:
        return 0.0
    return ticker_sales['PercentSold'].sum() / 100.0

def get_next_profit_target(ticker, sales_df, current_gain=None):
    """Returns a string like 'sell 10% at +30%' or None if completed."""
    cum_sold = get_cumulative_sold(ticker, sales_df)
    total_planned = 0.0
    for threshold, pct in PROFIT_PLAN:
        total_planned += pct
        if cum_sold < total_planned:
            already = cum_sold - (total_planned - pct)
            if already < pct:
                remaining = pct - already
                if current_gain is not None and current_gain >= threshold:
                    return f"Sell {remaining*100:.0f}% (to complete {pct*100:.0f}% tranche at +{threshold*100:.0f}%)"
                else:
                    return f"Next: sell {remaining*100:.0f}% at +{threshold*100:.0f}%"
    return None

def get_next_downside_target(ticker, sales_df, current_drawdown):
    """Returns a string for which drawdown bucket needs selling, or None."""
    cum_sold = get_cumulative_sold(ticker, sales_df)
    total_planned = 0.0
    for dd, frac in DOWNSIDE_PLAN:
        total_planned += frac
        if cum_sold < total_planned:
            already = cum_sold - (total_planned - frac)
            if already < frac:
                remaining = frac - already
                if current_drawdown >= dd * 100:
                    return f"Sell {remaining*100:.0f}% (drawdown {current_drawdown:.1f}% from ATH)"
                else:
                    return f"Next: sell {remaining*100:.0f}% if drawdown reaches {dd*100:.0f}%"
    return None

# ---------- CHECK TICKER (returns list of action lines) ----------
def check_ticker(ticker, entry_price, quantity):
    print(f"DEBUG: checking {ticker}")
    stock = yf.Ticker(ticker)
    try:
        hist = stock.history(period="1d")
        if hist.empty:
            send_telegram(f"⚠️ *{ticker}*: No data today")
            return []
        current_price = hist['Close'].iloc[-1]
    except Exception as e:
        send_telegram(f"⚠️ *{ticker}*: yfinance error – {str(e)[:100]}")
        return []

    # Compute ATH (1 year or entry)
    try:
        hist_1y = stock.history(period="1y")
        ath = max(hist_1y['High'].max(), entry_price) if not hist_1y.empty else entry_price
        drawdown_pct = (ath - current_price) / ath * 100
    except Exception as e:
        drawdown_pct = None
        send_telegram(f"⚠️ *{ticker}*: Cannot compute ATH – {str(e)[:100]}")

    gain_pct = (current_price - entry_price) / entry_price
    sales_log = load_sales_log()
    cum_sold = get_cumulative_sold(ticker, sales_log)
    print(f"DEBUG: {ticker} gain {gain_pct*100:.1f}%, drawdown {drawdown_pct:.1f}%, cum_sold {cum_sold*100:.1f}%")

    action_lines = []

    # --- DOWNSIDE ALERTS (daily reminder until sold) ---
    if drawdown_pct is not None:
        for dd, frac in DOWNSIDE_PLAN:
            if drawdown_pct >= dd * 100:
                total_planned_before = sum(f for d, f in DOWNSIDE_PLAN if d < dd)
                if cum_sold < total_planned_before + frac:
                    already = max(0, cum_sold - total_planned_before)
                    remaining = frac - already
                    if remaining > 0.01:
                        line = f"🔻 {ticker}: sell {remaining*100:.0f}% (drawdown {drawdown_pct:.1f}% from ATH)"
                        action_lines.append(line)
                        # Also individual alert
                        send_telegram(
                            f"🔻 *{ticker}* drawdown {drawdown_pct:.1f}% "
                            f"(threshold {dd*100:.0f}% reached)\n"
                            f"👉 Sell {remaining*100:.0f}% of your position "
                            f"({frac*100:.0f}% tranche, already sold {already*100:.0f}%)"
                        )

    # --- UPSIDE ALERTS (daily reminder until sold) ---
    for threshold, pct in PROFIT_PLAN:
        if gain_pct >= threshold:
            total_planned_before = sum(p for t, p in PROFIT_PLAN if t < threshold)
            if cum_sold < total_planned_before + pct:
                already = max(0, cum_sold - total_planned_before)
                remaining = pct - already
                if remaining > 0.01:
                    line = f"💰 {ticker}: sell {remaining*100:.0f}% (at +{threshold*100:.0f}% gain)"
                    action_lines.append(line)
                    send_telegram(
                        f"💰 *{ticker}* gained {gain_pct*100:.1f}% "
                        f"(threshold +{threshold*100:.0f}% reached)\n"
                        f"👉 Sell {remaining*100:.0f}% of your position "
                        f"(={pct*100:.0f}% tranche, already sold {already*100:.0f}%)"
                    )

    time.sleep(0.5)
    return action_lines

# ---------- SALE PARSING ----------
def parse_sale_messages():
    print("DEBUG: parsing sale messages")
    offset = 0
    if os.path.exists(UPDATE_ID_FILE):
        with open(UPDATE_ID_FILE) as f:
            offset = int(f.read().strip())

    updates = get_updates(offset)
    print(f"DEBUG: received {len(updates)} updates")
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
                # Provide next target (profit or downside)
                # Use the current gain from the sale – we assume this is the latest.
                # For downside, we need drawdown, but we don't have it here.
                # We'll simply use the general get_next_* functions.
                temp_sales = pd.concat([sales_log, pd.DataFrame(new_entries)], ignore_index=True)
                next_profit = get_next_profit_target(ticker, temp_sales, current_gain=gain_at_sale)
                if next_profit:
                    send_telegram(f"📈 *{ticker}*: {next_profit}")
                # Downside reminder independent of gain – the next daily run will handle it.
                # Optionally check downside if drawdown is known? Not needed.
            except (IndexError, ValueError) as e:
                send_telegram(f"⚠️ Could not parse sale message: {msg} – {str(e)[:100]}")

    if new_entries:
        new_df = pd.DataFrame(new_entries)
        sales_log = pd.concat([sales_log, new_df], ignore_index=True)
        save_sales_log(sales_log)

    with open(UPDATE_ID_FILE, 'w') as f:
        f.write(str(offset))
    print("DEBUG: finished parsing sale messages")

# ---------- MAIN ----------
def main():
    print("DEBUG: script started")
    positions = load_positions()
    if positions.empty:
        print("WARNING: positions.csv is empty.")
        return

    all_actions = []

    for _, row in positions.iterrows():
        ticker = row['Ticker']
        entry = row['EntryPrice']
        qty = row['Quantity']
        actions = check_ticker(ticker, entry, qty)
        if actions:
            all_actions.extend(actions)

    # Send summary
    if all_actions:
        msg = "📋 *Actions needed today:*\n\n" + "\n".join(all_actions)
        send_telegram(msg)
    else:
        send_telegram("✅ No actions required today.")

    print("DEBUG: now parsing incoming messages")
    parse_sale_messages()
    print("DEBUG: script finished successfully")

if __name__ == '__main__':
    main()
