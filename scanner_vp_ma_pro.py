#!/usr/bin/env python3
# file: scanner_vp_ma_pro.py
# Final version – Volume Profile + MA + RSI scanner with categorized non‑signal summary

import os
import sys
import datetime
import json
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
import warnings
warnings.filterwarnings("ignore")

# ========== CONFIG ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WATCHLIST_FILE = "positions.csv"          # must be in repo root
MIN_RISK_REWARD = 1.5
VOLUME_PROFILE_LOOKBACK = 300
VALUE_AREA_PERCENT = 0.70

# ========== HELPER CLASS: Volume Profile ==========
class VolumeProfile:
    def __init__(self, df, lookback=300):
        """
        Expects a DataFrame with columns 'Close', 'High', 'Low', 'Volume'.
        Computes VP on the last `lookback` bars.
        """
        self.df = df.tail(lookback).copy()
        self.price_min = self.df['Low'].min()
        self.price_max = self.df['High'].max()
        self.price_range = self.price_max - self.price_min
        # dynamic bucket width (minimum $0.01, at least 0.1% of price)
        median_price = self.df['Close'].median()
        bucket_width = max(median_price * 0.001, 0.01)
        num_buckets = min(int(self.price_range / bucket_width), 500)
        if num_buckets < 10:
            num_buckets = 10
        self.bucket_width = self.price_range / num_buckets
        self.buckets = np.linspace(self.price_min, self.price_max, num_buckets + 1)
        self.bucket_centers = (self.buckets[:-1] + self.buckets[1:]) / 2

        # Accumulate volume per bucket
        volume_by_bucket = np.zeros(num_buckets)
        for _, row in self.df.iterrows():
            low_idx = np.searchsorted(self.buckets, row['Low']) - 1
            high_idx = np.searchsorted(self.buckets, row['High']) - 1
            # simpler: assign volume to bucket containing close (or spread)
            close_idx = np.searchsorted(self.buckets, row['Close']) - 1
            if 0 <= close_idx < num_buckets:
                volume_by_bucket[close_idx] += row['Volume']

        self.volume_by_bucket = volume_by_bucket
        total_volume = volume_by_bucket.sum()
        self.total_volume = total_volume

        # Value Area: find MinVA to MaxVA that contains VA% of volume
        sorted_indices = np.argsort(-volume_by_bucket)  # descending volume
        target_volume = total_volume * VALUE_AREA_PERCENT
        cum_vol = 0.0
        va_indices = set()
        for idx in sorted_indices:
            if cum_vol >= target_volume:
                break
            va_indices.add(idx)
            cum_vol += volume_by_bucket[idx]

        if va_indices:
            self.val = self.buckets[min(va_indices)] if min(va_indices) > 0 else self.price_min
            self.vah = self.buckets[max(va_indices) + 1] if max(va_indices) < num_buckets else self.price_max
        else:
            self.val = self.price_min
            self.vah = self.price_max

        self.va_midpoint = (self.val + self.vah) / 2

        # Point of Control (POC) – bucket with highest volume
        poc_index = np.argmax(volume_by_bucket)
        self.poc = self.bucket_centers[poc_index]

        # High Volume Nodes (HVN) and Low Volume Nodes (LVN)
        mean_vol = volume_by_bucket.mean()
        std_vol = volume_by_bucket.std()
        threshold_hvn = mean_vol + 1.5 * std_vol  # HVN: >1.5 sigma
        threshold_lvn = mean_vol - 1.0 * std_vol  # LVN: <1 sigma (low volume)
        self.hvn_prices = [self.bucket_centers[i] for i in range(num_buckets) if volume_by_bucket[i] > threshold_hvn]
        self.lvn_prices = [self.bucket_centers[i] for i in range(num_buckets) if volume_by_bucket[i] < threshold_lvn]

        # nearest LVN above/below current price helper
        self.nearest_lvn_above = None
        self.nearest_lvn_below = None
        current_price = self.df['Close'].iloc[-1]
        above = [p for p in self.lvn_prices if p > current_price]
        below = [p for p in self.lvn_prices if p < current_price]
        if above:
            self.nearest_lvn_above = min(above)
        if below:
            self.nearest_lvn_below = max(below)

    def print_summary(self):
        print(f"  VAL: {self.val:.2f}, VAH: {self.vah:.2f}, POC: {self.poc:.2f}")
        print(f"  VA Midpoint: {self.va_midpoint:.2f}")
        print(f"  Nearest LVN below: {self.nearest_lvn_below}, above: {self.nearest_lvn_above}")

