#!/usr/bin/env python3
"""
VP_MA_Scan – Automated Stock Scanner with Volume Profile, 50-MA, and RSI confluence.
Generates trade signals and sends Telegram notifications.
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple

import pandas as pd
import numpy as np
import yfinance as yf
import requests

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WATCHLIST_FILE = "positions.csv"

# Trading parameters
VA_PERCENT = 0.70
MIN_RR = 1.5
LOOKBACK_DAYS = 120  # enough for volume profile
MAX_BUCKETS = 500
MIN_BUCKET_PRICE = 0.01
VA_MIDPOINT_MAX_PERCENT = 0.50  # support BUY: price must be in lower 50% of VA
MA_PERIOD = 50
RSI_PERIOD = 14

# Breakout & Short targets/stops
BREAKOUT_TARGET_MULT = 1.05
BREAKOUT_STOP_MULT = 0.98
SHORT_TARGET_MULT = 0.95
SHORT_STOP_MULT = 1.02

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------
def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns from yfinance."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(col).strip() for col in df.columns.values]
    return df


def calculate_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_volume_profile(price: pd.Series, volume: pd.Series, va_percent: float = VA_PERCENT, max_buckets: int = MAX_BUCKETS):
    """
    Compute Value Area (VAH, VAL, POC), Volume Nodes (HVN, LVN).
    Returns dict or None if insufficient data.
    """
    if len(price) < 20:
        return None

    # Determine bucket width dynamically (0.1% of average price, min $0.01)
    avg_price = price.mean()
    bucket_width = max(avg_price * 0.001, MIN_BUCKET_PRICE)

    # Build price buckets
    min_price = price.min()
    max_price = price.max()
    nb_buckets = min(max_buckets, int((max_price - min_price) / bucket_width) + 1)
    if nb_buckets < 10:
        return None

    bins = np.linspace(min_price, max_price, nb_buckets + 1)
    bucket_indices = np.digitize(price, bins=bins) - 1  # 0-indexed

    # Aggregate volume per bucket
    bucket_volumes = np.zeros(nb_buckets)
    for idx, vol in zip(bucket_indices, volume):
        if 0 <= idx < nb_buckets:
            bucket_volumes[idx] += vol

    # Find POC (bucket with max volume)
    poc_bucket = np.argmax(bucket_volumes)
    poc_price = (bins[poc_bucket] + bins[poc_bucket + 1]) / 2

    # Build sorted volume profile (by price)
    bucket_centers = (bins[:-1] + bins[1:]) / 2
    profile = sorted(zip(bucket_centers, bucket_volumes), key=lambda x: x[0])
    centers, volumes = zip(*profile) if profile else ([], [])

    total_volume = sum(volumes)
    target_volume = total_volume * va_percent

    # Start from POC outward
    poc_idx = np.where(np.isclose(centers, poc_price))[0][0]
    included = [poc_idx]
    current_vol = volumes[poc_idx]
    left = poc_idx - 1
    right = poc_idx + 1

    while current_vol < target_volume:
        left_vol = volumes[left] if left >= 0 else 0
        right_vol = volumes[right] if right < len(volumes) else 0
        if left_vol >= right_vol and left >= 0:
            included.append(left)
            current_vol += left_vol
            left -= 1
        elif right < len(volumes):
            included.append(right)
            current_vol += right_vol
            right += 1
        else:
            break

    included.sort()
    val_price = centers[included[0]]
    vah_price = centers[included[-1]]

    # Determine High Volume Nodes (HVN) and Low Volume Nodes (LVN)
    mean_vol = np.mean(volumes)
    std_vol = np.std(volumes)
    hvn_threshold = mean_vol + 0.5 * std_vol
    lvn_threshold = mean_vol - 0.5 * std_vol

    hvn_prices = [c for c, v in zip(centers, volumes) if v >= hvn_threshold]
    lvn_prices = [c for c, v in zip(centers, volumes) if v <= lvn_threshold and v > 0]

    return {
        "val": val_price,
        "vah": vah_price,
        "poc": poc_price,
        "hvn": hvn_prices,
        "lvn": lvn_prices,
        "buckets": list(zip(centers, volumes)),
    }


def find_nearest_lvn(price: float, lvn_list: List[float], direction: str = "above") -> Optional[float]:
    """Find nearest LVN above or below the given price."""
    if not lvn_list:
        return None
    candidates = [lvn for lvn in lvn_list if (direction == "above" and lvn > price) or (direction == "below" and lvn < price)]
    if not candidates:
        return None
    return min(candidates, key=lambda x: abs(x - price))


def categorize_reason(ticker: str, close: float, ma50: float, rsi: float, vp: Optional[dict]) -> str:
    """
    Determine the reason a ticker did NOT generate a signal.
    Returns a human-readable string.
    """
    reasons = []
    if ma50 is None or np.isnan(ma50):
        return "Insufficient data (no 50‑MA)"
    ma50 = float(ma50)
    close = float(close)

    # Distance from 50-MA
    pct_from_ma = abs(close - ma50) / ma50 * 100
    if pct_from_ma > 10:
        return "Very far from 50‑MA"
    if pct_from_ma > 5:
        reasons.append("Far from 50‑MA")

    if rsi is None or np.isnan(rsi):
        reasons.append("RSI not available")
    else:
        rsi = float(rsi)

    if vp is None:
        reasons.append("Volume Profile not available")
        return "; ".join(reasons) if reasons else "Unknown"

    val = vp["val"]
    vah = vp["vah"]
    poc = vp["poc"]

    # Check for Support BUY conditions
    va_midpoint = (val + vah) / 2
    if close <= va_midpoint:
        reasons.append("Below VA midpoint (support BUY possible)")
    else:
        if close <= vah * 1.02:
            reasons.append("Near VAH (breakout candidate but no LVN gap)")
        else:
            reasons.append("Above VAH with no clear pattern")

    # Check RSI for each signal type
    if rsi is not None:
        if 40 <= rsi <= 60:
            pass  # neutral
        elif rsi < 40:
            reasons.append("RSI oversold")
        else:  # rsi > 60
            reasons.append("RSI overbought")

    # Check LVN for breakout/short
    if close > vah:
        nearest_lvn_above = find_nearest_lvn(close, vp.get("lvn", []), "above")
        if nearest_lvn_above is not None and (nearest_lvn_above - close) / close < 0.02:
            reasons.append("LVN gap for breakout present")
        else:
            reasons.append("No clear LVN above for breakout")
    elif close < val:
        nearest_lvn_below = find_nearest_lvn(close, vp.get("lvn", []), "below")
        if nearest_lvn_below is not None and (close - nearest_lvn_below) / close < 0.02:
            reasons.append("LVN gap for short present")
        else:
            reasons.append("No clear LVN below for short")

    # Combine reasons
    return "; ".join(reasons) if reasons else "No specific reason"


# -------------------------------------------------------------------
# Signal detection
# -------------------------------------------------------------------
def check_support_buy(ticker: str, close: float, ma50: float, rsi: float, vp: dict) -> Optional[dict]:
    """Support BUY logic: price in lower 50% of VA, within 2% of 50-MA, RSI 40-60."""
    if ma50 is None or np.isnan(ma50) or rsi is None or np.isnan(rsi):
        return None
    if not (40 <= rsi <= 60):
        return None

    val = vp["val"]
    vah = vp["vah"]
    va_midpoint = (val + vah) / 2
    if close > va_midpoint:
        return None

    # Close within 2% of 50-MA
    pct_from_ma = abs(close - ma50) / ma50 * 100
    if pct_from_ma > 2:
        return None

    # Stops: VAL or POC (whichever is lower)
    stop = min(val, vp["poc"])
    # Target: VAH or next HVN above
    hvns_above = [h for h in vp.get("hvn", []) if h > close]
    target = vp["vah"] if not hvns_above else min(hvns_above)

    # Risk/Reward
    risk = close - stop
    reward = target - close
    if risk <= 0 or reward <= 0 or (reward / risk) < MIN_RR:
        return None

    return {
        "action": "BUY",
        "type": "Support",
        "entry": round(close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(reward / risk, 2),
        "rationale": f"Support BUY: price {close:.2f} below VA midpoint {va_midpoint:.2f}, "
                     f"within {pct_from_ma:.1f}% of 50‑MA ({ma50:.2f}), RSI {rsi:.1f} (40‑60). "
                     f"Stop at VAL/POC ${stop:.2f}, target VAH/HVN ${target:.2f}.",
        "stop_reason": "VAL / POC",
    }


def check_breakout_buy(ticker: str, close: float, ma50: float, rsi: float, vp: dict) -> Optional[dict]:
    """Breakout BUY: price above VAH, near LVN, RSI <65, above 50-MA."""
    if ma50 is None or np.isnan(ma50) or rsi is None or np.isnan(rsi):
        return None
    if close <= ma50:
        return None
    if rsi >= 65:
        return None

    vah = vp["vah"]
    if close <= vah:
        return None

    # Near an LVN above? (within 2% of close)
    lvn_above = find_nearest_lvn(close, vp.get("lvn", []), "above")
    if lvn_above is None:
        return None
    gap_pct = (lvn_above - close) / close * 100
    if gap_pct > 2:
        return None

    # Target: close * 1.05
    target = close * BREAKOUT_TARGET_MULT
    stop = vah * BREAKOUT_STOP_MULT

    risk = close - stop
    reward = target - close
    if risk <= 0 or reward <= 0 or (reward / risk) < MIN_RR:
        return None

    return {
        "action": "BUY",
        "type": "Breakout",
        "entry": round(close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(reward / risk, 2),
        "rationale": f"Breakout BUY: price {close:.2f} above VAH {vah:.2f}, "
                     f"near LVN {lvn_above:.2f} (gap {gap_pct:.1f}%), "
                     f"RSI {rsi:.1f} (<65), above 50‑MA {ma50:.2f}. "
                     f"Target ${target:.2f}, stop ${stop:.2f} (VAH × 0.98).",
        "stop_reason": "VAH × 0.98",
    }


def check_short(ticker: str, close: float, ma50: float, rsi: float, vp: dict) -> Optional[dict]:
    """SELL short: price below VAL, near LVN, RSI 30-50, below 50-MA."""
    if ma50 is None or np.isnan(ma50) or rsi is None or np.isnan(rsi):
        return None
    if close >= ma50:
        return None
    if not (30 <= rsi <= 50):
        return None

    val = vp["val"]
    if close >= val:
        return None

    # Near an LVN below?
    lvn_below = find_nearest_lvn(close, vp.get("lvn", []), "below")
    if lvn_below is None:
        return None
    gap_pct = (close - lvn_below) / close * 100
    if gap_pct > 2:
        return None

    target = close * SHORT_TARGET_MULT
    stop = val * SHORT_STOP_MULT

    risk = stop - close
    reward = close - target
    if risk <= 0 or reward <= 0 or (reward / risk) < MIN_RR:
        return None

    return {
        "action": "SELL",
        "type": "Short",
        "entry": round(close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(reward / risk, 2),
        "rationale": f"Short SELL: price {close:.2f} below VAL {val:.2f}, "
                     f"near LVN {lvn_below:.2f} (gap {gap_pct:.1f}%), "
                     f"RSI {rsi:.1f} (30‑50), below 50‑MA {ma50:.2f}. "
                     f"Target ${target:.2f}, stop ${stop:.2f} (VAL × 1.02).",
        "stop_reason": "VAL × 1.02",
    }


# -------------------------------------------------------------------
# Scanner
# -------------------------------------------------------------------
def scan_ticker(ticker: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    Scan a single ticker. Returns (signal_dict, reason_string).
    signal_dict is None if no signal.
    """
    try:
        # Download data
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=LOOKBACK_DAYS + 20)  # extra buffer
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if df.empty:
            return None, "No data"

        df = flatten_columns(df)

        # Rename columns based on yfinance output
        # Typical columns: 'Open', 'High', 'Low', 'Close', 'Volume', or 'Adj Close'
        # We need close and volume
        close_col = "Close" if "Close" in df.columns else ("Adj Close" if "Adj Close" in df.columns else None)
        volume_col = "Volume" if "Volume" in df.columns else None
        if close_col is None or volume_col is None:
            return None, "Required columns missing"

        close = df[close_col].dropna()
        volume = df[volume_col].dropna()
        # Align index
        common_idx = close.index.intersection(volume.index)
        close = close.loc[common_idx]
        volume = volume.loc[common_idx]

        if len(close) < MA_PERIOD + RSI_PERIOD:
            return None, "Insufficient data for indicators"

        # Last values
        last_close = float(close.iloc[-1])
        # 50-day MA
        ma50 = float(close.rolling(MA_PERIOD).mean().iloc[-1])
        # RSI
        rsi_series = calculate_rsi(close, RSI_PERIOD)
        last_rsi = float(rsi_series.iloc[-1])

        # Volume Profile on recent data (last LOOKBACK_DAYS bars)
        recent_close = close.iloc[-LOOKBACK_DAYS:]
        recent_volume = volume.iloc[-LOOKBACK_DAYS:]
        vp = calculate_volume_profile(recent_close, recent_volume)

        if vp is None:
            return None, "Volume Profile calculation failed"

        # Check signals in order
        signal = check_support_buy(ticker, last_close, ma50, last_rsi, vp)
        if signal:
            return signal, None

        signal = check_breakout_buy(ticker, last_close, ma50, last_rsi, vp)
        if signal:
            return signal, None

        signal = check_short(ticker, last_close, ma50, last_rsi, vp)
        if signal:
            return signal, None

        # No signal: get reason
        reason = categorize_reason(ticker, last_close, ma50, last_rsi, vp)
        return None, reason

    except Exception as e:
        logger.exception(f"Error scanning {ticker}: {e}")
        return None, str(e)


