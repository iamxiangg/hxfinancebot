#!/usr/bin/env python3
"""
VP_MA_Scan – Volume Profile + 50-MA + RSI Scanner
Sends Telegram message with signals AND non-signal reasons.
"""

import os
import sys
import math
import traceback
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

# ─── Configuration ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WATCHLIST_FILE     = "positions.csv"          # must have column 'Ticker'
LOOKBACK           = 300                      # VP lookback bars
VALUE_AREA_PCT     = 0.70                     # 70% Value Area
MIN_RR_RATIO       = 1.5                      # risk/reward minimum
RSI_BUY_LOW        = 40
RSI_BUY_HIGH       = 60
RSI_SELL_LOW       = 30
RSI_SELL_HIGH      = 50
RSI_BREAKOUT_MAX   = 65

# ─── Helper Functions ───────────────────────────────────────────

def flatten_multiindex(df):
    """Flatten yfinance MultiIndex columns to single level."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ['_'.join(col).strip() for col in df.columns.values]
    return df

def get_ticker_data(ticker):
    """Download OHLCV data using yfinance."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="6mo", interval="1d")
        if df.empty:
            return None
        df = flatten_multiindex(df)
        rename_map = {}
        for col in df.columns:
            lower = col.lower()
            if 'close' in lower:
                rename_map[col] = 'Close'
            elif 'high' in lower:
                rename_map[col] = 'High'
            elif 'low' in lower:
                rename_map[col] = 'Low'
            elif 'volume' in lower:
                rename_map[col] = 'Volume'
        df.rename(columns=rename_map, inplace=True)
        required = ['Close', 'High', 'Low', 'Volume']
        if not all(c in df.columns for c in required):
            return None
        return df
    except Exception:
        return None

def compute_volume_profile(df, lookback=LOOKBACK):
    """Compute Volume Profile for last `lookback` bars."""
    if len(df) < lookback:
        lookback = len(df)
    recent = df.tail(lookback).copy()
    price_min = recent['Low'].min()
    price_max = recent['High'].max()
    price_range = price_max - price_min
    if price_range == 0:
        return None

    avg_price = recent['Close'].mean()
    bucket_width = max(avg_price * 0.001, 0.01)
    num_buckets = int(price_range / bucket_width)
    if num_buckets > 500:
        bucket_width = price_range / 500
        num_buckets = 500
    elif num_buckets < 10:
        bucket_width = price_range / 10
        num_buckets = 10

    buckets = {}
    for _, row in recent.iterrows():
        low = row['Low']
        high = row['High']
        vol = row['Volume']
        if vol == 0:
            continue
        low_idx = int((low - price_min) / bucket_width)
        high_idx = int((high - price_min) / bucket_width)
        for i in range(low_idx, high_idx + 1):
            price_level = price_min + i * bucket_width
            price_level = round(price_level, 2)
            buckets[price_level] = buckets.get(price_level, 0) + vol / (high_idx - low_idx + 1)

    if not buckets:
        return None

    sorted_prices = sorted(buckets.keys())
    sorted_volumes = [buckets[p] for p in sorted_prices]
    total_volume = sum(sorted_volumes)

    poc_price = max(buckets, key=buckets.get)

    target_volume = total_volume * VALUE_AREA_PCT
    poc_idx = sorted_prices.index(poc_price)
    cum_vol = 0
    left_idx = poc_idx
    right_idx = poc_idx
    while cum_vol < target_volume and (left_idx > 0 or right_idx < len(sorted_prices)-1):
        if left_idx > 0 and (right_idx == len(sorted_prices)-1 or 
                             sorted_volumes[left_idx-1] >= sorted_volumes[right_idx+1]):
            left_idx -= 1
            cum_vol += sorted_volumes[left_idx]
        elif right_idx < len(sorted_prices)-1:
            right_idx += 1
            cum_vol += sorted_volumes[right_idx]
        else:
            break

    val = sorted_prices[left_idx]
    vah = sorted_prices[right_idx]

    volume_threshold_high = np.percentile(sorted_volumes, 80)
    volume_threshold_low  = np.percentile(sorted_volumes, 20)
    hvn_levels = [p for p, v in zip(sorted_prices, sorted_volumes) if v >= volume_threshold_high]
    lvn_levels = [p for p, v in zip(sorted_prices, sorted_volumes) if v <= volume_threshold_low]

    return {
        'value_area_low': val,
        'value_area_high': vah,
        'poc': poc_price,
        'hvn_levels': hvn_levels,
        'lvn_levels': lvn_levels,
        'bucket_width': bucket_width,
        'buckets': buckets
    }

