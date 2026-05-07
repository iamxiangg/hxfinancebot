#!/usr/bin/env python3
"""
Gamma Amplification Scanner — v2.2 (Raspberry Pi 5 Optimized)

Optimizations:
1. Single yf.Ticker session per ticker (was 5-6 redundant sessions)
2. ThreadPoolExecutor parallelism (2 workers for Pi 5, rate-limit safe)
3. Startup stagger to avoid synchronized rate limiting
4. Rate-limit retry with exponential backoff
5. Early exit on NO_SIGNAL (skips report building)
6. All derived data computed inline from pre-fetched data
"""
import os
import logging
import time
import math
import random
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

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
# Reduced to 2 workers to avoid Yahoo Finance rate limiting
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))

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
# SECTION 2: CONCENTRATION ANALYSIS
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

    # Wall Sharpness (use nearest 3 strikes each side)
    peak_idx = abs_gex.index(max(abs_gex))
    peak_strike = strikes[peak_idx]
    peak_gex = abs_gex[peak_idx]
    left_start = max(0, peak_idx - 3)
    right_end = min(len(strikes), peak_idx + 4)
    nearby_strikes = abs_gex[left_start:right_end]
    nearby_gex = sum(nearby_strikes)
    wall_sharpness = peak_gex / nearby_gex if nearby_gex > 0 else 1.0
    wall_sharpness = min(wall_sharpness, 0.95)

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

    # Calibrated loop gain
    loop_gain = concentration_score * (1 + wall_sharpness) * 0.45

    if loop_gain < 0.25:
        cascade_class = "NO_CASCADE"
        expected_multiplier = 1.0
    elif loop_gain < 0.40:
        cascade_class = "MILD"
        expected_multiplier = 2.5
    elif loop_gain < 0.55:
        cascade_class = "MODERATE"
        expected_multiplier = 4.0
    elif loop_gain < 0.70:
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
# SECTION 3: MONTE CARLO CASCADE SIMULATION
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
    """Monte Carlo simulation of gamma cascade feedback loop.
    
    net_gex should be the ORIGINAL signed value (negative = short gamma = squeeze potential).
    If net_gex >= 0 (long gamma), there's no cascade possible.
    
    percent_self_sustaining = percentage of SIMULATIONS where the feedback loop
    dominates the initial move for at least one step.
    """
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
    self_sustaining_simulations = 0  # count SIMULATIONS, not steps

    for sim in range(num_simulations):
        price = current_price
        cumulative_move = 0
        simulation_was_self_sustaining = False  # one boolean per simulation

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

            # Check if feedback dominates the initial move (self-sustaining)
            if step > 0 and abs(price_impact / 100) > abs(step_move) * 0.5:
                simulation_was_self_sustaining = True

            if abs(total_step_move) < 0.0001:
                break

        if simulation_was_self_sustaining:
            self_sustaining_simulations += 1

        total_amplifications.append(abs(cumulative_move) * 100)

    arr = sorted(total_amplifications)

    # Percentage of SIMULATIONS that were self-sustaining (0-100%)
    percent_self_sustaining = round(self_sustaining_simulations / num_simulations * 100, 1)

    return {
        "median_total_amplification": round(np.median(arr), 1),
        "mean_total_amplification": round(np.mean(arr), 1),
        "p25": round(np.percentile(arr, 25), 1),
        "p75": round(np.percentile(arr, 75), 1),
        "p95": round(np.percentile(arr, 95), 1),
        "max_amplification": round(max(arr), 1),
        "percent_self_sustaining": percent_self_sustaining,
    }


# =====================================================================
# SECTION 4: DIRECTIONAL FACTOR
# =====================================================================

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
# SECTION 5: FINVIZ SCREENER
# =====================================================================

def finviz_screen() -> list:
    """Screen Finviz for gamma squeeze candidates."""
    try:
        from finvizfinance.screener.ticker import Ticker

        logger.info("Connecting to Finviz...")
        screener = Ticker()
        screener.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        }

        time.sleep(3)

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
# SECTION 6: TELEGRAM NOTIFICATION
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
        f"self-sustaining: {mc.get('percent_self_sustaining', '?')}% of sims",
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
# SECTION 7: OPTIMIZED TICKER DATA FETCHER (single yf.Ticker session)
# =====================================================================

