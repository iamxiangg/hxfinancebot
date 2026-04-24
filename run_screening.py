import os
import time
import requests
import yfinance as yf
import pandas as pd  # Added this back in
from datetime import datetime

# Get Secrets from GitHub
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("❌ Error: Missing Telegram Secrets")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram Response: {r.status_code}")
    except Exception as e:
        print(f"Telegram Connection Error: {e}")

def main():
    # This MUST be the first thing printed
    print(f"--- Scan Started: {datetime.now()} ---", flush=True)
    
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL"]
    hits = []

    for t in tickers:
        try:
            print(f"Checking {t}...", flush=True)
            # Fetch data - auto_adjust=True helps keep headers simple
            df = yf.download(t, period="3mo", interval="1d", progress=False, auto_adjust=True)
            
            if not df.empty and len(df) >= 50:
                # The 'Flatten' method to bypass Multi-Index errors
                close_prices = df['Close'].values.flatten()
                
                # Remove any bad data
                close_prices = [p for p in close_prices if p == p] # Faster way to remove NaN
                
                if len(close_prices) >= 50:
                    current = float(close_prices[-1])
                    # Manual SMA calculation to avoid 'Series' errors
                    sma50 = sum(close_prices[-50:]) / 50
                    
                    print(f"{t}: Price={current:.2f}, SMA50={sma50:.2f}", flush=True)
                    
                    if current > sma50:
                        hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {str(e)}", flush=True)

    # Final Message Construction
    if hits:
        message = "🚀 **Daily Screen Results**\n\nStocks in uptrend (Price > SMA50):\n" + "\n".join([f"• `{h}`" for h in hits])
    else:
        message = "📊 **Daily Screen**\nNo stocks found in a technical uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---", flush=True)

if __name__ == "__main__":
    main()
