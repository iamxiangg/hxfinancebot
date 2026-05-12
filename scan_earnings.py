#!/usr/bin/env python3
"""
Earnings IV Crush Scanner v3
Screens S&P 500 stocks for overpriced earnings options.
Sends actionable trade setups to Telegram.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, date
from io import StringIO
from typing import Optional, Tuple, List, Dict

import numpy as np
import pandas as pd
import yfinance as yf
import requests
from scipy.optimize import curve_fit

# ---------------------- Configuration ----------------------
# Thresholds (adjust based on market conditions)
VOLUME_MIN = 1_500_000          # 30-day avg volume
IV_RV_RATIO_MIN = 1.25          # IV30 / RV30 minimum
SLOPE_MAX = -0.00406            # Term structure slope threshold (negative)
FRONT_DTE_MAX = 4               # Max days to expiry for front month (after earnings)
BACK_DTE_MIN = 25               # Min days to expiry for back month
BACK_DTE_MAX = 50               # Max days to expiry for back month

# Telegram credentials from environment variables
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# URLs
SP500_CSV_URL = "https://raw.githubusercontent.com/sinnaj-r/IV-Crush-Scanner/main/tickers/sp500.csv"  # example – replace with actual CSV URL

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------------------- Helper Functions ----------------------
def download_sp500_csv() -> pd.DataFrame:
    """Download S&P 500 list with volume column from GitHub."""
    try:
        resp = requests.get(SP500_CSV_URL, timeout=30)
        resp.raise_for_status()
        # Assume CSV has headers: Symbol, Name, Sector, Volume (or similar)
        df = pd.read_csv(StringIO(resp.text))
        # Standardise column names – adjust as needed
        if 'Volume' not in df.columns and 'volume' in df.columns:
            df.rename(columns={'volume': 'Volume'}, inplace=True)
        return df
    except Exception as e:
        logger.error(f"Failed to download S&P 500 CSV: {e}")
        raise

def get_next_earnings_date(ticker: str, max_retries: int = 3) -> Optional[date]:
    """Fetch next earnings date using yfinance calendar."""
    for attempt in range(max_retries):
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            if cal is None or 'Earnings Date' not in cal:
                return None
            # Sometimes it's a list, sometimes a single value
            e_date = cal['Earnings Date']
            if isinstance(e_date, (list, pd.Series)):
                e_date = e_date.iloc[0]
            if isinstance(e_date, datetime):
                e_date = e_date.date()
            return e_date
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} for {ticker} earnings date: {e}")
            time.sleep(2)
    return None

def get_option_chain(ticker: str, expiry_str: str) -> Optional[Dict]:
    """Fetch calls and puts for a given expiry."""
    try:
        stock = yf.Ticker(ticker)
        chain = stock.option_chain(expiry_str)
        return {'calls': chain.calls, 'puts': chain.puts}
    except Exception as e:
        logger.error(f"Failed to get chain for {ticker} expiry {expiry_str}: {e}")
        return None

def find_atm_strike(current_price: float, strikes: List[float]) -> float:
    """Find the strike closest to current price."""
    return min(strikes, key=lambda s: abs(s - current_price))

def compute_straddle_price(chain: Dict, strike: float) -> Optional[float]:
    """Compute mid price of ATM straddle (call + put)."""
    calls = chain['calls']
    puts = chain['puts']
    call_row = calls[calls['strike'] == strike]
    put_row = puts[puts['strike'] == strike]
    if call_row.empty or put_row.empty:
        return None
    call_mid = (call_row['bid'].iloc[0] + call_row['ask'].iloc[0]) / 2
    put_mid = (put_row['bid'].iloc[0] + put_row['ask'].iloc[0]) / 2
    return call_mid + put_mid

def compute_expected_move(straddle_price: float, current_price: float) -> float:
    """Expected move as a percentage of current price (simplified)."""
    if current_price == 0:
        return 0
    return (straddle_price / current_price) * 100

def compute_iv30_rv30(ticker: str, current_price: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Estimate IV30 and RV30.
    IV30: Use front-month ATM straddle IV (mid).
    RV30: Use yfinance historical daily returns (30 days).
    Returns (iv30, rv30) as decimals.
    """
    try:
        stock = yf.Ticker(ticker)
        # Get nearest monthly expiry > 30 days away? Simpler: use 30-day vol from history
        hist = stock.history(period="2mo")
        if len(hist) < 30:
            return None, None
        returns = hist['Close'].pct_change().dropna()
        rv30 = returns.tail(30).std() * np.sqrt(252)  # annualised
        # IV30: use front month ATM option's implied vol (simplified: compute from straddle)
        # For a quick proxy, we can fetch the near-term option with ~30 DTE
        # But we already computed straddle for the front expiry. Instead approximate from mid price using Black-Scholes inverse?
        # Simpler: use yfinance's implied volatility if available.
        # Fallback: use historical vol as proxy? Not ideal. We'll compute IV from the ATM straddle.
        # Since we already fetch chains later, we can compute IV there.
        # For filter purpose, we'll compute IV30 from the front-month ATM option after we have it.
        return None, rv30  # placeholder – will be filled later in main loop
    except Exception as e:
        logger.error(f"compute_iv30_rv30 {ticker}: {e}")
        return None, None

