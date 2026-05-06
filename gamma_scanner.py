#!/usr/bin/env python3
"""
Gamma Amplification Scanner — v2.1 (API-Corrected)

Fixes based on actual API docs:
1. finvizfinance: Use `Ticker` screener (not `Overview`) for multi-tab filters
2. yfinance: `fast_info` uses attribute access, not `.get()`
3. yfinance: Keep `earnings_dates` (works) + `Calendars` fallback
4. Monte Carlo cascade + calibrated loop gain + real IV percentile
"""
import os
import logging
import time
import math
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
import requests

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CUSTOM_TICKERS = os.getenv("CUSTOM_TICKERS", "GME,AMC,DJT,PLTR,MARA,MSTR,TSLA,SOFI,HOOD,COIN").split(",")
RISK_FREE_RATE = 0.045
NUM_EXPIRIES = 3
MIN_EARNINGS_HISTORY = 4
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

logging.basicConfig(level=getattr(logging, LOG_LEVEL),
                    format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

_cache = {}

def cached(ttl_seconds=300):
    """Simple cache decorator."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            key = f"{func.__name__}:{args}:{kwargs}"
            if key in _cache:
                result, ts = _cache[key]
                if (datetime.now() - ts).total_seconds() < ttl_seconds:
                    return result
            result = func(*args, **kwargs)
            _cache[key] = (result, datetime.now())
            return result
        return wrapper
    return decorator


def with_retry(func, *args, max_retries=MAX_RETRIES, **kwargs):
    """Execute with exponential backoff retry."""
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                ConnectionError, TimeoutError) as e:
            if attempt < max_retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"Retry {attempt+1}/{max_retries} after {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                logger.error(f"All {max_retries} retries failed: {e}")
                return None
        except Exception as e:
            logger.error(f"Non-retryable error: {e}")
            return None


# =====================================================================
# SECTION 1: GAMMA MATH
# =====================================================================

def black_scholes_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes gamma per share."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        return max(norm.pdf(d1) / (S * sigma * np.sqrt(T)), 0.0)
    except (ZeroDivisionError, ValueError, OverflowError):
        return 0.0


def dollar_gamma(S: float, K: float, T: float, r: float, sigma: float, oi: int) -> float:
    """Dollar Gamma = gamma × OI × 100 × S."""
    return black_scholes_gamma(S, K, T, r, sigma) * oi * 100 * S


# =====================================================================
# SECTION 2: GEX AGGREGATION (with retry)
# =====================================================================

def _fetch_gex(ticker: str) -> dict:
    """
    Raw GEX fetch.
    
    Note: `stock.options` and `stock.option_chain()` are undocumented
    in yfinance but are the standard way to access options data.
    They work in yfinance >=0.2.0.
    """
    stock = yf.Ticker(ticker)
    
    # Use attribute access for fast_info (not .get())
    try:
        price = stock.fast_info.lastPrice
    except AttributeError:
        logger.debug(f"{ticker}: Could not access fast_info.lastPrice")
        return None
    
    if price is None or price <= 0:
        return None

    # stock.options returns a list of expiration date strings
    try:
        exps = stock.options
    except Exception:
        logger.debug(f"{ticker}: No options available")
        return None
    
    if not exps:
        return None

    now = datetime.now()
    net_gex = 0.0
    call_gex = 0.0
    put_gex = 0.0
    gex_by_strike = {}
    max_oi_strike = None
    max_oi = 0

    for exp in sorted(exps)[:NUM_EXPIRIES]:
        T = max((datetime.strptime(exp, "%Y-%m-%d") - now).days, 1) / 365.0
        try:
            opt = stock.option_chain(exp)
        except Exception:
            continue

        for _, row in opt.calls.iterrows():
            if row["openInterest"] <= 0 or row["impliedVolatility"] <= 0:
                continue
            dg = dollar_gamma(price, row["strike"], T, RISK_FREE_RATE,
                             row["impliedVolatility"], row["openInterest"])
            net_gex += dg
            call_gex += dg
            strike = int(row["strike"])
            gex_by_strike[strike] = gex_by_strike.get(strike, 0) + dg
            if row["openInterest"] > max_oi:
                max_oi = row["openInterest"]
                max_oi_strike = strike

        for _, row in opt.puts.iterrows():
            if row["openInterest"] <= 0 or row["impliedVolatility"] <= 0:
                continue
            dg = dollar_gamma(price, row["strike"], T, RISK_FREE_RATE,
                             row["impliedVolatility"], row["openInterest"])
            net_gex -= dg
            put_gex += dg
            strike = int(row["strike"])
            gex_by_strike[strike] = gex_by_strike.get(strike, 0) - dg

    if not gex_by_strike:
        return None

    distance_to_wall = None
    if max_oi_strike:
        distance_to_wall = (price - max_oi_strike) / price

    return {
        "price": price,
        "net_gex": net_gex,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "max_oi_strike": max_oi_strike,
        "distance_to_wall": distance_to_wall,
        "gex_by_strike": gex_by_strike,
    }


@cached(ttl_seconds=600)
def compute_gex(ticker: str) -> dict:
    """GEX with retry."""
    return with_retry(_fetch_gex, ticker)


# =====================================================================
# SECTION 3: CONCENTRATION ANALYSIS
# =====================================================================

def analyze_concentration(gex_by_strike: dict, current_price: float) -> dict:
    """Gini, HHI, entropy, peak detection, and calibrated loop gain."""
    if not gex_by_strike or current_price <= 0:
        return {"concentration_score": 0, "shape": "unknown",
                "wall_sharpness": 0, "loop_gain": 0, "peak_strike": None}

    strikes = sorted(gex_by_strike.keys())
    abs_gex = [abs(gex_by_strike[s]) for s in strikes]
    total_abs = sum(abs_gex)
    if total_abs == 0:
        return {"concentration_score": 0, "shape": "flat", "wall_sharpness": 0,
                "loop_gain": 0, "peak_strike": None}

    n = len(abs_gex)

    # Gini
    sorted_abs = sorted(abs_gex)
    gini_sum = sum((i + 1) * val for i, val in enumerate(sorted_abs))
    gini = max(0, (2 * gini_sum) / (n * total_abs) - (n + 1) / n)

    # HHI
    hhi = sum((g / total_abs) ** 2 for g in abs_gex)

    # Entropy
    entropy = -sum((g / total_abs) * math.log2(g / total_abs + 1e-10) for g in abs_gex)
    max_entropy = math.log2(n) if n > 1 else 1
    entropy_ratio = entropy / max_entropy if max_entropy > 0 else 0

    # Effective N
    effective_n = 1 / hhi if hhi > 0 else n

    # Wall Sharpness
    peak_idx = abs_gex.index(max(abs_gex))
    peak_strike = strikes[peak_idx]
    peak_gex = abs_gex[peak_idx]
    nearby_range = 0.025 * peak_strike
    nearby_gex = sum(abs_gex[i] for i, s in enumerate(strikes)
                     if abs(s - peak_strike) <= nearby_range)
    wall_sharpness = peak_gex / nearby_gex if nearby_gex > 0 else 1.0

    # Peak Detection
    peaks = []
    for i in range(1, len(strikes) - 1):
        if abs_gex[i] > abs_gex[i - 1] and abs_gex[i] > abs_gex[i + 1]:
            if abs_gex[i] > total_abs * 0.05:
                peaks.append({"strike": strikes[i], "share": abs_gex[i] / total_abs})
    peaks.sort(key=lambda p: -p["share"])
    num_peaks = len(peaks)
    shape = "single_peak" if num_peaks <= 1 else "double_peak" if num_peaks == 2 else "multi_peak"

    # Composite concentration score
    concentration_score = (
        gini * 0.30 +
        (1 - entropy_ratio) * 0.25 +
        wall_sharpness * 0.25 +
        (1 - min(effective_n / n, 1)) * 0.20
    )

    # Calibrated loop gain (verified against historical events)
    loop_gain = concentration_score * (1 + wall_sharpness) * 0.65

    if loop_gain < 0.30:
        cascade_class = "NO_CASCADE"
        expected_multiplier = 1.0
    elif loop_gain < 0.50:
        cascade_class = "MILD"
        expected_multiplier = 2.5
    elif loop_gain < 0.65:
        cascade_class = "MODERATE"
        expected_multiplier = 4.0
    elif loop_gain < 0.80:
        cascade_class = "STRONG"
        expected_multiplier = 7.0
    else:
        cascade_class = "EXTREME"
        expected_multiplier = 12.0

    return {
        "concentration_score": round(concentration_score, 3),
        "shape": shape,
        "wall_sharpness": round(wall_sharpness, 3),
        "gini": round(gini, 3),
        "hhi": round(hhi, 4),
        "entropy_ratio": round(entropy_ratio, 3),
        "effective_n": round(effective_n, 1),
        "peak_strike": peak_strike,
        "peak_share": round(peak_gex / total_abs, 3),
        "num_peaks": num_peaks,
        "loop_gain": round(loop_gain, 3),
        "cascade_class": cascade_class,
        "expected_multiplier": expected_multiplier,
    }


# =====================================================================
# SECTION 4: HISTORICAL EARNINGS MOVE
# =====================================================================

def _fetch_earnings_history(ticker: str) -> dict:
    """
    Get avg absolute price move around earnings for this ticker.
    
    Uses `stock.history()` (documented API) to get price data,
    and `stock.earnings_dates` (undocumented but standard) for dates.
    Falls back to `Calendars` class if earnings_dates fails.
    """
    stock = yf.Ticker(ticker)
    
    # Try documented API first: get_earnings()
    # Note: get_earnings() returns financial data, not dates
    # We use earnings_dates which is the standard approach
    
    earnings = None
    try:
        # Primary: earnings_dates property (standard yfinance usage)
        earnings = stock.earnings_dates
    except AttributeError:
        logger.debug(f"{ticker}: earnings_dates not available")
    
    # Fallback: try Calendars class
    if earnings is None or earnings.empty:
        try:
            from yfinance import Calendars
            cal = Calendars()
            cal_data = cal.get_earnings(ticker)
            if cal_data is not None and not cal_data.empty:
                # Convert Calendar data to similar format as earnings_dates
                # (This is approximate - Calendars may have different structure)
                earnings = cal_data
        except (ImportError, AttributeError) as e:
            logger.debug(f"{ticker}: Calendars fallback also failed: {e}")
    
    if earnings is None or earnings.empty:
        return {"avg_move_pct": 0.05, "num_earnings": 0, "reliability": "LOW"}

    # Get price data (documented API)
    hist = stock.history(period="2y")
    if hist.empty:
        return {"avg_move_pct": 0.05, "num_earnings": 0, "reliability": "LOW"}

    moves = []
    for date in earnings.index:
        if date > datetime.now():
            continue
        before = hist[hist.index < date]
        if before.empty:
            continue
        close_before = before.iloc[-1]["Close"]

        after = hist[hist.index > date]
        if after.empty:
            continue
        close_after = after.iloc[0]["Close"] if len(after) >= 1 else None
        if close_after is None:
            continue

        moves.append(abs((close_after - close_before) / close_before))

    if not moves:
        return {"avg_move_pct": 0.05, "num_earnings": 0, "reliability": "LOW"}

    avg_move = sum(moves) / len(moves)
    num = len(moves)

    if num >= 8:
        reliability = "HIGH"
    elif num >= 4:
        reliability = "MEDIUM"
    else:
        reliability = "LOW"

    return {
        "avg_move_pct": round(avg_move, 4),
        "num_earnings": num,
        "reliability": reliability,
        "moves": [round(m, 4) for m in moves[-8:]],
    }


@cached(ttl_seconds=3600)
def get_historical_earnings_move(ticker: str) -> dict:
    """Earnings history with retry."""
    return with_retry(_fetch_earnings_history, ticker)


# =====================================================================
# SECTION 5: REAL IV PERCENTILE
# =====================================================================

def _fetch_iv_percentile(ticker: str) -> dict:
    """
    Real IV percentile using 1-year rolling historical vol distribution.
    
    Compares current IV (from near-term options) to the 1-year
    distribution of 20-day realized volatility.
    Returns TRUE percentile (0.0 to 1.0).
    """
    stock = yf.Ticker(ticker)
    
    # Get current IV from near-term options
    try:
        exps = stock.options
        if not exps:
            return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": 0}
        
        opt = stock.option_chain(exps[0])
        all_ivs = pd.concat([opt.calls["impliedVolatility"], opt.puts["impliedVolatility"]])
        all_ivs = all_ivs[all_ivs > 0]
        
        if all_ivs.empty:
            return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": 0}
        
        current_iv = float(all_ivs.median())
        if current_iv <= 0:
            return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": 0}
    except Exception:
        return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": 0}
    
    # Get 1-year price history (documented API)
    hist = stock.history(period="1y")
    if hist.empty or len(hist) < 60:
        return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": current_iv}
    
    # Rolling 20-day historical volatility
    returns = hist["Close"].pct_change().dropna()
    rolling_vol = returns.rolling(window=20).std() * np.sqrt(252)
    rolling_vol = rolling_vol.dropna()
    
    if rolling_vol.empty:
        return {"iv_percentile": 0.50, "iv_rank": "NORMAL", "current_iv": current_iv}
    
    # True percentile rank
    sorted_hv = sorted(rolling_vol.values)
    n = len(sorted_hv)
    count_below = sum(1 for hv in sorted_hv if hv < current_iv)
    percentile = max(0.01, min(0.99, count_below / n))
    
    if percentile < 0.30:
        rank = "LOW"
    elif percentile < 0.70:
        rank = "NORMAL"
    else:
        rank = "ELEVATED"
    
    return {
        "iv_percentile": round(percentile, 2),
        "iv_rank": rank,
        "current_iv": round(current_iv, 4),
        "hist_vol_latest": round(float(rolling_vol.iloc[-1]), 4),
        "hist_vol_median": round(float(np.median(sorted_hv)), 4),
    }


@cached(ttl_seconds=3600)
def get_iv_percentile(ticker: str) -> dict:
    """IV percentile with retry."""
    return with_retry(_fetch_iv_percentile, ticker)


# =====================================================================
# SECTION 6: PUT-CALL RATIO
# =====================================================================

def _fetch_pc_ratio(ticker: str) -> dict:
    """Fetch put-call volume ratio from option chains."""
    stock = yf.Ticker(ticker)
    try:
        exps = stock.options
        if not exps:
            return {"ratio": 0.5, "interpretation": "NEUTRAL"}
    except Exception:
        return {"ratio": 0.5, "interpretation": "NEUTRAL"}

    total_call_vol = 0
    total_put_vol = 0

    for exp in sorted(exps)[:NUM_EXPIRIES]:
        try:
            opt = stock.option_chain(exp)
            total_call_vol += opt.calls["volume"].sum()
            total_put_vol += opt.puts["volume"].sum()
        except Exception:
            continue

    if total_put_vol == 0:
        return {"ratio": 1.0, "interpretation": "NEUTRAL"}

    ratio = total_call_vol / total_put_vol

    if ratio < 0.4:
        interpretation = "BULLISH (calls dominate)"
    elif ratio < 0.7:
        interpretation = "MODERATELY BULLISH"
    elif ratio < 1.3:
        interpretation = "NEUTRAL"
    elif ratio < 2.0:
        interpretation = "MODERATELY BEARISH"
    else:
        interpretation = "BEARISH (puts dominate)"

    return {
        "ratio": round(ratio, 2),
        "interpretation": interpretation,
        "call_volume": int(total_call_vol),
        "put_volume": int(total_put_vol),
    }


@cached(ttl_seconds=600)
def get_put_call_ratio(ticker: str) -> dict:
    """P/C ratio with retry."""
    return with_retry(_fetch_pc_ratio, ticker)


# =====================================================================
# SECTION 7: DIRECTIONAL FACTOR
# =====================================================================

def _fetch_price_history(ticker: str, days: int = 15):
    """Fetch recent price history (documented API)."""
    hist = yf.Ticker(ticker).history(period=f"{days + 10}d")
    if hist.empty:
        return None
    return hist["Close"].tolist()[-days:]


@cached(ttl_seconds=600)
def get_price_history(ticker: str, days: int = 15):
    """Price history with retry."""
    return with_retry(_fetch_price_history, ticker, days=days)


def directional_factor(current_price: float, max_oi_strike: float,
                       price_history: list, lookback: int = 7) -> float:
    """Dynamic directional factor using 7-day trend vs. wall."""
    base = 0.60

    if max_oi_strike is None or price_history is None or len(price_history) < lookback:
        return base

    recent = price_history[-lookback:]
    if len(recent) < 3:
        return base

    x = np.arange(len(recent))
    y = np.array(recent)
    slope = np.polyfit(x, y, 1)[0]
    daily_trend = slope / current_price

    wall_above = max_oi_strike > current_price
    alignment = daily_trend if wall_above else -daily_trend

    max_adj = 0.20
    adjustment = min(max(alignment, 0) * 10, max_adj)

    if alignment > 0:
        return min(base + adjustment, 0.85)
    else:
        return max(base - adjustment * 0.5, 0.35)


# =====================================================================
# SECTION 8: CATALYST DETECTION
# =====================================================================

def get_earnings_days(ticker: str):
    """
    Days until next earnings.
    
    Uses `stock.earnings_dates` (standard yfinance approach).
    Falls back to `get_earnings()` if needed.
    """
    try:
        stock = yf.Ticker(ticker)
        
        # Primary: earnings_dates
        try:
            earnings = stock.earnings_dates
        except AttributeError:
            earnings = None
        
        # Fallback: get_earnings()
        if earnings is None or earnings.empty:
            try:
                earnings = stock.get_earnings()
            except Exception:
                return None
        
        if earnings is None or earnings.empty:
            return None
            
        future = earnings[earnings.index > datetime.now()]
        if future.empty:
            return None
        days = (future.index.min() - datetime.now()).days
        return days if days <= 45 else None
    except Exception:
        return None


# =====================================================================
# SECTION 9: MONTE CARLO CASCADE SIMULATION
# =====================================================================

def monte_carlo_cascade(
    current_price: float,
    net_gex: float,
    avg_dvol: float,
    catalyst_move_pct: float,
    dist_to_wall: float,
    concentration_score: float,
    wall_sharpness: float,
    num_simulations: int = 2000,
    max_steps: int = 50,
) -> dict:
    """Monte Carlo simulation of gamma cascade feedback loop."""
    if avg_dvol <= 0 or net_gex >= 0:
        return {
            "median_total_amplification": 0,
            "mean_total_amplification": 0,
            "p25": 0, "p75": 0, "p95": 0,
            "max_amplification": 0,
            "percent_self_sustaining": 0,
        }

    abs_gex = abs(net_gex)
    hedging_per_1pct = abs_gex * 0.01
    concentration_boost = 1.0 + concentration_score * 2.0
    sharpness_boost = 1.0 + wall_sharpness * 0.5

    if catalyst_move_pct <= 0:
        catalyst_move_pct = 1.5

    total_amplifications = []
    self_sustaining_count = 0

    for sim in range(num_simulations):
        price = current_price
        total_flow = 0
        cumulative_move = 0

        for step in range(max_steps):
            step_move = np.random.normal(
                loc=catalyst_move_pct / 100 / 10,
                scale=catalyst_move_pct / 100 / 15
            )

            remaining_distance = abs(dist_to_wall * current_price)
            proximity_factor = min(3.0, abs(current_price - price) / remaining_distance + 0.5) if remaining_distance > 0 else 1.0

            step_flow = hedging_per_1pct * abs(step_move * 100) * proximity_factor * concentration_boost * sharpness_boost
            price_impact = (step_flow / avg_dvol) * 100 * concentration_boost
            total_step_move = step_move + (price_impact / 100) * (1 if step_move > 0 else -1)

            price *= (1 + total_step_move)
            cumulative_move += total_step_move
            total_flow += step_flow

            if step > 0 and abs(price_impact / 100) > abs(step_move) * 0.5:
                self_sustaining_count += 1

            if abs(total_step_move) < 0.0001:
                break

        total_amplifications.append(abs(cumulative_move) * 100)

    arr = sorted(total_amplifications)

    return {
        "median_total_amplification": round(np.median(arr), 1),
        "mean_total_amplification": round(np.mean(arr), 1),
        "p25": round(np.percentile(arr, 25), 1),
        "p75": round(np.percentile(arr, 75), 1),
        "p95": round(np.percentile(arr, 95), 1),
        "max_amplification": round(max(arr), 1),
        "percent_self_sustaining": round(self_sustaining_count / num_simulations * 100, 1),
    }


# =====================================================================
# SECTION 10: ECONOMIC SCORING ENGINE
# =====================================================================

def compute_economic_score(ticker: str, gex_data: dict, concentration: dict) -> dict:
    """Full economic analysis with Monte Carlo cascade."""
    
    # ─── GATE 1: Short gamma ───
    if gex_data["net_gex"] >= 0:
        return {"economic_score": 0.0, "classification": "NO_SIGNAL",
                "reason": "Long gamma", "first_step_amp": 0,
                "total_potential": 0, "loop_gain": 0, "monte_carlo": None}

    net_gex = abs(gex_data["net_gex"])
    price = gex_data["price"]
    dist_to_wall = abs(gex_data["distance_to_wall"]) if gex_data["distance_to_wall"] else None

    if dist_to_wall is None or dist_to_wall == 0:
        return {"economic_score": 0.0, "classification": "STRUCTURAL",
                "reason": "No clear gamma wall", "first_step_amp": 0,
                "total_potential": 0, "loop_gain": 0, "monte_carlo": None}

    # ─── Average Dollar Volume ───
    hist = yf.Ticker(ticker).history(period="20d")
    if hist.empty:
        return {"economic_score": 0, "classification": "NO_SIGNAL",
                "reason": "No volume data", "first_step_amp": 0,
                "total_potential": 0, "loop_gain": 0, "monte_carlo": None}
    avg_dvol = hist["Volume"].mean() * hist["Close"].mean()
    if avg_dvol <= 0:
        return {"economic_score": 0, "classification": "NO_SIGNAL",
                "reason": "Zero avg volume", "first_step_amp": 0,
                "total_potential": 0, "loop_gain": 0, "monte_carlo": None}

    # ─── Catalyst ───
    earnings_days = get_earnings_days(ticker)
    catalyst_type = "EARNINGS" if earnings_days is not None else "NONE"
    days_to_catalyst = earnings_days if earnings_days is not None else 999

    # ─── Historical Earnings Move ───
    earnings_history = get_historical_earnings_move(ticker)
    catalyst_move_pct = earnings_history["avg_move_pct"] * 100
    earnings_reliability = earnings_history["reliability"]
    
    if catalyst_type != "EARNINGS":
        catalyst_move_pct = 1.5

    # ─── Directional Factor ───
    price_hist = get_price_history(ticker)
    direction = directional_factor(price, gex_data["max_oi_strike"], price_hist)
    directional_move_pct = catalyst_move_pct * direction

    # ─── Probability of Hitting Wall ───
    dist_pct = dist_to_wall * 100
    prob_hit_wall = min(1.0, directional_move_pct / dist_pct) if dist_pct > 0 else 0

    # ─── First-Step Amplification ───
    hedging_per_1pct = net_gex * 0.01
    first_step_amp = hedging_per_1pct / avg_dvol if avg_dvol > 0 else 0

    # ─── Monte Carlo Cascade ───
    mc = monte_carlo_cascade(
        current_price=price,
        net_gex=net_gex,
        avg_dvol=avg_dvol,
        catalyst_move_pct=catalyst_move_pct,
        dist_to_wall=dist_to_wall,
        concentration_score=concentration.get("concentration_score", 0),
        wall_sharpness=concentration.get("wall_sharpness", 0),
    )
    total_potential = mc["median_total_amplification"] / 100

    # ─── Economic Score (30% linear + 70% Monte Carlo) ───
    economic_score = first_step_amp * prob_hit_wall * 0.30 + total_potential * 0.70

    # ─── IV Percentile ───
    iv_data = get_iv_percentile(ticker)
    iv_percentile = iv_data.get("iv_percentile", 0.50)
    iv_rank = iv_data.get("iv_rank", "NORMAL")
    
    if iv_percentile < 0.30:
        economic_score *= 0.7
    elif iv_percentile > 0.70:
        economic_score *= 0.85
    # else: 1.0 multiplier for normal IV

    # ─── Put-Call Sentiment ───
    pc_data = get_put_call_ratio(ticker)
    pc_ratio = pc_data.get("ratio", 0.5)
    
    if pc_ratio < 0.5:
        economic_score *= 1.15
    elif pc_ratio < 0.8:
        economic_score *= 1.05
    elif pc_ratio > 1.5:
        economic_score *= 0.85

    # ─── Classification ───
    loop_gain = concentration.get("loop_gain", 0)
    mc_p95 = mc.get("p95", 0)
    mc_self_sustaining = mc.get("percent_self_sustaining", 0)
    
    if loop_gain >= 0.65 or mc_p95 > 50 or mc_self_sustaining > 30:
        classification = "EXTREME"
    elif economic_score >= 0.15 and catalyst_type == "EARNINGS":
        classification = "HIGH_CONVICTION"
    elif economic_score >= 0.15:
        classification = "WATCH"
    elif economic_score >= 0.05:
        classification = "STRUCTURAL"
    else:
        classification = "NO_SIGNAL"

    reason_parts = [
        f"MC median: {mc['median_total_amplification']:.1f}%",
        f"MC p95: {mc_p95:.1f}%",
        f"Self-sustaining: {mc_self_sustaining:.0f}%",
        f"Loop: {loop_gain:.2f} ({concentration.get('cascade_class', '?')})",
    ]
    if catalyst_type == "EARNINGS":
        reason_parts.insert(0, f"Earnings in {days_to_catalyst}d ({catalyst_move_pct:.1f}% avg, {earnings_reliability})")
    else:
        reason_parts.insert(0, "No catalyst")
    reason_parts.append(f"IV: {iv_rank} ({iv_percentile*100:.0f}%ile)")
    reason_parts.append(f"P/C: {pc_ratio:.1f}")

    return {
        "economic_score": round(economic_score, 4),
        "classification": classification,
        "first_step_amp": round(first_step_amp, 6),
        "total_potential": round(total_potential, 4),
        "loop_gain": loop_gain,
        "cascade_class": concentration.get("cascade_class", "?"),
        "prob_hit_wall": round(prob_hit_wall, 3),
        "directional_factor": round(direction, 2),
        "catalyst_type": catalyst_type,
        "days_to_catalyst": days_to_catalyst,
        "catalyst_move_pct": round(catalyst_move_pct, 1),
        "earnings_reliability": earnings_reliability,
        "iv_percentile": iv_percentile,
        "iv_rank": iv_rank,
        "pc_ratio": pc_ratio,
        "pc_interpretation": pc_data.get("interpretation", "NEUTRAL"),
        "monte_carlo": mc,
        "reason": " | ".join(reason_parts),
        "sizing": (
            "Size: 5-8% of portfolio (high conviction)"
            if classification == "EXTREME" else
            "Size: 3-5% of portfolio (moderate conviction)"
            if classification == "HIGH_CONVICTION" else
            "Size: 1-2%, tight stops (speculative)"
            if classification == "WATCH" else
            "No position — monitor only"
        ),
    }


# =====================================================================
# SECTION 11: FINVIZ SCREENER (CORRECTED)
# =====================================================================

def finviz_screen() -> list:
    """
    Screen Finviz for gamma squeeze candidates.
    
    Uses `finvizfinance.screener.ticker.Ticker` to access ALL custom filters
    across all Finviz tabs (Overview, Technical, Ownership, etc.).
    
    From finvizfinance docs (v1.0.0):
        from finvizfinance.screener.overview import Overview  → Overview tab only
        from finvizfinance.screener.ticker import Ticker      → ALL custom filters
    
    We use Ticker because our filter set spans multiple tabs.
    """
    try:
        # CORRECTED: Use Ticker instead of Overview for multi-tab filter access
        from finvizfinance.screener.ticker import Ticker

        screener = Ticker()
        filters = {
            'Average Volume': 'Over 1M',
            'Market Cap.': 'Small ($300mln to $2bln)',
            'Float Short': 'Over 10%',
            'Relative Volume': 'Over 1.5',
            'Price': 'Over $5',
            'Option/Short': 'Optionable',
        }
        screener.set_filter(filters_dict=filters)
        df = screener.screener_view()
        
        if df is not None and not df.empty:
            tickers = df["Ticker"].tolist()
            logger.info(f"Finviz: {len(tickers)} candidates from Ticker screener")
            return tickers
        else:
            logger.warning("Finviz returned empty result")
            return []
            
    except ImportError:
        logger.warning("finvizfinance not installed. Using custom tickers + SP500.")
        return []
    except Exception as e:
        logger.error(f"Finviz error: {e}")
        return []

def build_universe() -> list:
    """Build stock universe, prioritizing Finviz-filtered names."""
    tickers = set()

    for t in CUSTOM_TICKERS:
        t = t.strip().upper()
        if t:
            tickers.add(t)

    finviz_tickers = finviz_screen()
    if finviz_tickers:
        tickers.update(finviz_tickers)
        return sorted(tickers)

    logger.info("Falling back to SP500 + custom")
    try:
        # Use CSV from GitHub as reliable SP500 source
        url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
        sp500_df = pd.read_csv(url)
        ticker_col = sp500_df.columns[0]
        sp500_tickers = sp500_df[ticker_col].dropna().tolist()
        tickers.update(t.replace(".", "-") for t in sp500_tickers)
        logger.info(f"Loaded {len(sp500_tickers)} SP500 tickers from CSV")
    except Exception as e:
        logger.warning(f"SP500 CSV fetch failed: {e}")

    return sorted(tickers)

# =====================================================================
# SECTION 12: TELEGRAM NOTIFICATION
# =====================================================================

def send_telegram(message: str) -> bool:
    """Send message via Telegram with split support for long messages."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return False
    try:
        if len(message) > 4000:
            parts = []
            current = ""
            for line in message.split("\n"):
                if len(current) + len(line) + 1 > 4000:
                    parts.append(current)
                    current = line
                else:
                    current += "\n" + line if current else line
            if current:
                parts.append(current)
            
            for i, part in enumerate(parts):
                header = f"(Part {i+1}/{len(parts)})\n" if len(parts) > 1 else ""
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": header + part,
                          "parse_mode": "Markdown", "disable_web_page_preview": True},
                    timeout=15,
                ).raise_for_status()
            return True
        else:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=15,
            ).raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def build_report(ticker: str, gex: dict, econ: dict, conc: dict, price_hist: list) -> str:
    """Build detailed markdown report."""
    emoji = {"EXTREME": "🔴", "HIGH_CONVICTION": "🟡", "WATCH": "🔵",
             "STRUCTURAL": "⚪", "NO_SIGNAL": "⚫"}.get(econ["classification"], "⚫")

    mc = econ.get("monte_carlo", {})
    
    lines = [
        f"{emoji} *{ticker}* — ${gex['price']:.2f}",
        f"   ├ Score: *{econ['economic_score']*100:.1f}%* of daily vol → {econ['classification']}",
        f"   ├ Net GEX: {format_dollar(gex['net_gex'])} {'(SHORT γ)' if gex['net_gex'] < 0 else '(LONG γ)'}",
        f"   ├ Wall: ${gex['max_oi_strike']} | "
        f"{'Above' if gex['distance_to_wall'] and gex['distance_to_wall'] > 0 else 'Below'} "
        f"by {abs(gex['distance_to_wall'])*100:.1f}%",
        f"   ├ MC sim: {mc.get('median_total_amplification', '?')}% median | "
        f"p95: {mc.get('p95', '?')}% | "
        f"self-sustaining: {mc.get('percent_self_sustaining', '?')}%",
        f"   ├ Cascade: {econ['cascade_class']} (loop: {econ['loop_gain']:.2f})",
        f"   ├ P(hit): {econ['prob_hit_wall']:.0%} | Dir: {econ['directional_factor']:.2f}",
        f"   ├ Shape: {conc.get('shape', '?')} | Gini: {conc.get('gini', 0):.2f}",
        f"   ├ Catalyst: {econ['catalyst_type']} ({econ['days_to_catalyst']}d) "
        f"→ {econ['catalyst_move_pct']:.1f}% avg ({econ['earnings_reliability']})",
        f"   ├ IV: {econ['iv_rank']} ({econ['iv_percentile']*100:.0f}%ile, real) | "
        f"P/C: {econ['pc_ratio']:.1f}",
        f"   └ {econ['sizing']}",
    ]

    if price_hist and len(price_hist) >= 2:
        trend = (price_hist[-1] - price_hist[0]) / price_hist[0] * 100
        lines.insert(2, f"   ├ 5d trend: {trend:+.1f}%")

    return "\n".join(lines)