# ========== FETCH DATA ==========
def fetch_data(ticker):
    """Return DataFrame with OHLCV for last 400+ days (to allow indicators)."""
    try:
        df = yf.download(ticker, period="2y", progress=False)
        if df.empty:
            return None
        # Flatten MultiIndex columns if present (yfinance bug)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [col.capitalize() for col in df.columns]
        # Ensure required columns exist
        for c in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if c not in df.columns:
                return None
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        print(f"  Error fetching {ticker}: {e}")
        return None

# ========== INDICATORS ==========
def add_indicators(df):
    """Add 50‑day SMA (close) and 14‑bar RSI."""
    df['SMA_50'] = SMAIndicator(close=df['Close'], window=50).sma_indicator()
    df['RSI_14'] = RSIIndicator(close=df['Close'], window=14).rsi()
    return df

# ========== SIGNAL LOGIC ==========
def evaluate_ticker(ticker):
    """
    Returns a dict with:
      - 'signal': None or dict with keys type, price, stop, target, reason, confidence
      - 'reason_no_signal': string (primary category) if no signal
      - 'details': extra info for debugging
    """
    result = {"ticker": ticker, "signal": None, "reason_no_signal": "", "details": ""}
    df_raw = fetch_data(ticker)
    if df_raw is None or len(df_raw) < 60:
        result["reason_no_signal"] = "Insufficient data"
        return result

    df = add_indicators(df_raw)
    last = df.iloc[-1]
    price = last['Close']
    sma50 = last['SMA_50']
    rsi = last['RSI_14']

    # Check for NaN indicators (not enough data)
    if pd.isna(sma50) or pd.isna(rsi):
        result["reason_no_signal"] = "Indicators not ready"
        return result

    # Volume Profile
    vp = VolumeProfile(df, lookback=VOLUME_PROFILE_LOOKBACK)

    # ========== Support BUY ==========
    # Criteria:
    #   - Price inside Value Area (or slightly above VAL)
    #   - Price in lower half of VA (below VA_midpoint)
    #   - Price near 50‑MA (±2% or ±0.5%)
    #   - RSI between 40 and 60
    #   - Risk/Reward >= MIN_RISK_REWARD
    support_signal = None
    if (vp.val <= price <= vp.vah) and (price <= vp.va_midpoint):
        # distance to 50‑MA
        pct_from_ma = abs(price - sma50) / sma50 * 100
        if pct_from_ma > 5.0:
            result["reason_no_signal"] = "Far from 50‑MA"
        elif not (40 <= rsi <= 60):
            result["reason_no_signal"] = "RSI out of range (support)"
        else:
            # Use VAL as stop, if too tight (<1%) use POC
            stop_candidate = vp.val
            if (price - stop_candidate) / price < 0.01:
                stop_candidate = vp.poc
            # Ensure stop is still below price
            if stop_candidate >= price:
                stop_candidate = price * 0.98  # fallback 2% stop
            risk = price - stop_candidate
            # Target: nearest LVN above, else VAH+1 Atr? Simplified: VAH + (VAH-VAL)*0.5
            if vp.nearest_lvn_above:
                target = vp.nearest_lvn_above
            else:
                target = vp.vah + (vp.vah - vp.val) * 0.5
            reward = target - price
            if reward <= 0 or risk <= 0:
                result["reason_no_signal"] = "No valid target/stop"
            else:
                rr = reward / risk
                if rr < MIN_RISK_REWARD:
                    result["reason_no_signal"] = "Low risk/reward"
                else:
                    support_signal = {
                        "type": "BUY (Support)",
                        "price": round(price, 2),
                        "stop": round(stop_candidate, 2),
                        "target": round(target, 2),
                        "rr": round(rr, 2),
                        "reason": f"VAL={vp.val:.2f}, POC={vp.poc:.2f}, SMA50={sma50:.2f}, RSI={rsi:.1f}",
                        "chart_link": f"https://www.tradingview.com/chart/?symbol={ticker}"
                    }

    # ========== Breakout BUY ==========
    breakout_signal = None
    if support_signal is None:  # only evaluate if no support signal
        # Conditions: price above VAH, RSI <65, price above 50‑MA, near LVN (optional)
        if price > vp.vah and price > sma50 and rsi < 65:
            # Near an LVN? (within 1% of price)
            near_lvn = False
            for lvn_price in vp.lvn_prices:
                if abs(price - lvn_price) / price < 0.01:
                    near_lvn = True
                    break
            if near_lvn:
                # Stop below LVN or below VAH? Use VAH as stop (breakout failure)
                stop_candidate = vp.vah
                risk = price - stop_candidate
                if risk <= 0:
                    risk = price * 0.02  # fallback
                    stop_candidate = price - risk
                # Target: nearest HVN above, else POC + 1 ATR? Simple: price + 3*risk
                target = price + 3 * risk
                reward = target - price
                rr = reward / risk
                if rr >= MIN_RISK_REWARD:
                    breakout_signal = {
                        "type": "BUY (Breakout)",
                        "price": round(price, 2),
                        "stop": round(stop_candidate, 2),
                        "target": round(target, 2),
                        "rr": round(rr, 2),
                        "reason": f"VAH={vp.vah:.2f}, near LVN, SMA50={sma50:.2f}, RSI={rsi:.1f}",
                        "chart_link": f"https://www.tradingview.com/chart/?symbol={ticker}"
                    }
                else:
                    result["reason_no_signal"] = "Low risk/reward (breakout)"
            else:
                result["reason_no_signal"] = "No nearby LVN (breakout)"
        else:
            if price <= vp.vah:
                result["reason_no_signal"] = "Below VAH (breakout)"
            elif rsi >= 65:
                result["reason_no_signal"] = "RSI overbought (breakout)"
            elif price <= sma50:
                result["reason_no_signal"] = "Below 50‑MA (breakout)"

    # ========== SELL ==========
    sell_signal = None
    if support_signal is None and breakout_signal is None:
        # Conditions: price below VAL, below 50‑MA, RSI 30‑50, near LVN
        if price < vp.val and price < sma50 and (30 <= rsi <= 50):
            # Near LVN below?
            near_lvn = False
            for lvn_price in vp.lvn_prices:
                if abs(price - lvn_price) / price < 0.01:
                    near_lvn = True
                    break
            if near_lvn:
                stop_candidate = vp.val  # if price bounces back above VAL, exit
                risk = stop_candidate - price
                if risk <= 0:
                    risk = price * 0.02
                    stop_candidate = price + risk
                target = price - 2 * risk  # simple 2:1 reward
                reward = price - target
                rr = reward / risk
                if rr >= MIN_RISK_REWARD:
                    sell_signal = {
                        "type": "SELL",
                        "price": round(price, 2),
                        "stop": round(stop_candidate, 2),
                        "target": round(target, 2),
                        "rr": round(rr, 2),
                        "reason": f"VAL={vp.val:.2f}, near LVN, SMA50={sma50:.2f}, RSI={rsi:.1f}",
                        "chart_link": f"https://www.tradingview.com/chart/?symbol={ticker}"
                    }
                else:
                    result["reason_no_signal"] = "Low risk/reward (sell)"
            else:
                result["reason_no_signal"] = "No nearby LVN (sell)"
        else:
            if price >= vp.val:
                result["reason_no_signal"] = "Above VAL (sell)"
            elif price >= sma50:
                result["reason_no_signal"] = "Above 50‑MA (sell)"
            else:
                result["reason_no_signal"] = "RSI out of range (sell)"

    # Pick first valid signal (priority: support, breakout, sell)
    if support_signal:
        result["signal"] = support_signal
        result["reason_no_signal"] = ""
    elif breakout_signal:
        result["signal"] = breakout_signal
        result["reason_no_signal"] = ""
    elif sell_signal:
        result["signal"] = sell_signal
        result["reason_no_signal"] = ""

    # If we didn't set a reason_no_signal earlier, set a generic one
    if not result["signal"] and not result["reason_no_signal"]:
        result["reason_no_signal"] = "No setup"

    return result

