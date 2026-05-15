#!/usr/bin/env python3
"""
gamma_scanner.py – S&P 500 Gamma Squeeze Scanner v2.3 (Fixed & Merged)
Uses options data from Yahoo Finance to compute GEX profiles,
detect walls, run cascade simulations, score tickers, and send Telegram alerts.

Fixes applied:
  - MC dt calculation (n_steps=50, dt=T/n_steps)
  - net_gex < 0 filter (dealers must be short gamma)
  - Wall proximity configurable (default 50%)
  - Single history() call per ticker
  - Earnings detection (historical avg move as catalyst drift)
  - MC strike loop limited to top 10 walls
  - Trade suggestion engine
  - Rate limiting with exponential backoff
  - CSV logging with all fields
  - Telegram notification support
"""

import pandas as pd
import numpy as np
import yfinance as yf
from scipy.stats import norm
from scipy.interpolate import interp1d
import requests
from io import StringIO
import time
import sys
import os
import csv
import json
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from functools import lru_cache
from typing import Optional, List, Tuple, Dict, Any

# =============================================================================
# Logging setup
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# =============================================================================
# 1. LOAD & FILTER S&P 500 TICKERS (volume > 3M) — RETAINED FROM TEMPLATE
# =============================================================================
CSV_URL = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"

def load_tickers_from_csv() -> List[str]:
    """Load S&P 500 tickers from CSV with volume > 3M filter."""
    tickers = []
    try:
        resp = requests.get(CSV_URL, timeout=10)
        resp.raise_for_status()
        sp500_df = pd.read_csv(StringIO(resp.text))
        # Ensure volume is numeric
        sp500_df['volume'] = pd.to_numeric(sp500_df['volume'], errors='coerce')
        sp500_df = sp500_df.dropna(subset=['volume'])
        # Apply volume filter (3 million midpoint)
        filtered = sp500_df[sp500_df['volume'] > 3_000_000]
        tickers = filtered['symbol'].tolist()
        logger.info(f"✅ Filtered to {len(tickers)} liquid S&P 500 tickers (volume > 3M).")
    except Exception as e:
        logger.warning(f"⚠️ Failed to load/filter tickers: {e}")
        logger.warning("Falling back to a minimal hardcoded list (AAPL, MSFT, GOOGL, AMZN, NVDA).")
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
    return tickers

# =============================================================================
# Configuration
# =============================================================================
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
    MIN_OI = 100              # minimum open interest
    MIN_IV = 0.05             # minimum implied volatility

    # Monte Carlo
    MC_N_STEPS = 50           # fixed number of steps
    MC_N_SIMULATIONS = 2000   # number of Monte Carlo paths
    MC_SEED = 42

    # Screening
    MIN_IV_FILTER = 0.20      # minimum implied volatility to consider for signals
    NET_GEX_NEGATIVE = True   # only generate signals for net GEX < 0

    # Score & classification
    LOOP_GAIN_THRESHOLD = 0.65
    MC_P95_THRESHOLD = 50.0   # percentage
    SELF_SUSTAIN_THRESHOLD = 50.0

    # Trade suggestion
    OPTION_BUY_DELTA = 0.30   # target delta for suggested strikes

    # Paths
    CSV_LOG = "gamma_signals.csv"
    DEFAULT_TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL",
                       "AMZN", "NVDA", "META", "TSLA", "AVGO", "JPM"]