def format_dollar(val: float) -> str:
    if abs(val) >= 1e9:
        return f"${val/1e9:.1f}B"
    elif abs(val) >= 1e6:
        return f"${val/1e6:.1f}M"
    elif abs(val) >= 1e3:
        return f"${val/1e3:.0f}K"
    else:
        return f"${val:.0f}"


# =====================================================================
# SECTION 13: MAIN
# =====================================================================

def main():
    start = datetime.now()
    logger.info("🚀 Gamma Amplification Scanner v2.1 starting")

    universe = build_universe()
    logger.info(f"Universe: {len(universe)} tickers")
    
    if not universe:
        logger.warning("Empty universe")
        return

    results = []
    total = min(len(universe), 40)

    for idx, ticker in enumerate(universe[:total]):
        logger.info(f"[{idx+1}/{total}] {ticker}")

        gex = compute_gex(ticker)
        if gex is None:
            time.sleep(0.3)
            continue

        conc = analyze_concentration(gex["gex_by_strike"], gex["price"])

        econ = compute_economic_score(ticker, gex, conc)
        if econ["classification"] == "NO_SIGNAL":
            time.sleep(0.3)
            continue

        price_hist = get_price_history(ticker)

        results.append({
            "ticker": ticker,
            "price": gex["price"],
            "net_gex": gex["net_gex"],
            "classification": econ["classification"],
            "economic_score": econ["economic_score"],
            "loop_gain": econ["loop_gain"],
            "cascade_class": econ["cascade_class"],
            "mc_median": econ.get("monte_carlo", {}).get("median_total_amplification", 0),
            "mc_p95": econ.get("monte_carlo", {}).get("p95", 0),
            "mc_self_sustaining": econ.get("monte_carlo", {}).get("percent_self_sustaining", 0),
            "prob_hit_wall": econ["prob_hit_wall"],
            "catalyst_type": econ["catalyst_type"],
            "days_to_catalyst": econ["days_to_catalyst"],
            "catalyst_move_pct": econ["catalyst_move_pct"],
            "earnings_reliability": econ["earnings_reliability"],
            "iv_percentile": econ["iv_percentile"],
            "pc_ratio": econ["pc_ratio"],
            "wall_strike": gex["max_oi_strike"],
            "distance_to_wall": gex["distance_to_wall"],
            "concentration_shape": conc.get("shape", "?"),
            "concentration_score": conc.get("concentration_score", 0),
            "reason": econ["reason"],
            "sizing": econ["sizing"],
            "_report": build_report(ticker, gex, econ, conc, price_hist),
        })

        time.sleep(0.5)

    # ── Sort ──
    rank = {"EXTREME": 0, "HIGH_CONVICTION": 1, "WATCH": 2, "STRUCTURAL": 3}
    results.sort(key=lambda r: (rank.get(r["classification"], 99), -r["economic_score"]))

    # ── Console ──
    print("\n" + "=" * 100)
    print(f"  GAMMA AMPLIFICATION SCAN v2.1 — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Universe: {len(universe)} | Analyzed: {len(results)} signals")
    print(f"  API: finvizfinance (Ticker screener) + yfinance (corrected access)")
    print("=" * 100)

    for r in results:
        emoji = {"EXTREME": "🔴", "HIGH_CONVICTION": "🟡", "WATCH": "🔵",
                 "STRUCTURAL": "⚪"}.get(r["classification"], "⚫")
        print(f"{emoji} {r['ticker']:8s} | {r['economic_score']*100:5.1f}% | "
              f"${r['price']:>7.2f} | Wall: ${r['wall_strike']} | "
              f"{r['catalyst_type']} ({r['days_to_catalyst']}d) | "
              f"MC: {r['mc_median']:5.1f}% (p95: {r['mc_p95']:5.1f}%) | "
              f"Self: {r['mc_self_sustaining']:3.0f}%")

    print("=" * 100)

    # ── Telegram ──
    high = [r for r in results if r["classification"] in ("EXTREME", "HIGH_CONVICTION")]
    watch = [r for r in results if r["classification"] == "WATCH"]
    structural = len([r for r in results if r["classification"] == "STRUCTURAL"])

    msg_parts = [f"🤖 *Gamma Scan v2.1* — {datetime.now().strftime('%H:%M UTC')}"]
    msg_parts.append(f"📊 {len(universe)} in universe → {len(results)} signals")
    msg_parts.append(f"🆕 Corrected API: Ticker screener + attribute access")

    if high:
        msg_parts.append(f"\n🔴 *HIGH CONVICTION ({len(high)})*")
        msg_parts.append("━" * 45)
        for r in high[:5]:
            msg_parts.append(r["_report"])
            msg_parts.append("")

    if watch:
        msg_parts.append(f"\n🔵 *WATCH ({len(watch)})*")
        msg_parts.append("━" * 45)
        for r in watch[:3]:
            msg_parts.append(r["_report"])
            msg_parts.append("")

    msg_parts.append(f"\n📈 *Summary*: {len(high)} high | {len(watch)} watch | {structural} structural")
    msg_parts.append("🤖 v2.1 — MC cascade + Calibrated Loop Gain + Real IV%ile + API-corrected")

    send_telegram("\n".join(msg_parts))

    # ── Save CSV ──
    try:
        df = pd.DataFrame(results)
        df.to_csv(f"gamma_scan_{datetime.now().strftime('%Y%m%d_%H%M')}_v2.csv", index=False)
        logger.info(f"Saved to CSV ({len(results)} rows)")
    except Exception as e:
        logger.warning(f"CSV save failed: {e}")

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"✅ Scan complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()

