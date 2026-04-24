#!/usr/bin/env python3
"""
Daily stock screener: technical + fundamental → Telegram report.
Three pillars: SMA60, VWAP oversold, volume-confirmed reversal.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from google import genai
import requests

# ── Configuration ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Secrets (set as environment variables, locally or via GitHub secrets)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Technical parameters
SMA_PERIOD = 60
VWAP_STD_MULT = 1.0          # oversold below VWAP - 1σ
VOLUME_SURGE_MULT = 1.5      # > 1.5x 20-day average
SCORE_GREEN_THRESHOLD = 15   # composite ≥15 → green

# Universe: S&P500 + Nasdaq100 + S&P400 (approximate tickers from yfinance)
SP500 = yf.Ticker("^GSPC").info.get("components", [])
if not SP500:
    SP500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]["Symbol"].tolist()
NASDAQ100 = yf.Ticker("^NDX").info.get("components", [])
if not NASDAQ100:
    NASDAQ100 = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]["Ticker"].tolist()
SP400 = yf.Ticker("^MID").info.get("components", [])
if not SP400:
    SP400 = []  # fallback: fetch from Wikipedia or skip
UNIVERSE = list(set(SP500 + NASDAQ100 + SP400))
logger.info(f"Universe size: {len(UNIVERSE)} tickers (after dedup)")

# ── Helper Functions ───────────────────────────────────────────────────────

def get_historical_data(ticker, period="3mo"):
    """Download daily OHLCV data for a ticker using yfinance."""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty or len(hist) < 65:  # need at least 65 days for SMA60
            return None
        return hist
    except Exception as e:
        logger.warning(f"Failed to fetch {ticker}: {e}")
        return None

def compute_vwap(hist):
    """VWAP = cumulative(price*volume) / cumulative(volume) over the whole period."""
    vwap = (hist['Close'] * hist['Volume']).cumsum() / hist['Volume'].cumsum()
    return vwap

def compute_sma(hist, window=60):
    return hist['Close'].rolling(window=window).mean()

def compute_volume_surge(hist, window=20):
    avg_vol = hist['Volume'].rolling(window=window).mean()
    return hist['Volume'] / avg_vol

def technical_score(ticker):
    """Return composite score (0-30) for one ticker, or None if data missing."""
    hist = get_historical_data(ticker, period="3mo")
    if hist is None:
        return None

    close = hist['Close']
    volume = hist['Volume']
    latest = hist.iloc[-1]
    prev = hist.iloc[-2]

    # 1) SMA60 score (0-10): distance from SMA60 normalized
    sma60 = compute_sma(hist, SMA_PERIOD)
    if sma60.isna().all():
        return None
    latest_sma = sma60.iloc[-1]
    if pd.isna(latest_sma):
        return None
    # Score: 0 if price ≤ SMA60, 10 if price ≥ 1.1 * SMA60, linear in between
    if latest['Close'] <= latest_sma:
        sma_score = 0
    else:
        ratio = (latest['Close'] / latest_sma - 1)  # e.g., 0.05 = 5% above
        sma_score = min(10, ratio * 200)  # 5% → 10, cap at 10

    # 2) VWAP oversold score (0-10): price below VWAP - 1σ
    vwap = compute_vwap(hist)
    latest_vwap = vwap.iloc[-1]
    # std of (close - vwap) over the period
    diff = hist['Close'] - vwap
    std_diff = diff.std()
    threshold = latest_vwap - VWAP_STD_MULT * std_diff
    # Score: 10 if price ≤ threshold, 0 if price ≥ VWAP, linear in between
    if latest['Close'] >= latest_vwap:
        vwap_score = 0
    elif latest['Close'] <= threshold:
        vwap_score = 10
    else:
        # fraction below VWAP relative to threshold
        frac = (latest_vwap - latest['Close']) / (latest_vwap - threshold)
        vwap_score = frac * 10

    # 3) Volume-confirmed reversal score (0-10)
    # Condition: latest close > prev close AND volume surge > multiplier
    vol_surge = compute_volume_surge(hist, 20).iloc[-1]
    if pd.isna(vol_surge):
        vol_score = 0
    else:
        if latest['Close'] > prev['Close'] and vol_surge > VOLUME_SURGE_MULT:
            # score scales with surge magnitude, capped at 10
            vol_score = min(10, (vol_surge - 1) * 20)  # 1.5x → 10, 2x → 20capped
        else:
            vol_score = 0

    composite = sma_score + vwap_score + vol_score
    return round(composite, 1)

def fetch_fundamentals(ticker):
    """Return dict with revenue growth (%), D/E ratio, free cash flow (in $M)."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        # Revenue growth (year-over-year)
        rev_growth = info.get("revenueGrowth", None)
        rev_growth_pct = round(rev_growth * 100, 1) if rev_growth is not None else "N/A"
        # Debt-to-equity
        de = info.get("debtToEquity", None)
        if de is not None:
            de = round(de, 2)
        # Free cash flow (in millions)
        fcf = info.get("freeCashflow", None)
        fcf_m = round(fcf / 1e6, 1) if fcf is not None else "N/A"
        return {
            "revenue_growth_%": rev_growth_pct,
            "debt_to_equity": de if de is not None else "N/A",
            "free_cash_flow_M": fcf_m
        }
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {ticker}: {e}")
        return {"revenue_growth_%": "N/A", "debt_to_equity": "N/A", "free_cash_flow_M": "N/A"}

