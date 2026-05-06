#!/usr/bin/env python3
"""
Gamma Squeeze Breakout Scanner
Uses Finviz -> yfinance -> Telegram notification
"""

import os
import sys
import time
import logging
import pandas as pd
import yfinance as yf
from finvizfinance.screener.overview import Overview

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAX_TICKERS_TO_ANALYSE = 50   # max after Finviz filter (avoid yfinance overload)
TOP_RESULTS_TO_SEND = 5       # number of stocks in Telegram message
YFINANCE_SLEEP = 0.3          # seconds between yfinance requests (rate limit)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Helper functions ----------

def finviz_prefilter() -> list:
    """Return list of tickers from Finviz with high short interest, volume, etc."""
    filters = {
        'Volume': 'Over 1M',
        'Short Float': 'Over 20%',
        'Price': 'Over $5',
        'Float': 'Under 50M',
        # optionally add: 'Borrow Rate' if available (FinvizElite)
    }
    screener = Overview()
    try:
        # Unpack dict as keyword arguments (fix for newer finvizfinance versions)
        screener.set_filter(**filters)
        df = screener.screener_view(columns=['Ticker', 'Volume', 'Short Float', 'Float'])
        tickers = df['Ticker'].tolist()
        logger.info(f"Finviz pre-filter returned {len(tickers)} tickers.")
        return tickers[:MAX_TICKERS_TO_ANALYSE]  # limit further
    except Exception as e:
        logger.error(f"Finviz screening failed: {e}")
        return []

def get_otm_oi(ticker: yf.Ticker, price: float) -> float:
    """Return total open interest of OTM calls (strike > price * 1.02) for nearest expiry."""
    try:
        expirations = ticker.options
        if not expirations:
            return 0
        # Use the nearest expiration (first in list)
        chain = ticker.option_chain(expirations[0]).calls
        otm = chain[chain['strike'] > price * 1.02]
        return otm['openInterest'].sum()
    except Exception as e:
        logger.debug(f"Error getting OI for {ticker.ticker}: {e}")
        return 0

def compute_composite_score(ticker_symbol: str) -> dict:
    """
    Compute composite score (0-100) for a single ticker.
    Returns dict with ticker, composite, and sub‑scores for debugging.
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info

        # --- Basic data ---
        price = info.get('regularMarketPrice', 0)
        if price <= 0:
            return None

        # --- Engine (50%) ---
        # OI on OTM calls (nearest expiry)
        oi_otm = get_otm_oi(ticker, price)
        oi_score = min(100, oi_otm / 10_000 * 100) if oi_otm else 0

        # Short interest % of float
        si = info.get('shortPercentOfFloat', 0) or 0
        si_score = min(100, si * 10)  # 10% SI -> 100, 50% -> 100 (capped)

        # Float ratio (low is good)
        float_shares = info.get('floatShares', 0)
        outstanding = info.get('sharesOutstanding', 1)
        float_ratio = float_shares / outstanding if float_shares else 1
        float_score = max(0, 100 - (float_ratio * 330))  # 30% float -> 0

        engine = 0.4 * oi_score + 0.3 * si_score + 0.3 * float_score

        # --- Accelerator (30%) ---
        # Days to cover
        dtc = info.get('shortRatio', 0) or 0
        dtc_score = min(100, dtc * 20)   # 5 days -> 100

        # Borrow fee not directly available from yfinance; we skip (weight redistributed)
        # Instead, we double weight on DTC and add a dummy (0) for borrow
        # Gamma concentration: % of OI from top two strikes (simplified: use oi_score as proxy)
        gamma_score = min(100, oi_otm / 5000 * 100)  # proxy

        accelerator = 0.6 * dtc_score + 0.4 * gamma_score

        # --- Trigger (20%) ---
        # Historical data for SMA & volume
        hist = ticker.history(period="2mo")
        if hist.empty or len(hist) < 20:
            return None

        # SMA-20 breakout
        sma20 = hist['Close'].rolling(20).mean().iloc[-1]
        sma20_prev = hist['Close'].rolling(20).mean().iloc[-2]
        breakout = hist['Close'].iloc[-1] > sma20 and hist['Close'].iloc[-2] <= sma20_prev
        sma_score = 100 if breakout else 0

        # Volume spike
        avg_vol = hist['Volume'].rolling(20).mean().iloc[-1]
        today_vol = hist['Volume'].iloc[-1]
        vol_spike = (today_vol / avg_vol) - 1 if avg_vol > 0 else 0
        vol_score = min(100, vol_spike * 100)  # 2x -> 100

        # RSI (avoid overbought)
        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        rsi_score = max(0, 100 - (rsi - 50))  # RSI=60->90, RSI=80->30

        # Expiration proximity (days to nearest)
        expirations = ticker.options
        if expirations:
            from datetime import datetime
            nearest_exp = datetime.strptime(expirations[0], "%Y-%m-%d")
            days_to_exp = (nearest_exp - datetime.now()).days
            exp_score = 100 if days_to_exp <= 7 else (50 if days_to_exp <= 14 else 0)
        else:
            exp_score = 0

        # News (catalyst proxy)
        news_count = len(ticker.news) if ticker.news else 0
        cat_score = 50 if news_count > 0 else 0

        trigger = (0.30 * sma_score + 0.25 * vol_score + 0.20 * exp_score +
                   0.15 * rsi_score + 0.10 * cat_score)

        # Composite
        composite = 0.50 * engine + 0.30 * accelerator + 0.20 * trigger

        return {
            'ticker': ticker_symbol,
            'composite': round(composite, 1),
            'price': price,
            'short_float': si,
            'days_to_cover': dtc,
            'oi_otm': int(oi_otm),
            'breakout': breakout,
            'volume_spike': round(vol_spike, 2),
            'rsi': round(rsi, 1)
        }

    except Exception as e:
        logger.warning(f"Failed to process {ticker_symbol}: {e}")
        return None


# ---------- Main ----------
def main():
    logger.info("Starting gamma squeeze scanner...")

    # 1. Finviz pre-filter
    tickers = finviz_prefilter()
    if not tickers:
        logger.error("No tickers from Finviz. Exiting.")
        return

    # 2. Deep analysis with yfinance
    results = []
    for i, symbol in enumerate(tickers):
        logger.info(f"Processing {i+1}/{len(tickers)}: {symbol}")
        result = compute_composite_score(symbol)
        if result:
            results.append(result)
        time.sleep(YFINANCE_SLEEP)

    # 3. Sort by composite score descending
    results.sort(key=lambda x: x['composite'], reverse=True)
    top = results[:TOP_RESULTS_TO_SEND]

    # 4. Prepare Telegram message
    msg_lines = ["🔥 **Gamma Squeeze Breakout Scan**", ""]
    if top:
        for r in top:
            msg_lines.append(
                f"• {r['ticker']} (Score: {r['composite']})\n"
                f"  Price: ${r['price']:.2f} | SI: {r['short_float']:.1%} | DTC: {r['days_to_cover']:.1f}\n"
                f"  OI OTM: {r['oi_otm']:,} | Breakout: {'✅' if r['breakout'] else '❌'}\n"
                f"  Vol Spike: {r['volume_spike']*100:.0f}% | RSI: {r['rsi']}"
            )
    else:
        msg_lines.append("No candidates found today.")

    message = "\n\n".join(msg_lines)

    # 5. Send Telegram notification (if token provided)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        from telegram_notifier import send_telegram
        send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)
    else:
        logger.info("No Telegram credentials set – printing message:\n")
        print(message)

if __name__ == "__main__":
    main()
