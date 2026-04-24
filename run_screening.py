#!/usr/bin/env python3
import os
import time
import logging
import pandas as pd
import numpy as np
import yfinance as yf
import google.generativeai as genai
import requests
from datetime import datetime

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Parameters
SMA_PERIOD = 60
VWAP_STD_MULT = 1.0
VOLUME_SURGE_MULT = 1.5
SCORE_GREEN_THRESHOLD = 15

# --- Data Fetching ---
def get_universe():
    """Fetch S&P 500 and Nasdaq 100 tickers via Wikipedia."""
    try:
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]["Symbol"].tolist()
        nasdaq100 = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]["Ticker"].tolist()
        combined = list(set(sp500 + nasdaq100))
        # Cleanup: yfinance uses '-' instead of '.' for tickers like BRK.B
        return [t.replace('.', '-') for t in combined]
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"]

def get_historical_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="4mo") # Extra padding for SMA calculation
        if hist.empty or len(hist) < 65:
            return None
        return hist
    except:
        return None

# --- Scoring Logic ---
def technical_score(ticker):
    hist = get_historical_data(ticker)
    if hist is None: return None
    
    try:
        close = hist['Close']
        latest = hist.iloc[-1]
        prev = hist.iloc[-2]

        # 1. SMA60 (Trend)
        sma60 = close.rolling(window=SMA_PERIOD).mean().iloc[-1]
        if pd.isna(sma60): return None
        sma_score = min(10, ((latest['Close'] / sma60) - 1) * 200) if latest['Close'] > sma60 else 0

        # 2. VWAP (Oversold)
        vwap_series = (hist['Close'] * hist['Volume']).cumsum() / hist['Volume'].cumsum()
        latest_vwap = vwap_series.iloc[-1]
        std_diff = (hist['Close'] - vwap_series).std()
        threshold = latest_vwap - (VWAP_STD_MULT * std_diff)
        
        if latest['Close'] <= threshold:
            vwap_score = 10
        elif latest['Close'] >= latest_vwap:
            vwap_score = 0
        else:
            vwap_score = ((latest_vwap - latest['Close']) / (latest_vwap - threshold)) * 10

        # 3. Volume Reversal
        avg_vol = hist['Volume'].rolling(window=20).mean().iloc[-1]
        vol_surge = latest['Volume'] / avg_vol
        vol_score = min(10, (vol_surge - 1) * 20) if (latest['Close'] > prev['Close'] and vol_surge > VOLUME_SURGE_MULT) else 0

        return round(float(sma_score + vwap_score + vol_score), 1)
    except:
        return None

def fetch_fundamentals(ticker):
    try:
        info = yf.Ticker(ticker).info
        return {
            "rev_growth": round(info.get("revenueGrowth", 0) * 100, 1) if info.get("revenueGrowth") else "N/A",
            "de": round(info.get("debtToEquity", 0) / 100, 2) if info.get("debtToEquity") else "N/A",
            "fcf": round(info.get("freeCashflow", 0) / 1e6, 1) if info.get("freeCashflow") else "N/A"
        }
    except:
        return {"rev_growth": "N/A", "de": "N/A", "fcf": "N/A"}

def call_gemini(ticker, fundamentals, score):
    if not GEMINI_API_KEY: return "AI Key not found."
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash-001")
        prompt = (f"Forensic equity analysis for {ticker}. Tech Score: {score}/30. "
                  f"Growth: {fundamentals['rev_growth']}%, D/E: {fundamentals['de']}, FCF: ${fundamentals['fcf']}M. "
                  "Provide a concise summary: Bull case, Bear case, and Recommendation.")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Gemini Error: {e}"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"--- TELEGRAM LOG ---\n{message}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Try sending with Markdown
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload)
    
    # If Markdown fails (status 400), retry as Plain Text
    if r.status_code != 200:
        logger.warning("Markdown failed, sending as plain text.")
        payload.pop("parse_mode")
        requests.post(url, json=payload)

# --- Execution ---
def main():
    print(f"🚀 Starting Scan at {datetime.now()}")
    universe = get_universe()
    print(f"Analyzing {len(universe)} tickers...")
    
    scores = {}
    for i, ticker in enumerate(universe):
        if i % 50 == 0: print(f"Progress: {i}/{len(universe)}")
        time.sleep(0.2) # To prevent yfinance rate limits
        s = technical_score(ticker)
        if s is not None:
            scores[ticker] = s

    # Sort results
    sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_10 = sorted_stocks[:10]

    if not top_10:
        send_telegram("❌ Screener ran but no stock data was retrieved.")
        return

    # Header
    report = f"📊 **Daily Technical Screen ({datetime.now().strftime('%Y-%m-%d')})**\n\n"
    report += "| Ticker | Score | Status |\n|---|---|---|\n"
    
    for t, s in top_10:
        light = "🟢" if s >= SCORE_GREEN_THRESHOLD else "🟡" if s >= 10 else "🔴"
        report += f"| {t} | {s} | {light} |\n"

    # Deep Dive on Green Stocks (or just the top stock if none are green)
    green_stocks = [t for t, s in sorted_stocks if s >= SCORE_GREEN_THRESHOLD]
    
    # If no green stocks, let's analyze the #1 stock anyway for testing
    targets = green_stocks[:3] if green_stocks else [top_10[0][0]]
    
    report += "\n---\n🔍 **Deep Dive Analysis**"
    for t in targets:
        print(f"Running Gemini for {t}...")
        f = fetch_fundamentals(t)
        analysis = call_gemini(t, f, scores[t])
        report += f"\n\n**{t}** (Score: {scores[t]})\n{analysis}\n"

    send_telegram(report)
    print("✅ Done!")

if __name__ == "__main__":
    main()
