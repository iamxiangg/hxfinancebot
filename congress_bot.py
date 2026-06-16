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

MAX_DAYS_AGO   = 45      # Lookback window for actual PURCHASE date (handles reporting lag)
MAX_PCT_CHANGE = 8       # Hard ceiling: Ignore anything that ran up > 8% from their buy price
FRESH_DAYS     = 21      # Green window threshold (bought within 3 weeks)
FRESH_PCT      = 3       # Green price change ceiling (flat, down, or up < 3%)

# Direct CDN pointer to Kadoa's real 'trades.json' data file 
RAW_KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"

TELEGRAM_CHAR_LIMIT = 3800   
CHUNK_PADDING       = 10     
INTER_CHUNK_DELAY   = 1.5    

# Global cache to avoid redundant yfinance calls for the same ticker
INDUSTRY_CACHE = {}

# ── Core Processing Functions ───────────────────────────────────────────────

def fetch_trades():
    """Fetch and pre-filter raw entries focusing on equity stock purchases across House/Senate structures."""
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

        trades.append({
            'ticker': str(ticker).strip().upper(),
            'transaction_date': item.get('transaction_date', ''),
            'filing_date': item.get('filing_date', ''),
            'representative': item.get('filer_name', item.get('representative', ''))
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

def enrich_trades(raw_trades):
    """Filter and enrich raw trades evaluated strictly by PURCHASE date with distinct color sub-tiers."""
    enriched = []
    for trade in raw_trades:
        ticker = trade.get('ticker', '').strip().upper()
        if len(ticker) > 5:
            continue

        trade_date_str = trade.get('transaction_date', '')
        filing_date_str = trade.get('filing_date', '')
        if not trade_date_str or not filing_date_str:
            continue

        try:
            trade_date = datetime.strptime(trade_date_str, '%Y-%m-%d')
            filing_date = datetime.strptime(filing_date_str, '%Y-%m-%d')
        except ValueError:
            continue

        # Structural validation focusing on the actual transaction buy window
        days_since_purchase = (datetime.now() - trade_date).days
        reporting_lag = (filing_date - trade_date).days
        
        if days_since_purchase > MAX_DAYS_AGO or days_since_purchase < 0:
            continue

        logging.info(f"Checking pricing metrics for purchase-tracked asset: ${ticker}")
        purchase_price, current_price = get_price_info(ticker, trade_date_str)
        if purchase_price is None:
            continue

        pct_change = (current_price - purchase_price) / purchase_price * 100
        if pct_change > MAX_PCT_CHANGE:
            continue

        industry = get_industry(ticker)

        name = trade.get('representative', '').strip()
        parts = name.split()
        last_name = parts[-1] if len(parts) >= 2 else parts[0] if parts else 'Unknown'

        # HIGHLY DISCERNING COLOR TIER RULE SYSTEM:
        if days_since_purchase <= FRESH_DAYS and pct_change <= FRESH_PCT:
            status = "🟢"  # Perfect Entry Option: Fresh buy, flat price action.
        elif pct_change < 0:
            status = "🟡"  # Discount Zone: Older purchase, but trading BELOW what they paid.
        else:
            status = "🟠"  # Premium Zone: Older purchase, trading at a slight premium (up to 8%).

        enriched.append({
            'ticker': ticker,
            'name': last_name,
            'industry': industry,
            'pct_change': round(pct_change, 1),
            'days_since_purchase': days_since_purchase,
            'reporting_lag': reporting_lag,
            'status': status,
            'purchase_date': trade_date
        })
        time.sleep(0.15)

    return enriched

def group_trades(enriched):
    """Group entries by ticker, tracking overlapping purchases for cluster identification."""
    groups = {}
    for t in enriched:
        groups.setdefault(t['ticker'], []).append(t)

    aggregated = []
    for ticker, trades in groups.items():
        # Target the absolute newest purchase entry to represent timing metrics
        latest = max(trades, key=lambda x: x['purchase_date'])
        unique_buyers = len(set(t['name'] for t in trades))
        buyer_names = sorted(set(t['name'] for t in trades))
        
        if len(buyer_names) > 3:
            buyer_str = f"{buyer_names[0]}, ... +{len(buyer_names)-1}"
        else:
            buyer_str = ", ".join(buyer_names)

        aggregated.append({
            'ticker': ticker,
            'buyer_str': buyer_str,
            'unique_buyers': unique_buyers,
            'pct_change': latest['pct_change'],
            'days_since_purchase': latest['days_since_purchase'],
            'reporting_lag': latest['reporting_lag'],
            'status': latest['status']
        })
    return aggregated

def build_chunks(aggregated):
    """Sort and compile output pushing multi-buyer cluster signals to the top."""
    if not aggregated:
        return []

    # Order priority: Multi-filer clusters always sit at the top,
    # followed by Green (Fresh), Yellow (Discounted), and Orange (Premium).
    status_order = {'🟢': 0, '🟡': 1, '🟠': 2}
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
              f"🟠 = Premium Zone (Bought > {FRESH_DAYS}d ago, Stock is UP up to 8%)\n"
              f"👥👥 = CLUSTER SIGNALS (2+ politicians buying same stock)")

    lines = []
    for t in aggregated:
        prefix = f"👥👥 {t['status']}" if t['unique_buyers'] >= 2 else t['status']
        line = (f"{prefix} ${t['ticker']} | {t['buyer_str']} | "
                f"Bought {t['days_since_purchase']}d ago | "
                f"Price: {t['pct_change']:+.1f}%")
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

    logging.info("Executing purchase-centric tracking cycle...")
    raw = fetch_trades()
    if not raw:
        return

    enriched = enrich_trades(raw)
    if not enriched:
        logging.info("No trades fit our strict copy parameters today.")
        return

    aggregated = group_trades(enriched)
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
