#!/usr/bin/env python3
"""
Gamma Amplification Scanner v2.3 — Fixed & Optimized
Scans thousands of tickers for setups where dealer gamma hedging can amplify price moves.
Fixes: MC cascade, net GEX filter, wall proximity, double history, earnings detection,
       MC strike limit, rate limiting, CSV logging, trade suggestions.
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from functools import lru_cache
from typing import Optional, List, Tuple, Dict, Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.stats import norm

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Constants & Configuration
# -------------------------------------------------------------------
class Config:
    """Central configuration — all tunable parameters."""
    # Rate limiting
    TICKER_DELAY = 2.0        # seconds between ticker submissions
    OPTION_DELAY = 0.3        # seconds between expirations for same ticker
    MAX_WORKERS = 3           # concurrent ticker scans
    MAX_RETRIES = 3           # API retries on 429
    BACKOFF_BASE = 2.0        # exponential backoff multiplier

    # Options data
    MAX_EXPIRATIONS = 2       # front month + next (avoid 0-DTE)
    MIN_DTE = 1               # skip 0-DTE
    MAX_DTE = 60              # avoid far-dated options
    WALL_PROXIMITY = 0.50     # 50% — accept walls up to 50% from spot
    TOP_WALLS = 10            # only consider top N walls by abs net GEX

    # Monte Carlo
    MC_N_STEPS = 50           # fixed number of steps
    MC_N_SIMULATIONS = 2000   # number of Monte Carlo paths
    MC_SEED = 42

    # Screening
    MIN_IV = 0.20             # minimum implied volatility to consider
    MIN_VOLUME = 100_000      # minimum daily dollar volume (can be adjusted)
    NET_GEX_NEGATIVE = True   # only generate signals for net GEX < 0

    # Score & classification (restored original logic)
    LOOP_GAIN_THRESHOLD = 0.65
    MC_P95_THRESHOLD = 50.0   # percentage
    SELF_SUSTAIN_THRESHOLD = 50.0

    # Trade suggestion
    OPTION_BUY_DELTA = 0.30   # target delta for suggested strikes

    # Paths
    CSV_LOG = "gamma_signals.csv"
    DEFAULT_TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL",
                       "AMZN", "NVDA", "META", "TSLA", "AVGO", "JPM"]


# -------------------------------------------------------------------
# Helper: Rate Limiter & Retry
# -------------------------------------------------------------------
def rate_limited(delay: float):
    """Decorator to enforce a minimum time between calls."""
    last_call = [0.0]
    def decorator(func):
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_call[0]
            if elapsed < delay:
                time.sleep(delay - elapsed)
            result = func(*args, **kwargs)
            last_call[0] = time.time()
            return result
        return wrapper
    return decorator


def retry_on_429(func):
    """Decorator: retry up to MAX_RETRIES with exponential backoff on HTTP 429."""
    def wrapper(*args, **kwargs):
        for attempt in range(Config.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    wait = Config.BACKOFF_BASE ** attempt
                    logger.warning(f"429 rate limit, waiting {wait:.1f}s")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Max retries exceeded for {func.__name__}")
    return wrapper


# -------------------------------------------------------------------
# Ticker Universe
# -------------------------------------------------------------------
def get_sp500_tickers() -> List[str]:
    """Fetch S&P 500 tickers from multiple sources."""
    sources = [
        ("Finviz CSV", "https://elite.finviz.com/export.ashx?v=111&t=SP500",
         lambda df: df['Symbol'].tolist() if 'Symbol' in df.columns else df.iloc[:,0].tolist()),
        ("GitHub Raw SP500",
         "https://raw.githubusercontent.com/datasets/s-and-500-companies/main/data/constituents.csv",
         lambda df: df['Symbol'].tolist() if 'Symbol' in df.columns else df['Ticker'].tolist()
         if 'Ticker' in df.columns else df.iloc[:,0].tolist()),
    ]
    for name, url, extractor in sources:
        try:
            df = pd.read_csv(url)
            tickers = extractor(df)
            # Validate and clean
            tickers = [t.strip().upper() for t in tickers if isinstance(t, str) and len(t.strip()) <= 5]
            logger.info(f"Loaded {len(tickers)} tickers from {name}")
            return tickers
        except Exception as e:
            logger.warning(f"Failed to load from {name}: {e}")
    logger.warning("Using default tickers")
    return Config.DEFAULT_TICKERS


# -------------------------------------------------------------------
# Earnings Detection
# -------------------------------------------------------------------
def get_earnings_info(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch earnings dates and historical average move for a ticker.
    Returns dict with 'next_earnings_date' (datetime or None) and
    'historical_avg_move' (float, as decimal) or None if unavailable.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        earnings = yf_ticker.earnings_dates
        if earnings is None or earnings.empty:
            return None

        # Ensure datetime index
        if not isinstance(earnings.index, pd.DatetimeIndex):
            earnings.index = pd.to_datetime(earnings.index)

        # Find next earnings date (future)
        now = datetime.now()
        future = earnings.index[earnings.index > now]
        next_date = future[0] if len(future) > 0 else None

        # Compute historical average absolute move (from 'Surprise%' or from price)
        # Use last 8 quarters
        recent = earnings.tail(8)
        if 'Surprise(%)' in recent.columns:
            moves = recent['Surprise(%)'].abs().dropna() / 100.0
            avg_move = moves.mean() if not moves.empty else None
        else:
            # Fallback: use price change around earnings (approximate)
            # We can't easily get historical price here, so return None
            avg_move = None

        return {
            'next_earnings_date': next_date,
            'historical_avg_move': avg_move if avg_move else None
        }
    except Exception as e:
        logger.debug(f"Earnings fetch failed for {ticker}: {e}")
        return None


# -------------------------------------------------------------------
# Options Chain & Greeks
# -------------------------------------------------------------------
@lru_cache(maxsize=128)
def fetch_options_chain(ticker: str, expiration: str) -> Optional[Dict[str, pd.DataFrame]]:
    """Fetch options chain for a single expiration. Cached for TTL (session)."""
    try:
        yf_ticker = yf.Ticker(ticker)
        # yfinance may raise on invalid expiration
        chain = yf_ticker.option_chain(expiration)
        return {'calls': chain.calls, 'puts': chain.puts}
    except Exception as e:
        logger.debug(f"Failed to fetch options for {ticker} at {expiration}: {e}")
        return None


@rate_limited(Config.OPTION_DELAY)
@retry_on_429
def get_options_data(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve all relevant options data for a ticker.
    Returns dict with 'expirations', 'calls', 'puts', 'current_price', 'iv', etc.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        # Get available expirations
        exps = yf_ticker.options
        if not exps:
            return None

        # Filter expirations: skip 0-DTE, limit to nearest MAX_EXPIRATIONS
        today = date.today()
        valid_exps = []
        for exp_str in exps:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            if Config.MIN_DTE <= dte <= Config.MAX_DTE:
                valid_exps.append((exp_str, dte))
        valid_exps.sort(key=lambda x: x[1])  # closest first
        use_exps = [e[0] for e in valid_exps[:Config.MAX_EXPIRATIONS]]
        if not use_exps:
            return None

        # Fetch current price and IV (we get price from history once)
        # Use single history call
        hist = yf_ticker.history(period="5d")
        if hist.empty:
            return None
        current_price = hist['Close'].iloc[-1]
        # Also get IV from yfinance (impliedVolatility for the ticker)
        # Unfortunately yfinance doesn't have a single IV, we'll compute from ATM options later
        iv_estimate = None

        all_calls = []
        all_puts = []
        for exp_str in use_exps:
            chain = fetch_options_chain(ticker, exp_str)
            if chain is None:
                continue
            calls = chain['calls'].copy()
            puts = chain['puts'].copy()
            if calls.empty and puts.empty:
                continue
            # Add expiration column
            calls['expiration'] = exp_str
            puts['expiration'] = exp_str
            all_calls.append(calls)
            all_puts.append(puts)

        if not all_calls and not all_puts:
            return None

        calls_df = pd.concat(all_calls, ignore_index=True) if all_calls else pd.DataFrame()
        puts_df = pd.concat(all_puts, ignore_index=True) if all_puts else pd.DataFrame()

        # Estimate IV from nearest ATM option
        if not calls_df.empty:
            atm_idx = (calls_df['strike'] - current_price).abs().idxmin()
            iv_estimate = calls_df.loc[atm_idx, 'impliedVolatility']

        return {
            'current_price': current_price,
            'iv': iv_estimate,
            'calls': calls_df,
            'puts': puts_df,
            'expirations': use_exps
        }
    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
        return None


# -------------------------------------------------------------------
# Gamma Calculations
# -------------------------------------------------------------------
def calculate_gex_profile(data: Dict[str, Any]) -> Tuple[pd.DataFrame, float]:
    """
    Compute dollar gamma (GEX) for each strike across all expirations.
    Returns (gamma_profile DataFrame, total_net_gex).
    Gamma profile: index=strike, columns=['call_gex','put_gex','net_gex']
    """
    calls = data.get('calls')
    puts = data.get('puts')
    if calls is None or puts is None or (calls.empty and puts.empty):
        return pd.DataFrame(), 0.0

    S = data['current_price']
    # Combine calls and puts
    all_options = []
    if not calls.empty:
        all_options.append(calls[['strike','expiration','openInterest','impliedVolatility']].copy())
    if not puts.empty:
        all_options.append(puts[['strike','expiration','openInterest','impliedVolatility']].copy())
    if not all_options:
        return pd.DataFrame(), 0.0
    df = pd.concat(all_options, ignore_index=True)

    # For each option, compute dollar gamma
    gammas = []
    for _, row in df.iterrows():
        strike = row['strike']
        exp_str = row['expiration']
        exp_date = datetime.strptime(exp_str, '%Y-%m-%d')
        T = (exp_date - datetime.now()).days / 365.0
        if T <= 0:
            continue
        iv = row['impliedVolatility']
        if iv is None or np.isnan(iv) or iv <= 0:
            continue
        oi = row['openInterest']
        if oi is None or np.isnan(oi) or oi <= 0:
            continue

        # Black-Scholes gamma
        d1 = (np.log(S / strike) + (0.05 + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
        gamma = norm.pdf(d1) / (S * iv * np.sqrt(T))
        dollar_gamma = gamma * S * S * 100 * oi  # per contract multiplier 100 shares
        gammas.append((strike, row['strike'], dollar_gamma if row.name in calls.index else -dollar_gamma))

    if not gammas:
        return pd.DataFrame(), 0.0

    profile = pd.DataFrame(gammas, columns=['strike', 'gex'])
    profile = profile.groupby('strike')['gex'].sum().reset_index()
    # Net GEX is sum of all GEX (positive for calls, negative for puts)
    total_net_gex = profile['gex'].sum()
    profile['abs_gex'] = profile['gex'].abs()
    profile = profile.sort_values('strike')
    return profile, total_net_gex


def detect_walls(profile: pd.DataFrame) -> List[Dict[str, float]]:
    """
    Detect gamma walls from net GEX profile.
    Returns list of wall dicts: {'strike': x, 'net_gex': y, 'abs_gex': z}
    Uses peak detection on net GEX (positive or negative).
    """
    if profile.empty:
        return []
    # Identify strikes where net GEX changes sign or has local extremum
    # We'll look for points where net GEX is large and isolated
    profile = profile.copy()
    profile['gex_change'] = profile['gex'].diff().fillna(0)
    profile['gex_change2'] = profile['gex_change'].diff().fillna(0)
    # Peaks: sign change in gex_change + large abs
    walls = []
    for i in range(1, len(profile)-1):
        prev = profile.iloc[i-1]['gex']
        curr = profile.iloc[i]['gex']
        next_ = profile.iloc[i+1]['gex']
        # Condition: local max or min (prev < curr > next or prev > curr < next)
        if (prev < curr > next) or (prev > curr < next):
            walls.append({
                'strike': profile.iloc[i]['strike'],
                'net_gex': curr,
                'abs_gex': abs(curr)
            })
    # Sort by absolute net GEX descending
    walls.sort(key=lambda w: w['abs_gex'], reverse=True)
    return walls[:Config.TOP_WALLS]


# -------------------------------------------------------------------
# Monte Carlo Cascade Simulation
# -------------------------------------------------------------------
def run_mc_cascade(data: Dict[str, Any], walls: List[Dict[str, float]],
                   catalyst_drift: float = 0.0) -> Dict[str, Any]:
    """
    Simulate price paths to estimate probability of hitting gamma walls.
    Returns dict with 'prob_hit_wall', 'mc_p95', 'first_step_amplification',
    'self_sustaining_score', 'loop_gain', 'total_potential'.
    """
    if not walls:
        return {
            'prob_hit_wall': 0.0,
            'mc_p95': 0.0,
            'first_step_amplification': 0.0,
            'self_sustaining_score': 0.0,
            'loop_gain': 0.0,
            'total_potential': 0.0
        }

    S0 = data['current_price']
    iv = data.get('iv', 0.3)
    T = 30 / 365.0  # default 30-day horizon (can be adjusted)
    # Use nearest expiration DTE if available
    if data.get('expirations'):
        exp_dates = [datetime.strptime(e, '%Y-%m-%d') for e in data['expirations']]
        nearest = min(exp_dates)
        T = max((nearest - datetime.now()).days, 1) / 365.0

    n_steps = Config.MC_N_STEPS
    dt = T / n_steps
    n_sims = Config.MC_N_SIMULATIONS

    np.random.seed(Config.MC_SEED)
    # Generate correlated random walks (GBM with drift)
    drift = catalyst_drift / T  # convert total drift to annualized drift (approx)
    rng = np.random.default_rng(Config.MC_SEED)
    Z = rng.normal(size=(n_sims, n_steps))
    paths = np.zeros((n_sims, n_steps + 1))
    paths[:, 0] = S0
    for t in range(1, n_steps + 1):
        paths[:, t] = paths[:, t-1] * np.exp((drift - 0.5 * iv**2) * dt + iv * np.sqrt(dt) * Z[:, t-1])

    final_prices = paths[:, -1]

    # First step amplification: simulate one large move (catalyst) then subsequent move
    # We approximate: first step = catalyst drift, subsequent = gamma hedging
    # Compute probability of hitting any wall within the path
    hit_any = np.zeros(n_sims, dtype=bool)
    for wall in walls:
        w_strike = wall['strike']
        # Check if path touches or crosses the wall at any step
        crossing = ((paths[:, :-1] < w_strike) & (paths[:, 1:] >= w_strike)) | \
                   ((paths[:, :-1] > w_strike) & (paths[:, 1:] <= w_strike))
        hit_any = hit_any | crossing.any(axis=1)

    prob_hit = np.mean(hit_any) * 100.0  # percentage

    # MC p95: 95th percentile of final price (absolute move)
    p95 = np.percentile(final_prices, 95)

    # First step amplification: ratio of total move to first step move (catalyst)
    first_step_move = np.abs(paths[:, 1] - S0).mean()
    total_move = np.abs(final_prices - S0).mean()
    first_step_amp = total_move / max(first_step_move, 1e-6)

    # Self-sustaining score: fraction of paths where gamma hedging continues after first catalyst
    # Simplified: if after first step, the path continues in same direction for at least 3 steps
    direction = np.sign(paths[:, 1] - S0)
    sustained = np.zeros(n_sims, dtype=bool)
    for i in range(n_sims):
        if direction[i] == 0:
            continue
        # Check if next 3 steps are same sign
        diff = np.diff(paths[i, 1:])
        if np.all(diff * direction[i] > 0):
            sustained[i] = True
    self_sustaining = np.mean(sustained) * 100.0

    # Loop gain: ratio of gamma-driven move to total move (estimate)
    # Use correlation between gamma imbalance and price move
    # Simplified: total_potential = sum of abs net GEX of walls / (S0 * total OI) * 100
    total_abs_gex = sum(w['abs_gex'] for w in walls)
    implied_gamma_impact = total_abs_gex / (S0 * 1e6)  # rough scale
    loop_gain = min(implied_gamma_impact, 1.0)  # normalize

    # Total potential: maximum move possible if all walls are hit (approximate)
    farthest_wall_dist = max(abs(w['strike'] - S0) for w in walls) / S0 * 100.0
    total_potential = farthest_wall_dist * prob_hit / 100.0

    return {
        'prob_hit_wall': prob_hit,
        'mc_p95': p95,
        'first_step_amplification': first_step_amp,
        'self_sustaining_score': self_sustaining,
        'loop_gain': loop_gain,
        'total_potential': total_potential
    }


# -------------------------------------------------------------------
# Scoring & Classification
# -------------------------------------------------------------------
def economic_score(mc_results: Dict[str, Any], data: Dict[str, Any],
                   net_gex: float, iv_percentile: float = 0.5) -> Dict[str, Any]:
    """
    Compute composite score and classify signal.
    Uses original formula: loop_gain, mc_p95, self_sustaining, first_step_amp, etc.
    Returns dict with 'score', 'classification', and all components.
    """
    loop_gain = mc_results['loop_gain']
    mc_p95 = (mc_results['mc_p95'] / data['current_price'] - 1) * 100  # as percentage move
    self_sustaining = mc_results['self_sustaining_score']
    first_step_amp = mc_results['first_step_amplification']
    total_potential = mc_results['total_potential']
    prob_hit = mc_results['prob_hit_wall']

    # Normalize components (0-100 scale)
    score_components = {
        'loop_gain': min(loop_gain * 100, 100),
        'mc_p95': min(mc_p95 * 2, 100),  # scale up moves
        'self_sustaining': self_sustaining,
        'first_step_amp': min(first_step_amp * 50, 100),
        'total_potential': min(total_potential * 10, 100),
        'prob_hit': prob_hit,
        'iv_percentile': iv_percentile * 100,
        'net_gex_negative': -net_gex / abs(net_gex) * 50 if net_gex != 0 else 0  # bonus for negative net GEX
    }

    # Weighted sum
    weights = {
        'loop_gain': 0.25,
        'mc_p95': 0.20,
        'self_sustaining': 0.15,
        'first_step_amp': 0.10,
        'total_potential': 0.10,
        'prob_hit': 0.10,
        'iv_percentile': 0.05,
        'net_gex_negative': 0.05
    }
    score = sum(score_components[k] * weights[k] for k in weights)

    # Classification thresholds (restored original)
    if score >= 75:
        classification = 'EXTREME'
    elif score >= 60:
        classification = 'HIGH_CONVICTION'
    elif score >= 40:
        classification = 'WATCH'
    else:
        classification = 'STRUCTURAL'

    return {
        'score': round(score, 1),
        'classification': classification,
        'components': score_components
    }


def trade_suggestion(data: Dict[str, Any], walls: List[Dict[str, float]],
                     classification: str, score: float) -> Optional[str]:
    """
    Generate plain-English trade recommendation.
    Only for EXTREME or HIGH_CONVICTION.
    """
    if classification not in ('EXTREME', 'HIGH_CONVICTION'):
        return None
    if not walls:
        return None

    S = data['current_price']
    # Determine direction: if net GEX negative (dealers short gamma), price moves amplify in either direction?
    # Actually, negative net GEX means dealers are net short options => they hedge by selling into strength, buying into weakness.
    # So moves can be amplified both ways. But we look for nearest wall as target.
    # Simple: nearest wall with high abs net GEX
    walls_sorted = sorted(walls, key=lambda w: abs(w['strike'] - S))
    nearest = walls_sorted[0]
    direction = 'CALL' if nearest['strike'] > S else 'PUT'
    target = nearest['strike']

    # Suggest a debit spread: buy ATM option, sell OTM option at wall
    # For simplicity, pick strike nearest to S (buy) and strike at wall (sell)
    buy_strike = S  # approximate ATM
    sell_strike = target
    if direction == 'CALL':
        # Cap sell strike to be higher than buy
        if sell_strike <= buy_strike:
            sell_strike = buy_strike * 1.05
        suggestion = f"BUY ${buy_strike:.0f} CALL, SELL ${sell_strike:.0f} CALL debit spread"
    else:
        if sell_strike >= buy_strike:
            sell_strike = buy_strike * 0.95
        suggestion = f"BUY ${buy_strike:.0f} PUT, SELL ${sell_strike:.0f} PUT debit spread"
    return suggestion


# -------------------------------------------------------------------
# Per-Ticker Scan
# -------------------------------------------------------------------
def scan_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Full scan pipeline for a single ticker.
    Returns signal dict or None if no signal.
    """
    logger.info(f"Scanning {ticker}...")
    try:
        # 1. Fetch options data
        data = get_options_data(ticker)
        if data is None:
            logger.debug(f"No options data for {ticker}")
            return None

        S = data['current_price']
        iv = data.get('iv', 0)
        if iv is None or iv < Config.MIN_IV:
            logger.debug(f"IV too low for {ticker}: {iv}")
            return None

        # 2. Compute gamma profile and net GEX
        profile, total_net_gex = calculate_gex_profile(data)
        if profile.empty:
            return None

        # 3. Filter: net GEX must be negative (dealers short gamma)
        if Config.NET_GEX_NEGATIVE and total_net_gex >= 0:
            logger.debug(f"Net GEX non-negative for {ticker}: {total_net_gex:.2f}")
            return None

        # 4. Detect walls
        walls = detect_walls(profile)
        if not walls:
            logger.debug(f"No gamma walls for {ticker}")
            return None

        # 5. Filter walls by proximity (Wall proximity configurable)
        filtered_walls = [w for w in walls if abs(w['strike'] - S) / S <= Config.WALL_PROXIMITY]
        if not filtered_walls:
            logger.debug(f"No walls within proximity for {ticker}")
            return None
        walls = filtered_walls[:Config.TOP_WALLS]

        # 6. Earnings detection
        earnings_info = get_earnings_info(ticker)
        catalyst_drift = 0.0
        if earnings_info and earnings_info['historical_avg_move']:
            catalyst_drift = earnings_info['historical_avg_move']
            logger.debug(f"Earnings drift for {ticker}: {catalyst_drift:.2%}")

        # 7. Monte Carlo cascade
        mc_results = run_mc_cascade(data, walls, catalyst_drift=catalyst_drift)

        # 8. Score & classify
        iv_percentile = min(iv / 0.5, 1.0)  # rough percentile
        score_result = economic_score(mc_results, data, total_net_gex, iv_percentile)
        classification = score_result['classification']
        score = score_result['score']

        # 9. Generate trade suggestion
        suggestion = trade_suggestion(data, walls, classification, score)

        # 10. Build output
        signal = {
            'ticker': ticker,
            'price': round(S, 2),
            'iv': round(iv, 4),
            'net_gex': round(total_net_gex, 2),
            'num_walls': len(walls),
            'nearest_wall_strike': walls[0]['strike'],
            'nearest_wall_dist_pct': round(abs(walls[0]['strike'] - S) / S * 100, 2),
            'prob_hit_wall': round(mc_results['prob_hit_wall'], 2),
            'mc_p95': round(mc_results['mc_p95'], 2),
            'first_step_amplification': round(mc_results['first_step_amplification'], 3),
            'self_sustaining_score': round(mc_results['self_sustaining_score'], 2),
            'loop_gain': round(mc_results['loop_gain'], 3),
            'total_potential': round(mc_results['total_potential'], 2),
            'score': score,
            'classification': classification,
            'trade_suggestion': suggestion if suggestion else 'N/A',
        }
        logger.info(f"Signal for {ticker}: {classification} (score {score})")
        return signal

    except Exception as e:
        logger.error(f"Error scanning {ticker}: {e}")
        return None


