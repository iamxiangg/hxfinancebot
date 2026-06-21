import os
import time
import math
import logging
import asyncio
from datetime import datetime, timedelta
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

MAX_DAYS_AGO   = 45
MAX_PCT_CHANGE = 8
FRESH_DAYS     = 21
FRESH_PCT      = 3

RAW_KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"

TELEGRAM_CHAR_LIMIT = 3800
CHUNK_PADDING       = 10
INTER_CHUNK_DELAY   = 1.5

INDUSTRY_CACHE = {}

# ── Amount / Scoring Helpers ────────────────────────────────────────────────

def estimate_amounts(item):
    """Return low, midpoint, high disclosed transaction estimates."""
    try:
        low = item.get("amount_range_low")
        high = item.get("amount_range_high")

        if low is None or high is None:
            return 0, 0, 0

        low = float(low)
        high = float(high)
        midpoint = (low + high) / 2

        return low, midpoint, high

    except (TypeError, ValueError):
        return 0, 0, 0


def format_amount(value):
    """Compact amount formatting for Telegram."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def calculate_signal_score(total_midpoint_amount, unique_buyers, days_since_purchase, pct_change):
    """
    Dollar-conviction-heavy scoring model.

    Approx weighting:
    - 65% dollar conviction
    - 20% freshness
    - 10% cluster confirmation
    - 5% entry quality
    """

    # 1. Dollar conviction dominates.
    # $1m midpoint roughly reaches full amount score.
    amount_score = min(total_midpoint_amount / 1_000_000, 1.0) * 65

    # 2. Freshness decays across MAX_DAYS_AGO.
    freshness_score = max(0, 1 - (days_since_purchase / MAX_DAYS_AGO)) * 20

    # 3. Cluster confirmation helps, but does not dominate.
    cluster_score = min(max(unique_buyers - 1, 0) * 5, 10)

    # 4. Entry quality.
    if pct_change < 0:
        entry_score = 5
    elif pct_change <= FRESH_PCT:
        entry_score = 4
    elif pct_change <= MAX_PCT_CHANGE:
        entry_score = 2
    else:
        entry_score = 0

    return round(amount_score + freshness_score + cluster_score + entry_score, 1)

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

        name = item.get('filer_name', item.get('representative', '')).strip()
        parts = name.split()
        last_name = parts[-1] if len(parts) >= 2 else parts[0] if parts else 'Unknown'

        low, midpoint, high = estimate_amounts(item)

        trades.append({
            'ticker': str(ticker).strip().upper(),
            'transaction_date': item.get('transaction_date', ''),
            'filing_date': item.get('filing_date', ''),
            'name': last_name,
            'amount_low': low,
            'amount_midpoint': midpoint,
            'amount_high': high
        })

    return trades


def get_price_info(ticker, trade_date_str, retries=3):
    """Return purchase_price and current_price calculated from the purchase date."""
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

        except Exception:
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
        except Exception:
            time.sleep(1)

    return 'N/A'


def process_and_filter_clusters(raw_trades):
    """
    Groups trades by ticker, aggregates disclosed amount ranges,
    applies price filters, then scores each ticker by conviction.
    """

    ticker_groups = {}
    for t in raw_trades:
        ticker_groups.setdefault(t['ticker'], []).append(t)

    aggregated_results = []

    for ticker, trades in ticker_groups.items():
        if len(ticker) > 5:
            continue

        unique_buyers_list = sorted(set(t['name'] for t in trades))
        unique_buyers_count = len(unique_buyers_list)

        total_low_amount = sum(t.get('amount_low', 0) for t in trades)
        total_midpoint_amount = sum(t.get('amount_midpoint', 0) for t in trades)
        total_high_amount = sum(t.get('amount_high', 0) for t in trades)

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

        valid_trades.sort(key=lambda x: x[0], reverse=True)
        latest_trade_date, latest_filing_date, latest_trade_raw = valid_trades[0]

        days_since_purchase = (datetime.now() - latest_trade_date).days
        reporting_lag = (latest_filing_date - latest_trade_date).days

        if days_since_purchase > MAX_DAYS_AGO or days_since_purchase < 0:
            continue

        logging.info(f"Checking pricing metrics for purchase cluster: ${ticker}")

        purchase_price, current_price = get_price_info(
            ticker,
            latest_trade_raw['transaction_date']
        )

        if purchase_price is None:
            continue

        pct_change = (current_price - purchase_price) / purchase_price * 100

        # Keep original breakout discipline.
        if unique_buyers_count < 2 and pct_change > MAX_PCT_CHANGE:
            continue
        elif unique_buyers_count >= 2 and pct_change > (MAX_PCT_CHANGE + 7):
            continue

        industry = get_industry(ticker)

        if len(unique_buyers_list) > 3:
            buyer_str = f"{unique_buyers_list[0]}, ... +{len(unique_buyers_list) - 1}"
        else:
            buyer_str = ", ".join(unique_buyers_list)

        if days_since_purchase <= FRESH_DAYS and pct_change <= FRESH_PCT:
            status = "🟢"
        elif pct_change < 0:
            status = "🟡"
        else:
            status = "🟠"

        signal_score = calculate_signal_score(
            total_midpoint_amount=total_midpoint_amount,
            unique_buyers=unique_buyers_count,
            days_since_purchase=days_since_purchase,
            pct_change=pct_change
        )

        aggregated_results.append({
            'ticker': ticker,
            'buyer_str': buyer_str,
            'unique_buyers': unique_buyers_count,
            'pct_change': round(pct_change, 1),
            'days_since_purchase': days_since_purchase,
            'reporting_lag': reporting_lag,
            'status': status,
            'industry': industry,
            'total_low_amount': total_low_amount,
            'total_midpoint_amount': total_midpoint_amount,
            'total_high_amount': total_high_amount,
            'signal_score': signal_score
        })

        time.sleep(0.15)

    return aggregated_results


def build_chunks(aggregated):
    """Sort and compile Telegram output by conviction score."""
    if not aggregated:
        return []

    aggregated.sort(
        key=lambda x: x.get('signal_score', 0),
        reverse=True
    )

    counts = Counter(t['status'] for t in aggregated)

    summary_line = (
        f"🟢 Fresh Window: {counts['🟢']}  |  "
        f"🟡 Discounted: {counts['🟡']}  |  "
        f"🟠 Premium: {counts['🟠']}\n\n"
    )

    base_header = (
        f"📊 CONGRESS PURCHASES\n"
        f"Sorted by Dollar Conviction, Freshness & Cluster Signal\n"
    )

    footer = (
        f"\n🟢 = New Buy ≤ {FRESH_DAYS}d and Price ≤ {FRESH_PCT}%\n"
        f"🟡 = Stock below latest purchase price\n"
        f"🟠 = Stock up, but within filter limit\n"
        f"👥👥 = 2+ politicians buying same ticker\n"
        f"Est = Sum of midpoint disclosed purchase ranges"
    )

    lines = []

    for t in aggregated:
        prefix = f"👥👥 {t['status']}" if t['unique_buyers'] >= 2 else t['status']

        est_mid = format_amount(t.get('total_midpoint_amount', 0))
        est_low = format_amount(t.get('total_low_amount', 0))
        est_high = format_amount(t.get('total_high_amount', 0))

        line = (
            f"{prefix} ${t['ticker']} | {t['buyer_str']} | "
            f"Est: {est_mid} [{est_low}-{est_high}] | "
            f"Score: {t['signal_score']} | "
            f"Bought {t['days_since_purchase']}d ago | "
            f"Price: {t['pct_change']:+.1f}% | "
            f"{t['industry'][:10]}"
        )

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

    logging.info("Executing conviction-weighted congress purchase pipeline...")

    raw = fetch_trades()

    if not raw:
        logging.info("No raw purchase trades found.")
        return

    aggregated = process_and_filter_clusters(raw)

    if not aggregated:
        logging.info("No trades fit the strict copy parameters today.")
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