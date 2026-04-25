import os
import yfinance as yf
import pandas as pd
import requests

def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})

def main():
    # Pre-screened list (High volatility / Mid-Cap stocks are best for squeezes)
    url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/top_500.csv"
    tickers = pd.read_csv(url)['symbol'].tolist()
    
    candidates = []
    print(f"Scanning {len(tickers)} stocks for Squeeze potential...")

    for t in tickers:
        try:
            stock = yf.Ticker(t)
            info = stock.info
            
            # 1. Short Data (The "Fuel")
            short_pct = info.get("shortPercentOfFloat", 0) # e.g. 0.20 = 20%
            days_to_cover = info.get("shortRatio", 0)
            
            # 2. Trigger Check (Price moving up)
            # We fetch 1 month to check the 10-day trend
            hist = stock.history(period="1mo")
            if hist.empty or len(hist) < 20: continue
            
            current_price = hist['Close'].iloc[-1]
            sma10 = hist['Close'].rolling(window=10).mean().iloc[-1]

            # --- Squeeze Criteria ---
            # Short Interest > 15% AND Price > SMA10 (Momentum starting)
            if short_pct > 0.15 and current_price > sma10:
                candidates.append({
                    "ticker": t,
                    "si": round(short_pct * 100, 2),
                    "dtc": round(days_to_cover, 2)
                })
        except:
            continue

    # 3. Report
    if candidates:
        msg = "🔥 **Short Squeeze Alert** 🔥\n\nHigh Short Interest + Price Momentum detected:\n"
        for c in candidates:
            msg += f"• `${c['ticker']}`: SI: {c['si']}% | Days to Cover: {c['dtc']}\n"
        send_telegram(msg)
    else:
        print("No squeezes found today.")

if __name__ == "__main__":
    main()