def compute_term_structure_slope(ticker: str, front_expiry: str, back_expiry: str, strike: float) -> Optional[float]:
    """
    Compute slope of IV term structure between front and back expiries.
    Returns negative if front IV > back IV.
    """
    front_chain = get_option_chain(ticker, front_expiry)
    back_chain = get_option_chain(ticker, back_expiry)
    if front_chain is None or back_chain is None:
        return None
    front_straddle = compute_straddle_price(front_chain, strike)
    back_straddle = compute_straddle_price(back_chain, strike)
    if front_straddle is None or back_straddle is None:
        return None
    # Convert to implied volatility? We'll use straddle price / strike as a proxy for volatility.
    # But better to compute implied vol (not needed for slope if we assume same strike).
    # Since strike is same, the ratio of straddle prices reflects relative volatility.
    # Simpler: slope = (back_iv - front_iv) / (back_dte - front_dte)
    # We'll compute approximate IV using straddle price / (strike * sqrt(dte/365)) * something.
    # For robustness, we use a simplified IV approximation.
    def approx_iv(straddle_price, strike, days_to_expiry):
        if strike <= 0 or days_to_expiry <= 0:
            return 0
        # Approximate ATM straddle = 0.8 * strike * iv * sqrt(dte/365) (Naïve)
        # → iv ≈ straddle_price / (0.8 * strike * sqrt(dte/365))
        return straddle_price / (0.8 * strike * np.sqrt(days_to_expiry / 365))

    front_dte = (datetime.strptime(front_expiry, '%Y-%m-%d') - datetime.now()).days
    back_dte = (datetime.strptime(back_expiry, '%Y-%m-%d') - datetime.now()).days
    front_iv = approx_iv(front_straddle, strike, front_dte)
    back_iv = approx_iv(back_straddle, strike, back_dte)
    if front_dte == back_dte:
        return None
    slope = (back_iv - front_iv) / (back_dte - front_dte)
    return slope

def send_telegram_message(text: str, parse_mode: str = 'HTML') -> bool:
    """Send message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not set. Skipping message.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': True
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Telegram message sent successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False

def format_trade_setup(ticker: str, price: float, earnings_date: str,
                       front_expiry: str, back_expiry: str, strike: float,
                       front_straddle_price: float, back_straddle_price: float,
                       expected_move_pct: float, expected_move_dollar: float,
                       volume: float, iv_rv_ratio: float, slope: float) -> str:
    """Format a trade setup card as HTML."""
    net_debit = front_straddle_price - back_straddle_price
    max_risk = net_debit * 100  # per contract
    breakeven_low = strike - net_debit
    breakeven_high = strike + net_debit
    msg = f"""
📈 {ticker} — EARNINGS TRADE SETUP
📅 Earnings: {earnings_date}  |  💰 Price: ${price:.2f}  |  🎯 Strike: ${strike:.2f}
📆 Front expiry: {front_expiry} (1 DTE)
📆 Back expiry: {back_expiry}
📊 Expected Move: ±{expected_move_pct:.2f}% (±${expected_move_dollar:.2f})
💵 Straddle (sell naked): Sell {front_expiry} ${strike:.2f} Call + Put = ${front_straddle_price:.2f} credit
   Breakeven: ${breakeven_low:.2f} – ${breakeven_high:.2f}
📊 Calendar Spread (preferred):
   Sell {front_expiry} ${strike:.2f} straddle
   Buy  {back_expiry} ${strike:.2f} straddle
   Net: ${net_debit:+.2f} debit per spread  |  Max Risk: ${max_risk:.0f}/contract
