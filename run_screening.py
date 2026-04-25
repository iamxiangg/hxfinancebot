#!/usr/bin/env python3
"""
Daily Allocation Signal Sheet – new template
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

# ── Data helpers (scalar-safe) ─────────────────────────────────────────────

def fetch_close(ticker: str) -> float:
    try:
        hist = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
        if hist.empty:
            return None
        close_series = hist['Close'].squeeze()
        return float(close_series.iloc[-1])
    except Exception as e:
        logger.warning(f"Failed to fetch {ticker}: {e}")
        return None

def compute_sma200(ticker: str) -> float:
    try:
        hist = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
        if len(hist) < 200:
            return None
        close_series = hist['Close'].squeeze()
        sma = close_series.rolling(window=200).mean().iloc[-1]
        return float(sma)
    except Exception as e:
        logger.warning(f"Failed to compute SMA200 for {ticker}: {e}")
        return None

# ── Report builder ─────────────────────────────────────────────────────────

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
    gld_above_sma = gld_close > gld_sma200

    # Nasdaq instrument & reasons
    if qqq_above_sma and not vix_high:
        nasdaq_instrument = "TQQQ (3×)"
        nasdaq_reasons = [
            f"VIX ≤ 30 ({vix_close:.2f}) – low volatility environment",
            f"QQQ > SMA200 (${qqq_close:.2f} > ${qqq_sma200:.2f}) – uptrend confirmed",
            "Use 3× leverage to maximise upside"
        ]
    elif qqq_above_sma and vix_high:
        nasdaq_instrument = "QLD (2×)"
        nasdaq_reasons = [
            f"VIX > 30 ({vix_close:.2f}) – elevated volatility",
            f"QQQ > SMA200 (${qqq_close:.2f} > ${qqq_sma200:.2f}) – still in uptrend",
            "Reduce leverage to 2× to manage risk"
        ]
    elif not qqq_above_sma and vix_high:
        nasdaq_instrument = "Cash (SHY/T‑bills)"
        nasdaq_reasons = [
            f"VIX > 30 ({vix_close:.2f}) – high volatility",
            f"QQQ ≤ SMA200 (${qqq_close:.2f} ≤ ${qqq_sma200:.2f}) – downtrend signal",
            "Move Nasdaq portion to cash for capital preservation"
        ]
    else:  # not above sma and vix <= 30
        nasdaq_instrument = "QLD (2×)"
        nasdaq_reasons = [
            f"VIX ≤ 30 ({vix_close:.2f}) – moderate volatility",
            f"QQQ ≤ SMA200 (${qqq_close:.2f} ≤ ${qqq_sma200:.2f}) – below trend",
            "Use 2× leverage for moderate exposure with safety"
        ]

    # Gold instrument & reasons
    if gld_above_sma:
        gold_instrument = "GLD"
        gold_reasons = [
            f"GLD > SMA200 (${gld_close:.2f} > ${gld_sma200:.2f}) – gold in uptrend",
            "Hold as portfolio hedge"
        ]
    else:
        gold_instrument = "Cash (SHY/T‑bills)"
        gold_reasons = [
            f"GLD ≤ SMA200 (${gld_close:.2f} ≤ ${gld_sma200:.2f}) – gold in downtrend",
            "Move gold portion to cash"
        ]

    # Next trading day
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    next_day_str = next_day.isoformat()

    # Build report
    report = f"""📊 **Daily Allocation Signal Sheet**
{today} after market close

**Strategy Overview**
• 85% Nasdaq‑100 (VIX+SMA200 matrix)
• 15% Gold (GLD trend)
• Execute next trading day at market open

**Proposal:**
- 85% **{nasdaq_instrument}**
- 15% **{gold_instrument}**

━━━━━━━━━━━━━━━━━━━━━━━

**Why {nasdaq_instrument}?**
"""
    for reason in nasdaq_reasons:
        report += f"- {reason}\n"

    report += f"\n**Why {gold_instrument}?**\n"
    for reason in gold_reasons:
        report += f"- {reason}\n"

    report += f"""
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
