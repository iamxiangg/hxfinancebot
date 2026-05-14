#!/usr/bin/env python3
"""
Gamma Amplification Scanner v2.3
- Scans thousands of tickers for dealer gamma amplification setups
- Calculates Black-Scholes dollar gamma, GEX concentration, Monte Carlo cascade
- Includes catalyst drift, wall detection, trade suggestions
- Supports --quick and --ticker CLI arguments
- Optimized for Raspberry Pi 5 / GitHub Actions (2 cores)
"""

import os
import sys
import time
import json
import csv
import logging
import hashlib
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from io import StringIO

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.stats import norm, entropy
from tqdm import tqdm  # new in v2.3

# --------------------- CONFIGURATION --------------------- #
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Rate limiter settings (v2.3 - increased delays, backoff)
BATCH_DELAY = 0.5       # was 0.3, increased to reduce 429s
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0     # seconds base

# Yfinance cache TTL (seconds) – new in v2.3
CACHE_TTL = 300  # 5 minutes

# Black-Scholes constants
RISK_FREE_RATE = 0.05
DEFAULT_TICKERS = os.environ.get(
    'CUSTOM_TICKERS',
    'GME,AMC,DJT,PLTR,MARA,MSTR,TSLA,SOFI,HOOD,COIN'
).split(',')

# MC simulation
NUM_SIMS = 2000
NUM_STEPS = 50

# Class thresholds
EXTREME_LOOP_GAIN = 0.65
EXTREME_MC_P95 = 50
EXTREME_SELF_SUSTAIN = 50
EXTREME_ECON_SCORE = 0.10
HIGH_CONVICTION_SCORE = 0.05
WATCH_SCORE = 0.02

# Finviz screen URL
FINVIZ_URL = (
    "https://finviz.com/export.ashx?v=152&f="
    "avgvol1000,cap_smallover,sh_short_high,sh_relvol_o1.5,sh_price_o5,options_yes"
    "&ft=4&ar=180&c=1,2,3,4,5,6,7,25,61,65,67,68,69,70,71,72,73,74,75,76,77"
)

# SP500 fallback list
SP500_CSV = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"

# --------------------- CACHE WITH TTL --------------------- #
class TTLCache:
    """Simple TTL cache for options data to reduce memory usage (v2.3)."""
    def __init__(self, ttl: int = CACHE_TTL):
        self.cache = {}
        self.ttl = ttl
    
    def get(self, key: str):
        if key in self.cache:
            timestamp, value = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value):
        self.cache[key] = (time.time(), value)
    
    def clear(self):
        self.cache.clear()

_cache = TTLCache()

# --------------------- DATA STRUCTURES --------------------- #
@dataclass
class TickerData:
    ticker: str
    price: float
    iv: float
    dte: float
    catalyst_move_pct: float
    days_to_catalyst: Optional[int]

@dataclass
class ConcentrationResult:
    gini: float
    hhi: float
    entropy: float
    loop_gain: float
    peak_strike: float   # strike with maximum absolute GEX (wall)
    peak_gex: float
    strikes: list
    gex_values: list

@dataclass
class MonteCarloResult:
    p95: float
    mean: float
    self_sustaining: float
    prob_hit_wall: float
    catalyst_contrib: float
    gamma_contrib: float

@dataclass
class Signal:
    ticker: str
    price: float
    score: float
    wall_strike: float
    catalyst_move: float
    gamma_amplification: float
    mc_total: float
    prob_hit_wall: float
    days_to_catalyst: int
    trade_suggestion: str
    signal_type: str = "WATCH"
    catalyst_contrib: float = 0.0
    gamma_contrib: float = 0.0

# --------------------- OPTIONS CACHING & RATE LIMITING ----- #
_yf_sessions = {}

def _get_session(ticker: str):
    """Reuse yfinance session for the same ticker."""
    if ticker not in _yf_sessions:
        session = requests.Session()
        session.headers['User-Agent'] = 'Mozilla/5.0'
        _yf_sessions[ticker] = session
    return _yf_sessions[ticker]