📋 Filters: ✅ Vol {volume:,.0f}  |  ✅ IV/RV {iv_rv_ratio:.2f}x  |  ✅ Slope {slope:.5f}
"""
    return msg

# ---------------------- Main Scanner Logic ----------------------
def scan():
    logger.info("Starting earnings IV crush scanner...")
    start_time = time.time()

    # 1. Download S&P 500 CSV and pre-filter by volume
    df = download_sp500_csv()
    if 'Volume' not in df.columns:
        logger.error("CSV missing 'Volume' column. Using default volume column.")
        # Try to use first numeric column as volume?
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        if len(numeric_cols) >= 1:
            df['Volume'] = df[numeric_cols[0]]
        else:
            raise ValueError("Cannot identify volume column in CSV.")

    # Filter by volume
    df_filtered = df[df['Volume'] >= VOLUME_MIN].copy()
    logger.info(f"Total S&P 500: {len(df)} | Volume-qualified: {len(df_filtered)}")

    # 2. Iterate over qualifying tickers
    hits = []
    scanned = 0
    for _, row in df_filtered.iterrows():
        ticker = row.get('Symbol', row.get('Ticker', row.get('symbol', ''))).strip()
        if not ticker:
            continue
        scanned += 1
        logger.info(f"Scanning {ticker} ({scanned}/{len(df_filtered)})...")

        try:
            # Get stock price
            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d")
            if hist.empty:
                continue
            current_price = hist['Close'].iloc[-1]

            # Get next earnings date
            earnings_date = get_next_earnings_date(ticker)
            if earnings_date is None:
                continue

            # Determine front expiry: first expiry after earnings
            expirations = stock.options
            if not expirations:
                continue
            # Filter expirations after earnings date
            expirations_dates = [datetime.strptime(e, '%Y-%m-%d').date() for e in expirations]
            front_expiry_date = None
            for ed in expirations_dates:
                if ed >= earnings_date:
                    front_expiry_date = ed
                    break
            if front_expiry_date is None:
                continue
            front_expiry_str = front_expiry_date.strftime('%Y-%m-%d')
            front_dte = (front_expiry_date - date.today()).days
            if front_dte > FRONT_DTE_MAX or front_dte <= 0:
                continue  # not near enough

            # Back expiry: next monthly expiry after front (at least BACK_DTE_MIN days)
            back_expiry_date = None
            for ed in expirations_dates:
                if ed > front_expiry_date:
                    dte = (ed - date.today()).days
                    if BACK_DTE_MIN <= dte <= BACK_DTE_MAX:
                        back_expiry_date = ed
                        break
            if back_expiry_date is None:
                continue
            back_expiry_str = back_expiry_date.strftime('%Y-%m-%d')

            # Get option chains for front and back
            front_chain = get_option_chain(ticker, front_expiry_str)
            back_chain = get_option_chain(ticker, back_expiry_str)
            if front_chain is None or back_chain is None:
                continue

            # Find ATM strike
            strikes = sorted(set(front_chain['calls']['strike'].tolist() + front_chain['puts']['strike'].tolist()))
            if not strikes:
                continue
            strike = find_atm_strike(current_price, strikes)

            # Compute straddle prices
            front_straddle = compute_straddle_price(front_chain, strike)
            back_straddle = compute_straddle_price(back_chain, strike)
            if front_straddle is None or back_straddle is None:
                continue

            # Expected move
            expected_move_pct = compute_expected_move(front_straddle, current_price)
            expected_move_dollar = front_straddle  # straddle price ~ expected move in dollars

            # IV30 / RV30 filter
            iv_rv_ratio = None
            # Compute RV30 using historical returns
            hist_full = stock.history(period="2mo")
            if len(hist_full) >= 30:
                returns = hist_full['Close'].pct_change().dropna()
                rv30 = returns.tail(30).std() * np.sqrt(252)
                # IV30 approximate from front straddle (using simplified IV)
                dte_front = (front_expiry_date - date.today()).days
                if dte_front > 0:
                    iv_approx = front_straddle / (0.8 * strike * np.sqrt(dte_front / 365))
                    if rv30 > 0:
                        iv_rv_ratio = iv_approx / rv30
            if iv_rv_ratio is None or iv_rv_ratio < IV_RV_RATIO_MIN:
                logger.info(f"{ticker}: IV/RV {iv_rv_ratio} fails")
                continue

            # Term structure slope
            slope = compute_term_structure_slope(ticker, front_expiry_str, back_expiry_str, strike)
            if slope is None or slope > SLOPE_MAX:  # slope must be more negative than threshold
                logger.info(f"{ticker}: slope {slope} fails")
                continue

            # Volume check (already pre-filtered)
            volume = row['Volume']

            # All filters passed → build setup
            earnings_date_str = earnings_date.strftime('%b %d, %Y')
            trade_card = format_trade_setup(
                ticker, current_price, earnings_date_str,
                front_expiry_str, back_expiry_str, strike,
                front_straddle, back_straddle,
                expected_move_pct, expected_move_dollar,
                volume, iv_rv_ratio, slope
            )
            hits.append(trade_card)
            logger.info(f"{ticker}: ALL FILTERS PASSED ✓")

        except Exception as e:
            logger.error(f"Error processing {ticker}: {e}")
            continue

    # 3. Compile and send Telegram message
    scan_time = time.time() - start_time
    header = f"""📈 EARNINGS IV CRUSH — SCAN RESULTS
📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
───────────────
{len(hits)} hits from {scanned} scanned (pre-filtered from {len(df)} volume-qualified)
Scan time: {scan_time:.1f}s
───────────────
"""
    footer = """
───────────────
⚠️ Sizing: Use 10% fractional Kelly. Max 6% per calendar, 2% per straddle.
"""
    if hits:
        message = header + "\n".join(hits) + footer
    else:
        message = header + "\nNo setups found today."
    send_telegram_message(message)

    logger.info(f"Scan completed. {len(hits)} hits found.")

if __name__ == "__main__":
    scan()
