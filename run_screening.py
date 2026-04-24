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
                # This line extracts just the numbers, ignoring all the complex headers
                close_prices = df['Close'].values.flatten()
                
                # Filter out any 'NaN' (missing) values just in case
                close_prices = close_prices[~pd.isna(close_prices)]
                
                if len(close_prices) >= 50:
                    current = float(close_prices[-1])
                    # Manual calculation of SMA50 from the flattened list
                    sma50 = sum(close_prices[-50:]) / 50
                    
                    print(f"{t}: Price={current:.2f}, SMA50={sma50:.2f}")
                    
                    if current > sma50:
                        hits.append(t)
            time.sleep(0.5)
        except Exception as e:
            print(f"Error on {t}: {str(e)}")

    if hits:
        message = "🚀 **Daily Screen Results**\n\nStocks in uptrend (Price > SMA50):\n" + "\n".join([f"• `{h}`" for h in hits])
    else:
        message = "📊 **Daily Screen**\nNo stocks found in a technical uptrend today."

    send_telegram(message)
    print("--- Scan Finished ---")
