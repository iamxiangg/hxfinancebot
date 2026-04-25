#!/usr/bin/env python3
"""
Daily Allocation Signal Sheet (Nasdaq + Gold matrix)
Runs every weekday at 0800 SG time. Reports current recommended instruments.
"""

import os
import logging
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ── Data helpers ───────────────────────────────────────────────────────────

def fetch_close(ticker: str) -> float:
    try:
        hist = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
        if hist.empty:
            return None
        return hist['Close'].iloc[-1]
    except Exception as e:
        logger.warning(f"Failed to fetch {ticker}: {e}")
        return None

def compute_sma200(ticker: str) -> float:
    try:
        hist = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
        if len(hist) < 200:
            return None
        return hist['Close'].rolling(window=200).mean().iloc[-1]
    except Exception as e:
        logger.warning(f"Failed to compute SMA200 for {ticker}: {e}")
        return None

# ── Logic ──────────────────────────────────────────────────────────────────

def build_report() -> str:
    today = date.today()
    logger.info("Generating daily signal report...")

    qqq_close = fetch_close("QQQ")
    gld_close = fetch_close("GLD")
    vix_close = fetch_close("^VIX")
    qqq_sma200 = compute_sma200("QQQ")
    gld_sma200 = compute_sma200("GLD")

    if any(x is None for x in [qqq_close, gld_close, vix_close, qqq_sma200, gld_sma200]):
        logger.error("Missing market data")
        return "⚠️ **Daily signal unavailable** – missing market data."

    vix_high = vix_close > 30
    qqq_above_sma = qqq_close > qqq_sma200

    # Nasdaq instrument
    if qqq_above_sma and not vix_high:
        nasdaq_instrument = "TQQQ (3×)"
        nasdaq_reason = f"QQQ (${qqq_close:.2f}) > SMA200 (${qqq_sma200:.2f}) and VIX ({vix_close:.2f}) ≤ 30 → use TQQQ for 3× leverage."
    elif qqq_above_sma and vix_high:
        nasdaq_instrument = "QLD (2×)"
        nasdaq_reason = f"QQQ > SMA200 but VIX > 30 → use QLD for reduced leverage."
    elif not qqq_above_sma and vix_high:
        nasdaq_instrument = "Cash (SHY/T‑bills)"
        nasdaq_reason = f"QQQ ≤ SMA200 and VIX > 30 → move Nasdaq portion to cash."
    else:
        nasdaq_instrument = "QLD (2×)"
        nasdaq_reason = f"QQQ ≤ SMA200 but VIX ≤ 30 → use QLD (2×) for moderate exposure."

    # Gold instrument
    gld_above_sma = gld_close > gld_sma200
    if gld_above_sma:
        gold_instrument = "GLD"
        gold_reason = f"GLD (${gld_close:.2f}) > SMA200 (${gld_sma200:.2f}) → buy/hold GLD."
    else:
        gold_instrument = "Cash (SHY/T‑bills)"
        gold_reason = f"GLD ≤ SMA200 → move gold portion to cash."

    # Next trading day
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    next_day_str = next_day.isoformat()

    report = f"""📊 **Daily Allocation Signal Sheet**
*{today} after market close*

**Strategy Overview**
• 85% Nasdaq‑100 (VIX+SMA200 matrix)
• 15% Gold (GLD trend)
• Execute next trading day at market open

━━━━━━━━━━━━━━━━━━━━━━━
**Nasdaq Allocation (85%)**
| Indicator | Value |
|-----------|-------|
| QQQ Close | ${qqq_close:.2f} |
| QQQ SMA200 | ${qqq_sma200:.2f} |
| QQQ vs SMA | {"↑ Above" if qqq_above_sma else "↓ Below"} |
| VIX Close | {vix_close:.2f} |
| VIX Condition | {"≤ 30" if not vix_high else "> 30"} |
| **Recommended** | **{nasdaq_instrument}** |

**Why:** {nasdaq_reason}

━━━━━━━━━━━━━━━━━━━━━━━
**Gold Allocation (15%)**
| Indicator | Value |
|-----------|-------|
| GLD Close | ${gld_close:.2f} |
| GLD SMA200 | ${gld_sma200:.2f} |
| GLD vs SMA | {"↑ Above" if gld_above_sma else "↓ Below"} |
| **Recommended** | **{gold_instrument}** |

**Why:** {gold_reason}

━━━━━━━━━━━━━━━━━━━━━━━
**Execution:** Buy these instruments at market open on {next_day_str} if not already held. Rebalance when signal changes.
"""
    return report

# ── Telegram ───────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing – printing instead.")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info("Telegram message sent.")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    report = build_report()
    send_telegram(report)

if __name__ == "__main__":
    main()