# -------------------------------------------------------------------
# Telegram output
# -------------------------------------------------------------------
def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not set, message not sent.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode}
    try:
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def build_signal_message(signal: dict) -> str:
    """HTML formatted message for a trade signal."""
    lines = [
        f"<b>{signal['action']} {signal['type']} – {signal['ticker']}</b>",
        f"Entry: ${signal['entry']:.2f}",
        f"Stop: ${signal['stop']:.2f} ({signal['stop_reason']})",
        f"Target: ${signal['target']:.2f}",
        f"Risk/Reward: {signal['rr']:.2f}",
        f"Rationale: {signal['rationale']}",
    ]
    return "\n".join(lines)


def build_full_message(signals: List[dict], non_signals: Dict[str, List[str]]) -> str:
    """Build complete Telegram message with signal details and grouped non-signal tickers."""
    parts = []
    # Signals first
    if signals:
        parts.append("<b>🚦 TRADE SIGNALS FOUND</b>")
        for s in signals:
            parts.append(build_signal_message(s))
            parts.append("")  # blank line

    # Non-signals grouped by reason
    if non_signals:
        parts.append("<b>❌ NO SIGNAL – Grouped by Reason</b>")
        for reason, tickers in non_signals.items():
            # Provide a "what to watch" suggestion
            suggestion = ""
            if "Very far from 50‑MA" in reason:
                suggestion = "💡 Watch if price approaches 50‑MA."
            elif "Far from 50‑MA" in reason:
                suggestion = "💡 Monitor for closer proximity to 50‑MA."
            elif "Below VA midpoint" in reason:
                suggestion = "💡 Could become Support BUY if RSI enters 40‑60."
            elif "Near VAH" in reason:
                suggestion = "💡 Breakout candidate – wait for LVN gap."
            elif "Above VAH" in reason:
                suggestion = "💡 Possible breakout – need LVN confirmation."
            elif "RSI oversold" in reason:
                suggestion = "💡 Oversold – may bounce to 50‑MA."
            elif "RSI overbought" in reason:
                suggestion = "💡 Overbought – could pull back."
            elif "LVN gap" in reason:
                suggestion = "💡 Gap exists – confirm volume before trade."
            else:
                suggestion = "💡 Continue monitoring."

            ticker_list = ", ".join(tickers)
            parts.append(f"🔹 <b>{reason}</b>: {ticker_list}")
            parts.append(f"   {suggestion}")
            parts.append("")

    # Remove trailing empty lines
    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts)


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    # Read watchlist
    if not os.path.exists(WATCHLIST_FILE):
        logger.error(f"Watchlist file {WATCHLIST_FILE} not found.")
        sys.exit(1)
    try:
        df = pd.read_csv(WATCHLIST_FILE)
        if "Ticker" not in df.columns:
            logger.error("CSV must contain 'Ticker' column.")
            sys.exit(1)
        tickers = df["Ticker"].dropna().unique().tolist()
        tickers = [t.strip().upper() for t in tickers if isinstance(t, str) and t.strip()]
    except Exception as e:
        logger.error(f"Failed to read {WATCHLIST_FILE}: {e}")
        sys.exit(1)

    if not tickers:
        logger.warning("No tickers in watchlist.")
        return

    logger.info(f"Scanning {len(tickers)} tickers: {', '.join(tickers)}")

    signals = []
    non_signal_reasons = {}  # reason -> list of tickers

    for ticker in tickers:
        logger.info(f"Processing {ticker}...")
        sig, reason = scan_ticker(ticker)
        if sig:
            sig["ticker"] = ticker
            signals.append(sig)
            logger.info(f"  → SIGNAL: {sig['action']} {sig['type']}")
        else:
            # Group by reason
            non_signal_reasons.setdefault(reason, []).append(ticker)
            logger.info(f"  → No signal: {reason}")

    # Build and send message
    message = build_full_message(signals, non_signal_reasons)
    if not message.strip():
        message = "<b>No signals and no non‑signal tickers found.</b>"

    success = send_telegram(message)
    if success:
        logger.info("Telegram notification sent.")
    else:
        logger.error("Failed to send Telegram notification.")


if __name__ == "__main__":
    main()
