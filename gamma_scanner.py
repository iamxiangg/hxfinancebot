#!/usr/bin/env python3
"""
gamma_scanner.py – S&P 500 Gamma Squeeze Scanner
Uses options data from Yahoo Finance to compute GEX profiles,
detect walls, run cascade simulations, and score tickers.
"""

import pandas as pd
import numpy as np
import yfinance as yf
from scipy.stats import norm
import requests
from io import StringIO
import time
import datetime
import sys
import os
import csv
from datetime import datetime, timedelta

# =============================================================================
# 1. LOAD & FILTER S&P 500 TICKERS (volume > 3M)
# =============================================================================
CSV_URL = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
tickers = []

try:
    resp = requests.get(CSV_URL, timeout=10)
    resp.raise_for_status()
    sp500_df = pd.read_csv(StringIO(resp.text))
    # Ensure volume is numeric
    sp500_df['volume'] = pd.to_numeric(sp500_df['volume'], errors='coerce')
    sp500_df = sp500_df.dropna(subset=['volume'])
    # <-- NEW: Apply volume filter (3 million midpoint)
    filtered = sp500_df[sp500_df['volume'] > 3_000_000]
    tickers = filtered['symbol'].tolist()
    print(f"✅ Filtered to {len(tickers)} liquid S&P 500 tickers (volume > 3M).")
except Exception as e:
    print(f"⚠️ Failed to load/filter tickers: {e}")
    print("Falling back to a minimal hardcoded list (AAPL, MSFT, GOOGL, AMZN, NVDA).")
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]

# =============================================================================
# 2. OPTIONS DATA FETCHER
# =============================================================================
def get_options_chain(ticker, expiration_date):
    """Fetch call and put data for a specific expiration."""
    stock = yf.Ticker(ticker)
    try:
        opt = stock.option_chain(expiration_date)
        return opt.calls, opt.puts
    except Exception as e:
        print(f"  - Error fetching options for {ticker} @ {expiration_date}: {e}")
        return None, None

def get_nearest_expirations(ticker, num_expiries=3):
    """Return the nearest n expiration dates (excluding today)."""
    stock = yf.Ticker(ticker)
    try:
        expirations = stock.options
    except:
        return []
    if not expirations:
        return []
    today = datetime.now().date()
    future = [e for e in expirations if datetime.strptime(e, '%Y-%m-%d').date() > today]
    return future[:num_expiries]

