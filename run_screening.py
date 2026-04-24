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
VWAP_STD_MULT = 1.0
VOLUME_SURGE_MULT = 1.5
SCORE_GREEN_THRESHOLD = 15

# ── Universe Setup ─────────────────────────────────────────────────────────
def get_universe():
    """Fetches S&P 500 and NASDAQ 100 tickers via Finviz library."""
    try:
        logger.info("Connecting to Finviz...")
        foverview = Overview()
        
        # Pull S&P 500
        foverview.set_filter(filters_dict={'Index': 'S&P 500'})
        sp500_df = foverview.screener_view()
        sp500 = sp500_df['Ticker'].tolist() if not sp500_df.empty else []
        
        # Pull Nasdaq 100
        foverview.set_filter(filters_dict={'Index': 'NASDAQ 100'})
        nasdaq_df = foverview.screener_view()
        nasdaq100 = nasdaq_df['Ticker'].tolist() if not nasdaq_df.empty else []
        
        full_list = list(set(sp500 + nasdaq100))
        if not full_list:
            raise ValueError("Finviz returned empty list.")
            
        logger.info(f"Universe size: {len(full_list)} tickers.")
        return [t.replace('.', '-') for t in full_list]
    except Exception as e:
        logger.warning(f"Finviz failed, using core backup: {e}")
        return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "AMD", "META", "AVGO", "NFLX"]

# ── Analysis Helpers ───────────────────────────────────────────────────────

def get_safe_data(ticker):
    """Downloads yfinance data and flattens to prevent Series errors."""
    try:
        df = yf.download(ticker, period="4mo", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 65: return None
        
        # Flatten columns to 1D numpy arrays to avoid 'Series' errors
        data = {
            'Close': df['Close'].values.flatten(),
            'Volume': df['Volume'].values.flatten()
        }
        # Filter out NaNs
        mask = ~np.isnan(data['Close'])
        return {k: v[mask] for k, v in data.items()}
    except Exception as e:
        logger.warning(f"YFinance error for {ticker}: {e}")
        return None

def compute_score(ticker):
    data = get_safe_data(ticker)
    if not data: return None
    close, vol = data['Close'], data['Volume']

    # 1. SMA60 (Trend)
    sma60 = pd.Series(close).rolling(window=SMA_PERIOD).mean().values
    latest_sma = sma60[-1]
    if np.isnan(latest_sma): return None
    sma_score = min(10, max(0, ((close[-1] / latest_sma) - 1) * 200))

    # 2. VWAP (Value)
    vwap = np.cumsum(close * vol) / np.cumsum(vol)
    latest_vwap, std_diff = vwap[-1], np.std(close - vwap)
    thresh = latest_vwap - (VWAP_STD_MULT * std_diff)
    vwap_score = 10 if close[-1] <= thresh else (0 if close[-1] >= latest_vwap else ((latest_vwap - close[-1]) / (latest_vwap - thresh)) * 10)

    # 3. Volume Reversal
    avg_vol = pd.Series(vol).rolling(window=20).mean().values[-1]
    vol_surge = vol[-1] / avg_vol
    vol_score = min(10, (vol_surge - 1) * 20) if (close[-1] > close[-2] and vol_surge > VOLUME_SURGE_MULT) else 0

    return round(sma_score + vwap_score + vol_score, 1)

def call_gemini(ticker, score):
    if not GEMINI_API_KEY: return "AI Analysis disabled (Key missing)."
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"Analyze {ticker} with a technical score of {score}/30. Provide a 2-sentence bullish/bearish outlook and one primary risk."
        response = client.models.generate_content(model="gemini-3-flash", contents=prompt)
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Chunking for long reports
    for i in range(0, len(message), 4000):
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message[i:i+4000], "parse_mode": "Markdown"})

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting Daily Screen...")
    universe = get_universe()
    results = {}

    # Scan the first 100 tickers to stay within GitHub Action limits
    for t in universe[:100]:
        score = compute_score(t)
        if score: results[t] = score
        time.sleep(0.2)

    sorted_res = sorted(results.items(), key=lambda x: x[1], reverse=True)[:10]
    
    report = f"📊 **Technical Screen ({datetime.now().strftime('%Y-%m-%d')})**\n\n"
    report += "| Ticker | Score | Status |\n|---|---|---|\n"
    
    green_stocks = []
    for t, s in sorted_res:
        light = "🟢" if s >= SCORE_GREEN_THRESHOLD else "🟡" if s >= 10 else "🔴"
        report += f"| {t} | {s} | {light} |\n"
        if s >= SCORE_GREEN_THRESHOLD: green_stocks.append(t)

    if green_stocks:
        report += "\n**🔍 Gemini Analysis (Top Green Stocks)**\n"
        for g in green_stocks[:2]: # Deep dive on top 2
            ai_text = call_gemini(g, results[g])
            report += f"\n*{g} Score {results[g]}*:\n{ai_text}\n"

    send_telegram(report)
    logger.info("Process Complete.")

if __name__ == "__main__":
    main()
