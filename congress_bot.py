import os
import csv
import time
import logging
import asyncio
from datetime import datetime, timedelta
from io import StringIO
from collections import Counter

import requests
import yfinance as yf
from telegram import Bot

# ── Logging Setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("congress_bot.log", encoding="utf-8")
    ]
)

# ── Configuration ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID')

MAX_DAYS_AGO   = 45      # Lookback window for actual PURCHASE date
MAX_PCT_CHANGE = 8       # Hard ceiling: Ignore single trades that ran up > 8%
FRESH_DAYS     = 21      # Green window threshold (bought within 3 weeks)
FRESH_PCT      = 3       # Green price change ceiling (flat, down, or up < 3%)

RAW_KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"

TELEGRAM_CHAR_LIMIT = 3800   
CHUNK_PADDING       = 10     
INTER_CHUNK_DELAY   = 1.5    

INDUSTRY_CACHE = {}

# ── Core Processing Functions ───────────────────────────────────────────────

def fetch_trades():
    """Fetch raw entries focusing on equity stock purchases across House/Senate structures."""
    try:
        logging.info(f"Connecting to Kadoa GitHub Storage Core: {RAW_KADOA_URL}")
        resp = requests.get(RAW_KADOA_URL, timeout=30)
        resp.raise_for_status()
        raw_data = resp.json()
    except requests.RequestException as e:
        logging.error(f"Failed to stream raw data payload from GitHub CDN: {e}")
        return []

    trades = []
    for item in raw_data:
        tx_type = str(item.get('transaction_type', item.get('type', ''))).lower()
        if 'purchase' not in tx_type and 'buy' not in tx_type:
            continue
            
        asset_category = str(item.get('asset_type', '')).lower().strip()
        valid_stock_tags = ('stock', 'common stock', 'equity', 'st')
        if asset_category not in valid_stock_tags:
            continue

        ticker = item.get('ticker')
        if not ticker or str(ticker).lower() in ('null', 'none', '--', 'n/a'):
            continue

        # Basic parsing to extract last name
        name = item.get('filer_name', item.get('representative', '')).strip()
        parts = name.split()
        last_name = parts[-1] if len(parts) >= 2 else parts[0] if parts else 'Unknown'

        trades.append({
            'ticker': str(ticker).strip().upper(),
            'transaction_date': item.get('transaction_date', ''),
            'filing_date': item.get('filing_date', ''),
            'name': last_name
        })
        
    return trades

def get_price_info(ticker, trade_date_str, retries=3):
    """Return (purchase_price, current_price) calculated from the purchase date."""
    for attempt in range(retries):
        try:
            stock = yf.Ticker(ticker)
            end_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
            hist = stock.history(start=trade_date_str, end=end_date)
            if hist.empty:
                return None, None
            purchase = float(hist.iloc[0]['Close'])
            current  = float(hist.iloc[-1]['Close'])
            return purchase, current
        except Exception as e:
            wait = (attempt + 1) * 2
            time.sleep(wait)
    return None, None

def get_industry(ticker, retries=2):
    """Fetch corporate industry metadata, cached internally per ticker."""
    if ticker in INDUSTRY_CACHE:
        return INDUSTRY_CACHE[ticker]
    for attempt in range(retries):
        try:
            info = yf.Ticker(ticker).info
            industry = info.get('industry', info.get('sector', 'N/A'))
            INDUSTRY_CACHE[ticker] = industry
            return industry
        except Exception as e:
            time.sleep(1)
    return 'N/A'