# =============================================================================
# 3. GAMMA CALCULATIONS
# =============================================================================
def black_scholes_gamma(S, K, T, r, sigma):
    """Compute Black‑Scholes gamma for a single option."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))

def calculate_gex_profile(ticker, expirations, min_oi=100, min_iv=0.05):
    """
    Build a gamma exposure profile across all provided expirations.
    Returns DataFrame with columns ['strike', 'gex'].
    """
    S = yf.Ticker(ticker).info.get('regularMarketPrice', None)
    if not S:
        print(f"  ⚠️ Cannot get spot price for {ticker}")
        return None

    r = 0.05  # risk‑free rate (approx)
    gammas = {}  # strike -> total dollar gamma

    for exp_date in expirations:
        calls, puts = get_options_chain(ticker, exp_date)
        if calls is None or puts is None or calls.empty or puts.empty:
            continue

        T = (datetime.strptime(exp_date, '%Y-%m-%d') - datetime.now()).days / 365.0
        if T <= 0:
            continue

        # Combine calls and puts
        calls = calls.copy()
        puts = puts.copy()
        calls['type'] = 'call'
        puts['type'] = 'put'

        # Ensure required columns exist
        if 'impliedVolatility' not in calls.columns or 'openInterest' not in calls.columns:
            continue
        if 'impliedVolatility' not in puts.columns or 'openInterest' not in puts.columns:
            continue

        # Filter by minimum OI and IV
        calls = calls[(calls['openInterest'] > min_oi) & (calls['impliedVolatility'] > min_iv)]
        puts  = puts[(puts['openInterest'] > min_oi) & (puts['impliedVolatility'] > min_iv)]

        if calls.empty and puts.empty:
            continue

        # Process calls
        for idx, row in calls.iterrows():
            K = row['strike']
            sigma = row['impliedVolatility']
            oi = row['openInterest']
            gamma_per_share = black_scholes_gamma(S, K, T, r, sigma)
            # Dollar gamma = gamma * S^2 * OI * 100 (1 contract = 100 shares)
            dollar_gamma = gamma_per_share * (S**2) * oi * 100
            if K not in gammas:
                gammas[K] = 0.0
            gammas[K] += dollar_gamma   # calls add positive gamma

        # Process puts (negative gamma)
        for idx, row in puts.iterrows():
            K = row['strike']
            sigma = row['impliedVolatility']
            oi = row['openInterest']
            gamma_per_share = black_scholes_gamma(S, K, T, r, sigma)
            dollar_gamma = gamma_per_share * (S**2) * oi * 100
            if K not in gammas:
                gammas[K] = 0.0
            gammas[K] -= dollar_gamma   # puts add negative gamma

    if not gammas:
        print(f"  ⚠️ No gamma data computed for {ticker}")
        return None

    # Build DataFrame – ensure 2 columns only
    # <-- FIX: removed duplicate 'strike' from tuple; now (strike, gex)
    profile = [(strike, gex) for strike, gex in gammas.items()]
    df = pd.DataFrame(profile, columns=['strike', 'gex'])
    df = df.sort_values('strike').reset_index(drop=True)
    return df

# =============================================================================
# 4. WALL DETECTION
# =============================================================================
def find_gamma_walls(gex_df, num_walls=5, min_distance_pct=0.02):
    """
    Identify largest positive and negative gamma walls.
    Returns two lists: (pos_walls, neg_walls) each sorted by magnitude.
    """
    if gex_df is None or gex_df.empty:
        return [], []

    pos = gex_df[gex_df['gex'] > 0].nlargest(num_walls, 'gex')
    neg = gex_df[gex_df['gex'] < 0].nsmallest(num_walls, 'gex')  # most negative = largest downward force
    return pos.to_dict('records'), neg.to_dict('records')

# =============================================================================
# 5. MONTE CARLO CASCADE SIMULATION (simplified)
# =============================================================================
def simulate_cascade(ticker, gex_df, spot, num_simulations=1000, steps=50):
    """
    Simple gamma cascade simulation using GEX profile.
    Returns probability of a >5% squeeze within steps.
    """
    if gex_df is None or gex_df.empty:
        return 0.0

    strikes = gex_df['strike'].values
    gex_vals = gex_df['gex'].values
    # Interpolate GEX at any price
    from scipy.interpolate import interp1d
    f_gex = interp1d(strikes, gex_vals, kind='linear', bounds_error=False, fill_value=0.0)

    squeeze_count = 0
    for _ in range(num_simulations):
        price = spot
        for _ in range(steps):
            # Gamma feedback: if positive gex, price tends to rise, etc.
            gamma_at_price = f_gex(price)
            # Convert to a small drift (arbitrary scaling)
            drift = gamma_at_price * 1e-6 / (spot * 100)  # normalize
            shock = np.random.normal(0, 0.01)  # daily volatility approx
            price *= (1 + drift + shock)
            if price <= 0:
                break
        if price / spot - 1 > 0.05:
            squeeze_count += 1
    return squeeze_count / num_simulations

# =============================================================================
# 6. ECONOMIC SCORING
# =============================================================================
def economic_score(ticker, gex_df, spot):
    """
    Score based on GEX concentration, spot proximity to walls, etc.
    Returns a score 0–100.
    """
    if gex_df is None or gex_df.empty:
        return 0.0

    # GEX intensity: total absolute gamma per $1M market cap
    total_abs_gex = gex_df['gex'].abs().sum()
    mcap = yf.Ticker(ticker).info.get('marketCap', 1)
    intensity = total_abs_gex / mcap * 1e6  # scaled per $1M

    # Proximity to nearest positive wall
    pos_walls, _ = find_gamma_walls(gex_df, num_walls=3)
    dist_to_wall = min([abs(w['strike'] - spot)/spot for w in pos_walls]) if pos_walls else 1.0
    proximity_factor = max(0, 1 - dist_to_wall * 10)  # 10% away -> 0 score

    # Combine
    score = min(100, intensity * 10 + proximity_factor * 50)
    return score

# =============================================================================
# 7. MAIN SCANNER
# =============================================================================
def scan_ticker(ticker):
    """Run full analysis for a single ticker and return a result dict."""
    print(f"\n🔍 Scanning {ticker}...")
    result = {'ticker': ticker, 'error': None}

    try:
        expirations = get_nearest_expirations(ticker, num_expiries=3)
        if not expirations:
            print(f"  ⚠️ No near‑term expirations for {ticker}")
            result['error'] = 'no expirations'
            return result

        gex_df = calculate_gex_profile(ticker, expirations)
        if gex_df is None:
            result['error'] = 'no gex data'
            return result

        spot = yf.Ticker(ticker).info.get('regularMarketPrice', None)
        if not spot:
            result['error'] = 'no spot price'
            return result

        # Wall detection
        pos_walls, neg_walls = find_gamma_walls(gex_df)
        result['positive_walls'] = pos_walls
        result['negative_walls'] = neg_walls

        # Cascade simulation
        prob_squeeze = simulate_cascade(ticker, gex_df, spot)
        result['prob_squeeze'] = prob_squeeze

        # Score
        result['score'] = economic_score(ticker, gex_df, spot)

        # GEX profile (strikes and values) for later analysis
        result['gex_profile'] = gex_df

        print(f"  ✅ Score: {result['score']:.1f}, Squeeze prob: {prob_squeeze:.3f}")

    except Exception as e:
        print(f"  ❌ Error scanning {ticker}: {e}")
        result['error'] = str(e)

    return result

def run_scanner(ticker_list, output_csv='gamma_results.csv'):
    """Scan all tickers and write results to CSV."""
    results = []
    for i, ticker in enumerate(ticker_list):
        print(f"\n[{i+1}/{len(ticker_list)}] Processing {ticker}")
        res = scan_ticker(ticker)
        if res.get('error') is None:
            results.append(res)
        time.sleep(0.5)  # be kind to Yahoo

    # Write summary CSV
    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['ticker', 'score', 'prob_squeeze', 'positive_walls', 'negative_walls'])
        for r in results:
            pos = [w['strike'] for w in r.get('positive_walls', [])]
            neg = [w['strike'] for w in r.get('negative_walls', [])]
            writer.writerow([
                r['ticker'],
                round(r.get('score', 0), 2),
                round(r.get('prob_squeeze', 0), 4),
                ';'.join(map(str, pos)),
                ';'.join(map(str, neg))
            ])

    print(f"\n✅ Scan complete. Results saved to {output_csv}")
    return results

# =============================================================================
# 8. ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    # Use the volume‑filtered tickers from above
    if not tickers:
        print("No tickers to scan. Exiting.")
        sys.exit(1)

    print(f"Starting gamma scanner with {len(tickers)} tickers (filtered by volume > 3M)...")
    run_scanner(tickers, output_csv='gamma_results.csv')
