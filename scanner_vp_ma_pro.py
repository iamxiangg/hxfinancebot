#!/usr/bin/env python3
"""
VP_MA_Scan - High-probability stock scanner
Uses Volume Profile (Value Area), 50‑day MA, and RSI.
Generates support buys, breakout buys, and sell signals.
Sends Telegram notification.
"""

import os
import sys
import time
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator

# ==============================
# Configuration
# ==============================
WATCHLIST_FILE = "positions.csv"
TICKER_COL = "Ticker"
MIN_LOOKBACK = 300           # bars for Volume Profile
MA_LENGTH = 50
RSI_LENGTH = 14
VA_PERCENT = 0.70            # 70% Value Area
MIN_BUCKET_PRICE = 0.01      # minimum tick size for bucket width
BUCKET_PERCENT_OF_PRICE = 0.001  # 0.1% of current price

# TradingView chart base URL
TV_BASE = "https://www.tradingview.com/chart/?symbol="

# Telegram settings (from environment variables)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==============================
# Helper functions
# ==============================

def load_watchlist(filepath):
    """Load tickers from CSV. Expected column 'Ticker'."""
    if not os.path.exists(filepath):
        print(f"Watchlist file {filepath} not found.")
        return []
    df = pd.read_csv(filepath)
    if TICKER_COL not in df.columns:
        print(f"Column '{TICKER_COL}' not found in {filepath}.")
        return []
    return df[TICKER_COL].dropna().str.strip().tolist()

def compute_vp(df, lookback=300):
    """
    Compute Volume Profile using last `lookback` bars.
    Returns (VAH, VAL, POC, VC_HVN, VC_LVN, last_close, last_volume)
    """
    if len(df) < 50:
        return None, None, None, None, None, None, None

    df_vp = df.tail(lookback).copy()
    df_vp = df_vp[df_vp['Volume'] > 0].copy()
    if df_vp.empty:
        return None, None, None, None, None, None, None

    # Compute price range
    price_min = df_vp['Low'].min()
    price_max = df_vp['High'].max()
    price_range = price_max - price_min
    if price_range == 0:
        return price_min, price_max, price_min, [price_min], [price_min], df['Close'].iloc[-1], df['Volume'].iloc[-1]

    current_price = df['Close'].iloc[-1]
    current_volume = df['Volume'].iloc[-1]

    # Dynamic bucket width
    bucket_width = max(current_price * BUCKET_PERCENT_OF_PRICE, MIN_BUCKET_PRICE)
    num_buckets = int(price_range / bucket_width) + 1
    if num_buckets > 500:
        bucket_width = price_range / 500.0
        num_buckets = 500

    # Create buckets
    buckets = {}
    for i in range(num_buckets):
        low = price_min + i * bucket_width
        high = low + bucket_width
        buckets[i] = {'low': low, 'high': high, 'volume': 0, 'trades': 0}

    # Assign volume to buckets
    for _, bar in df_vp.iterrows():
        bar_low = bar['Low']
        bar_high = bar['High']
        bar_vol = bar['Volume']
        bar_typ = (bar_low + bar_high) / 2.0
        idx = int((bar_typ - price_min) / bucket_width)
        if 0 <= idx < num_buckets:
            buckets[idx]['volume'] += bar_vol
            buckets[idx]['trades'] += 1

    # Convert to sorted list of (price_mid, volume)
    vol_profile = []
    for idx, b in sorted(buckets.items()):
        mid = (b['low'] + b['high']) / 2.0
        vol_profile.append((mid, b['volume']))

    # Total volume and Value Area
    total_vol = sum(v for _, v in vol_profile)
    if total_vol == 0:
        return None, None, None, None, None, None, None
        
    target_vol = total_vol * VA_PERCENT

    # Find POC (max volume)
    poc_idx = max(range(len(vol_profile)), key=lambda i: vol_profile[i][1])
    poc_price = vol_profile[poc_idx][0]
    poc_volume = vol_profile[poc_idx][1]

    cum_vol = poc_volume
    left = poc_idx - 1
    right = poc_idx + 1
    while cum_vol < target_vol and (left >= 0 or right < len(vol_profile)):
        vol_left = vol_profile[left][1] if left >= 0 else 0
        vol_right = vol_profile[right][1] if right < len(vol_profile) else 0
        if vol_left >= vol_right and left >= 0:
            cum_vol += vol_left
            left -= 1
        elif right < len(vol_profile):
            cum_vol += vol_right
            right += 1
        else:
            break

    # VA boundaries
    va_low = vol_profile[0][0] if left < 0 else vol_profile[left+1][0]
    va_high = vol_profile[-1][0] if right >= len(vol_profile) else vol_profile[right-1][0]

    # Identify High Volume Nodes (HVN) – volume > 1.5x average
    avg_volume = total_vol / num_buckets
    hvn_threshold = 1.5 * avg_volume
    hvn_prices = [price for price, vol in vol_profile if vol > hvn_threshold]
    
    # Identify Low Volume Nodes (LVN) – volume < 0.5x average
    lvn_threshold = 0.5 * avg_volume
    lvn_prices = [price for price, vol in vol_profile if 0 < vol < lvn_threshold]

    return (round(va_high, 2), round(va_low, 2), round(poc_price, 2),
            hvn_prices, lvn_prices, current_price, current_volume)

def find_next_hvn_above(price, hvn_list):
    above = [p for p in hvn_list if p > price]
    return min(above) if above else None

def find_next_hvn_below(price, hvn_list):
    below = [p for p in hvn_list if p < price]
    return max(below) if below else None