def compute_indicators(df):
    """Add 50-MA and RSI (14) to dataframe."""
    df = df.copy()
    df['MA50'] = SMAIndicator(close=df['Close'], window=50).sma_indicator()
    df['RSI']  = RSIIndicator(close=df['Close'], window=14).rsi()
    return df

def calculate_risk_reward(entry, stop, target):
    """Return risk:reward ratio. 0 if invalid."""
    risk  = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return 0
    return reward / risk

def nearest_lvn(lvn_list, price):
    """Return the LVN level closest to price."""
    if not lvn_list:
        return None
    return min(lvn_list, key=lambda x: abs(x - price))

# ─── Signal Detection ───────────────────────────────────────────

def detect_signals(ticker, df):
    """Check for Support BUY, Breakout BUY, SELL signals."""
    signals = []
    vp = compute_volume_profile(df)
    if vp is None:
        return signals, None   # No VP data

    df_indicators = compute_indicators(df)
    last = df_indicators.iloc[-1]

    close_price = last['Close']
    ma50 = last['MA50']
    rsi  = last['RSI']
    val  = vp['value_area_low']
    vah  = vp['value_area_high']
    poc  = vp['poc']
    va_mid = (val + vah) / 2
    lvn_levels = vp['lvn_levels']

    if pd.isna(ma50) or pd.isna(rsi):
        return signals, None

    # Collect reasons for no signal (used for debug output)
    reasons = []

    # ── Support BUY ──────────────────────────────────────────
    if (close_price >= ma50 * 0.98) and (close_price <= ma50 * 1.02):
        if val <= close_price <= va_mid:
            if RSI_BUY_LOW <= rsi <= RSI_BUY_HIGH:
                stop = max(val, poc)
                if close_price - stop < 0.01 * close_price:
                    stop = val * 0.98
                target = vah
                rr = calculate_risk_reward(close_price, stop, target)
                if rr >= MIN_RR_RATIO:
                    signals.append({
                        'type': 'Support BUY',
                        'ticker': ticker,
                        'entry': close_price,
                        'stop': stop,
                        'target': target,
                        'rr': round(rr, 2),
                        'rsi': round(rsi, 1),
                        'ma50': round(ma50, 2),
                        'va_range': f"${round(val,2)}–${round(vah,2)}",
                        'poc': round(poc, 2)
                    })
                    return signals, None  # signal found, no reason needed
                else:
                    reasons.append("risk/reward below 1.5")
            else:
                reasons.append(f"RSI out of buy range (currently {rsi:.1f})")
        else:
            reasons.append("price not in lower half of VA")
    else:
        reasons.append(f"price not near 50-MA (difference {abs(close_price-ma50)/ma50*100:.1f}%)")

    # ── Breakout BUY (only if no Support BUY) ────────────────
    if not signals and (close_price > vah) and (rsi < RSI_BREAKOUT_MAX):
        lvn_near = nearest_lvn(lvn_levels, close_price)
        if lvn_near and abs(close_price - lvn_near) / close_price <= 0.02:
            if close_price > ma50:
                stop = vah * 0.98
                target = close_price * 1.05
                rr = calculate_risk_reward(close_price, stop, target)
                if rr >= MIN_RR_RATIO:
                    signals.append({
                        'type': 'Breakout BUY',
                        'ticker': ticker,
                        'entry': close_price,
                        'stop': stop,
                        'target': target,
                        'rr': round(rr, 2),
                        'rsi': round(rsi, 1),
                        'ma50': round(ma50, 2),
                        'va_range': f"${round(val,2)}–${round(vah,2)}",
                        'poc': round(poc, 2),
                        'near_lvn': round(lvn_near, 2)
                    })
                    return signals, None
                else:
                    reasons.append("breakout risk/reward below 1.5")
            else:
                reasons.append("breakout but below 50-MA")
        else:
            reasons.append("breakout but no nearby LVN")
    elif not signals:
        reasons.append(f"price below VAH or RSI too high ({rsi:.1f})")

    # ── SELL signal ─────────────────────────────────────────
    if not signals and (close_price < val) and (rsi >= RSI_SELL_LOW) and (rsi <= RSI_SELL_HIGH):
        lvn_near = nearest_lvn(lvn_levels, close_price)
        if lvn_near and abs(close_price - lvn_near) / close_price <= 0.02:
            if close_price < ma50:
                stop = val * 1.02
                target = close_price * 0.95
                rr = calculate_risk_reward(close_price, stop, target)
                if rr >= MIN_RR_RATIO:
                    signals.append({
                        'type': 'SELL (Short)',
                        'ticker': ticker,
                        'entry': close_price,
                        'stop': stop,
                        'target': target,
                        'rr': round(rr, 2),
                        'rsi': round(rsi, 1),
                        'ma50': round(ma50, 2),
                        'va_range': f"${round(val,2)}–${round(vah,2)}",
                        'poc': round(poc, 2)
                    })
                    return signals, None
                else:
                    reasons.append("sell risk/reward below 1.5")
            else:
                reasons.append("sell but above 50-MA")
        else:
            reasons.append("sell but no nearby LVN")
    else:
        if close_price < val:
            reasons.append("below VAL but RSI not in sell range")

    if not reasons:
        reasons.append("unknown (no signal criteria met)")
    return signals, ", ".join(reasons)

