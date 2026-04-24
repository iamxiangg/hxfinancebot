#!/usr/bin/env python3
import os
import time
import pandas as pd
import yfinance as yf
import google.generativeai as genai
import requests
from datetime import datetime

# --- Secrets ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- Logic ---
def get_universe():
    print("Fetching stock universe...")
    try:
        # S&P 500 fallback list to ensure the script doesn't stop if Wikipedia is down
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]["Symbol"].tolist()
        return [t.replace('.', '-') for t in sp500[:100]] # Testing with top 100 for speed
    except Exception as e:
        print(f"Wiki fetch failed: {e}")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA"]

def technical_score(ticker):
    try:
        data = yf.download(ticker, period="3mo", interval="1d", progress=False)
        if len(data) < 60: return 0
        
        # Simple Logic: Price vs SMA60
        close = data['Close'].iloc[-1]
        sma60 = data['Close'].rolling(window=60).mean().iloc[-1]
        
        score = 10 if close > sma60 else 0
        return float(score)
    except:
        return 0

def send_telegram(message):
    print(f"Sending to Telegram: {message[:50]}...")
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def main():
    print(f"--- STARTING SCAN: {datetime.now()} ---")
    
    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN:
        print("CRITICAL ERROR: Missing API Keys in GitHub Secrets!")
        return

    tickers = get_universe()
    results = []

    for t in tickers:
        print(f"Scanning: {t}")
        score = technical_score(t)
        if score > 0:
            results.append((t, score))
        time.sleep(0.1)

    results.sort(key=lambda x: x[1], reverse=True)
    top_5 = results[:5]

    if not top_5:
        send_telegram("Screener ran but found no stocks matching the criteria.")
        return

    report = "🚀 **Daily Stock Signal**\n\n"
    for t, s in top_5:
        report += f"• **{t}**: Score {s}\n"
    
    # Simple Gemini Call
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        ai_res = model.generate_content(f"Briefly explain why {top_5[0][0]} is a trending stock today.")
        report += f"\n**AI Analysis ({top_5[0][0]}):**\n{ai_res.text}"
    except Exception as e:
        report += f"\n(AI Analysis unavailable: {e})"

    send_telegram(report)
    print("--- SCAN COMPLETE ---")

if __name__ == "__main__":
    main()