def tradingview_url(ticker):
    return f"{TV_BASE}{ticker.upper()}"

def analyze_ticker(ticker):
    """Analyze a single ticker and return a signal dict or None."""
    try:
        df = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
    except Exception as e:
        print(f"Error downloading {ticker}: {e}")
        return None

    # Handle case where yf returns an empty dataframe (e.g. invalid/delisted ticker)
    if df.empty:
        print(f"{ticker}: No data returned from yfinance. Skipping.")
        return None

    # -------- Robust column flattening --------
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[-1] if col[-1] else col[0] for col in df.columns]

    # Ensure all columns are 1D Series
    for col in df.columns:
        if isinstance(df[col], pd.DataFrame):
            df[col] = df[col].squeeze()

    # Verify that we have the required columns
    required = ['Open', 'High', 'Low', 'Close', 'Volume']
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"{ticker}: Missing columns {missing}. Skipping.")
        return None

    if len(df) < MA_LENGTH + RSI_LENGTH + 5:
        return None

    # Calculate indicators
    close = df['Close']
    df['SMA50'] = SMAIndicator(close=close, window=MA_LENGTH).sma_indicator()
    df['RSI14'] = RSIIndicator(close=close, window=RSI_LENGTH).rsi()

    latest = df.iloc[-1]
    close_latest = latest['Close']
    sma50 = latest['SMA50']
    rsi = latest['RSI14']

    if pd.isna(close_latest) or pd.isna(sma50) or pd.isna(rsi):
        return None

    # Compute Volume Profile
    va_high, va_low, poc_price, hvn_list, lvn_list, _, _ = compute_vp(df, MIN_LOOKBACK)
    if va_high is None:
        return None

    # Determine conditions
    above_ma = close_latest > sma50
    below_ma = close_latest < sma50
    inside_va = va_low <= close_latest <= va_high
    above_va = close_latest > va_high
    below_va = close_latest < va_low

    # RSI zones
    rsi_buy_zone = 40 <= rsi <= 60
    rsi_breakout_zone = rsi < 65
    rsi_sell_zone = 30 <= rsi <= 50

    # LVN condition
    tolerance = close_latest * 0.002
    near_lvn = any(abs(close_latest - lvn) <= tolerance for lvn in lvn_list)

    action = None
    target = None
    stop = None
    note = ""

    # --- Support BUY ---
    if inside_va and above_ma and rsi_buy_zone:
        action = "BUY"
        note = "Support buy inside Value Area"
        target = find_next_hvn_above(close_latest, hvn_list)
        if target is None:
            target = round(va_high * 1.02, 2)
        stop = round(va_low * 0.99, 2)
        if stop >= close_latest * 0.98:
            stop = round(poc_price * 0.99, 2)

    # --- Breakout BUY ---
    elif above_va and near_lvn and above_ma and rsi_breakout_zone:
        action = "BREAKOUT"
        note = "Breakout above VA High with LVN"
        target = find_next_hvn_above(close_latest, hvn_list)
        if target is None:
            target = round(close_latest * 1.03, 2)
        stop = round(va_high * 0.99, 2)
        if stop >= close_latest * 0.99:
            stop = round(close_latest * 0.98, 2)

    # --- SELL signal ---
    elif below_va and near_lvn and below_ma and rsi_sell_zone:
        action = "SELL"
        note = "Sell signal: below VA Low + LVN"
        target = find_next_hvn_below(close_latest, hvn_list)
        if target is None:
            target = round(va_low * 0.98, 2)
        stop = round(va_low * 1.01, 2)
        if stop <= close_latest * 1.01:
            stop = round(poc_price * 1.01, 2)

    if action is None:
        return None

    return {
        'ticker': ticker,
        'action': action,
        'price': round(close_latest, 2),
        'rsi': round(rsi, 1),
        'va_range': f"VAH {va_high} / VAL {va_low}",
        'poc': poc_price,
        'target': target,
        'stop': stop,
        'note': note,
        'chart_url': tradingview_url(ticker)
    }

def send_telegram(signal):
    """Send formatted signal via Telegram (using standard line breaks for HTML mode)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Skipping notification.")
        return

    # Native string breaks work perfectly for line breaks in Telegram HTML parse mode
    msg_html = (
        f"<b>{signal['action']} Signal</b>: {signal['ticker']} @ ${signal['price']}\n"
        f"📊 RSI: {signal['rsi']}\n"
        f"📐 Value Area: {signal['va_range']}\n"
        f"📍 Point of Control: ${signal['poc']}\n"
        f"🎯 Target: ${signal['target']}\n"
        f"🛑 Stop: ${signal['stop']}\n"
        f"📝 {signal['note']}\n"
        f"🔗 <a href=\"{signal['chart_url']}\">TradingView Chart</a>"
    )

    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': msg_html,
        'parse_mode': 'HTML'
    }
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def main():
    tickers = load_watchlist(WATCHLIST_FILE)
    if not tickers:
        print("No tickers found. Exiting.")
        sys.exit(0)

    signals = []
    for i, ticker in enumerate(tickers):
        print(f"Scanning {ticker}... ({i+1}/{len(tickers)})")
        sig = analyze_ticker(ticker)
        if sig:
            signals.append(sig)
            send_telegram(sig)
        time.sleep(0.5)

    if not signals:
        print("No signals found today.")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            msg = "🔍 VP_MA Scan completed – no setups found."
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg})

    print(f"Done. Found {len(signals)} signals.")

if __name__ == "__main__":
    main()
