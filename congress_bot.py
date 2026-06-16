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

MAX_DAYS_AGO   = 14      # only trades filed within last N days
MAX_PCT_CHANGE = 8       # max acceptable price change since purchase
FRESH_DAYS     = 7       # trades <= N days ago are "fresh"
FRESH_PCT      = 5       # max change for "fresh" status

# Direct CDN pointer to Kadoa's main repository tracking matrix database file
RAW_KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/all_transactions.json"

TELEGRAM_CHAR_LIMIT = 3800   # safe buffer below 4096 bytes
CHUNK_PADDING       = 10     # padding buffer for line breaks and string joins
INTER_CHUNK_DELAY   = 1.5    # seconds to sleep between multi-part telegram messages

# Global cache to avoid redundant yfinance calls for the same ticker
INDUSTRY_CACHE = {}

# ── Helper Functions ────────────────────────────────────────────────────────

def fetch_trades():
    """
    Fetch normalized data directly from the Kadoa repository file tree via GitHub's CDN.
    Extracts the compiled static JSON array without touching external web app routers.
    """
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
        # Standardize transaction parameters to search specifically for purchases
        tx_type = item.get('type', item.get('transaction_type', '')).lower()
        if tx_type != 'purchase':
            continue
            
        # Filter explicitly for stock equity assets
        asset_category = item.get('asset_type', '').lower()
        if asset_category and asset_category not in ('stock', 'common stock', 'equity'):
            continue

        # Remap Kadoa's data dictionary keys back to our tracking structure
        trades.append({
            'ticker': item.get('ticker', '').strip().upper(),
            'transaction_date': item.get('date', item.get('transaction_date', '')),
            'representative': item.get('filer_name', item.get('representative', '')),
            'asset_type': asset_category
        })
        
    return trades

def get_price_info(ticker, trade_date_str, retries=3):
    """Return (purchase_price, current_price) with retries."""
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
            logging.warning(f"yfinance error for {ticker} (attempt {attempt+1}/{retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    logging.error(f"Failed to fetch price history for {ticker} after {retries} attempts.")
    return None, None

def get_industry(ticker, retries=2):
    """Fetch industry, cached per ticker."""
    if ticker in INDUSTRY_CACHE:
        return INDUSTRY_CACHE[ticker]

    for attempt in range(retries):
        try:
            info = yf.Ticker(ticker).info
            industry = info.get('industry', info.get('sector', 'N/A'))
            INDUSTRY_CACHE[ticker] = industry
            return industry
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5)
            else:
                logging.warning(f"Could not fetch industry for {ticker}: {e}")
    INDUSTRY_CACHE[ticker] = 'N/A'
    return 'N/A'

def enrich_trades(raw_trades):
    """Filter and enrich raw trades with price, industry, status."""
    enriched = []
    for trade in raw_trades:
        ticker = trade.get('ticker', '').strip().upper()
        if not ticker or ticker in ('--', 'N/A') or len(ticker) > 5:
            continue

        trade_date_str = trade.get('transaction_date', '')
        if not trade_date_str:
            continue

        try:
            trade_date = datetime.strptime(trade_date_str, '%Y-%m-%d')
        except ValueError:
            continue

        # Date parameters checked BEFORE initiating heavy pricing loops
        days_ago = (datetime.now() - trade_date).days
        if days_ago > MAX_DAYS_AGO or days_ago < 0:
            continue

        logging.info(f"Processing historical price metrics for active ticker: ${ticker}")
        purchase_price, current_price = get_price_info(ticker, trade_date_str)
        if purchase_price is None:
            continue

        pct_change = (current_price - purchase_price) / purchase_price * 100
        if pct_change > MAX_PCT_CHANGE:
            continue

        industry = get_industry(ticker)

        # Extract last name from politician full name values
        name = trade.get('representative', '').strip()
        parts = name.split()
        last_name = parts[-1] if len(parts) >= 2 else parts[0] if parts else 'Unknown'

        # Segmenting classification indicators
        if days_ago <= FRESH_DAYS and pct_change <= FRESH_PCT:
            status = "🟢"
        elif days_ago <= MAX_DAYS_AGO and pct_change <= MAX_PCT_CHANGE:
            status = "🟡"
        else:
            status = "🔴"

        enriched.append({
            'ticker': ticker,
            'name': last_name,
            'industry': industry,
            'pct_change': round(pct_change, 1),
            'days_ago': days_ago,
            'status': status,
            'transaction_date': trade_date
        })

        # Internal loop throttle baseline to conform with Yahoo rate limits
        time.sleep(0.2)

    return enriched