def fetch_all_ticker_data(ticker: str) -> dict | None:
    """
    Fetch ALL yfinance data for one ticker using a SINGLE yf.Ticker session.
    Includes startup stagger and rate-limit retry for Yahoo Finance.
    """
    # ─── Startup stagger: random delay to avoid synchronized rate limiting ───
    stagger = random.uniform(0.3, 1.5)
    time.sleep(stagger)

    max_attempts = 3
    stock = None
    for attempt in range(max_attempts):
        try:
            stock = yf.Ticker(ticker)
            break
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "too many" in err_str:
                wait = (2 ** attempt) * 5 + random.uniform(1, 3)
                logger.warning(f"Rate limited on {ticker}, retrying in {wait:.0f}s (attempt {attempt+1}/{max_attempts})")
                time.sleep(wait)
                if attempt == max_attempts - 1:
                    logger.error(f"Rate limited on {ticker} after {max_attempts} attempts, skipping")
                    return None
            else:
                return None

    if stock is None:
        return None

    result = {"ticker": ticker}

    # ─── 1. Current Price (fast_info) ───
    price = None
    try:
        price = stock.fast_info.last_price
    except AttributeError:
        pass
    if price is None:
        try:
            price = stock.fast_info.lastPrice
        except AttributeError:
            pass
    if price is None:
        try:
            price = stock.fast_info.regularMarketPrice
        except AttributeError:
            pass
    if price is None:
        try:
            info = stock.info
            price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
        except Exception:
            pass
    if price is None or price <= 0:
        return None
    result["price"] = price

    # ─── 2. Options Chain (GEX + IV + P/C ratio in one pass) ───
    try:
        exps = stock.options
    except Exception:
        exps = None

    if not exps:
        return None

    now = datetime.now()
    net_gex = 0.0
    call_gex = 0.0
    put_gex = 0.0
    gex_by_strike = {}
    max_oi_strike = None
    max_oi = 0
    total_call_vol = 0
    total_put_vol = 0
    all_ivs = []

    for exp in sorted(exps)[:NUM_EXPIRIES]:
        T = max((datetime.strptime(exp, "%Y-%m-%d") - now).days, 1) / 365.0
        try:
            opt = stock.option_chain(exp)
        except Exception:
            continue

        # ── Calls ──
        for _, row in opt.calls.iterrows():
            if row["openInterest"] > 0 and row["impliedVolatility"] > 0:
                dg = dollar_gamma(price, row["strike"], T, RISK_FREE_RATE,
                                  row["impliedVolatility"], row["openInterest"])
                net_gex += dg
                call_gex += dg
                strike = int(row["strike"])
                gex_by_strike[strike] = gex_by_strike.get(strike, 0) + dg
                if row["openInterest"] > max_oi:
                    max_oi = row["openInterest"]
                    max_oi_strike = strike
            if row["volume"] > 0:
                total_call_vol += int(row["volume"])
            if row["impliedVolatility"] > 0:
                all_ivs.append(row["impliedVolatility"])

        # ── Puts ──
        for _, row in opt.puts.iterrows():
            if row["openInterest"] > 0 and row["impliedVolatility"] > 0:
                dg = dollar_gamma(price, row["strike"], T, RISK_FREE_RATE,
                                  row["impliedVolatility"], row["openInterest"])
                net_gex -= dg
                put_gex += dg
                strike = int(row["strike"])
                gex_by_strike[strike] = gex_by_strike.get(strike, 0) - dg
            if row["volume"] > 0:
                total_put_vol += int(row["volume"])
            if row["impliedVolatility"] > 0:
                all_ivs.append(row["impliedVolatility"])

    if not gex_by_strike:
        return None

    result["net_gex"] = net_gex
    result["call_gex"] = call_gex
    result["put_gex"] = put_gex
    result["gex_by_strike"] = gex_by_strike
    result["max_oi_strike"] = max_oi_strike
    result["distance_to_wall"] = (price - max_oi_strike) / price if max_oi_strike else None
    result["total_call_vol"] = total_call_vol
    result["total_put_vol"] = total_put_vol

    if all_ivs:
        result["current_iv"] = float(np.median(all_ivs))
    else:
        result["current_iv"] = 0.0

    # ─── Rate limit cooldown before history call ───
    time.sleep(0.3)

    # ─── 3. Full Price History ───
    try:
        hist = stock.history(period="2y")
        result["history_2y"] = hist
    except Exception:
        result["history_2y"] = pd.DataFrame()

    # ─── 4. Earnings Dates ───
    result["earnings_dates"] = None
    try:
        ed = stock.earnings_dates
        if ed is not None and not ed.empty:
            result["earnings_dates"] = ed
    except (AttributeError, Exception):
        pass
    if result["earnings_dates"] is None:
        try:
            from yfinance import Calendars
            cal = Calendars()
            cal_data = cal.get_earnings(ticker)
            if cal_data is not None and not cal_data.empty:
                result["earnings_dates"] = cal_data
        except (ImportError, AttributeError):
            pass

    return result