def call_gemini(ticker, fundamentals):
    """Use Gemini to generate a forensic equity report for a green stock."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set")
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("models/gemini-2.0-flash-001")  # free tier

    # Build prompt with your forensic template
    prompt = f"""
You are a forensic equity analyst. Provide a technical and fundamental report for {ticker}.

Fundamental data:
- Revenue Growth (YoY): {fundamentals['revenue_growth_%']}%
- Debt-to-Equity: {fundamentals['debt_to_equity']}
- Free Cash Flow: ${fundamentals['free_cash_flow_M']}M

Technical score (composite of SMA60, VWAP oversold, volume reversal): see above.

Format your analysis into:
1. **Summary** (bull/bear case)
2. **Technical Analysis** (trend, support/resistance)
3. **Fundamental Check** (strengths/weaknesses)
4. **Catalysts** (earnings, news)
5. **Risk** (key risks)
6. **Recommendation** (buy/hold/sell with conviction level)
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini call failed for {ticker}: {e}")
        return f"Gemini error: {e}"

def send_telegram(message):
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing – skipping send.")
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

# ── Main Screening Logic ───────────────────────────────────────────────────

def main():
    logger.info("Starting daily screen...")

    # Step 1: Compute technical scores for all tickers (with rate limit ~200/min)
    scores = {}
    for idx, ticker in enumerate(UNIVERSE):
        if idx % 100 == 0:
            logger.info(f"Processing ticker {idx+1}/{len(UNIVERSE)}")
        time.sleep(0.3)  # ~3 per second to stay under yfinance limits
        score = technical_score(ticker)
        if score is not None:
            scores[ticker] = score
        # yfinance also imposes per-minute limit; 0.3s is safe for ~200/min

    # Sort by score descending
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top10 = sorted_scores[:10]
    green_stocks = [t for t, s in sorted_scores if s >= SCORE_GREEN_THRESHOLD]

    logger.info(f"Top 10: {[t for t,s in top10]}")
    logger.info(f"Green stocks (score≥{SCORE_GREEN_THRESHOLD}): {green_stocks}")

    # Step 2: Build top-10 table with traffic lights
    table_header = "| Ticker | Price | Technical Score | Light |\n|---|---|---|---|\n"
    table_rows = ""
    for ticker, score in top10:
        # Get latest price
        hist = get_historical_data(ticker, period="5d")
        price = hist['Close'].iloc[-1] if hist is not None else "N/A"
        if score >= SCORE_GREEN_THRESHOLD:
            light = "🟢"
        elif score >= 10:   # yellow between 10 and 14
            light = "🟡"
        else:
            light = "🔴"
        table_rows += f"| {ticker} | ${price} | {score} | {light} |\n"

    report = "📊 **Daily Technical Screen (Top 10)**\n"
    report += table_header + table_rows

    # Step 3: Fetch fundamentals for green stocks and call Gemini
    if green_stocks:
        report += "\n\n---\n**🔍 Green Stock Deep Dive (Gemini Analysis)**\n"
        for ticker in green_stocks:
            if ticker not in [t for t,s in top10]:   # only if in top10? Or all green? User said "green technical score" → all green
                # Still include but maybe outside top10 – fine.
                pass
            fundamentals = fetch_fundamentals(ticker)
            gemini_output = call_gemini(ticker, fundamentals)
            report += f"\n\n**{ticker}** – Technical Score: {scores[ticker]}\n"
            report += f"Fundamentals: Revenue Growth={fundamentals['revenue_growth_%']}%, D/E={fundamentals['debt_to_equity']}, FCF={fundamentals['free_cash_flow_M']}M\n"
            if gemini_output:
                report += gemini_output
            else:
                report += "*(Gemini analysis unavailable)*"
            report += "\n\n---"
    else:
        report += "\n\n*(No green stocks identified today)*"

    # Step 4: Send to Telegram
    send_telegram(report)

if __name__ == "__main__":
    main()
    for t in tickers:
        try:
            df = yf.download(t, period="3mo", interval="1d", progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 50:
                close_prices = df['Close'].values.flatten()
                close_prices = [p for p in close_prices if p == p]
                
                current = float(close_prices[-1])
                sma50 = sum(close_prices[-50:]) / 50
                
                if current > sma50:
                    hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {e}")

    if hits:
        # Get AI insight for the first stock in the list
        ai_insight = get_ai_analysis(hits[0])
        
        message = "🚀 **Daily Screen Results**\n\n"
        message += "Stocks > SMA50: " + ", ".join([f"`{h}`" for h in hits]) + "\n\n"
        message += f"🤖 **AI Insight ({hits[0]}):**\n{ai_insight}"
    else:
        message = "📊 **Daily Screen**\nNo stocks in uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---", flush=True)

if __name__ == "__main__":
    main()
