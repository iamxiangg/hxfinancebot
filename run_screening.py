#!/usr/bin/env python3
import os
import time
import yfinance as yf
import google.generativeai as genai
import requests
from datetime import datetime

# --- Configuration ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message):
    print(f"Attempting to send Telegram message...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Error: Telegram secrets are missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("✅ Telegram message sent successfully!")
        else:
            print(f"❌ Telegram API Error: {r.text}")
    except Exception as e:
        print(f"❌ Connection Error: {e}")

def main():
    print(f"--- STARTING SESSION: {datetime.now()} ---")
    
    # 1. Immediate Credential Check
    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ CRITICAL ERROR: One or more GitHub Secrets are missing!")
        return

    # 2. Hardcoded Core Universe (Guaranteed to work for testing)
    tickers = ["AAPL", "TSLA", "NVDA", "MSFT", "AMD", "GOOGL", "AMZN", "META", "NFLX", "COIN"]
    scored_results = []

    print(f"Scanning {len(tickers)} core tickers...")
    
    for t in tickers:
        try:
            print(f"Fetching data for: {t}")
            # period='3mo' ensures we have enough data for a 60-day average
            df = yf.download(t, period="3mo", interval="1d", progress=False)
            
            if not df.empty and len(df) >= 60:
                # Basic Score: Is the current price above the 60-day moving average?
                current_price = float(df['Close'].iloc[-1])
                sma60 = float(df['Close'].rolling(window=60).mean().iloc[-1])
                
                if current_price > sma60:
                    scored_results.append(t)
            time.sleep(0.5) 
        except Exception as e:
            print(f"Skipping {t} due to error: {e}")

    # 3. Build the Report
    if not scored_results:
        send_telegram("📊 **Daily Screen Complete**\nNo stocks in the core list are currently above their 60-day average.")
        return

    report = f"🚀 **Stock Signals ({datetime.now().strftime('%Y-%m-%d')})**\n"
    report += "Stocks in Uptrend (Above SMA60): " + ", ".join(scored_results) + "\n\n"

    # 4. Gemini Analysis
    try:
        print("Requesting Gemini Analysis...")
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        # Analyze the first stock in the list
        target = scored_results[0]
        prompt = f"Give a 2-sentence summary of the current market sentiment for {target}."
        response = model.generate_content(prompt)
        report += f"**AI Quick Take ({target}):**\n{response.text}"
    except Exception as ai_err:
        print(f"Gemini Error: {ai_err}")
        report += f"\n*(AI Analysis currently unavailable)*"

    # 5. Final Send
    send_telegram(report)
    print(f"--- SESSION FINISHED ---")

if __name__ == "__main__":
    main()