# ========== READ WATCHLIST ==========
def read_watchlist():
    """Return list of tickers from positions.csv (assuming one column header 'Symbol')."""
    try:
        df = pd.read_csv(WATCHLIST_FILE)
        if 'Symbol' in df.columns:
            return df['Symbol'].dropna().str.strip().str.upper().tolist()
        else:
            # fallback: first column
            return df.iloc[:, 0].dropna().str.strip().str.upper().tolist()
    except Exception as e:
        print(f"Error reading {WATCHLIST_FILE}: {e}")
        return []

# ========== TELEGRAM MESSAGE ==========
def send_telegram_message(text, parse_mode='HTML'):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing, printing message instead.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.text}")
    except Exception as e:
        print(f"Telegram send exception: {e}")

def build_full_message(results):
    """
    Build an HTML message with:
      - Header with date
      - Actionable signals (with TradingView links)
      - Categorized non‑signal summary with advice
    """
    lines = []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"<b>📊 VP+MA+RSI Scanner</b> | {now}")
    lines.append("")

    # Separate signals and non‑signals
    signals = [r for r in results if r["signal"] is not None]
    no_signals = [r for r in results if r["signal"] is None]

    # ---- Actionable Signals ----
    if signals:
        lines.append("<b>✅ Actionable Setups</b>")
        lines.append("")
        for r in signals:
            s = r["signal"]
            lines.append(
                f"<b>{s['type']}</b> | {r['ticker']} @ ${s['price']} "
                f"(Stop ${s['stop']} | Target ${s['target']} | RR {s['rr']})"
            )
            lines.append(f"   Reason: {s['reason']}")
            lines.append(f"   <a href='{s['chart_link']}'>📈 TradingView Chart</a>")
            lines.append("")
    else:
        lines.append("<b>⚠️ No actionable setups</b>")
        lines.append("")

    # ---- Categorized Non‑Signal Summary ----
    if no_signals:
        lines.append("<b>❌ Non‑Signal Summary</b>")
        lines.append("")
        # Group by reason_no_signal
        categories = {}
        for r in no_signals:
            cat = r["reason_no_signal"]
            categories.setdefault(cat, []).append(r["ticker"])

        # Advice mapping
        advice_map = {
            "Insufficient data": "Check ticker symbol or data availability.",
            "Indicators not ready": "Need more price history.",
            "Far from 50‑MA": "Wait for pullback to ~50‑MA before entering.",
            "RSI out of range (support)": "RSI not in oversold/neutral zone; wait for RSI <60.",
            "RSI overbought (breakout)": "RSI too high; wait for RSI <60.",
            "RSI out of range (sell)": "RSI not in sell zone (30–50); wait for RSI <50.",
            "No valid target/stop": "Volume Profile insufficient; monitor.",
            "Low risk/reward": "Risk/reward below 1.5; wait for better entry.",
            "No nearby LVN (breakout)": "Breakout lacks low‑volume validation; wait for LVN test.",
            "No nearby LVN (sell)": "Sell signal lacks low‑volume validation.",
            "Below VAH (breakout)": "Price not above value area high; wait for breakout.",
            "Below 50‑MA (breakout)": "Price below trend filter; wait for reclaim of 50‑MA.",
            "Above VAL (sell)": "Price not below value area low; wait for breakdown.",
            "Above 50‑MA (sell)": "Sell requires price below 50‑MA.",
            "No setup": "No specific condition met; monitor.",
        }

        for cat, tickers in categories.items():
            ticker_list = ", ".join(tickers)
            advice = advice_map.get(cat, "Check manually.")
            lines.append(f"<b>{cat}</b> ({len(tickers)}): {ticker_list}")
            lines.append(f"   💡 {advice}")
            lines.append("")

    return "\n".join(lines)

# ========== MAIN ==========
def main():
    print("="*60)
    print("VP+MA+RSI Scanner")
    print("="*60)

    tickers = read_watchlist()
    if not tickers:
        print("No tickers found in watchlist. Exiting.")
        return

    print(f"Scanning {len(tickers)} tickers...\n")
    results = []
    for ticker in tickers:
        print(f"Processing {ticker}...")
        res = evaluate_ticker(ticker)
        results.append(res)
        if res["signal"]:
            print(f"  -> SIGNAL: {res['signal']['type']} @ ${res['signal']['price']}")
        else:
            print(f"  -> No signal ({res['reason_no_signal']})")
        print()

    # Build message
    message = build_full_message(results)
    print("="*60)
    print("Sending Telegram message...")
    send_telegram_message(message)
    print("Done.")

if __name__ == "__main__":
    main()
