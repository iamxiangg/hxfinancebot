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
MIN_RISK_REWARD_RATIO = 1.5   # minimum reward/risk for any signal
VA_LOWER_HALF = True         # support buys only if price in lower 50% of VA

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
    Returns (VAH, VAL, POC, HVN_list, LVN_list, last_close, last_volume)
    """
    if len(df) < 50:
        return None, None, None, None, None, None, None

    df_vp = df.tail(lookback).copy()
    df_vp = df_vp[df_vp['Volume'] > 0].copy()
    if df_vp.empty:
        return None, None, None, None, None, None, None

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
        bar_typ = (bar['Low'] + bar['High']) / 2.0
        idx = int((bar_typ - price_min) / bucket_width)
        if 0 <= idx < num_buckets:
            buckets[idx]['volume'] += bar['Volume']
            buckets[idx]['trades'] += 1

    # Volume profile sorted by price
    vol_profile = []
    for idx, b in sorted(buckets.items()):
        mid = (b['low'] + b['high']) / 2.0
        vol_profile.append((mid, b['volume']))

    total_vol = sum(v for _, v in vol_profile)
    target_vol = total_vol * VA_PERCENT

    # Find POC
    poc_idx = max(range(len(vol_profile)), key=lambda i: vol_profile[i][1])
    poc_price = vol_profile[poc_idx][0]

    # Build Value Area around POC
    left = poc_idx - 1
    right = poc_idx + 1
    cum_vol = vol_profile[poc_idx][1]
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

    va_low = vol_profile[0][0] if left < 0 else vol_profile[left+1][0]
    va_high = vol_profile[-1][0] if right >= len(vol_profile) else vol_profile[right-1][0]

    # HVN / LVN
    avg_volume = total_vol / num_buckets
    threshold_hvn = 1.5 * avg_volume
    threshold_lvn = 0.5 * avg_volume
    hvn_prices = [price for price, vol in vol_profile if vol > threshold_hvn]
    lvn_prices = [price for price, vol in vol_profile if vol < threshold_lvn and vol > 0]

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
    try:
        df = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
    except Exception as e:
        print(f"Error downloading {ticker}: {e}")
        return None

    if df.empty:
        return None

    # Flatten MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df.columns = [str(col) for col in df.columns]

    required = ['Open', 'High', 'Low', 'Close', 'Volume']
    if not all(col in df.columns for col in required):
        print(f"Missing columns for {ticker} – skipping")
        return None

    if len(df) < MA_LENGTH + RSI_LENGTH + 5:
        print(f"Insufficient data for {ticker}")
        return None

    # Indicators
    df['SMA50'] = SMAIndicator(close=df['Close'], window=MA_LENGTH).sma_indicator()
    df['RSI14'] = RSIIndicator(close=df['Close'], window=RSI_LENGTH).rsi()

    latest = df.iloc[-1]
    close = latest['Close']
    sma50 = latest['SMA50']
    rsi = latest['RSI14']

    if pd.isna(close) or pd.isna(sma50) or pd.isna(rsi):
        return None

    # Volume Profile
    va_high, va_low, poc_price, hvn_list, lvn_list, _, _ = compute_vp(df, MIN_LOOKBACK)
    if va_high is None:
        return None

    above_ma = close > sma50
    below_ma = close < sma50
    inside_va = va_low <= close <= va_high
    above_va = close > va_high
    below_va = close < va_low
    rsi_buy_zone = 40 <= rsi <= 60
    rsi_breakout_zone = rsi < 65
    rsi_sell_zone = 30 <= rsi <= 50

    tolerance = close * 0.002
    near_lvn = any(abs(close - lvn) <= tolerance for lvn in lvn_list)

    signal = None
    action = None
    target = None
    stop = None
    note = ""

    # --- Support BUY (Score 3) ---
    if inside_va and above_ma and rsi_buy_zone:
        # Require price in lower half of VA to avoid near-VAH false signals
        va_mid = (va_high + va_low) / 2
        if VA_LOWER_HALF and close > va_mid:
            return None  # too close to VAH, not a support buy

        action = "BUY"
        note = "Support buy inside Value Area"
        target = find_next_hvn_above(close, hvn_list)
        if target is None:
            target = round(va_high * 1.02, 2)
        stop = round(va_low * 0.99, 2)
        if stop >= close * 0.98:
            stop = round(poc_price * 0.99, 2)
        # Ensure stop < close
        if stop >= close:
            stop = round(close * 0.97, 2)

    # --- Breakout BUY ---
    elif above_va and near_lvn and above_ma and rsi_breakout_zone:
        action = "BREAKOUT"
        note = "Breakout above VA High with LVN"
        target = find_next_hvn_above(close, hvn_list)
        if target is None:
            target = round(close * 1.03, 2)
        stop = round(va_high * 0.99, 2)
        if stop >= close * 0.99:
            stop = round(close * 0.98, 2)
        # Stop must be below entry
        if stop >= close:
            stop = round(close * 0.97, 2)

    # --- SELL signal ---
    elif below_va and near_lvn and below_ma and rsi_sell_zone:
        action = "SELL"
        note = "Sell signal: below VA Low + LVN"
        target = find_next_hvn_below(close, hvn_list)
        if target is None:
            target = round(va_low * 0.98, 2)
        stop = round(va_low * 1.01, 2)
        if stop <= close * 1.01:
            stop = round(poc_price * 1.01, 2)
        # Stop must be above entry
        if stop <= close:
            stop = round(close * 1.03, 2)

    if action is None:
        return None

    # --- Risk/Reward Check ---
    if target is not None and stop is not None:
        if action in ("BUY", "BREAKOUT"):
            risk = close - stop
            reward = target - close
        else:  # SELL
            risk = stop - close
            reward = close - target

        if risk <= 0 or reward <= 0:
            return None  # invalid targets/stops

        if reward / risk < MIN_RISK_REWARD_RATIO:
            # Unacceptable risk/reward, skip signal
            return None

    # Build signal dict
    signal = {
        'ticker': ticker,
        'action': action,
        'price': round(close, 2),
        'rsi': round(rsi, 1),
        'va_range': f"VAH {va_high} / VAL {va_low}",
        'poc': poc_price,
        'target': round(target, 2) if target else None,
        'stop': round(stop, 2) if stop else None,
        'note': note,
        'chart_url': tradingview_url(ticker)
    }
    return signal

def send_telegram(signal):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Skipping notification.")
        return

    msg = (
        f"<b>{signal['action']} Signal</b>: {signal['ticker']} @ ${signal['price']}\n"
        f"📊 RSI: {signal['rsi']}\n"
        f"📐 Value Area: {signal['va_range']}\n"
        f"📍 Point of Control: ${signal['poc']}\n"
        f"🎯 Target: ${signal['target']}\n"
        f"🛑 Stop: ${signal['stop']}\n"
        f"📝 {signal['note']}\n"
        f"🔗 <a href=\"{signal['chart_url']}\">Chart</a>"
    )
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': msg,
        'parse_mode': 'HTML'
    }
    import requests
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
            import requests
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg})

    print(f"Done. Found {len(signals)} signals.")

if __name__ == "__main__":
    main()