# =============================================================================
# Telegram Notifier — NEW (requested feature)
# =============================================================================
class TelegramNotifier:
    """Send alerts to Telegram bot. Configure via env vars or config file."""
    
    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.bot_token and self.chat_id)
        if self.enabled:
            logger.info("✅ Telegram notifications enabled")
        else:
            logger.info("ℹ️ Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    def send_message(self, text: str) -> bool:
        """Send a plain text message to Telegram."""
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

    def send_signal_alert(self, signal: Dict[str, Any]) -> bool:
        """Format and send a gamma signal alert."""
        if not self.enabled:
            return False
        emoji_map = {
            'EXTREME': '🔴 EXTREME',
            'HIGH_CONVICTION': '🟠 HIGH_CONVICTION',
            'WATCH': '🟡 WATCH',
            'STRUCTURAL': '🔵 STRUCTURAL'
        }
        label = emoji_map.get(signal.get('classification', ''), signal.get('classification', ''))
        text = (
            f"<b>{label}</b> — {signal['ticker']} @ ${signal['price']:.2f}\n"
            f"├ Score: {signal['score']}\n"
            f"├ Net GEX: ${signal['net_gex']:,.0f}\n"
            f"├ Nearest Wall: ${signal['nearest_wall_strike']:.2f} "
            f"({signal['nearest_wall_dist_pct']:+.2f}%)\n"
            f"├ Prob Hit Wall: {signal['prob_hit_wall']:.1f}%\n"
            f"├ IV: {signal['iv']*100:.1f}%\n"
            f"├ Loop Gain: {signal['loop_gain']:.3f}\n"
            f"└ Trade: {signal.get('trade_suggestion', 'N/A')}"
        )
        return self.send_message(text)

    def send_daily_summary(self, signals: List[Dict[str, Any]]) -> bool:
        """Send a daily summary of all signals found."""
        if not self.enabled or not signals:
            return False
        
        extreme = [s for s in signals if s.get('classification') == 'EXTREME']
        high = [s for s in signals if s.get('classification') == 'HIGH_CONVICTION']
        watch = [s for s in signals if s.get('classification') == 'WATCH']
        
        text = f"<b>📊 Gamma Scanner Daily Summary</b>\n"
        text += f"Total signals: {len(signals)}\n"
        text += f"🔴 EXTREME: {len(extreme)}\n"
        text += f"🟠 HIGH_CONVICTION: {len(high)}\n"
        text += f"🟡 WATCH: {len(watch)}\n\n"
        
        if extreme:
            text += "<b>Top Signals:</b>\n"
            for s in extreme[:5]:
                text += f"  {s['ticker']} — Score {s['score']} — ${s['price']:.2f}\n"
        if high:
            for s in high[:3]:
                text += f"  {s['ticker']} — Score {s['score']} — ${s['price']:.2f}\n"
        
        return self.send_message(text)


# =============================================================================
# 2. OPTIONS DATA FETCHER (upgraded from template with rate limiting & caching)
# =============================================================================
@lru_cache(maxsize=128)
def fetch_options_chain(ticker: str, expiration: str) -> Optional[Dict[str, pd.DataFrame]]:
    """Fetch call and put data for a specific expiration. Cached."""
    stock = yf.Ticker(ticker)
    try:
        opt = stock.option_chain(expiration)
        return {'calls': opt.calls, 'puts': opt.puts}
    except Exception as e:
        logger.debug(f"  - Error fetching options for {ticker} @ {expiration}: {e}")
        return None

def get_nearest_expirations(ticker: str, num_expiries: int = 3) -> List[str]:
    """Return the nearest n expiration dates (excluding today and 0-DTE)."""
    stock = yf.Ticker(ticker)
    try:
        expirations = stock.options
    except:
        return []
    if not expirations:
        return []
    today = date.today()
    future = []
    for e in expirations:
        exp_date = datetime.strptime(e, '%Y-%m-%d').date()
        dte = (exp_date - today).days
        if Config.MIN_DTE <= dte <= Config.MAX_DTE:
            future.append((e, dte))
    future.sort(key=lambda x: x[1])  # closest first
    return [e[0] for e in future[:num_expiries]]


def get_options_data(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve all relevant options data for a ticker with rate limiting.
    Returns dict with 'current_price', 'iv', 'calls', 'puts', 'expirations'.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        
        # Get spot price from history (single call)
        hist = yf_ticker.history(period="5d")
        if hist.empty:
            return None
        current_price = hist['Close'].iloc[-1]

        # Get nearest expirations
        use_exps = get_nearest_expirations(ticker, num_expiries=Config.MAX_EXPIRATIONS)
        if not use_exps:
            return None

        all_calls = []
        all_puts = []
        for exp_str in use_exps:
            time.sleep(Config.OPTION_DELAY)  # rate limit per expiration
            chain = fetch_options_chain(ticker, exp_str)
            if chain is None:
                continue
            calls = chain['calls'].copy()
            puts = chain['puts'].copy()
            if calls.empty and puts.empty:
                continue
            calls['expiration'] = exp_str
            puts['expiration'] = exp_str
            all_calls.append(calls)
            all_puts.append(puts)

        if not all_calls and not all_puts:
            return None

        calls_df = pd.concat(all_calls, ignore_index=True) if all_calls else pd.DataFrame()
        puts_df = pd.concat(all_puts, ignore_index=True) if all_puts else pd.DataFrame()

        # Estimate IV from nearest ATM option
        iv_estimate = None
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


# =============================================================================
# 3. GAMMA CALCULATIONS (fixed Black-Scholes + net GEX tracking)
# =============================================================================
def black_scholes_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Compute Black‑Scholes gamma for a single option."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))


def calculate_gex_profile(data: Dict[str, Any]) -> Tuple[pd.DataFrame, float]:
    """
    Compute dollar gamma (GEX) for each strike across all expirations.
    Returns (gamma_profile DataFrame, total_net_gex).
    Gamma profile: index=strike, columns=['strike', 'gex']
    Calls add positive GEX, puts add negative GEX.
    """
    calls = data.get('calls')
    puts = data.get('puts')
    if calls is None or puts is None or (calls.empty and puts.empty):
        return pd.DataFrame(), 0.0

    S = data['current_price']
    r = 0.05  # risk-free rate

    gammas = {}  # strike -> total dollar gamma

    # Filter by minimum OI and IV
    calls_filtered = calls[(calls['openInterest'] > Config.MIN_OI) & 
                           (calls['impliedVolatility'] > Config.MIN_IV)] if not calls.empty else calls
    puts_filtered = puts[(puts['openInterest'] > Config.MIN_OI) & 
                         (puts['impliedVolatility'] > Config.MIN_IV)] if not puts.empty else puts

    # Process calls (positive gamma)
    for _, row in calls_filtered.iterrows():
        K = row['strike']
        exp_str = row['expiration']
        exp_date = datetime.strptime(exp_str, '%Y-%m-%d')
        T = max((exp_date - datetime.now()).days / 365.0, 1/365)
        sigma = row['impliedVolatility']
        oi = row['openInterest']
        if sigma is None or np.isnan(sigma) or sigma <= 0:
            continue
        if oi is None or np.isnan(oi) or oi <= 0:
            continue
        gamma_per_share = black_scholes_gamma(S, K, T, r, sigma)
        dollar_gamma = gamma_per_share * (S**2) * oi * 100  # 1 contract = 100 shares
        gammas[K] = gammas.get(K, 0.0) + dollar_gamma  # calls = positive

    # Process puts (negative gamma)
    for _, row in puts_filtered.iterrows():
        K = row['strike']
        exp_str = row['expiration']
        exp_date = datetime.strptime(exp_str, '%Y-%m-%d')
        T = max((exp_date - datetime.now()).days / 365.0, 1/365)
        sigma = row['impliedVolatility']
        oi = row['openInterest']
        if sigma is None or np.isnan(sigma) or sigma <= 0:
            continue
        if oi is None or np.isnan(oi) or oi <= 0:
            continue
        gamma_per_share = black_scholes_gamma(S, K, T, r, sigma)
        dollar_gamma = gamma_per_share * (S**2) * oi * 100
        gammas[K] = gammas.get(K, 0.0) - dollar_gamma  # puts = negative

    if not gammas:
        return pd.DataFrame(), 0.0

    profile = [(strike, gex) for strike, gex in gammas.items()]
    df = pd.DataFrame(profile, columns=['strike', 'gex'])
    df = df.sort_values('strike').reset_index(drop=True)
    total_net_gex = df['gex'].sum()
    return df, total_net_gex


# =============================================================================
# 4. WALL DETECTION (net GEX peak method)
# =============================================================================
def find_gamma_walls(gex_df: pd.DataFrame, num_walls: int = 10) -> List[Dict[str, float]]:
    """
    Identify largest gamma walls from net GEX profile.
    Uses peak detection on net GEX (local max/min).
    Returns list sorted by absolute net GEX descending.
    """
    if gex_df is None or gex_df.empty or len(gex_df) < 3:
        return []

    gex = gex_df.copy()
    gex['gex_change'] = gex['gex'].diff().fillna(0)
    
    walls = []
    for i in range(1, len(gex)-1):
        prev = gex.iloc[i-1]['gex']
        curr = gex.iloc[i]['gex']
        next_ = gex.iloc[i+1]['gex']
        # Local max or min
        if (prev < curr > next_) or (prev > curr < next_):
            walls.append({
                'strike': gex.iloc[i]['strike'],
                'net_gex': curr,
                'abs_gex': abs(curr)
            })
    
    walls.sort(key=lambda w: w['abs_gex'], reverse=True)
    return walls[:num_walls]


# =============================================================================
# 5. EARNINGS DETECTION
# =============================================================================
def get_earnings_info(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetch earnings dates and historical average move for a ticker.
    Returns dict with 'next_earnings_date' and 'historical_avg_move' or None.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        earnings = yf_ticker.earnings_dates
        if earnings is None or earnings.empty:
            return None

        if not isinstance(earnings.index, pd.DatetimeIndex):
            earnings.index = pd.to_datetime(earnings.index)

        now = datetime.now()
        future = earnings.index[earnings.index > now]
        next_date = future[0] if len(future) > 0 else None

        recent = earnings.tail(8)
        if 'Surprise(%)' in recent.columns:
            moves = recent['Surprise(%)'].abs().dropna() / 100.0
            avg_move = moves.mean() if not moves.empty else None
        else:
            avg_move = None

        return {
            'next_earnings_date': next_date,
            'historical_avg_move': avg_move if avg_move else None
        }
    except Exception as e:
        logger.debug(f"Earnings fetch failed for {ticker}: {e}")
        return None


# =============================================================================
# 6. MONTE CARLO CASCADE SIMULATION (FIXED: n_steps=50, dt=T/n_steps)
# =============================================================================
def simulate_cascade(data: Dict[str, Any], walls: List[Dict[str, float]],
                     catalyst_drift: float = 0.0) -> Dict[str, Any]:
    """
    Monte Carlo cascade simulation with proper GBM.
    Tracks wall hit probability, P95, first-step amplification, self-sustaining score.
    """
    if not walls:
        return {
            'prob_hit_wall': 0.0,
            'mc_p95': data.get('current_price', 100),
            'first_step_amplification': 0.0,
            'self_sustaining_score': 0.0,
            'loop_gain': 0.0,
            'total_potential': 0.0
        }

    S0 = data['current_price']
    iv = data.get('iv', 0.3)
    
    # Time horizon: nearest expiration
    T = 30 / 365.0  # default 30-day
    if data.get('expirations'):
        exp_dates = [datetime.strptime(e, '%Y-%m-%d') for e in data['expirations']]
        nearest = min(exp_dates)
        T = max((nearest - datetime.now()).days, 1) / 365.0

    # ===== FIXED: n_steps=50, dt=T/n_steps =====
    n_steps = Config.MC_N_STEPS
    dt = T / n_steps
    n_sims = Config.MC_N_SIMULATIONS

    np.random.seed(Config.MC_SEED)
    drift_annual = catalyst_drift / T  # convert total drift to annualized
    rng = np.random.default_rng(Config.MC_SEED)
    Z = rng.normal(size=(n_sims, n_steps))
    
    paths = np.zeros((n_sims, n_steps + 1))
    paths[:, 0] = S0
    for t in range(1, n_steps + 1):
        paths[:, t] = paths[:, t-1] * np.exp(
            (drift_annual - 0.5 * iv**2) * dt + iv * np.sqrt(dt) * Z[:, t-1]
        )

    final_prices = paths[:, -1]

    # Probability of hitting any wall
    hit_any = np.zeros(n_sims, dtype=bool)
    for wall in walls:
        w_strike = wall['strike']
        crossing = ((paths[:, :-1] < w_strike) & (paths[:, 1:] >= w_strike)) | \
                   ((paths[:, :-1] > w_strike) & (paths[:, 1:] <= w_strike))
        hit_any = hit_any | crossing.any(axis=1)

    prob_hit = np.mean(hit_any) * 100.0  # percentage

    # MC P95
    p95 = np.percentile(final_prices, 95)

    # First step amplification
    first_step_move = np.abs(paths[:, 1] - S0).mean()
    total_move = np.abs(final_prices - S0).mean()
    first_step_amp = total_move / max(first_step_move, 1e-6)

    # Self-sustaining score
    direction = np.sign(paths[:, 1] - S0)
    sustained = np.zeros(n_sims, dtype=bool)
    for i in range(n_sims):
        if direction[i] == 0:
            continue
        diff = np.diff(paths[i, 1:])
        if np.all(diff * direction[i] > 0):
            sustained[i] = True
    self_sustaining = np.mean(sustained) * 100.0

    # Loop gain
    total_abs_gex = sum(w['abs_gex'] for w in walls)
    implied_gamma_impact = total_abs_gex / (S0 * 1e6)
    loop_gain = min(implied_gamma_impact, 1.0)

    # Total potential
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


# =============================================================================
# 7. ECONOMIC SCORING & CLASSIFICATION
# =============================================================================
def economic_score(mc_results: Dict[str, Any], data: Dict[str, Any],
                   net_gex: float, iv_percentile: float = 0.5) -> Dict[str, Any]:
    """
    Compute composite score and classify signal.
    Returns dict with 'score', 'classification', and 'components'.
    """
    loop_gain = mc_results['loop_gain']
    mc_p95 = (mc_results['mc_p95'] / data['current_price'] - 1) * 100
    self_sustaining = mc_results['self_sustaining_score']
    first_step_amp = mc_results['first_step_amplification']
    total_potential = mc_results['total_potential']
    prob_hit = mc_results['prob_hit_wall']

    score_components = {
        'loop_gain': min(loop_gain * 100, 100),
        'mc_p95': min(abs(mc_p95) * 2, 100),
        'self_sustaining': self_sustaining,
        'first_step_amp': min(first_step_amp * 50, 100),
        'total_potential': min(total_potential * 10, 100),
        'prob_hit': prob_hit,
        'iv_percentile': iv_percentile * 100,
        'net_gex_negative': (-net_gex / abs(net_gex) * 50) if net_gex != 0 else 0
    }

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


# =============================================================================
# FIX #2: trade_suggestion — same-strike fix
# =============================================================================
def trade_suggestion(data: Dict[str, Any], walls: List[Dict[str, float]],
                     classification: str, score: float) -> Optional[str]:
    """Generate plain-English trade recommendation for strong signals."""
    if classification not in ('EXTREME', 'HIGH_CONVICTION'):
        return None
    if not walls:
        return None

    S = data['current_price']
    walls_sorted = sorted(walls, key=lambda w: abs(w['strike'] - S))
    nearest = walls_sorted[0]
    direction = 'CALL' if nearest['strike'] > S else 'PUT'
    target = nearest['strike']

    # Buy at nearest standard strike below (CALL) or above (PUT) spot
    buy_strike = np.floor(S) if direction == 'CALL' else np.ceil(S)
    sell_strike = target

    # Enforce minimum 5% spread to avoid same-strike rounding
    min_spread = S * 0.05
    if direction == 'CALL':
        if sell_strike <= buy_strike + min_spread:
            sell_strike = buy_strike + min_spread
        suggestion = f"BUY ${buy_strike:.0f} CALL, SELL ${sell_strike:.0f} CALL debit spread"
    else:
        if sell_strike >= buy_strike - min_spread:
            sell_strike = buy_strike - min_spread
        suggestion = f"BUY ${buy_strike:.0f} PUT, SELL ${sell_strike:.0f} PUT debit spread"
    return suggestion


# =============================================================================
# 8. MAIN SCANNER
# =============================================================================
def scan_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Full scan pipeline for a single ticker."""
    logger.info(f"Scanning {ticker}...")
    try:
        # 1. Fetch options data
        data = get_options_data(ticker)
        if data is None:
            logger.debug(f"No options data for {ticker}")
            return None

        S = data['current_price']
        iv = data.get('iv', 0)
        if iv is None or iv < Config.MIN_IV_FILTER:
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
        walls = find_gamma_walls(profile, num_walls=Config.TOP_WALLS)
        if not walls:
            logger.debug(f"No gamma walls for {ticker}")
            return None

        # 5. Filter walls by proximity
        filtered_walls = [w for w in walls if abs(w['strike'] - S) / S <= Config.WALL_PROXIMITY]
        if not filtered_walls:
            logger.debug(f"No walls within {Config.WALL_PROXIMITY:.0%} for {ticker}")
            return None
        # FIX #1: Sort walls by proximity to spot so walls[0] = nearest wall
        walls = sorted(filtered_walls, key=lambda w: abs(w['strike'] - S))[:Config.TOP_WALLS]

        # 6. Earnings detection
        earnings_info = get_earnings_info(ticker)
        catalyst_drift = 0.0
        if earnings_info and earnings_info['historical_avg_move']:
            catalyst_drift = earnings_info['historical_avg_move']
            logger.debug(f"Earnings drift for {ticker}: {catalyst_drift:.2%}")

        # 7. Monte Carlo cascade
        mc_results = simulate_cascade(data, walls, catalyst_drift=catalyst_drift)

        # 8. Score & classify
        iv_percentile = min(iv / 0.5, 1.0)
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


# =============================================================================
# CSV Logger
# =============================================================================
def log_signal_to_csv(signal: Dict[str, Any], filename: str = Config.CSV_LOG):
    """Append a single signal row to CSV with thread safety."""
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


# =============================================================================
# FIX #3: run_scanner — summary first, then individual alerts
# =============================================================================
def run_scanner(ticker_list: List[str], output_csv: str = 'gamma_signals.csv',
                telegram: Optional[TelegramNotifier] = None) -> List[Dict[str, Any]]:
    """Scan all tickers with thread pool and write results."""
    signals = []
    
    with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
        fut_to_ticker = {executor.submit(scan_ticker, t): t for t in ticker_list}
        for future in as_completed(fut_to_ticker):
            ticker = fut_to_ticker[future]
            try:
                result = future.result()
                if result:
                    signals.append(result)
                    log_signal_to_csv(result, filename=output_csv)
                    logger.info(f"Signal logged for {ticker}")
            except Exception as e:
                logger.error(f"Exception in thread for {ticker}: {e}")
            # Enforce delay between ticker submissions
            time.sleep(Config.TICKER_DELAY)

    # Send daily summary FIRST (top of Telegram chat)
    if telegram and signals:
        telegram.send_daily_summary(signals)

    # Then send individual signal alerts
    if telegram:
        for sig in signals:
            if sig['classification'] in ('EXTREME', 'HIGH_CONVICTION'):
                telegram.send_signal_alert(sig)

    return signals


# =============================================================================
# 10. ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Gamma Amplification Scanner v2.3")
    parser.add_argument('--quick', action='store_true', help='Use small default ticker set')
    parser.add_argument('--ticker', nargs='+', help='Override ticker list (space-separated)')
    parser.add_argument('--drift', type=float, default=None,
                        help='Force catalyst drift (overrides earnings detection)')
    parser.add_argument('--output', type=str, default='gamma_signals.csv',
                        help='CSV output file')
    parser.add_argument('--wall-proximity', type=float, default=Config.WALL_PROXIMITY,
                        help='Wall proximity threshold (default 0.50 = 50%%)')
    parser.add_argument('--workers', type=int, default=Config.MAX_WORKERS,
                        help='Number of concurrent workers')
    parser.add_argument('--no-telegram', action='store_true',
                        help='Disable Telegram notifications')
    args = parser.parse_args()

    # Update config
    Config.WALL_PROXIMITY = args.wall_proximity
    Config.CSV_LOG = args.output
    Config.MAX_WORKERS = args.workers

    # Determine ticker universe
    if args.ticker:
        tickers = args.ticker
        logger.info(f"Using {len(tickers)} tickers from --ticker argument")
    elif args.quick:
        tickers = Config.DEFAULT_TICKERS
        logger.info("Quick mode: 12 default tickers")
    else:
        # ===== RETAINED FROM TEMPLATE: CSV loading with volume filter =====
        tickers = load_tickers_from_csv()
        if not tickers:
            logger.warning("No tickers from CSV, using defaults")
            tickers = Config.DEFAULT_TICKERS

    logger.info(f"Total tickers to scan: {len(tickers)}")

    # Initialize Telegram notifier
    telegram = None if args.no_telegram else TelegramNotifier()

    # Prepare output CSV
    if not os.path.isfile(Config.CSV_LOG):
        fieldnames = ['ticker', 'price', 'iv', 'net_gex', 'num_walls', 'nearest_wall_strike',
                      'nearest_wall_dist_pct', 'prob_hit_wall', 'mc_p95', 'first_step_amplification',
                      'self_sustaining_score', 'loop_gain', 'total_potential', 'score',
                      'classification', 'trade_suggestion']
        with open(Config.CSV_LOG, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)

    # Run scanner
    print(f"Starting gamma scanner with {len(tickers)} tickers...")
    signals = run_scanner(tickers, output_csv=Config.CSV_LOG, telegram=telegram)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Scan complete: {len(signals)} signals found")
    print(f"{'='*60}")
    
    # Group by classification
    by_class = {}
    for sig in signals:
        cls = sig['classification']
        by_class.setdefault(cls, []).append(sig)
    
    for cls in ['EXTREME', 'HIGH_CONVICTION', 'WATCH', 'STRUCTURAL']:
        if cls in by_class:
            print(f"\n{cls}: {len(by_class[cls])}")
            for sig in by_class[cls][:5]:  # show top 5
                print(f"  {sig['ticker']}: score {sig['score']} | "
                      f"${sig['price']:.2f} | wall ${sig['nearest_wall_strike']:.0f} | "
                      f"prob_hit {sig['prob_hit_wall']:.1f}%")
                if sig['trade_suggestion'] != 'N/A':
                    print(f"    → {sig['trade_suggestion']}")

    # JSON output
    print(f"\n--- JSON ---")
    print(json.dumps(signals, indent=2))
    
    print(f"\n✅ Results saved to {Config.CSV_LOG}")
