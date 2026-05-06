import logging
import pandas as pd
import numpy as np
import yfinance as yf
import time
from datetime import datetime
from scipy.stats import norm
from finvizfinance.screener.overview import Overview

# Import your custom notifier
from telegram_notifier import send_telegram

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 1. GAMMA CALCULATION (Black-Scholes)
# ──────────────────────────────────────────────
def calculate_gamma(S, K, T, r, sigma):
    """Calculates Option Gamma."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0
    try:
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
        return gamma
    except:
        return 0

def get_gamma_exposure(ticker):
    """Fetches option chain and computes Net Gamma Exposure."""
    try:
        stock = yf.Ticker(ticker)
        # Use fast_info for current price to save API time
        current_price = stock.fast_info['lastPrice']
        exps = stock.options
        if not exps:
            return 0, None

        # Target the nearest monthly or weekly expiry (approx 7-10 days)
        now = datetime.now()
        best_exp = min(exps, key=lambda x: abs((datetime.strptime(x, '%Y-%m-%d') - now).days - 7))
        
        opt = stock.option_chain(best_exp)
        t_days = (datetime.strptime(best_exp, '%Y-%m-%d') - now).days
        T = max(t_days, 1) / 365.0 
        r = 0.04  # Proxy 4% risk-free rate

        # Calculate Call Gamma
        call_gex = 0
        for _, row in opt.calls.iterrows():
            if row['impliedVolatility'] > 0 and row['openInterest'] > 0:
                g = calculate_gamma(current_price, row['strike'], T, r, row['impliedVolatility'])
                call_gex += (g * row['openInterest'] * 100)

        # Calculate Put Gamma
        put_gex = 0
        for _, row in opt.puts.iterrows():
            if row['impliedVolatility'] > 0 and row['openInterest'] > 0:
                g = calculate_gamma(current_price, row['strike'], T, r, row['impliedVolatility'])
                put_gex += (g * row['openInterest'] * 100)

        # Net Gamma (Call GEX - Put GEX)
        # Positive values suggest dealers are long gamma and must buy as price rises
        net_gex = call_gex - put_gex
        
        # Max Gamma Wall (Strike with highest total exposure)
        combined = pd.concat([opt.calls, opt.puts])
        # Simple heuristic for 'Wall' using Open Interest
        max_strike = combined.loc[combined['openInterest'].idxmax(), 'strike']

        return net_gex, max_strike

    except Exception as e:
        logger.warning(f"Could not process options for {ticker}: {e}")
        return 0, None

# ──────────────────────────────────────────────
# 2. SCREENER & ANALYZER
# ──────────────────────────────────────────────
def run_scanner():
    logger.info("Scanning Finviz for candidates...")
    try:
        f = Overview()
        # Corrected filter logic for finvizfinance
        filters = {
            'Average Volume': 'Over 1M',
            'Market Cap.': 'Small ($300mln to $2bln)',
            'Short Float': 'Over 15%',
            'Relative Volume': 'Over 1.5'
        }
        f.set_filter(**filters)
        candidates = f.screener_view()
    except Exception as e:
        logger.error(f"Finviz API error: {e}")
        return pd.DataFrame()

    if candidates is None or candidates.empty:
        logger.info("No candidates found.")
        return pd.DataFrame()

    results = []
    for _, row in candidates.iterrows():
        ticker = row['Ticker']
        logger.info(f"Analyzing {ticker}...")
        
        net_gex, wall = get_gamma_exposure(ticker)
        
        # Cleaning data
        try:
            short_p = float(str(row['Short Float']).strip('%'))
            rel_vol = float(row['Rel Volume'])
            price = float(row['Price'])
        except:
            short_p, rel_vol, price = 0, 0, 0

        # Scoring Logic
        score = 0
        if short_p > 25: score += 4
        elif short_p > 15: score += 2
        
        if net_gex > 500_000: score += 3
        if rel_vol > 2.0: score += 2
        if price < 50: score += 1 # Cheaper stocks squeeze harder

        results.append({
            'Ticker': ticker,
            'Price': price,
            'Short_Pct': short_p,
            'Net_GEX': round(net_gex, 0),
            'Wall': wall,
            'Score': score
        })
        time.sleep(0.5) # Avoid Yahoo Finance rate limits

    return pd.DataFrame(results).sort_values(by='Score', ascending=False)

# ──────────────────────────────────────────────
# 3. MAIN EXECUTION
# ──────────────────────────────────────────────
if __name__ == "__main__":
    report = run_scanner()

    if not report.empty:
        # Filter for quality alerts
        alerts = report[report['Score'] >= 5]
        
        print("\n" + "="*60)
        print(report.to_string(index=False))
        print("="*60)

        if not alerts.empty:
            msg = "🚨 *GAMMA SQUEEZE ALERT* 🚨\n\n"
            for _, row in alerts.head(5).iterrows():
                msg += (f"Ticker: *{row['Ticker']}*\n"
                        f"Price: ${row['Price']}\n"
                        f"Short Float: {row['Short_Pct']}%\n"
                        f"Net GEX: {row['Net_GEX']:,.0f}\n"
                        f"Wall: {row['Wall']}\n"
                        f"Score: *{row['Score']}*\n"
                        f"---------------------------\n")
            
            send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
            logger.info("Alerts sent to Telegram.")
        
        report.to_csv("gamma_scan_results.csv", index=False)
    else:
        logger.info("Scan completed with no results.")
