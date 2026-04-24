import os
import time
import requests
import yfinance as yf
from datetime import datetime

# Get Secrets
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("❌ Error: Missing Telegram Secrets")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload)
    print(f"Telegram Response: {r.status_code}")

def main():
    print(f"--- Scan Started: {datetime.now()} ---")
    
    # Simple list for testing
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL"]
    hits = []

    for t in tickers:
        try:
            print(f"Checking {t}...")
            df = yf.download(t, period="3mo", interval="1d", progress=False)
            if not df.empty:
                current = float(df['Close'].iloc[-1])
                sma50 = float(df['Close'].rolling(window=50).mean().iloc[-1])
                # If price is above average, it's a 'Hit'
                if current > sma50:
                    hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {e}")

    # Build the message
    if hits:
        message = "🚀 **Daily Screen Results**\n\nStocks in uptrend:\n" + "\n".join([f"• `{h}`" for h in hits])
    else:
        message = "📊 **Daily Screen**\nNo stocks found in uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---")

if __name__ == "__main__":
    main()
