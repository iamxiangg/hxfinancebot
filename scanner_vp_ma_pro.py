#!/usr/bin/env python3
"""
VP_MA_Scan – Automated stock scanner using Volume Profile, 50-day MA, and RSI momentum.
Sends Telegram alerts with PEAD-enhanced signals.
"""

import os
import sys
import json
import warnings
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Suppress yfinance deprecation warning for earnings
warnings.filterwarnings("ignore", category=FutureWarning, message=".*earnings.*")

# ─── Configuration ──────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
RANGE_NAME = os.getenv("GOOGLE_SHEET_RANGE", "Watchlist!A:A")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Volume profile defaults
VA_PERCENT = 0.70
BUCKET_MAX = 500
MIN_BUCKET_PRICE = 0.01
MIN_BUCKET_PCT = 0.001

# Risk/reward
MIN_RR = 1.5

# RSI thresholds
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── Google Sheets ──────────────────────────────────────────────────────────────
def get_service_account_info():
    env_creds = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if env_creds:
        return json.loads(env_creds)
    raise ValueError("GCP_SERVICE_ACCOUNT_JSON not set")

def get_watchlist() -> List[str]:
    try:
        creds = service_account.Credentials.from_service_account_info(
            get_service_account_info(), scopes=SCOPES
        )
        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SHEET_ID, range=RANGE_NAME).execute()
        values = result.get("values", [])
        tickers = [row[0].strip().upper() for row in values if row and row[0].strip()]
        logger.info(f"Loaded {len(tickers)} tickers from sheet.")
        return tickers
    except HttpError as e:
        logger.error(f"Google Sheets API error: {e}")
        return []
    except Exception as e:
        logger.error(f"Error reading sheet: {e}")
        return []

