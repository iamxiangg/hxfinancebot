def main():
    print(f"--- Scan Started: {datetime.now()} ---")
    
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD", "GOOGL"]
    hits = []

    for t in tickers:
        try:
            print(f"Checking {t}...")
            # Download data
            df = yf.download(t, period="3mo", interval="1d", progress=False)
            
            if not df.empty and len(df) >= 50:
                # FIX: Ensure we get a single float, not a Series
                # We use .values.flatten() to ensure we get a simple array of numbers
                close_prices = df['Close'].values.flatten()
                current = float(close_prices[-1])
                
                # Calculate SMA50
                sma50 = pd.Series(close_prices).rolling(window=50).mean().iloc[-1]
                
                print(f"{t}: Price={current:.2f}, SMA50={sma50:.2f}")
                
                if current > sma50:
                    hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {e}")

    if hits:
        message = "🚀 **Daily Screen Results**\n\nStocks in uptrend:\n" + "\n".join([f"• `{h}`" for h in hits])
    else:
        message = "📊 **Daily Screen**\nNo stocks found in uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---")
