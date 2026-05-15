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
# Scan statistics counters (visible at INFO level)
# =============================================================================
scan_stats = {
    'total': 0,
    'no_data': 0,
    'low_iv': 0,
    'net_gex_positive': 0,
    'no_walls': 0,
    'walls_too_far': 0,
    'passed': 0
}


# =============================================================================
# Telegram Notifier
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
        """Send a daily summary of all signals found (sorted by score descending)."""
        if not self.enabled or not signals:
            return False
        
        # Signals should already be sorted by score descending from run_scanner
        extreme = [s for s in signals if s.get('classification') == 'EXTREME']
        high = [s for s in signals if s.get('classification') == 'HIGH_CONVICTION']
        watch = [s for s in signals if s.get('classification') == 'WATCH']
        
        text = f"<b>📊 Gamma Scanner Daily Summary</b>\n"
        text += f"Total signals: {len(signals)}\n"
        text += f"🔴 EXTREME: {len(extreme)}\n"
        text += f"🟠 HIGH_CONVICTION: {len(high)}\n"
        text += f"🟡 WATCH: {len(watch)}\n\n"
        
        text += "<b>🏆 Top Signals (by score):</b>\n"
        for s in signals[:10]:  # Show top 10 overall, sorted by score
            emoji = {'EXTREME': '🔴', 'HIGH_CONVICTION': '🟠', 'WATCH': '🟡'}.get(s['classification'], '⚪')
            text += f"  {emoji} {s['ticker']} — Score {s['score']} — ${s['price']:.2f}\n"
        
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

   