def process_and_filter_clusters(raw_trades):
    """
    FIXED PIPELINE: 
    1. Groups all raw trades by ticker first to accurately count cluster buyers.
    2. Runs pricing analytics and filters out trades that don't match criteria.
    """
    # Step 1: Group raw trades by ticker first so we don't drop cluster signals
    ticker_groups = {}
    for t in raw_trades:
        ticker_groups.setdefault(t['ticker'], []).append(t)

    aggregated_results = []

    # Step 2: Now analyze each grouped ticker cluster safely
    for ticker, trades in ticker_groups.items():
        if len(ticker) > 5:
            continue

        # Extract all unique buyers for this cluster BEFORE filtering out by price
        unique_buyers_list = sorted(set(t['name'] for t in trades))
        unique_buyers_count = len(unique_buyers_list)

        # Find the absolute newest trade in this cluster to evaluate timing parameters
        valid_trades = []
        for t in trades:
            try:
                t_date = datetime.strptime(t['transaction_date'], '%Y-%m-%d')
                f_date = datetime.strptime(t['filing_date'], '%Y-%m-%d')
                valid_trades.append((t_date, f_date, t))
            except (ValueError, TypeError):
                continue

        if not valid_trades:
            continue

        # Sort by transaction date to find the most recent one
        valid_trades.sort(key=lambda x: x[0], reverse=True)
        latest_trade_date, latest_filing_date, latest_trade_raw = valid_trades[0]

        days_since_purchase = (datetime.now() - latest_trade_date).days
        reporting_lag = (latest_filing_date - latest_trade_date).days

        # Filter Out completely stale data clusters
        if days_since_purchase > MAX_DAYS_AGO or days_since_purchase < 0:
            continue

        # Fetch pricing metrics relative to the latest purchase entry
        logging.info(f"Checking pricing metrics for purchase cluster: ${ticker}")
        purchase_price, current_price = get_price_info(ticker, latest_trade_raw['transaction_date'])
        if purchase_price is None:
            continue

        pct_change = (current_price - purchase_price) / purchase_price * 100

        # CRITICAL FILTER: Allow cluster trades an extra buffer, but cap single trades strictly
        if unique_buyers_count < 2 and pct_change > MAX_PCT_CHANGE:
            continue
        elif unique_buyers_count >= 2 and pct_change > (MAX_PCT_CHANGE + 7): 
            # Give high conviction cluster signals a wider breakout buffer (+7%) so you don't miss them
            continue

        industry = get_industry(ticker)

        if len(unique_buyers_list) > 3:
            buyer_str = f"{unique_buyers_list[0]}, ... +{len(unique_buyers_list)-1}"
        else:
            buyer_str = ", ".join(unique_buyers_list)

        # HIGHLY DISCERNING COLOR TIER RULE SYSTEM:
        if days_since_purchase <= FRESH_DAYS and pct_change <= FRESH_PCT:
            status = "🟢"  # Perfect Entry Option: Fresh buy, flat price action.
        elif pct_change < 0:
            status = "🟡"  # Discount Zone: Older purchase, trading BELOW what they paid.
        else:
            status = "🟠"  # Premium Zone: Older purchase, trading up to our ceiling limit.

        aggregated_results.append({
            'ticker': ticker,
            'buyer_str': buyer_str,
            'unique_buyers': unique_buyers_count,
            'pct_change': round(pct_change, 1),
            'days_since_purchase': days_since_purchase,
            'reporting_lag': reporting_lag,
            'status': status,
            'industry': industry
        })
        time.sleep(0.15)

    return aggregated_results

def build_chunks(aggregated):
    """Sort and compile output pushing multi-buyer cluster signals to the top."""
    if not aggregated:
        return []

    status_order = {'🟢': 0, '🟡': 1, '🟠': 2}
    # Priority sorting: Cluster signal presence -> Color Category -> Freshness age
    aggregated.sort(key=lambda x: (
        0 if x['unique_buyers'] >= 2 else 1, 
        status_order.get(x['status'], 99), 
        x['days_since_purchase']
    ))

    counts = Counter(t['status'] for t in aggregated)
    summary_line = f"🟢 Fresh Window: {counts['🟢']}  |  🟡 Discounted: {counts['🟡']}  |  🟠 Premium: {counts['🟠']}\n\n"
    base_header = f"📊 CONGRESS PURCHASES (Sorted by Alpha Signal & Clustered Conviction)\n"
    
    footer = (f"\n🟢 = New Buy (≤ {FRESH_DAYS}d, Price ≤ {FRESH_PCT}%)\n"
              f"🟡 = Discount Zone (Bought > {FRESH_DAYS}d ago, Stock is DOWN since purchase)\n"
              f"🟠 = Premium Zone (Bought > {FRESH_DAYS}d ago, Stock is UP up to limits)\n"
              f"👥👥 = CLUSTER SIGNALS (2+ politicians buying same stock)")

    lines = []
    for t in aggregated:
        prefix = f"👥👥 {t['status']}" if t['unique_buyers'] >= 2 else t['status']
        line = (f"{prefix} ${t['ticker']} | {t['buyer_str']} | "
                f"Bought {t['days_since_purchase']}d ago | "
                f"Price: {t['pct_change']:+.1f}% | {t['industry'][:10]}")
        lines.append(line)

    chunks = []
    current_chunk = base_header + summary_line

    for line in lines:
        if len(current_chunk) + len(line) + len(footer) + CHUNK_PADDING > TELEGRAM_CHAR_LIMIT:
            current_chunk += footer
            chunks.append(current_chunk)
            current_chunk = f"📊 CONGRESS PURCHASES [CONTINUED]\n\n" + line + "\n"
        else:
            current_chunk += line + "\n"

    current_chunk += footer
    chunks.append(current_chunk)
    return chunks

# ── Main Control Cycle ──────────────────────────────────────────────────────

def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Missing critical environment variables: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
        return

    logging.info("Executing pipeline optimized cluster cycle...")
    raw = fetch_trades()
    if not raw:
        return

    # Pipeline functions combined cleanly into process_and_filter_clusters
    aggregated = process_and_filter_clusters(raw)
    if not aggregated:
        logging.info("No trades fit our strict copy parameters today.")
        return

    message_chunks = build_chunks(aggregated)

    async def send_telegram(text):
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    async def send_all():
        for idx, chunk in enumerate(message_chunks):
            await send_telegram(chunk)
            if len(message_chunks) > 1 and idx < len(message_chunks) - 1:
                await asyncio.sleep(INTER_CHUNK_DELAY) 

    asyncio.run(send_all())
    logging.info("All dispatches executed successfully.")

if __name__ == "__main__":
    main()
