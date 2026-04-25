def main():
    # Using sp500.csv as discussed
    url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
    tickers = pd.read_csv(url)['symbol'].tolist()
    
    candidates = []
    print(f"Scanning {len(tickers)} stocks for Squeeze potential...")

    for t in tickers:
        try:
            stock = yf.Ticker(t)
            info = stock.info
            
            short_pct = info.get("shortPercentOfFloat", 0)
            days_to_cover = info.get("shortRatio", 0)
            
            # Trigger Check (Price > 10-day average)
            hist = stock.history(period="1mo")
            if hist.empty or len(hist) < 15: continue
            
            current_price = hist['Close'].iloc[-1]
            sma10 = hist['Close'].rolling(window=10).mean().iloc[-1]

            # Criteria: SI > 15% and Price Trending Up
            if short_pct > 0.15 and current_price > sma10:
                # --- RANKING LOGIC ---
                # We multiply SI by Days to Cover to get a "Pressure Score"
                # High SI + High DTC = High Score
                pressure_score = (short_pct * 100) * days_to_cover
                
                candidates.append({
                    "ticker": t,
                    "si": round(short_pct * 100, 2),
                    "dtc": round(days_to_cover, 2),
                    "score": round(pressure_score, 2)
                })
        except:
            continue

    # --- SORTING ---
    # Sort by the score in descending order (highest first)
    sorted_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)

    if sorted_candidates:
        msg = "🔥 **Ranked Short Squeeze Alerts** 🔥\n"
        msg += "_Sorted by Squeeze Pressure (SI × DTC)_\n\n"
        
        # Take the top 10 most probable
        for c in sorted_candidates[:10]:
            msg += f"• **${c['ticker']}**: SI: `{c['si']}%` | DTC: `{c['dtc']}` | Score: `{c['score']}`\n"
        
        send_telegram(msg)
    else:
        print("No candidates met the ranking criteria today.")