def yf_request_with_retry(func, *args, **kwargs):
    """Retry with exponential backoff on Too Many Requests."""
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** attempt)
                logger.warning(f"429 rate limited, sleeping {wait:.1f}s (attempt {attempt+1})")
                time.sleep(wait)
            else:
                raise
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF)
    return None

# --------------------- BLACK-SCHOLES GREEKS ---------------- #
def black_scholes_gamma(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    return gamma

def dollar_gamma(S: float, K: float, T: float, r: float, sigma: float, oi: int) -> float:
    if oi <= 0:
        return 0.0
    g = black_scholes_gamma(S, K, T, r, sigma)
    if g == 0:
        return 0.0
    return g * S * S * 0.01 * oi * 100

def get_options_chain(ticker: str, price: float, iv: float) -> Optional[list]:
    cache_key = f"options_{ticker}_{datetime.now().strftime('%Y%m%d')}"
    cached = _cache.get(cache_key)
    if cached:
        return cached

    try:
        session = _get_session(ticker)
        tk = yf.Ticker(ticker, session=session)
        exps = yf_request_with_retry(tk.options)
        if not exps:
            return None
        
        today = datetime.now().date()
        # Skip 0-DTE: only take expirations with days > 0
        valid_exps = [
            e for e in exps
            if (datetime.strptime(e, '%Y-%m-%d').date() - today).days > 0
        ]
        if not valid_exps:
            return None
        exp = valid_exps[0]
        T = (datetime.strptime(exp, '%Y-%m-%d').date() - today).days / 365.0
        if T <= 0:
            return None
        
        chains = yf_request_with_retry(tk.option_chain, exp)
        if not chains:
            return None
        
        calls = chains.calls
        puts = chains.puts
        combined = pd.concat([calls, puts])
        combined = combined[combined['openInterest'] > 0].copy()
        combined['Strike'] = combined['strike'].astype(float)
        combined['OI'] = combined['openInterest'].astype(int)
        combined['Type'] = combined['contractSymbol'].str.contains('C').map({True: 'call', False: 'put'})
        
        dg_list = []
        for _, row in combined.iterrows():
            gamma = black_scholes_gamma(price, row['Strike'], T, RISK_FREE_RATE, iv)
            if gamma == 0:
                continue
            dg = dollar_gamma(price, row['Strike'], T, RISK_FREE_RATE, iv, row['OI'])
            dg_list.append({
                'strike': row['Strike'],
                'oi': row['OI'],
                'type': row['Type'],
                'dollar_gamma': dg,
                'gamma': gamma,
                'T': T
            })
        
        result = sorted(dg_list, key=lambda x: x['strike'])
        _cache.set(cache_key, result)
        return result
    
    except Exception as e:
        logger.error(f"Options fetch error for {ticker}: {e}")
        return None

# --------------------- CONCENTRATION ANALYSIS -------------- #
def analyze_concentration(options: list, price: float) -> ConcentrationResult:
    if not options:
        return ConcentrationResult(0, 0, 0, 0, price, 0, [], [])
    
    strikes = [o['strike'] for o in options]
    gex = [o['dollar_gamma'] for o in options]
    total_gex = sum(abs(g) for g in gex)
    if total_gex == 0:
        return ConcentrationResult(0, 0, 0, 0, price, 0, strikes, gex)
    
    # Net GEX per strike
    net_gex = defaultdict(float)
    for o in options:
        if o['type'] == 'call':
            net_gex[o['strike']] += o['dollar_gamma']
        else:
            net_gex[o['strike']] -= o['dollar_gamma']
    
    peak_strike = max(net_gex, key=lambda k: abs(net_gex[k]))
    peak_gex = net_gex[peak_strike]
    
    sorted_gex = sorted([abs(g) for g in gex])
    n = len(sorted_gex)
    if n > 1:
        gini = (2 * sum((i+1)*v for i,v in enumerate(sorted_gex)) / (n * sum(sorted_gex)) - (n+1)/n)
    else:
        gini = 0
    
    weights = [abs(g)/total_gex for g in gex]
    hhi = sum(w**2 for w in weights)
    ent = entropy(weights, base=2) if weights else 0
    
    near_strikes = [g for s,g in zip(strikes, gex) if abs(s/price - 1) < 0.05 and g > 0]
    loop_gain = sum(near_strikes) / total_gex if total_gex > 0 else 0
    
    return ConcentrationResult(
        gini=round(gini, 4),
        hhi=round(hhi, 4),
        entropy=round(ent, 4),
        loop_gain=round(loop_gain, 4),
        peak_strike=peak_strike,
        peak_gex=peak_gex,
        strikes=strikes,
        gex_values=gex
    )

# --------------------- MONTE CARLO CASCADE ----------------- #
def monte_carlo_cascade(price: float, wall_strike: float, conc: ConcentrationResult,
                        catalyst_move_pct: float, days_to_catalyst: int) -> MonteCarloResult:
    np.random.seed(None)
    loop_gain = conc.loop_gain
    
    if days_to_catalyst is not None and days_to_catalyst < 365 and catalyst_move_pct > 0:
        drift_per_step = catalyst_move_pct / 100 / 10
        scale_per_step = catalyst_move_pct / 100 / 15
        catalyst_present = True
    else:
        drift_per_step = 0.0
        scale_per_step = 0.01
        catalyst_present = False
    
    results = []
    catalyst_total_moves = []
    gamma_total_moves = []
    
    for sim in range(NUM_SIMS):
        price_path = [price]
        catalyst_path = [0.0]
        gamma_path = [0.0]
        
        for step in range(NUM_STEPS):
            current_price = price_path[-1]
            if catalyst_present:
                random_move = np.random.normal(loc=drift_per_step, scale=scale_per_step)
            else:
                random_move = np.random.normal(loc=0, scale=0.01)
            
            catalyst_contrib = random_move * current_price if catalyst_present else 0.0
            
            distance_to_wall = (wall_strike - current_price) / current_price
            proximity = min(3.0, max(0.5, 1.0 / (abs(distance_to_wall) + 0.1)))
            gamma_amplify = loop_gain * proximity * 0.5
            gamma_contrib = current_price * gamma_amplify * np.sign(distance_to_wall) * 0.01
            
            new_price = current_price + catalyst_contrib + gamma_contrib
            price_path.append(new_price)
            catalyst_path.append(catalyst_path[-1] + catalyst_contrib)
            gamma_path.append(gamma_path[-1] + gamma_contrib)
        
        final_price = price_path[-1]
        total_move = (final_price - price) / price * 100
        results.append(abs(total_move))
        catalyst_total_moves.append(abs((catalyst_path[-1] / price) * 100))
        gamma_total_moves.append(abs((gamma_path[-1] / price) * 100))
    
    p95 = np.percentile(results, 95)
    mean = np.mean(results)
    self_sustaining = np.mean([1 for r in results if r > 20]) * 100
    
    wall_distance_pct = abs(wall_strike - price) / price * 100
    prob_hit_wall = min(100.0, np.mean([1 if r >= wall_distance_pct * 0.8 else 0 for r in results]) * 100)
    
    cat_contrib = np.median(catalyst_total_moves) if catalyst_total_moves else 0.0
    gam_contrib = np.median(gamma_total_moves) if gamma_total_moves else 0.0
    
    return MonteCarloResult(
        p95=round(p95, 1),
        mean=round(mean, 1),
        self_sustaining=round(self_sustaining, 1),
        prob_hit_wall=round(prob_hit_wall, 1),
        catalyst_contrib=round(cat_contrib, 1),
        gamma_contrib=round(gam_contrib, 1)
    )

# --------------------- TRADE SUGGESTION ENGINE -------------- #
def suggest_trade(price: float, wall_strike: float, gamma_amplification: float,
                  prob_hit_wall: float, catalyst_move: float, signal_type: str) -> str:
    if signal_type == "STRUCTURAL":
        return "Skip — no catalyst, low gamma"
    
    wall_above = wall_strike > price
    distance_to_wall = abs(wall_strike - price) / price * 100
    near_wall = round(wall_strike, 1)
    otm_strike = round(wall_strike * (1.02 if wall_above else 0.98), 1)
    
    if gamma_amplification > 100:
        if wall_above:
            return f"Buy {near_wall}c / sell {otm_strike}c debit spread (aggressive gamma)"
        else:
            return f"Buy {near_wall}p / sell {otm_strike}p debit spread"
    elif gamma_amplification > 30 and catalyst_move > 10:
        if wall_above:
            return f"Buy {near_wall}c (earnings drift toward wall)"
        else:
            return f"Buy {near_wall}p (earnings drift toward wall)"
    elif prob_hit_wall > 80 and distance_to_wall < 10:
        if wall_above:
            return f"Sell {otm_strike}c (low probability above wall)"
        else:
            return f"Sell {otm_strike}p (low probability below wall)"
    else:
        return "No clear setup – monitor"

# --------------------- SCORING & CLASSIFICATION ------------ #
def compute_economic_score(ticker_data: TickerData, conc: ConcentrationResult,
                           mc: MonteCarloResult) -> float:
    score = 0.0
    if ticker_data.catalyst_move_pct > 0 and ticker_data.days_to_catalyst is not None:
        days_factor = max(0, 1 - ticker_data.days_to_catalyst / 365)
        score += min(ticker_data.catalyst_move_pct / 50, 0.3) * days_factor
    
    score += min(mc.gamma_contrib / 200, 0.3)
    score += min(conc.loop_gain * 0.5, 0.2)
    score += min(mc.p95 / 200, 0.2)
    
    if abs(ticker_data.price - conc.peak_strike) / ticker_data.price < 0.1:
        score += 0.1
    
    return round(score, 4)

def classify_signal(score: float, conc: ConcentrationResult, mc: MonteCarloResult) -> str:
    if (conc.loop_gain >= EXTREME_LOOP_GAIN or mc.p95 > EXTREME_MC_P95 or
        mc.self_sustaining > EXTREME_SELF_SUSTAIN) and score >= EXTREME_ECON_SCORE:
        return "EXTREME"
    elif score >= HIGH_CONVICTION_SCORE:
        return "HIGH CONVICTION"
    elif score >= WATCH_SCORE:
        return "WATCH"
    else:
        return "STRUCTURAL"

# --------------------- TICKER PROCESSING ------------------- #
def process_ticker(ticker: str) -> Optional[Signal]:
    try:
        logger.debug(f"Processing {ticker}")
        session = _get_session(ticker)
        tk = yf.Ticker(ticker, session=session)
        
        info = yf_request_with_retry(tk.info)
        if not info or 'currentPrice' not in info or not info['currentPrice']:
            return None
        
        price = info['currentPrice']
        iv = info.get('impliedVolatility', 0.5)
        if not iv or iv <= 0:
            iv = 0.5
        
        # Catalyst detection
        earnings_next = info.get('earningsDate', None)
        days_to_earnings = 999
        catalyst_move_pct = 0.0
        if earnings_next:
            if isinstance(earnings_next, list):
                earnings_next = earnings_next[0] if earnings_next else None
            if isinstance(earnings_next, (int, float)):
                earnings_dt = datetime.fromtimestamp(earnings_next)
            elif isinstance(earnings_next, datetime):
                earnings_dt = earnings_next
            else:
                earnings_dt = None
            if earnings_dt:
                delta = (earnings_dt - datetime.now()).days
                days_to_earnings = delta if delta is not None else 999
                catalyst_move_pct = info.get('earningsAverageMove', 0.0) or 7.0
        
        # Allow day 0 (earnings today) – fix #5
        if days_to_earnings is not None and days_to_earnings < 365 and days_to_earnings >= 0:
            catalyst_days = days_to_earnings
        else:
            catalyst_days = None if days_to_earnings is None else 999
        
        # FIX: missing closing parenthesis was here – now closed correctly
        ticker_data = TickerData(
            ticker=ticker,
            price=price,
            iv=iv,
            dte=0,
            catalyst_move_pct=catalyst_move_pct,
            days_to_catalyst=catalyst_days
        )   # <-- parenthesis closed
        
        options = get_options_chain(ticker, price, iv)
        if not options or len(options) < 2:
            logger.debug(f"{ticker}: insufficient options data")
            return None
        
        conc = analyze_concentration(options, price)
        wall_strike = conc.peak_strike
        
        mc = monte_carlo_cascade(price, wall_strike, conc, catalyst_move_pct, catalyst_days)
        score = compute_economic_score(ticker_data, conc, mc)
        sig_type = classify_signal(score, conc, mc)
        
        gamma_amplification = mc.gamma_contrib if mc.gamma_contrib > 0 else 0.0
        trade_sug = suggest_trade(price, wall_strike, gamma_amplification,
                                  mc.prob_hit_wall, catalyst_move_pct, sig_type)
        
        signal = Signal(
            ticker=ticker,
            price=price,
            score=score,
            wall_strike=wall_strike,
            catalyst_move=catalyst_move_pct,
            gamma_amplification=gamma_amplification,
            mc_total=mc.p95,
            prob_hit_wall=mc.prob_hit_wall,
            days_to_catalyst=catalyst_days if catalyst_days else 999,
            trade_suggestion=trade_sug,
            signal_type=sig_type,
            catalyst_contrib=mc.catalyst_contrib,
            gamma_contrib=mc.gamma_contrib
        )
        
        logger.debug(f"{ticker}: score={score:.4f}, wall={wall_strike}, gamma={gamma_amplification:.1f}%")
        return signal
    
    except Exception as e:
        logger.error(f"Error processing {ticker}: {e}")
        return None

# --------------------- UNIVERSE BUILDING ------------------- #
def build_universe(quick: bool = False, custom_tickers: Optional[List[str]] = None) -> List[str]:
    """Build ticker universe: custom + Finviz + SP500 fallback.
    If quick=True, only use the CUSTOM_TICKERS env list.
    """
    if quick:
        tickers = set(t.strip().upper() for t in DEFAULT_TICKERS if t.strip())
        logger.info(f"Quick mode: using {len(tickers)} custom tickers")
        return sorted(tickers)
    
    if custom_tickers:
        return sorted(set(t.strip().upper() for t in custom_tickers if t.strip()))
    
    tickers = set()
    for t in DEFAULT_TICKERS:
        tickers.add(t.strip().upper())
    
    try:
        resp = requests.get(FINVIZ_URL, timeout=10)
        if resp.status_code == 200:
            df = pd.read_csv(StringIO(resp.text))
            finviz_tickers = df['Ticker'].dropna().unique().tolist()
            for t in finviz_tickers:
                tickers.add(t.upper())
            logger.info(f"Finviz returned {len(finviz_tickers)} tickers")
        else:
            logger.warning(f"Finviz HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Finviz fetch failed: {e}")
    
    if not tickers:
        try:
            df = pd.read_csv(SP500_CSV)
            for t in df['Symbol']:
                tickers.add(t.upper())
        except:
            logger.error("No tickers from any source")
            sys.exit(1)
    
    return sorted(tickers)

# --------------------- OUTPUT FORMATTING ------------------- #
def format_signal(sig: Signal) -> str:
    cat_str = f"{sig.catalyst_contrib:.1f}%" if sig.catalyst_contrib > 0 else "0.0%"
    gamma_str = f"{sig.gamma_contrib:.1f}%" if sig.gamma_contrib > 0 else "0.0%"
    mc_str = f"{sig.mc_total:.1f}%" if sig.mc_total > 0 else "0.0%"
    prob_str = f"{sig.prob_hit_wall:.0f}%"
    
    if sig.days_to_catalyst != 999 and sig.days_to_catalyst is not None:
        cat_icon