#!/usr/bin/env python3
import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# --- Secrets from GitHub ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message):
    """Sends a plain text message to Telegram."""
    print(f"Final Report:\n{message}", flush=True)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram secrets missing!")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": message, 
        "parse_mode": "Markdown"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"❌ Telegram Error: {r.text}")
    except Exception as e:
        print(f"❌ Connection error: {e}")

def main():
    print(f"--- STARTING NO-AI SCAN: {datetime.now()} ---", flush=True)

    # Core high-volume universe
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL", "AMZN", "META", "AVGO", "NFLX"]
    candidates = []

    for t in tickers:
        try:
            print(f"Scanning: {t}...", flush=True)
            # Fetch 4 months of data for a 60-day SMA
            df = yf.download(t, period="4mo", interval="1d", progress=False)
            
            if not df.empty and len(df) >= 60:
                current_price = float(df['Close'].iloc[-1])
                sma60 = float(df['Close'].rolling(window=60).mean().iloc[-1])
                
                # The "Green" Condition
                if current_price > sma60:
                    candidates.append(t)
            time.sleep(0.5) 
        except Exception as e:
            print(f"Error scanning {t}: {e}")

    # Build and send report
    if not candidates:
        msg = "📊 **Daily Screen Complete**\nNo stocks currently meet the uptrend criteria (Price > SMA60)."
    else:
        msg = f"🚀 **Stock Signals ({datetime.now().strftime('%Y-%m-%d')})**\n\n"
        msg += "The following stocks are in a technical uptrend (Above SMA60):\n"
        for c in candidates:
            msg += f"• `{c}`\n"
    
    send_telegram(msg)
    print("--- SCAN COMPLETE ---", flush=True)

if __name__ == "__main__":
    main()