# =====================================================================
# SECTION 8: SINGLE-TICKER PROCESSOR (runs in thread pool)
# =====================================================================

def process_ticker(ticker: str) -> dict | None:
    """
    Full analysis for a single ticker using ONE yfinance session.
    Designed to run in ThreadPoolExecutor — no blocking sleeps.
    Returns None if no signal (early exit for speed).
    """
    try:
        # ─── ONE round-trip to Yahoo ───
        data = fetch_all_ticker_data(ticker)
        if data is None:
            return None

        price = data["price"]
        gex_by_strike = data["gex_by_strike"]
        net_gex = data["net_gex"]
        max_oi_strike = data["max_oi_strike"]
        distance_to_wall = data["distance_to_wall"]

        # ─── GATE 1: Must be short gamma (net_gex < 0) ───
        if net_gex >= 0:
            return None

        # ─── Concentration Analysis ───
        conc = analyze_concentration(gex_by_strike, price)

        # ─── Earnings Days ───
        days_to_catalyst = None
        catalyst_type = "NONE"
        ed = data.get("earnings_dates")
        if ed is not None:
            tz = ed.index.tz if hasattr(ed.index, 'tz') and ed.index.tz is not None else None
            now_tz = datetime.now(tz) if tz else datetime.now()
            future = ed[ed.index > now_tz]
            if not future.empty:
                days = (future.index.min() - now_tz).days
                if days <= 45:
                    days_to_catalyst = days
                    catalyst_type = "EARNINGS"

        # ─── Historical Earnings Move ───
        hist = data["history_2y"]
        avg_move_pct = 0.05
        earnings_reliability = "LOW"
        if ed is not None and not hist.empty:
            moves = []
            tz = ed.index.tz if hasattr(ed.index, 'tz') and ed.index.tz is not None else None
            now_tz = datetime.now(tz) if tz else datetime.now()
            for date in ed.index:
                try:
                    date_dt = date.to_pydatetime() if hasattr(date, 'to_pydatetime') else date
                    if date_dt > now_tz:
                        continue
                except Exception:
                    continue
                before = hist[hist.index < date]
                if before.empty:
                    continue
                close_before = before.iloc[-1]["Close"]
                after = hist[hist.index > date]
                if after.empty:
                    continue
                close_after = after.iloc[0]["Close"]
                moves.append(abs((close_after - close_before) / close_before))
            if moves:
                avg_move_pct = sum(moves) / len(moves)
                if len(moves) >= 8:
                    earnings_reliability = "HIGH"
                elif len(moves) >= 4:
                    earnings_reliability = "MEDIUM"

        catalyst_move_pct = avg_move_pct * 100 if catalyst_type == "EARNINGS" else 1.5

        # ─── Average Dollar Volume ───
        avg_dvol = 0
        if len(hist) >= 20:
            recent = hist.tail(20)
            avg_dvol = recent["Volume"].mean() * recent["Close"].mean()
        if avg_dvol <= 0:
            return None

        # ─── Directional Factor ───
        if len(hist) >= 15:
            closes = hist["Close"].tolist()[-15:]
        else:
            closes = []

        direction = directional_factor(price, max_oi_strike, closes)
        directional_move_pct = catalyst_move_pct * direction

        # ─── Probability of Hitting Wall ───
        dist_pct = abs(distance_to_wall) * 100 if distance_to_wall else 0
        prob_hit_wall = min(1.0, directional_move_pct / dist_pct) if dist_pct > 0 else 0

        # ─── First-Step Amplification ───
        abs_gex = abs(net_gex)
        hedging_per_1pct = abs_gex * 0.01
        first_step_amp = hedging_per_1pct / avg_dvol if avg_dvol > 0 else 0

        # ─── IV Percentile ───
        current_iv = data.get("current_iv", 0)
        iv_percentile = 0.50
        iv_rank = "NORMAL"
        if current_iv > 0 and len(hist) >= 60:
            returns = hist["Close"].pct_change().dropna()
            rolling_vol = returns.rolling(window=20).std() * np.sqrt(252)
            rolling_vol = rolling_vol.dropna()
            if not rolling_vol.empty:
                sorted_hv = sorted(rolling_vol.values)
                count_below = sum(1 for hv in sorted_hv if hv < current_iv)
                iv_percentile = max(0.01, min(0.99, count_below / len(sorted_hv)))
                if iv_percentile < 0.30:
                    iv_rank = "LOW"
                elif iv_percentile < 0.70:
                    iv_rank = "NORMAL"
                else:
                    iv_rank = "ELEVATED"

        # ─── Put-Call Ratio ───
        total_put_vol = data.get("total_put_vol", 0)
        total_call_vol = data.get("total_call_vol", 0)
        pc_ratio = 0.5
        pc_interpretation = "NEUTRAL"
        if total_put_vol > 0:
            pc_ratio = total_call_vol / total_put_vol
            if pc_ratio < 0.4:
                pc_interpretation = "BULLISH (calls dominate)"
            elif pc_ratio < 0.7:
                pc_interpretation = "MODERATELY BULLISH"
            elif pc_ratio < 1.3:
                pc_interpretation = "NEUTRAL"
            elif pc_ratio < 2.0:
                pc_interpretation = "MODERATELY BEARISH"
            else:
                pc_interpretation = "BEARISH (puts dominate)"

        # ─── Monte Carlo Cascade ───
        mc = monte_carlo_cascade(
            current_price=price,
            net_gex=net_gex,
            avg_dvol=avg_dvol,
            catalyst_move_pct=catalyst_move_pct,
            dist_to_wall=abs(distance_to_wall) if distance_to_wall else 0,
            concentration_score=conc.get("concentration_score", 0),
            wall_sharpness=conc.get("wall_sharpness", 0),
        )
        total_potential = mc["median_total_amplification"] / 100

        # ─── Economic Score ───
        economic_score = first_step_amp * prob_hit_wall * 0.30 + total_potential * 0.70
        if iv_percentile < 0.30:
            economic_score *= 0.7
        elif iv_percentile > 0.70:
            economic_score *= 0.85
        if pc_ratio < 0.5:
            economic_score *= 1.15
        elif pc_ratio < 0.8:
            economic_score *= 1.05
        elif pc_ratio > 1.5:
            economic_score *= 0.85

        # ─── Classification ───
        loop_gain = conc.get("loop_gain", 0)
        mc_p95 = mc.get("p95", 0)
        mc_self_sustaining = mc.get("percent_self_sustaining", 0)

        if economic_score <= 0:
            return None

        if (loop_gain >= 0.65 or mc_p95 > 50 or mc_self_sustaining > 50) and economic_score >= 0.10:
            classification = "EXTREME"
        elif economic_score >= 0.15 and catalyst_type == "EARNINGS":
            classification = "HIGH_CONVICTION"
        elif economic_score >= 0.15:
            classification = "WATCH"
        elif economic_score >= 0.05:
            classification = "STRUCTURAL"
        else:
            return None

        # ─── Build Reason ───
        reason_parts = [
            f"MC median: {mc['median_total_amplification']:.1f}%",
            f"MC p95: {mc_p95:.1f}%",
            f"Self-sustaining: {mc_self_sustaining:.0f}% of sims",
            f"Loop: {loop_gain:.2f} ({conc.get('cascade_class', '?')})",
        ]
        if catalyst_type == "EARNINGS":
            reason_parts.insert(0, f"Earnings in {days_to_catalyst}d ({catalyst_move_pct:.1f}% avg, {earnings_reliability})")
        else:
            reason_parts.insert(0, "No catalyst")
        reason_parts.append(f"IV: {iv_rank} ({iv_percentile*100:.0f}%ile)")
        reason_parts.append(f"P/C: {pc_ratio:.1f}")

        # ─── Sizing ───
        if classification == "EXTREME":
            sizing = "Size: 5-8% of portfolio (high conviction)"
        elif classification == "HIGH_CONVICTION":
            sizing = "Size: 3-5% of portfolio (moderate conviction)"
        elif classification == "WATCH":
            sizing = "Size: 1-2%, tight stops (speculative)"
        else:
            sizing = "No position — monitor only"

        econ_dict = {
            "economic_score": round(economic_score, 4),
            "classification": classification,
            "first_step_amp": round(first_step_amp, 6),
            "total_potential": round(total_potential, 4),
            "loop_gain": loop_gain,
            "cascade_class": conc.get("cascade_class", "?"),
            "prob_hit_wall": round(prob_hit_wall, 3),
            "directional_factor": round(direction, 2),
            "catalyst_type": catalyst_type,
            "days_to_catalyst": days_to_catalyst if days_to_catalyst else 999,
            "catalyst_move_pct": round(catalyst_move_pct, 1),
            "earnings_reliability": earnings_reliability,
            "iv_percentile": iv_percentile,
            "iv_rank": iv_rank,
            "pc_ratio": round(pc_ratio, 2),
            "pc_interpretation": pc_interpretation,
            "monte_carlo": mc,
            "sizing": sizing,
        }

        gex_dict = {
            "price": price,
            "net_gex": net_gex,
            "max_oi_strike": max_oi_strike,
            "distance_to_wall": distance_to_wall,
        }

        report = build_report(ticker, gex_dict, econ_dict, conc, closes)

        return {
            "ticker": ticker,
            "price": price,
            "net_gex": net_gex,
            "classification": classification,
            "economic_score": round(economic_score, 4),
            "loop_gain": loop_gain,
            "cascade_class": conc.get("cascade_class", "?"),
            "mc_median": mc.get("median_total_amplification", 0),
            "mc_p95": mc_p95,
            "mc_self_sustaining": mc_self_sustaining,
            "prob_hit_wall": round(prob_hit_wall, 3),
            "catalyst_type": catalyst_type,
            "days_to_catalyst": days_to_catalyst if days_to_catalyst else 999,
            "catalyst_move_pct": round(catalyst_move_pct, 1),
            "earnings_reliability": earnings_reliability,
            "iv_percentile": iv_percentile,
            "pc_ratio": round(pc_ratio, 2),
            "wall_strike": max_oi_strike,
            "distance_to_wall": distance_to_wall,
            "concentration_shape": conc.get("shape", "?"),
            "concentration_score": conc.get("concentration_score", 0),
            "reason": " | ".join(reason_parts),
            "sizing": sizing,
            "_report": report,
        }

    except Exception as e:
        logger.error(f"Error processing {ticker}: {e}")
        return None


