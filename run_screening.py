import os
import time
import requests
import yfinance as yf
import pandas as pd
from google import genai # Modern 2026 SDK
from datetime import datetime

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

def get_ai_analysis(ticker):
    """Fetches a 2-sentence market sentiment summary."""
    if not GEMINI_KEY:
        return "AI Analysis skipped (No Key)."
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        response = client.models.generate_content(
            model="gemini-3-flash",
            contents=f"Why is {ticker} trending today? Give a 2-sentence bullish vs bearish summary."
        )
        return response.text
    except Exception as e:
        return f"AI Insight unavailable: {str(e)}"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=10)

def main():
    print(f"--- Scan Started: {datetime.now()} ---", flush=True)
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL"]
    hits = []

    for t in tickers:
        try:
            df = yf.download(t, period="3mo", interval="1d", progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 50:
                close_prices = df['Close'].values.flatten()
                close_prices = [p for p in close_prices if p == p]
                
                current = float(close_prices[-1])
                sma50 = sum(close_prices[-50:]) / 50
                
                if current > sma50:
                    hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {e}")

    if hits:
        # Get AI insight for the first stock in the list
        ai_insight = get_ai_analysis(hits[0])
        
        message = "🚀 **Daily Screen Results**\n\n"
        message += "Stocks > SMA50: " + ", ".join([f"`{h}`" for h in hits]) + "\n\n"
        message += f"🤖 **AI Insight ({hits[0]}):**\n{ai_insight}"
    else:
        message = "📊 **Daily Screen**\nNo stocks in uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---", flush=True)

if __name__ == "__main__":
    main()