def group_trades(enriched):
    """Group by ticker, return one dict per ticker with aggregated info."""
    groups = {}
    for t in enriched:
        groups.setdefault(t['ticker'], []).append(t)

    aggregated = []
    for ticker, trades in groups.items():
        latest = max(trades, key=lambda x: x['transaction_date'])
        unique_buyers = len(set(t['name'] for t in trades))
        buyer_names = sorted(set(t['name'] for t in trades))
        
        if len(buyer_names) > 4:
            buyer_str = f"{buyer_names[0]}, ... +{len(buyer_names)-1} others"
        else:
            buyer_str = ", ".join(buyer_names)

        aggregated.append({
            'ticker': ticker,
            'buyer_str': buyer_str,
            'unique_buyers': unique_buyers,
            'pct_change': latest['pct_change'],
            'days_ago': latest['days_ago'],
            'industry': latest['industry'],
            'status': latest['status']
        })
    return aggregated

def build_chunks(aggregated):
    """Sort items, calculate metrics, and build line arrays cleanly into message chunks."""
    if not aggregated:
        return []

    status_order = {'🟢': 0, '🟡': 1, '🔴': 2}
    aggregated.sort(key=lambda x: (status_order.get(x['status'], 99), -x['unique_buyers']))

    # Generate Telemetry Dashboard Summary Row
    counts = Counter(t['status'] for t in aggregated)
    summary_line = f"🟢 {counts['🟢']}  |  🟡 {counts['🟡']}  |  🔴 {counts['🔴']}\n\n"

    base_header = f"📊 CONGRESS BUYS (last {MAX_DAYS_AGO}d, change <{MAX_PCT_CHANGE}%)\n"
    cont_header = f"📊 CONGRESS BUYS [CONTINUED]\n\n"
    
    footer = (f"\n🟢 = Fresh & Flat (≤{FRESH_DAYS}d, ≤{FRESH_PCT}%)\n"
              f"🟡 = Watch\n🔴 = Already moved\n"
              f"Sorted by signal then by #buyers")

    lines = []
    for t in aggregated:
        prefix = f"{t['status']} 👥{t['unique_buyers']}x" if t['unique_buyers'] >= 2 else t['status']
        line = (f"{prefix} ${t['ticker']} | {t['buyer_str']} | "
                f"{t['industry'][:10]} | {t['pct_change']:+.1f}% | {t['days_ago']}d ago")
        lines.append(line)

    chunks = []
    current_chunk = base_header + summary_line

    for line in lines:
        if len(current_chunk) + len(line) + len(footer) + CHUNK_PADDING > TELEGRAM_CHAR_LIMIT:
            current_chunk += footer
            chunks.append(current_chunk)
            current_chunk = cont_header + line + "\n"
        else:
            current_chunk += line + "\n"

    current_chunk += footer
    chunks.append(current_chunk)
    return chunks

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Missing environment variables TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
        return

    logging.info("Fetching raw transactions...")
    raw = fetch_trades()
    if not raw:
        logging.warning("No raw trades found.")
        return
    logging.info(f"Downloaded {len(raw)} total historical trades. Synchronizing parameters...")

    enriched = enrich_trades(raw)
    logging.info(f"Enriched {len(enriched)} actionable trades within active window limits.")

    if not enriched:
        logging.info("No actionable trades found inside parameters during this run.")
        return

    aggregated = group_trades(enriched)
    message_chunks = build_chunks(aggregated)

    async def send_telegram(text):
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    async def send_all():
        for idx, chunk in enumerate(message_chunks):
            logging.info(f"Sending message block ({idx+1}/{len(message_chunks)})...")
            await send_telegram(chunk)
            if len(message_chunks) > 1 and idx < len(message_chunks) - 1:
                await asyncio.sleep(INTER_CHUNK_DELAY) 

    asyncio.run(send_all())
    logging.info("All dispatches executed successfully.")

if __name__ == "__main__":
    main()
