import os
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime

def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Telegram Error: {e}")

def main():
    # Using the verified S&P 500 list from the repository
    url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
    
    try:
        tickers = pd.read_csv(url)['symbol'].tolist()
    except Exception as e:
        print(f"Error loading ticker list: {e}")
        return

    candidates = []
    print(f"--- Squeeze Scan Started: {datetime.now()} ---")
    print(f"Scanning {len(tickers)} stocks...")

    for t in tickers:
        try:
            stock = yf.Ticker(t)
            # Use fast_info if available, or standard info
            info = stock.info
            
            # 1. Short Interest Data
            # Note: shortPercentOfFloat is a decimal (0.15 = 15%)
            short_pct = info.get("shortPercentOfFloat", 0)
            days_to_cover = info.get("shortRatio", 0)
            
            # 2. Strict Filter: 15% SI Minimum
            if short_pct is None or short_pct < 0.15:
                continue

            # 3. Momentum Trigger: Price > 10-day SMA
            hist = stock.history(period="1mo")
            if hist.empty or len(hist) < 15:
                continue
            
            current_price = hist['Close'].iloc[-1]
            sma10 = hist['Close'].rolling(window=10).mean().iloc[-1]

            if current_price > sma10:
                # Calculate Squeeze Pressure Score (SI % * DTC)
                # Higher score = Higher probability of explosive move
                si_value = short_pct * 100
                pressure_score = si_value * days_to_cover
                
                candidates.append({
                    "ticker": t,
                    "si": round(si_value, 2),
                    "dtc": round(days_to_cover, 2),
                    "score": round(pressure_score, 2),
                    "price": round(current_price, 2)
                })
                print(f"MATCH: {t} (SI: {si_value}%)")
                
        except Exception as e:
            # Silent skip for individual ticker errors to keep scan moving
            continue

    # --- Sort by Score (Highest Pressure First) ---
    sorted_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)

    # 4. Final Output
    if sorted_candidates:
        msg = "🔥 **Short Squeeze Rankings** 🔥\n"
        msg += "_Criteria: >15% SI + Price > SMA10_\n\n"
        
        for c in sorted_candidates[:10]: # Top 10 only
            msg += (f"• **${c['ticker']}** (Score: `{c['score']}`)\n"
                    f"  SI: `{c['si']}%` | DTC: `{c['dtc']}` | Price: `${c['price']}`\n")
        
        send_telegram(msg)
    else:
        # Optional: Send a heartbeat so you know the script finished
        send_telegram("✅ **Squeeze Scan Complete**: No S&P 500 stocks met the >15% SI + SMA10 criteria today.")

    print("--- Scan Finished ---")

if __name__ == "__main__":
    main()