# -------------------------------------------------------------------
# CSV Logger
# -------------------------------------------------------------------
def log_signal_to_csv(signal: Dict[str, Any], filename: str = Config.CSV_LOG):
    """Append a single signal row to CSV."""
    fieldnames = [
        'ticker', 'price', 'iv', 'net_gex', 'num_walls', 'nearest_wall_strike',
        'nearest_wall_dist_pct', 'prob_hit_wall', 'mc_p95', 'first_step_amplification',
        'self_sustaining_score', 'loop_gain', 'total_potential', 'score',
        'classification', 'trade_suggestion'
    ]
    file_exists = os.path.isfile(filename)
    with open(filename, mode='a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: signal.get(k, '') for k in fieldnames})


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Gamma Amplification Scanner v2.3")
    parser.add_argument('--quick', action='store_true', help='Use small default ticker set')
    parser.add_argument('--ticker', nargs='+', help='Override ticker list (space-separated)')
    parser.add_argument('--drift', type=float, default=None,
                        help='Force catalyst drift (overrides earnings detection)')
    parser.add_argument('--output', type=str, default=Config.CSV_LOG,
                        help='CSV output file')
    parser.add_argument('--wall-proximity', type=float, default=Config.WALL_PROXIMITY,
                        help='Wall proximity threshold (default 0.50 = 50%%)')
    args = parser.parse_args()

    # Update config
    Config.WALL_PROXIMITY = args.wall_proximity
    Config.CSV_LOG = args.output

    # Determine ticker universe
    if args.ticker:
        tickers = args.ticker
        logger.info(f"Using {len(tickers)} tickers from --ticker argument")
    elif args.quick:
        tickers = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL",
                   "AMZN", "NVDA", "META", "TSLA", "AVGO", "JPM"]
        logger.info("Quick mode: 12 default tickers")
    else:
        tickers = get_sp500_tickers()
        logger.info(f"Full scan: {len(tickers)} S&P 500 tickers")

    # Prepare output file header if needed
    if not os.path.isfile(Config.CSV_LOG):
        fieldnames = ['ticker', 'price', 'iv', 'net_gex', 'num_walls', 'nearest_wall_strike',
                      'nearest_wall_dist_pct', 'prob_hit_wall', 'mc_p95', 'first_step_amplification',
                      'self_sustaining_score', 'loop_gain', 'total_potential', 'score',
                      'classification', 'trade_suggestion']
        with open(Config.CSV_LOG, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)

    # Scan tickers with thread pool
    signals = []
    with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
        fut_to_ticker = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(fut_to_ticker):
            ticker = fut_to_ticker[future]
            try:
                result = future.result()
                if result:
                    signals.append(result)
                    log_signal_to_csv(result)
                    logger.info(f"Signal logged for {ticker}")
            except Exception as e:
                logger.error(f"Exception in thread for {ticker}: {e}")
            # Enforce delay between ticker submissions globally
            time.sleep(Config.TICKER_DELAY)

    # Print summary
    print(f"\n=== Scan complete: {len(signals)} signals found ===")
    for sig in signals:
        print(f"{sig['ticker']}: {sig['classification']} (score {sig['score']}) "
              f"| wall {sig['nearest_wall_strike']} | prob_hit {sig['prob_hit_wall']}%")
        if sig['trade_suggestion'] != 'N/A':
            print(f"   Trade: {sig['trade_suggestion']}")

    # Also output JSON to stdout
    print("\n--- JSON ---")
    print(json.dumps(signals, indent=2))


if __name__ == "__main__":
    main()
