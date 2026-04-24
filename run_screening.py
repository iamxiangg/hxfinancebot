#!/usr/bin/env python3
import os
import time
import requests
import pandas as pd
import yfinance as yf
from google import genai  # Modern 2026 SDK
from datetime import datetime

# --- Environment Setup ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message):
    """Sends a message to your Telegram bot. Retries in plain text if Markdown fails."""
    print(f"Telegram Log: {message[:100]}...", flush=True)
    if not TELEGRAM_BOT_TOKEN: return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            # Fallback to plain text if Markdown is broken
            payload.pop("parse_mode")
            requests.post(url, json=payload)
    except Exception as e:
        print(f"Connection error: {e}", flush=True)

def main():
    print(f"--- STARTING SCAN: {datetime.now()} ---", flush=True)

    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN:
        print("❌ CRITICAL: Secrets are missing!", flush=True)
        return

    # 1. Targeted Universe (High Volume S&P 500 & Nasdaq)
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL", "AMZN", "META", "AVGO", "NFLX"]
    candidates = []

    print(f"Scanning {len(tickers)} core tickers...", flush=True)

    for t in tickers:
        try:
            # period='4mo' to safely calculate a 60-day SMA
            df = yf.download(t, period="4mo", interval="1d", progress=False)
            
            if not df.empty and len(df) >= 60:
                current_price = float(df['Close'].iloc[-1])
                sma60 = float(df['Close'].rolling(window=60).mean().iloc[-1])
                
                # Screening Condition: Price must be in a healthy uptrend
                if current_price > sma60:
                    candidates.append(t)
            time.sleep(0.5) 
        except Exception as e:
            print(f"Error fetching {t}: {e}", flush=True)

    # 2. Build the Message
    if not candidates:
        send_telegram("✅ **Daily Screen Complete**\nNo stocks in the primary list are currently above their 60-day average.")
        return

    report = f"🚀 **Daily Signals ({datetime.now().strftime('%Y-%m-%d')})**\n"
    report += "Stocks in Uptrend: " + ", ".join(candidates) + "\n\n"

    # 3. Gemini 3 Analysis
    try:
        print("Initializing Gemini 3 Flash...", flush=True)
        # The new client automatically uses the GEMINI_API_KEY env var
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        target = candidates[0]
        prompt = f"Perform a quick forensic analysis on {target}. Mention one key bull factor and one risk. Keep it under 60 words."
        
        # New 2026 generate_content syntax
        response = client.models.generate_content(
            model="gemini-3-flash-preview", 
            contents=prompt
        )
        
        report += f"**AI Quick Analysis ({target}):**\n{response.text}"
    except Exception as e:
        print(f"AI Error: {e}", flush=True)
        report += f"\n*(AI Analysis skipped due to error)*"

    send_telegram(report)
    print("--- SCAN COMPLETE ---", flush=True)

if __name__ == "__main__":
    main()
