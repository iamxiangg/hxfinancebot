#!/usr/bin/env python3
"""
Gamma Amplification Scanner v2.3
Scans thousands of tickers for dealer gamma hedging setups.
Outputs trade suggestions based on gamma walls, probability, and catalysts.
"""

import argparse
import csv
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.stats import norm

# Optional progress bar
try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
BATCH_DELAY = 0.5          # seconds between API batches (rate limiting)
MAX_WORKERS = 10           # parallel threads for scanning
CACHE_TTL = 300            # seconds for options chain cache
GAMMA_THRESHOLD = 1_000    # minimum dollar gamma to consider a strike
SIGNAL_PROB_MIN = 0.55     # minimum Monte Carlo probability for a signal
WALL_PROXIMITY = 0.02      # max 2% away from a gamma wall to flag as "near"
DEFAULT_TICKERS = [
    "AAPL", "TSLA", "SPY", "QQQ", "AMZN", "GOOGL", "MSFT", "NVDA", "META", "AMD",
    "NFLX", "DIS", "BA", "JPM", "GS", "XOM", "CVX", "JNJ", "PFE", "UNH",
    "V", "MA", "WMT", "HD", "PG", "KO", "PEP", "ABNB", "DASH", "UBER",
    "LYFT", "SNAP", "PINS", "RIVN", "LCID", "PLTR", "SOFI", "COIN", "MARA", "RIOT",
    "AMC", "GME", "BB", "NOK", "TLRY", "MRNA", "ZM", "PTON", "DOCU", "SHOP",
]
TICKER_COLUMNS = ['Symbol', 'Ticker', 'symbol', 'ticker']  # accept any case
LOG_DIR = "logs"
CSV_LOG = os.path.join(LOG_DIR, "gamma_signals.csv")

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "gamma_scanner.log"), mode='a'),
    ],
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Ticker universe loader
# -------------------------------------------------------------------
def load_ticker_universe(quick_mode: bool = False,
                         custom_tickers: Optional[List[str]] = None) -> List[str]:
    """Build a list of tickers to scan.

    Priority:
    1. If custom_tickers given (--ticker), use those.
    2. If quick_mode (--quick), return DEFAULT_TICKERS only.
    3. Try Finviz S&P 500 table.
    4. Fallback to GitHub raw CSV.
    5. If all else fails, use DEFAULT_TICKERS.
    """
    if custom_tickers:
        logger.info(f"Using custom tickers: {custom_tickers}")
        return custom_tickers

    if quick_mode:
        logger.info(f"Quick mode – scanning {len(DEFAULT_TICKERS)} default tickers")
        return DEFAULT_TICKERS

    # Try Finviz
    try:
        logger.info("Fetching S&P 500 constituents from Finviz...")
        url = "https://finviz.com/export.ashx?v=111&sc=1&sp=500"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/91.0.4472.124 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Finviz returned status {resp.status_code}")

        df = pd.read_csv(StringIO(resp.text))
        # Normalise column names
        df.columns = [col.strip().lower() for col in df.columns]
        if 'symbol' in df.columns:
            tickers = df['symbol'].dropna().tolist()
        elif 'ticker' in df.columns:
            tickers = df['ticker'].dropna().tolist()
        else:
            raise KeyError("Finviz CSV missing Symbol/Ticker column")

        # Clean: remove nulls and whitespace
        tickers = [t.strip().upper() for t in tickers if isinstance(t, str) and t.strip()]
        logger.info(f"Loaded {len(tickers)} tickers from Finviz")
        return tickers
    except Exception as e:
        logger.warning(f"Finviz unavailable (status {e})")

    # Fallback to GitHub raw CSV
    try:
        logger.info("Trying GitHub raw CSV fallback...")
        csv_url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
        resp = requests.get(csv_url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        df.columns = [col.strip().lower() for col in df.columns]
        if 'symbol' in df.columns:
            tickers = df['symbol'].dropna().tolist()
        elif 'ticker' in df.columns:
            tickers = df['ticker'].dropna().tolist()
        else:
            # attempt first column
            tickers = df.iloc[:, 0].dropna().tolist()
            logger.warning("SP500 CSV missing expected column, using first column")
        tickers = [t.strip().upper() for t in tickers if isinstance(t, str) and t.strip()]
        logger.info(f"Loaded {len(tickers)} tickers from GitHub CSV")
        # If still empty, raise
        if not tickers:
            raise ValueError("No tickers found in fallback CSV")
        return tickers
    except Exception as e:
        logger.error(f"GitHub fallback failed: {e}")

    logger.warning("Could not load S&P 500; using default tickers")
    return DEFAULT_TICKERS

# -------------------------------------------------------------------
# Rate‑limited HTTP session (for non‑yfinance calls)
# -------------------------------------------------------------------
http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

def rate_limited_request(url: str, **kwargs) -> requests.Response:
    """Exponential backoff on 429 errors."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = http_session.get(url, timeout=15, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"429 rate limited – waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt + random.uniform(0, 1)
            logger.warning(f"Request error, retry in {wait:.1f}s: {e}")
            time.sleep(wait)
    raise RuntimeError("Max retries exceeded")

# -------------------------------------------------------------------
# Options data caching (yfinance) – no custom session passed
# -------------------------------------------------------------------
def get_options_chain(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch calls/puts for all expirations. Cached with TTL.

    IMPORTANT: Do NOT pass a requests.Session to yfinance – it now uses
    curl_cffi internally.
    """
    # Manual TTL cache using a global dict
    if not hasattr(get_options_chain, '_cache'):
        get_options_chain._cache = {}
    key = ticker.upper()
    now = time.time()
    if key in get_options_chain._cache:
        cached = get_options_chain._cache[key]
        if now - cached['time'] < CACHE_TTL:
            return cached['data']

    try:
        yf_ticker = yf.Ticker(ticker)          # no session=... !
        expirations = yf_ticker.options
        if not expirations:
            logger.warning(f"{ticker}: no options available")
            return None

        calls = []
        puts = []
        for exp in expirations:
            # Skip 0‑DTE options
            exp_date = datetime.strptime(exp, "%Y-%m-%d")
            if exp_date.date() == datetime.today().date():
                continue

            opt = yf_ticker.option_chain(exp)
            calls.append(opt.calls)
            puts.append(opt.puts)

        if not calls and not puts:
            return None

        data = {
            'calls': pd.concat(calls, ignore_index=True) if calls else pd.DataFrame(),
            'puts': pd.concat(puts, ignore_index=True) if puts else pd.DataFrame(),
            'underlying_price': yf_ticker.history(period="1d")['Close'].iloc[-1] if len(
                yf_ticker.history(period="1d")) > 0 else None,
            'expirations': expirations,
        }
        get_options_chain._cache[key] = {'data': data, 'time': now}
        return data
    except Exception as e:
        logger.error(f"Error processing {ticker}: {e}")
        return None

# -------------------------------------------------------------------
# Black-Scholes dollar gamma
# -------------------------------------------------------------------
def black_scholes_gamma(S: float, K: float, T: float, r: float,
                        sigma: float, option_type: str) -> float:
    """Return gamma of a single option (calls and puts have same gamma)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    pdf = norm.pdf(d1)
    gamma = pdf / (S * sigma * np.sqrt(T))
    return gamma

def dollar_gamma(gamma: float, S: float, open_interest: int, tick_size: float = 1.0) -> float:
    """Compute dollar gamma = gamma * S^2 * OI * tick_size."""
    return gamma * S * S * open_interest * tick_size

def compute_gamma_profile(data: Dict[str, Any],
                          risk_free_rate: float = 0.045) -> Dict[str, Any]:
    """Aggregate net GEX per strike. Return gamma walls and total exposure."""
    S = data.get('underlying_price')
    if S is None or S <= 0:
        return {'error': 'No underlying price'}

    calls_df = data.get('calls', pd.DataFrame())
    puts_df = data.get('puts', pd.DataFrame())

    strikes = {}
    total_gamma = 0.0
    today = datetime.today().date()

    for df, mult in [(calls_df, 1), (puts_df, -1)]:
        if df.empty:
            continue
        for _, row in df.iterrows():
            try:
                K = row['strike']
                expiry = row.get('expiration', row.get('contractSymbol', ''))
                if isinstance(expiry, str) and len(expiry) >= 8:
                    exp_date = datetime.strptime(expiry[:8], "%Y%m%d").date()
                else:
                    exp_date = None

                if exp_date and exp_date == today:
                    continue  # skip 0-DTE

                T = (exp_date - today).days / 365.0 if exp_date else 0.02
                T = max(T, 1 / 365)

                OI = row.get('openInterest', 0)
                if OI <= 0:
                    continue
                iv = row.get('impliedVolatility', 0.3)
                if iv <= 0:
                    iv = 0.3

                gamma = black_scholes_gamma(S, K, T, risk_free_rate, iv, 'call')
                dg = dollar_gamma(gamma, S, OI)
                # Net GEX: calls add, puts subtract
                net = mult * dg
                strikes[K] = strikes.get(K, 0.0) + net
                total_gamma += dg
            except Exception as e:
                logger.debug(f"Gamma calc row error: {e}")
                continue

    if not strikes:
        return {'error': 'No valid strikes'}

    # Identify gamma walls (strikes with highest concentration)
    sorted_strikes = sorted(strikes.items(), key=lambda x: abs(x[1]), reverse=True)
    top_n = min(5, len(sorted_strikes))
    walls = [{'strike': k, 'net_gex': v} for k, v in sorted_strikes[:top_n]]

    return {
        'strikes': strikes,
        'walls': walls,
        'total_gamma': total_gamma,
        'underlying_price': S,
    }

# -------------------------------------------------------------------
# Monte Carlo cascade with catalyst drift
# -------------------------------------------------------------------
def monte_carlo_cascade(S: float, sigma: float, T: float,
                        strikes: Dict[float, float], n_sims: int = 5000,
                        catalyst_drift: float = 0.0) -> Dict[str, float]:
    """Simulate price paths; return probability of hitting each gamma wall,
    and overall directional breakdown.

    Returns:
        dict with keys: prob_up, prob_down, avg_move_up, avg_move_down,
                        wall_hit_probs (dict strike->prob)
    """
    dt = T / 252.0  # daily steps approximating expiry time
    n_steps = max(1, int(252 * T))
    paths = np.zeros((n_sims, n_steps + 1))
    paths[:, 0] = S

    for i in range(n_sims):
        for j in range(1, n_steps + 1):
            z = np.random.normal()
            paths[i, j] = paths[i, j-1] * np.exp(
                (catalyst_drift - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * z
            )

    final_prices = paths[:, -1]

    # Overall direction
    prob_up = np.mean(final_prices > S)
    prob_down = 1 - prob_up
    avg_up = np.mean(final_prices[final_prices > S] - S) if np.any(final_prices > S) else 0.0
    avg_down = np.mean(final_prices[final_prices <= S] - S) if np.any(final_prices <= S) else 0.0

    # Wall hit probabilities
    wall_hit_probs = {}
    for K in strikes:
        hits = np.sum(final_prices >= K if strikes[K] > 0 else final_prices <= K)
        wall_hit_probs[K] = hits / n_sims

    return {
        'prob_up': prob_up,
        'prob_down': prob_down,
        'avg_move_up': avg_up,
        'avg_move_down': avg_down,
        'wall_hit_probs': wall_hit_probs,
        'final_prices': final_prices,
        'sigma': sigma,
        'catalyst_drift': catalyst_drift,
    }

# -------------------------------------------------------------------
# Trade suggestion engine
# -------------------------------------------------------------------
def suggest_trade(gamma_profile: Dict, mc_results: Dict,
                  ticker: str) -> Optional[str]:
    """Generate plain‑English trade recommendation.

    Looks for:
      - Strong upward gamma wall (large positive net GEX) near current price
      - High probability of hitting that wall
      - Catalyst (if drift > 0)
    Returns None if no clear signal.
    """
    if 'error' in gamma_profile or 'error' in mc_results:
        return None

    S = gamma_profile['underlying_price']
    walls = gamma_profile['walls']
    wall_hit_probs = mc_results.get('wall_hit_probs', {})
    prob_up = mc_results['prob_up']
    catalyst_drift = mc_results.get('catalyst_drift', 0.0)

    # Prefer strongest positive wall
    pos_walls = [w for w in walls if w['net_gex'] > 0]
    if not pos_walls:
        return None

    best_wall = max(pos_walls, key=lambda w: abs(w['net_gex']))
    K = best_wall['strike']
    # Check proximity (within 2% of current price)
    if abs(K - S) / S > WALL_PROXIMITY:
        return None

    # Check probability
    prob_hit = wall_hit_probs.get(K, 0)
    if prob_hit < SIGNAL_PROB_MIN:
        return None

    # Build suggestion
    direction = "call" if K > S else "put"
    # Approximate delta for strike: simplistic ATM assumption
    if direction == "call":
        suggestion = (f"BUY ${K:.0f} {direction.upper()} "
                      f"(debit spread recommended) – "
                      f"Gamma wall at ${K:.0f}, {prob_hit*100:.0f}% probability of reaching, "
                      f"catalyst drift {catalyst_drift*100:.1f}%")
    else:
        suggestion = (f"BUY ${K:.0f} {direction.upper()} "
                      f"(debit spread recommended) – "
                      f"Gamma wall at ${K:.0f}, {prob_hit*100:.0f}% probability of reaching, "
                      f"catalyst drift {catalyst_drift*100:.1f}%")
    return suggestion

# -------------------------------------------------------------------
# Signal classification
# -------------------------------------------------------------------
def classify_signal(gamma_profile: Dict, mc_results: Dict, ticker: str) -> Dict:
    """Return a structured signal dict for logging."""
    base = {
        'ticker': ticker,
        'timestamp': datetime.utcnow().isoformat(),
        'underlying_price': gamma_profile.get('underlying_price', 0),
        'total_gamma': gamma_profile.get('total_gamma', 0),
        'walls': gamma_profile.get('walls', []),
        'prob_up': mc_results.get('prob_up', 0),
        'prob_down': mc_results.get('prob_down', 0),
        'avg_up': mc_results.get('avg_move_up', 0),
        'avg_down': mc_results.get('avg_move_down', 0),
        'sigma': mc_results.get('sigma', 0),
        'catalyst_drift': mc_results.get('catalyst_drift', 0),
    }

    # Decide strength
    base['strength'] = 'NONE'
    if base['total_gamma'] > GAMMA_THRESHOLD * 10:
        base['strength'] = 'HIGH'
    elif base['total_gamma'] > GAMMA_THRESHOLD:
        base['strength'] = 'MODERATE'

    # Trade suggestion
    trade = suggest_trade(gamma_profile, mc_results, ticker)
    base['trade_suggestion'] = trade if trade else 'NO TRADE'

    return base

# -------------------------------------------------------------------
# CSV logging
# -------------------------------------------------------------------
def log_signal(signal: Dict, csv_path: str = CSV_LOG):
    """Append signal to CSV file. Create header if missing."""
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['timestamp', 'ticker', 'underlying_price',
                             'total_gamma', 'strength', 'prob_up', 'prob_down',
                             'avg_up', 'avg_down', 'walls', 'trade_suggestion'])
        writer.writerow([
            signal['timestamp'],
            signal['ticker'],
            signal.get('underlying_price', ''),
            signal.get('total_gamma', ''),
            signal['strength'],
            signal.get('prob_up', ''),
            signal.get('prob_down', ''),
            signal.get('avg_up', ''),
            signal.get('avg_down', ''),
            str(signal.get('walls', [])),
            signal.get('trade_suggestion', ''),
        ])

# -------------------------------------------------------------------
# Scan a single ticker
# -------------------------------------------------------------------
def scan_ticker(ticker: str,
                risk_free_rate: float = 0.045,
                n_sims: int = 5000,
                catalyst_drift: float = 0.02) -> Optional[Dict]:
    """Full pipeline for one ticker."""
    try:
        # Rate limit per ticker
        time.sleep(BATCH_DELAY / MAX_WORKERS)

        data = get_options_chain(ticker)
        if not data:
            return None

        gamma_profile = compute_gamma_profile(data, risk_free_rate)
        if 'error' in gamma_profile:
            logger.debug(f"{ticker}: {gamma_profile['error']}")
            return None

        S = gamma_profile['underlying_price']
        # Use median IV from calls as sigma proxy
        calls = data.get('calls', pd.DataFrame())
        if not calls.empty:
            sigma = calls['impliedVolatility'].median()
        else:
            sigma = 0.3
        if sigma <= 0:
            sigma = 0.3

        # Time to 0 expiry: use nearest expiration
        expirations = data.get('expirations', [])
        if expirations:
            today = datetime.today().date()
            nearest_exp = min(
                (datetime.strptime(e, "%Y-%m-%d").date() for e in expirations),
                key=lambda d: abs((d - today).days)
            )
            T = (nearest_exp - today).days / 365.0
        else:
            T = 0.02

        mc_results = monte_carlo_cascade(
            S, sigma, T,
            gamma_profile['strikes'],
            n_sims=n_sims,
            catalyst_drift=catalyst_drift,
        )

        signal = classify_signal(gamma_profile, mc_results, ticker)
        log_signal(signal)
        return signal
    except Exception as e:
        logger.error(f"Error scanning {ticker}: {e}")
        return None

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Gamma Amplification Scanner v2.3")
    parser.add_argument('--quick', action='store_true',
                        help='Scan only default tickers (fast)')
    parser.add_argument('--ticker', nargs='+',
                        help='Custom ticker list to scan')
    parser.add_argument('--n-sims', type=int, default=5000,
                        help='Number of Monte Carlo simulations')
    parser.add_argument('--drift', type=float, default=0.02,
                        help='Catalyst drift (e.g. 0.02 for 2%% bias)')
    args = parser.parse_args()

    start_time = time.time()
    logger.info(f"🤖 Gamma Scan v2.3 starting... (n_sims={args.n_sims}, drift={args.drift})")

    # Load universe
    universe = load_ticker_universe(quick_mode=args.quick,
                                    custom_tickers=args.ticker)
    if not universe:
        logger.error("No tickers to scan. Exiting.")
        sys.exit(1)
    logger.info(f"Scanning {len(universe)} tickers")

    # Scan with progress bar if available
    signals = []
    if TQDM_AVAILABLE:
        iterator = tqdm(universe, desc="Scanning", unit="ticker")
    else:
        iterator = universe

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scan_ticker, t, catalyst_drift=args.drift,
                                    n_sims=args.n_sims): t for t in iterator}
        for future in as_completed(futures):
            result = future.result()
            if result:
                signals.append(result)

    elapsed = time.time() - start_time
    logger.info(f"Scan complete in {elapsed:.0f}s – {len(signals)} signals generated")

    # Display top signals
    if signals:
        strong = [s for s in signals if s['strength'] in ('HIGH', 'MODERATE')]
        if strong:
            print("\n===== TOP SIGNALS =====")
            for s in sorted(strong, key=lambda x: x['total_gamma'], reverse=True)[:10]:
                print(f"{s['ticker']:6s} | Price ${s['underlying_price']:>8.2f} | "
                      f"Gamma {s['total_gamma']:>12,.0f} | {s['strength']:8s} | "
                      f"Up {s['prob_up']:.1%} Down {s['prob_down']:.1%} | "
                      f"Trade: {s['trade_suggestion']}")
            print("========================")
        else:
            print("No strong signals found.")
    else:
        print("No signals generated.")

    logger.info(f"Log written to {CSV_LOG}")
    print(f"\nLog written to {CSV_LOG}")

if __name__ == "__main__":
    main()
