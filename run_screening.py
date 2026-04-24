#!/usr/bin/env python3
import os
import time
import logging
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import google.genai as genai
from datetime import datetime
from finvizfinance.screener.overview import Overview

# ── Configuration ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SMA_PERIOD = 60
SCORE_GREEN_THRESHOLD = 10 # Lowered to ensure AI triggers on best available

# ── Universe Setup ─────────────────────────────────────────────────────────
def get_universe():
    """Fetches ALL S&P 500 and NASDAQ 100 tickers via Finviz."""
    try:
        logger.info("Fetching full market universe from Finviz...")
        foverview = Overview()
        
        # limit=-1 fetches all pages automatically
        foverview.set_filter(filters_dict={'Index': 'S&P 500'})
        sp500 = foverview.screener_view(limit=-1, verbose=0)['Ticker'].tolist()
        
        foverview.set_filter(filters_dict={'Index': 'NASDAQ 100'})
        nas100 = foverview.screener_view(limit=-1, verbose=0)['Ticker'].tolist()
        
        full_list = list(set(sp500 + nas100))
        logger.info(f"Universe size: {len(full_list)} tickers.")
        return [t.replace('.', '-') for t in full_list]
    except Exception as e:
        logger.warning(f"Finviz failed, using core backup: {e}")
        return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "AMD", "META"]

# ── Analysis Helpers ───────────────────────────────────────────────────────

def call_gemini(ticker, score):
    """2026 SDK implementation for forensic analysis."""
    if not GEMINI_API_KEY: return "AI Analysis disabled (Key missing)."
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (f"Forensic equity analysis for {ticker}. Technical Score: {score}/30. "
                  "Provide a 2-sentence bullish/bearish summary and identify the primary risk factor.")
        response = client.models.generate_content(model="gemini-3-flash", contents=prompt)
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def send_telegram(message):
    """Sends message with chunking to avoid Telegram character limits."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: 
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(message), 4000):
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message[i:i+4000], "parse_mode": "Markdown"})

# ── Main Logic ─────────────────────────────────────────────────────────────

def main():
    logger.info("Starting Full Market Screen...")
    universe = get_universe()
    results = {}

    # Step 1: Batch Download (The high-speed way)
    logger.info(f"Downloading data for {len(universe)} tickers...")
    try:
        all_data = yf.download(universe, period="4mo", interval="1d", group_by='ticker', progress=False, auto_adjust=True)
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return

    # Step 2: Process All Tickers
    for t in universe:
        try:
            # Extract ticker data from the batch multi-index dataframe
            df = all_data[t].dropna()
            if len(df) < 65: continue
            
            # Flatten to 1D arrays to prevent 'Series' errors
            close = df['Close'].values.flatten()
            
            # Calculation: Simple Trend Score
            sma60 = np.mean(close[-60:])
            current = close[-1]
            
            # Score: Percent distance above SMA60
            if current > sma60:
                score = round(((current / sma60) - 1) * 200, 1)
                results[t] = min(30, score) # Cap at 30
        except:
            continue

    # Step 3: Rank and Report
    sorted_res = sorted(results.items(), key=lambda x: x[1], reverse=True)[:10]
    
    if not sorted_res:
        send_telegram("📊 **Market Screen**: No stocks currently in an uptrend (Price > SMA60).")
        return

    report = f"📊 **Market Screen: Top Performers ({len(universe)} checked)**\n\n"
    report += "| Ticker | Trend Score | Light |\n|---|---|---|\n"
    
    for t, s in sorted_res:
        light = "🟢" if s >= SCORE_GREEN_THRESHOLD else "🟡"
        report += f"| {t} | {s} | {light} |\n"

    # Step 4: AI Deep Dive on the Top 2 winners
    report += "\n**🔍 Gemini Analysis (Market Leaders)**\n"
    for i in range(min(2, len(sorted_res))):
        t_name, t_score = sorted_res[i]
        ai_insight = call_gemini(t_name, t_score)
        report += f"\n*{t_name} (Score: {t_score})*:\n{ai_insight}\n"

    send_telegram(report)
    logger.info("Process Complete. Report sent to Telegram.")

if __name__ == "__main__":
    main()
