#!/usr/bin/env python3
import os
import time
import logging
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import google.genai as genai  # Updated for 2026
from datetime import datetime

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
    try:
        # Simplified robust universe fetch
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]["Symbol"].tolist()
        nasdaq100 = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]["Ticker"].tolist()
        # For testing/speed, we'll merge and take a manageable sample or full list
        return list(set(sp500 + nasdaq100))
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "AMD", "META"]

UNIVERSE = get_universe()

# ── Helper Functions ───────────────────────────────────────────────────────

def get_safe_data(ticker, period="4mo"):
    """Downloads data and flattens it to avoid 'Series' errors."""
    try:
        df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 65:
            return None
        
        # Flatten all relevant columns to 1D arrays immediately
        data = {
            'Close': df['Close'].values.flatten(),
            'Volume': df['Volume'].values.flatten(),
            'High': df['High'].values.flatten(),
            'Low': df['Low'].values.flatten()
        }
        # Filter NaNs
        mask = ~np.isnan(data['Close'])
        for k in data:
            data[k] = data[k][mask]
            
        return data
    except Exception as e:
        logger.warning(f"Fetch failed for {ticker}: {e}")
        return None

def compute_technical_score(ticker):
    data = get_safe_data(ticker)
    if not data: return None

    close = data['Close']
    vol = data['Volume']
    
    # 1. SMA60 Score
    sma60 = pd.Series(close).rolling(window=SMA_PERIOD).mean().values
    latest_sma = sma60[-1]
    if np.isnan(latest_sma): return None
    
    sma_ratio = (close[-1] / latest_sma) - 1
    sma_score = min(10, max(0, sma_ratio * 200))

    # 2. VWAP Score (Approximate)
    vwap = np.cumsum(close * vol) / np.cumsum(vol)
    latest_vwap = vwap[-1]
    std_diff = np.std(close - vwap)
    threshold = latest_vwap - (VWAP_STD_MULT * std_diff)
    
    if close[-1] >= latest_vwap: vwap_score = 0
    elif close[-1] <= threshold: vwap_score = 10
    else:
        vwap_score = ((latest_vwap - close[-1]) / (latest_vwap - threshold)) * 10

    # 3. Volume Surge
    avg_vol = pd.Series(vol).rolling(window=20).mean().values[-1]
    vol_surge = vol[-1] / avg_vol
    vol_score = min(10, (vol_surge - 1) * 20) if (close[-1] > close[-2] and vol_surge > VOLUME_SURGE_MULT) else 0

    return round(sma_score + vwap_score + vol_score, 1)

def fetch_fundamentals(ticker):
    try:
        info = yf.Ticker(ticker).info
        return {
            "rev_growth": f"{info.get('revenueGrowth', 0)*100:.1f}%",
            "de": round(info.get("debtToEquity", 0) / 100, 2), # yfinance returns D/E as whole number often
            "fcf": f"{info.get('freeCashflow', 0)/1e6:.1f}M"
        }
    except:
        return {"rev_growth": "N/A", "de": "N/A", "fcf": "N/A"}

def call_gemini(ticker, fund, score):
    if not GEMINI_API_KEY: return "AI Key Missing."
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"Forensic analysis for {ticker}. Score: {score}. Fundamentals: Growth {fund['rev_growth']}, D/E {fund['de']}, FCF {fund['fcf']}. Provide Summary, Catalysts, and Risk in 4 bullet points total."
        response = client.models.generate_content(model="gemini-3-flash", contents=prompt)
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN: return
    # Telegram has a 4096 character limit
    if len(message) > 4000: message = message[:4000] + "..."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting analysis...")
    results = {}
    
    # Process a subset to avoid GitHub Action timeouts (Top 50 tickers)
    test_universe = UNIVERSE[:50] 
    
    for t in test_universe:
        score = compute_technical_score(t)
        if score:
            results[t] = score
        time.sleep(0.2)

    sorted_res = sorted(results.items(), key=lambda x: x[1], reverse=True)[:10]
    
    report = "📊 **Daily Technical Screen**\n\n"
    report += "| Ticker | Score | Status |\n|---|---|---|\n"
    
    green_stocks = []
    for t, s in sorted_res:
        light = "🟢" if s >= SCORE_GREEN_THRESHOLD else "🟡" if s >= 10 else "🔴"
        report += f"| {t} | {s} | {light} |\n"
        if s >= SCORE_GREEN_THRESHOLD:
            green_stocks.append(t)

    if green_stocks:
        report += "\n**🔍 Deep Dive**\n"
        for g in green_stocks[:2]: # Deep dive on top 2 green stocks to save time
            f = fetch_fundamentals(g)
            ai = call_gemini(g, f, results[g])
            report += f"\n*{g}*:\n{ai}\n"

    send_telegram(report)
    logger.info("Report sent!")

if __name__ == "__main__":
    main()