# ─── Telegram Notification ──────────────────────────────────────

def send_telegram(text, parse_mode="HTML"):
    """Send a message via Telegram bot. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram credentials missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code == 200:
            return True
        else:
            print(f"❌ Telegram API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram send exception: {e}")
        return False

def format_signal_message(signal):
    """Build a single signal message with annotated fields."""
    lines = [
        f"<b>{signal['type']}</b> – {signal['ticker']}",
        f"Entry: ${signal['entry']:.2f} (current close)",
        f"Stop:  ${signal['stop']:.2f} (VAL / POC)",
        f"Target: ${signal['target']:.2f} (VAH)",
        f"R:R: {signal['rr']} → (target‑entry)/(entry‑stop)",
        f"RSI: {signal['rsi']} | 50‑MA: ${signal['ma50']:.2f}",
    ]
    if signal['type'] == 'Support BUY':
        lines.append("💡 Lower half of VA, above 50‑MA, RSI 40‑60")
    elif signal['type'] == 'Breakout BUY':
        lines.append("💡 Above VAH, near LVN, RSI <65, above 50‑MA")
    elif signal['type'] == 'SELL (Short)':
        lines.append("💡 Below VAL, near LVN, below 50‑MA, RSI 30‑50")
    tv_link = f"https://www.tradingview.com/chart/?symbol={signal['ticker']}"
    lines.append(f"<a href='{tv_link}'>📊 View on TradingView</a>")
    return "\n".join(lines)

def build_full_message(signals, no_signal_reasons):
    """Build the entire Telegram message (signals + non-signal reasons)."""
    parts = []
    # Signals section
    if signals:
        header = f"<b>🔍 VP_MA Scan – {len(signals)} signal(s)</b>"
        parts.append(header)
        for sig in signals:
            parts.append(format_signal_message(sig))
        if no_signal_reasons:
            parts.append("<b>Stocks without signals:</b>")
            for ticker, reason in no_signal_reasons.items():
                parts.append(f"• {ticker} – {reason}")
            # Simple "what to watch" hint
            parts.append("<b>⚠️ What to watch next:</b>")
            for ticker, reason in no_signal_reasons.items():
                if "RSI overbought" in reason or "RSI too high" in reason:
                    parts.append(f"• {ticker} → wait for RSI to drop below 60")
                elif "below VAL" in reason:
                    parts.append(f"• {ticker} → wait for recovery above VAL")
                elif "not near 50-MA" in reason:
                    parts.append(f"• {ticker} → wait for pullback to 50-MA")
                elif "no Volume Profile" in reason:
                    parts.append(f"• {ticker} → insufficient volume data")
                else:
                    parts.append(f"• {ticker} → monitor for improvement")
    else:
        # No signals at all – only non-signal reasons
        if no_signal_reasons:
            header = "<b>🔍 VP_MA Scan – no signals found</b>"
            parts.append(header)
            parts.append("<b>All watchlist tickers failed to meet criteria:</b>")
            for ticker, reason in no_signal_reasons.items():
                parts.append(f"• {ticker} – {reason}")
        else:
            parts = ["<b>🔍 VP_MA Scan completed – no setups found.</b>"]
    return "\n\n".join(parts)

# ─── Main Scan ──────────────────────────────────────────────────

def main():
    # Load watchlist
    if not os.path.exists(WATCHLIST_FILE):
        print(f"❌ Watchlist file '{WATCHLIST_FILE}' not found.")
        sys.exit(1)
    try:
        watchlist = pd.read_csv(WATCHLIST_FILE)
        if 'Ticker' not in watchlist.columns:
            print("❌ CSV must have a 'Ticker' column.")
            sys.exit(1)
        tickers = watchlist['Ticker'].dropna().str.strip().tolist()
        print(f"📋 Loaded {len(tickers)} tickers from {WATCHLIST_FILE}")
    except Exception as e:
        print(f"❌ Error reading watchlist: {e}")
        sys.exit(1)

    all_signals = []
    no_signal_reasons = {}
    total = len(tickers)
    for idx, ticker in enumerate(tickers, 1):
        print(f"Scanning {ticker}... ({idx}/{total})")
        ticker = ticker.upper().strip()
        df = get_ticker_data(ticker)
        if df is None or len(df) < 60:
            print(f"   ⚠️  Insufficient data for {ticker}")
            no_signal_reasons[ticker] = "insufficient data (<60 days)"
            continue
        signals, reason = detect_signals(ticker, df)
        if signals:
            all_signals.extend(signals)
            for s in signals:
                print(f"   ✅ {s['type']} on {ticker}")
        else:
            if reason:
                no_signal_reasons[ticker] = reason
                print(f"   ❌ No signal – {reason}")
            else:
                no_signal_reasons[ticker] = "unknown error"

    print(f"\nDone. Found {len(all_signals)} signals, {len(no_signal_reasons)} non-signaled.")

    # Build and send Telegram message
    full_msg = build_full_message(all_signals, no_signal_reasons)
    
    # Split into chunks if too long (Telegram limit ~4096 chars)
    # We split on double newline, trying to keep whole sections together
    chunk_size = 3800
    if len(full_msg) <= chunk_size:
        send_telegram(full_msg)
    else:
        # Simple split on paragraph boundaries
        paragraphs = full_msg.split("\n\n")
        chunk = ""
        for para in paragraphs:
            if len(chunk) + len(para) + 2 > chunk_size:
                send_telegram(chunk)
                chunk = para
            else:
                chunk = (chunk + "\n\n" + para).strip()
        if chunk:
            send_telegram(chunk)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)