# ─── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> Optional[pd.DataFrame]:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")
        if hist.empty or hist["Volume"].sum() == 0:
            logger.warning(f"{ticker}: no volume data")
            return None
        if len(hist) < 200:
            logger.warning(f"{ticker}: insufficient history ({len(hist)} days)")
            return None

        # Add 50-day MA and RSI (14-day)
        hist["MA50"] = hist["Close"].rolling(window=50).mean()
        delta = hist["Close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0))
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        hist["RSI"] = 100 - (100 / (1 + rs))

        # Check for NaN in recent values
        last = hist.iloc[-1]
        if pd.isna(last["MA50"]) or pd.isna(last["RSI"]):
            logger.warning(f"{ticker}: NaN in indicators")
            return None
        return hist
    except Exception as e:
        logger.error(f"{ticker}: fetch error – {e}")
        return None

# ─── Volume Profile ─────────────────────────────────────────────────────────────
def bucket_price(prices: np.ndarray, volume: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
    """Calculate volume profile buckets with dynamic width."""
    current_price = prices[-1]
    bucket_width = max(current_price * MIN_BUCKET_PCT, MIN_BUCKET_PRICE)
    min_price = prices.min()
    max_price = prices.max()
    n_buckets = min(int((max_price - min_price) / bucket_width) + 1, BUCKET_MAX)
    if n_buckets < 2:
        bucket_width = max((max_price - min_price) / 2, MIN_BUCKET_PRICE)
        n_buckets = 2
    bucket_edges = np.linspace(min_price, max_price, n_buckets + 1)
    bucket_centers = (bucket_edges[:-1] + bucket_edges[1:]) / 2
    vols = np.zeros(n_buckets)
    indices = np.digitize(prices, bucket_edges) - 1
    indices = np.clip(indices, 0, n_buckets - 1)
    np.add.at(vols, indices, volume)
    return bucket_centers, vols, bucket_width

def calculate_value_area(bucket_centers: np.ndarray, volumes: np.ndarray, pct: float) -> Tuple[float, float, float, float]:
    """Find VAL, VAH, POC, and value area total volume."""
    total_vol = volumes.sum()
    if total_vol == 0:
        return np.nan, np.nan, np.nan, np.nan
    target_vol = total_vol * pct
    poc_idx = np.argmax(volumes)
    poc_price = bucket_centers[poc_idx]
    left = right = poc_idx
    sum_vol = volumes[poc_idx]
    while sum_vol < target_vol and (left > 0 or right < len(volumes) - 1):
        if left > 0 and (right == len(volumes) - 1 or volumes[left - 1] >= volumes[right + 1]):
            left -= 1
            sum_vol += volumes[left]
        elif right < len(volumes) - 1:
            right += 1
            sum_vol += volumes[right]
        else:
            break
    return bucket_centers[left], bucket_centers[right], poc_price, total_vol

def find_hvn_lvn(bucket_centers: np.ndarray, volumes: np.ndarray, n: int = 3) -> Tuple[List[float], List[float]]:
    """Find highest and lowest volume nodes (peaks/valleys)."""
    # Simple: top/bottom n buckets by volume
    sorted_idx = np.argsort(volumes)[::-1]
    hvn = [float(bucket_centers[idx]) for idx in sorted_idx[:n] if volumes[idx] > 0]
    # LVN: lowest volume non-zero
    nonzero = volumes > 0
    sorted_low = np.argsort(volumes[nonzero])
    lvn = [float(bucket_centers[nonzero][idx]) for idx in sorted_low[:n]]
    return hvn, lvn

# ─── Signal Logic ───────────────────────────────────────────────────────────────
def check_signal(ticker: str, hist: pd.DataFrame) -> Optional[Dict]:
    """Determine if ticker qualifies for a signal. Returns signal dict or None."""
    last = hist.iloc[-1]
    price = last["Close"]
    ma50 = last["MA50"]
    rsi = last["RSI"]
    vol = last["Volume"]
    vol_ma20 = hist["Volume"].tail(20).mean()
    vol_conf = vol > vol_ma20 * 1.5

    # Volume profile on last 50 days
    lookback = hist.tail(50)
    prices = lookback["Close"].values
    volumes = lookback["Volume"].values
    if len(prices) < 20:
        return None

    bucket_centers, vols, bw = bucket_price(prices, volumes)
    val, vah, poc, _ = calculate_value_area(bucket_centers, vols, VA_PERCENT)
    if np.isnan(val) or np.isnan(vah):
        return None
    hvn, lvn = find_hvn_lvn(bucket_centers, vols)

    # Check price relative to 50-MA
    pct_above_ma = (price - ma50) / ma50 * 100

    signal_type = None
    stop = target = None
    rationale = []

    # --- Support BUY (near or in lower VA, near MA50, RSI 40-60) ---
    if ma50 and price >= ma50 * 0.98 and price <= ma50 * 1.02:
        if val <= price <= (val + vah) / 2 and 40 <= rsi <= 60:
            signal_type = "SUPPORT BUY"
            stop = val if val > 0 else price * 0.97
            target = vah if vah > 0 else price * 1.05
            rationale = ["Price in lower Value Area", "Near 50-MA", "RSI neutral (40-60)"]
            if vol_conf:
                rationale.append("High volume confirmation")

    # --- Breakout BUY (above VAH, near LVN, RSI <65, above MA50) ---
    if not signal_type:
        if price > vah and price > ma50 and rsi < 65 and rsi > 30:
            # Check if price is near an LVN (within 1%)
            near_lvn = any(abs(price - l) / price < 0.01 for l in lvn)
            if near_lvn:
                signal_type = "BREAKOUT BUY"
                stop = poc if poc > 0 else ma50 * 0.98
                target = price + (price - stop) * 2
                rationale = ["Above VAH (breakout)", f"Near LVN ({min(lvn):.2f})", f"RSI {rsi:.0f} (<65)"]
                if vol_conf:
                    rationale.append("High volume confirmation")

    # --- SELL (below VAL, near LVN, RSI 30-50, below MA50) ---
    if not signal_type:
        if price < val and price < ma50 and rsi >= 30 and rsi <= 50:
            near_lvn = any(abs(price - l) / price < 0.01 for l in lvn)
            if near_lvn:
                signal_type = "SELL"
                stop = vah if vah > 0 else ma50 * 1.02
                target = price - (stop - price) * 2
                rationale = ["Below VAL (breakdown)", f"Near LVN ({min(lvn):.2f})", f"RSI {rsi:.0f} (30-50)"]
                if vol_conf:
                    rationale.append("High volume confirmation")

    if not signal_type:
        return None

    # Risk/Reward check
    if stop is None or target is None or stop == price:
        return None
    if signal_type in ["SUPPORT BUY", "BREAKOUT BUY"]:
        risk = price - stop
        reward = target - price
    else:  # SELL
        risk = stop - price
        reward = price - target
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < MIN_RR:
        return None

    return {
        "ticker": ticker,
        "type": signal_type,
        "price": price,
        "stop": stop,
        "target": target,
        "rr": round(rr, 2),
        "ma50": ma50,
        "rsi": round(rsi, 1),
        "vol_confirm": vol_conf,
        "val": val,
        "vah": vah,
        "poc": poc,
        "rationale": rationale,
    }

# ─── PEAD Detection ─────────────────────────────────────────────────────────────
def get_earnings_surprise(ticker: str) -> Optional[float]:
    """Get most recent earnings surprise percentage (positive)."""
    try:
        stock = yf.Ticker(ticker)
        earnings = stock.earnings
        if earnings is None or earnings.empty:
            return None
        # Assuming last row is latest quarter
        latest = earnings.iloc[-1]
        surprise = latest.get("epsSurprisePercent") or latest.get("surprisePercent")
        if surprise is not None and surprise >= 2.0:
            return float(surprise)
        return None
    except Exception:
        return None

def get_pead_enhanced_signals(signals: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Separate signals into PEAD-enhanced and standard, and add PEAD info."""
    pead = []
    standard = []
    for s in signals:
        surprise = get_earnings_surprise(s["ticker"])
        s["pead_surprise"] = surprise
        if surprise:
            pead.append(s)
        else:
            standard.append(s)
    return pead, standard

# ─── Non-Signal Reasons ────────────────────────────────────────────────────────
def classify_non_signal(ticker: str, hist: pd.DataFrame) -> str:
    """Provide a short reason why no signal was generated."""
    last = hist.iloc[-1]
    price = last["Close"]
    ma50 = last["MA50"]
    rsi = last["RSI"]
    lookback = hist.tail(50)
    prices = lookback["Close"].values
    volumes = lookback["Volume"].values
    if len(prices) < 20:
        return "Insufficient data"
    bucket_centers, vols, _ = bucket_price(prices, volumes)
    val, vah, poc, _ = calculate_value_area(bucket_centers, vols, VA_PERCENT)
    if np.isnan(val) or np.isnan(vah):
        return "Volume Profile not available"
    hvn, lvn = find_hvn_lvn(bucket_centers, vols)

    # Check conditions sequentially
    if abs(price - ma50) / ma50 > 0.05:
        return f"Far from 50-MA ({ma50:.2f})"
    if not (30 <= rsi <= 70):
        return f"RSI out of range ({rsi:.0f})"
    if not val <= price <= vah and (price > vah or price < val):
        # Check proximity to LVN
        near_lvn = any(abs(price - l) / price < 0.01 for l in lvn)
        if not near_lvn:
            return "No nearby LVN"
        return "No volume pattern"
    # Could be potential support/breakout but not meeting exact conditions
    return "Other"

# ─── Telegram Messaging ─────────────────────────────────────────────────────────
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing – skipping message")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram max message length: 4096 chars; chunk if needed
    chunks = [message[i:i+4096] for i in range(0, len(message), 4096)]
    for chunk in chunks:
        try:
            resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "Markdown"})
            resp.raise_for_status()
            logger.info(f"Telegram message sent ({len(chunk)} chars)")
        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram send failed: {e}")

def format_signal(s: Dict) -> str:
    """Format a single signal into a readable line."""
    t = s["ticker"]
    typ = s["type"]
    price = s["price"]
    stop = s["stop"]
    target = s["target"]
    rr = s["rr"]
    ma50 = s["ma50"]
    rsi = s["rsi"]
    vol = "✅" if s["vol_confirm"] else ""
    # TradingView link
    tv_link = f"[TradingView](https://www.tradingview.com/chart/?symbol={t})"
    # PEAD
    pead_tag = ""
    if s.get("pead_surprise"):
        pead_tag = f" 🚀PEAD+{s['pead_surprise']:.1f}%"
    # Rationale
    rationale = " | ".join(s["rationale"])
    stop_reason = "VAL / POC" if s["stop"] == s.get("val") else "POC"
    # Line
    line = (
        f"*{t}* – {typ} {vol}{pead_tag}\n"
        f"Price: ${price:.2f} | Stop: ${stop:.2f} ({stop_reason}) | Target: ${target:.2f} (VAH)\n"
        f"RR: {rr} | MA50: ${ma50:.2f} | RSI: {rsi}\n"
        f"💡 {rationale}\n"
        f"{tv_link}\n"
    )
    return line

def format_non_signal(ticker: str, reason: str) -> str:
    return f"• *{ticker}* – {reason}"

def build_telegram_message(pead_signals: List[Dict], standard_signals: List[Dict], non_signals: Dict[str, str]) -> str:
    """Create the full Telegram message with sections."""
    lines = []
    lines.append(f"📊 *VP-MA Scan – {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    # Priority: PEAD-enhanced first
    if pead_signals:
        lines.append("🚀 *PEAD-Enhanced Signals*")
        for s in pead_signals:
            lines.append(format_signal(s))
        lines.append("")

    # Standard signals
    if standard_signals:
        lines.append("📈 *Standard Signals*")
        for s in standard_signals:
            lines.append(format_signal(s))
        lines.append("")

    # Non-signaled tickers (collapsed by category)
    if non_signals:
        lines.append("📌 *Watchlist (No Signal Today)*")
        # Group by reason
        reason_groups = {}
        for ticker, reason in non_signals.items():
            reason_groups.setdefault(reason, []).append(ticker)
        for reason, tickers in reason_groups.items():
            ticker_list = ", ".join(tickers)
            lines.append(f"*{reason}*: {ticker_list}")
        lines.append("")

    return "\n".join(lines)

# ─── Main Scan ──────────────────────────────────────────────────────────────────
def run_scan():
    logger.info("Starting VP-MA Scan...")
    tickers = get_watchlist()
    if not tickers:
        logger.error("No tickers loaded. Exiting.")
        return

    all_signals = []
    non_signals = {}

    for ticker in tickers:
        logger.info(f"Processing {ticker}...")
        hist = fetch_data(ticker)
        if hist is None:
            non_signals[ticker] = "Data fetch failed"
            continue
        sig = check_signal(ticker, hist)
        if sig:
            all_signals.append(sig)
        else:
            reason = classify_non_signal(ticker, hist)
            non_signals[ticker] = reason

    # PEAD overlay
    pead, standard = get_pead_enhanced_signals(all_signals)

    # Build and send message
    msg = build_telegram_message(pead, standard, non_signals)
    send_telegram_message(msg)
    logger.info(f"Scan complete. {len(pead)} PEAD, {len(standard)} standard, {len(non_signals)} non-signals.")

# ─── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_scan()