# =====================================================================
# SECTION 9: MAIN (ThreadPoolExecutor version)
# =====================================================================

def main():
    start = datetime.now()
    logger.info("🚀 Gamma Amplification Scanner v2.2 (Pi5 Optimized) starting")

    universe = build_universe()
    logger.info(f"Universe: {len(universe)} tickers")
    if not universe:
        logger.warning("Empty universe")
        return

    total = min(len(universe), 4000)
    logger.info(f"Scanning {total} tickers with {MAX_WORKERS} parallel workers...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_ticker, ticker): ticker
                   for ticker in universe[:total]}

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
                    classification = result["classification"]
                    emoji = {"EXTREME": "🔴", "HIGH_CONVICTION": "🟡",
                             "WATCH": "🔵", "STRUCTURAL": "⚪"}.get(classification, "⚫")
                    logger.info(f"{emoji} {ticker}: {result['economic_score']*100:.1f}% → {classification}")
            except Exception as e:
                logger.error(f"Exception processing {ticker}: {e}")

    rank = {"EXTREME": 0, "HIGH_CONVICTION": 1, "WATCH": 2, "STRUCTURAL": 3}
    results.sort(key=lambda r: (rank.get(r["classification"], 99), -r["economic_score"]))

    print("\n" + "=" * 100)
    print(f"  GAMMA AMPLIFICATION SCAN v2.2 — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Universe: {len(universe)} | Analyzed: {len(results)} signals")
    print(f"  Workers: {MAX_WORKERS} | Pi5 Optimized: 1 session/ticker, no blocking sleeps")
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

    high = [r for r in results if r["classification"] in ("EXTREME", "HIGH_CONVICTION")]
    watch = [r for r in results if r["classification"] == "WATCH"]
    structural = len([r for r in results if r["classification"] == "STRUCTURAL"])

    msg_parts = [f"🤖 *Gamma Scan v2.2* — {datetime.now().strftime('%H:%M UTC')}"]
    msg_parts.append(f"📊 {len(universe)} in universe → {len(results)} signals")
    msg_parts.append(f"⚡ Pi5 Optimized: {MAX_WORKERS} workers, 1 session/ticker")

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
    msg_parts.append("🤖 v2.2 — MC cascade + Calibrated Loop Gain + Real IV%ile + Pi5 Optimized")

    send_telegram("\n".join(msg_parts))

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

