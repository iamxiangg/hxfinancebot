#!/usr/bin/env python3
"""
Gamma Squeeze Scanner – v2.2 (Diagnostic Edition)
Scans S&P 500 stocks for gamma amplification signals.
Sends Telegram alerts with trade suggestions.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import logging
import time
import json
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from scipy.stats import norm
from typing import Optional, List, Tuple

# ---------- Configuration ----------
SP500_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","BRK-B","UNH","JPM","V",
    "XOM","JNJ","PG","MA","AVGO","HD","TSLA","MRK","CVX","PEP",
    "KO","BAC","ABBV","CRM","ORCL","COST","WMT","DIS","ADBE","CSCO",
    "PFE","AMD","TMO","NFLX","LIN","ACN","CMCSA","DHR","ABT","NKE",
    "WFC","INTU","UPS","BMY","QCOM","COP","AMGN","VZ","TXN","RTX",
    "SPGI","LOW","UNP","BA","AMAT","LMT","HON","SCHW","C","MS",
    "CAT","BLK","DE","AXP","MDT","PLD","SBUX","GILD","EL","ADP",
    "BIIB","CI","SBUX","MDLZ","ISRG","AMT","GE","BKNG","FIS","ETN",
    "ITW","ZTS","MMC","TFC","USBA","PNC","EOG","PSX","MSI","VRTX",
    "CL","CB","DUK","SO","NSC","WM","APD","EW","BSX","TGT",
    "CSX","NEM","STZ","DG","KMI","AON","HLT","YUM","KDP","CTVA",
    "ALL","TRV","PAYX","OTIS","AIG","MET","PRU","AEP","D","PEG",
    "EXC","XEL","WEC","ED","EIX","AWK","SRE","LNT","DTE","CMS",
    "AEE","AGR","ATO","TAP","BEN","BRO","CF","CHD","CHTR","CNP",
    "CNQ","COF","COO","CPB","CRM","CSTL","CTSH","DOV","DOW","DPZ",
    "DRI","DVA","EBAY","ECL","EFX","EMR","EQT","ES","FANG","FAST",
    "FCX","FDX","FE","FFIV","FIS","FITB","FL","FLR","FMC","FRT",
    "FTV","GD","GEHC","GILD","GIS","GLW","GM","GPC","GPN","GRMN",
    "GS","HAL","HAS","HBAN","HCA","HD","HES","HIG","HII","HOG",
    "HOLX","HON","HRL","HSIC","HST","HSY","HUBB","HUM","IBM","ICE",
    "IDXX","IEX","IFF","INCY","INTC","IP","IPG","IQV","IR","IRM",
    "ISRG","IT","J","JBHT","JBL","JCI","JKHY","JNPR","K","KEY",
    "KEYS","KHC","KLAC","KMB","KMI","KMX","KR","L","LDOS","LEN",
    "LHX","LII","LIN","LKQ","LLY","LMT","LNC","LNT","LOW","LRCX",
    "LUV","LW","LYB","LYV","MA","MAA","MAR","MAS","MCD","MCHP",
    "MCK","MCO","MDLZ","MDT","META","MGM","MHK","MKC","MKTX","MLM",
    "MMC","MMM","MNST","MO","MOH","MOS","MPC","MPWR","MRK","MRNA",
    "MRO","MS","MSCI","MSFT","MSI","MTB","MTD","MU","NCLH","NDAQ",
    "NEE","NEM","NFLX","NI","NKE","NOC","NOW","NRG","NSC","NTAP",
    "NTRS","NUE","NVDA","NVR","NWL","NWSA","O","ODFL","OKE","OMC",
    "ON","ORCL","ORLY","OTIS","OXY","PARA","PAYX","PCAR","PEG","PEP",
    "PFE","PFG","PG","PGR","PH","PHM","PKG","PLD","PM","PNC",
    "PNR","PNW","POOL","PPG","PPL","PRU","PSA","PSX","PTC","PWR",
    "PYPL","QCOM","QRVO","RCL","REG","REGN","RF","RHI","RJF","RL",
    "RMD","ROK","ROL","ROP","ROST","RPM","RS","RSG","RTX","SEDG",
    "SJM","SNA","SRE","STE","STLD","STT","STX","STZ","SWK","SWKS",
    "SYF","SYK","SYY","T","TAP","TEL","TER","TFC","TFX","TGT",
    "TJX","TMO","TMUS","TPR","TRGP","TROW","TRV","TSCO","TSLA","TSN",
    "TT","TTWO","TW","TXN","TXT","UAL","UDR","UHS","ULTA","UNH",
    "UNP","UPS","URI","USB","V","VLO","VMC","VRSK","VRSN","VRTX",
    "VST","VTR","VZ","WAB","WAT","WBA","WDC","WEC","WELL","WFC",
    "WHR","WM","WMB","WMS","WMT","WRB","WST","WTW","WY","WYNN",
    "XEL","XOM","XYL","YUM","ZBRA","ZION","ZTS"
]  # Simplified demo list – replace with your full list

# Filtering thresholds
MIN_IV_FILTER = 0.20        # Minimum average implied volatility (20%)
GEX_MAGNITUDE_FILTER = 0.15 # Minimum normalized GEX
WALL_PROXIMITY = 0.50       # Max distance from spot (as % of spot)
MAX_WORKERS = 2
OPTION_DELAY = 2.0          # Seconds between API calls to avoid rate limit

# Telegram configuration
TELEGRAM_BOT_TOKEN = "your_bot_token_here"
TELEGRAM_CHAT_ID = "your_chat_id_here"
TELEGRAM_ENABLED = True

# ---------- Helper Functions ----------

def send_telegram_message(message: str) -> bool:
    """Send a message via Telegram bot. Returns True on success."""
    if not TELEGRAM_ENABLED:
        print(f"[Telegram Disabled] Would send: {message[:80]}...")
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

def compute_bs_iv(S, K, T, r, market_price, option_type='call') -> Optional[float]:
    """
    Black-Scholes implied volatility via Newton's method.
    Returns None if no convergence or invalid inputs.
    """
    if S <= 0 or K <= 0 or T <= 0 or market_price <= 0:
        return None

    # Initial guess
    sigma_guess = 0.3
    for _ in range(100):
        d1 = (np.log(S / K) + (r + 0.5 * sigma_guess ** 2) * T) / (sigma_guess * np.sqrt(T))
        d2 = d1 - sigma_guess * np.sqrt(T)

        if option_type == 'call':
            price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:
            price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

        vega = S * norm.pdf(d1) * np.sqrt(T)
        if vega == 0:
            break

        diff = price - market_price
        sigma_guess -= diff / vega

        if abs(diff) < 1e-6:
            return sigma_guess

    return None  # No convergence

# ---------- Core Scanner Functions ----------

def get_spot_price(ticker: str) -> Optional[float]:
    """Retrieve current stock price from yfinance."""
    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            return None
        price = hist['Close'].iloc[-1]
        if pd.isna(price) or price <= 0:
            return None
        return price
    except Exception:
        return None

def get_options_data(ticker: str, spot: float, max_expiries: int = 3) -> Optional[pd.DataFrame]:
    """
    Fetch call option chains for the next few expiries.
    Uses spot passed from scan_ticker for consistency.
    Returns a DataFrame with columns: ['strike','price','volume','gex','iv','expiry_date','type']
    or None on failure.
    """
    try:
        tk = yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            return None

        # Filter to the next max_expiries weekly/monthly
        now = pd.Timestamp.now()
        valid_exps = [e for e in exps if pd.to_datetime(e) > now]
        if not valid_exps:
            return None
        valid_exps = valid_exps[:max_expiries]

        rows = []
        r = 0.05  # risk-free rate (approximate)

        for expiry in valid_exps:
            chain = tk.option_chain(expiry)
            calls = chain.calls.copy()
            T = (pd.to_datetime(expiry) - now).days / 365.0
            if T <= 0:
                continue

            for _, row in calls.iterrows():
                K = row['strike']
                last = row.get('lastPrice')
                bid = row.get('bid')
                ask = row.get('ask')
                # Prefer lastPrice, else mid price
                if pd.notna(last) and last > 0:
                    price = last
                elif bid and ask and bid > 0 and ask > 0:
                    price = (bid + ask) / 2
                else:
                    continue  # skip if no valid price

                volume = row.get('volume', 0)
                oi = row.get('openInterest', 0)
                if pd.isna(volume): volume = 0
                if pd.isna(oi): oi = 0

                # Compute IV using the passed spot
                iv = compute_bs_iv(spot, K, T, r, price, 'call')
                if iv is None or iv < 0.0001:
                    continue

                # GEX = gamma * openInterest * spot * 100
                d1 = (np.log(spot/K) + (r + 0.5*iv**2)*T) / (iv*np.sqrt(T))
                gamma = norm.pdf(d1) / (spot * iv * np.sqrt(T))
                gex = gamma * oi * spot * 100

                rows.append({
                    'strike': K,
                    'price': price,
                    'volume': volume,
                    'oi': oi,
                    'iv': iv,
                    'gex': gex,
                    'expiry_date': expiry,
                    'type': 'call'
                })

        if not rows:
            return None

        return pd.DataFrame(rows)
    except Exception as e:
        logger.error(f"get_options_data error for {ticker}: {e}")
        return None

def find_gamma_walls(opt_df: pd.DataFrame, spot: float) -> List[dict]:
    """
    Detect gamma walls as local GEX maxima with high positive GEX.
    Returns list of walls sorted by proximity to spot.
    """
    if opt_df is None or opt_df.empty:
        return []

    # Group by strike (average if multiple expiries)
    gex_by_strike = opt_df.groupby('strike')['gex'].sum().reset_index()
    gex_by_strike = gex_by_strike.sort_values('strike')

    # Smooth GEX to find local peaks
    window = 5
    gex_by_strike['gex_smooth'] = gex_by_strike['gex'].rolling(window, center=True).mean()
    gex_by_strike['gex_smooth'] = gex_by_strike['gex_smooth'].fillna(0)

    peaks = []
    for i in range(1, len(gex_by_strike)-1):
        left = gex_by_strike.iloc[i-1]['gex_smooth']
        center = gex_by_strike.iloc[i]['gex_smooth']
        right = gex_by_strike.iloc[i+1]['gex_smooth']
        if center > left and center > right and center > 0:
            strike = gex_by_strike.iloc[i]['strike']
            gex_val = gex_by_strike.iloc[i]['gex']
            peaks.append({
                'strike': strike,
                'gex': gex_val,
                'dist_pct': abs(strike - spot) / spot
            })

    # Filter by proximity to spot
    peaks = [p for p in peaks if p['dist_pct'] <= WALL_PROXIMITY]
    # Sort by proximity
    peaks.sort(key=lambda x: x['dist_pct'])
    return peaks

def monte_carlo_cascade(opt_df: pd.DataFrame, spot: float, num_sim: int = 1000) -> dict:
    """
    Simulate gamma squeeze cascade. Returns probability of >15% move.
    Simplified version – uses GEX profile to bias random walks.
    """
    if opt_df is None or opt_df.empty:
        return {'prob_squeeze': 0.0, 'expected_move': 0.0}

    # Calculate net GEX and total OI as a proxy for gamma pressure
    net_gex = opt_df['gex'].sum()
    total_oi = opt_df['oi'].sum()
    if total_oi == 0:
        return {'prob_squeeze': 0.0, 'expected_move': 0.0}

    # Simple model: probability of squeeze proportional to net GEX / spot
    gex_normalized = net_gex / spot
    prob_squeeze = min(max(gex_normalized / 1e6, 0), 1.0)
    expected_move = gex_normalized * 0.01  # arbitrary scaling
    return {'prob_squeeze': prob_squeeze, 'expected_move': expected_move}

def trade_suggestion(walls: List[dict], spot: float, prob: float) -> Optional[str]:
    """
    Generate a human-readable trade suggestion based on gamma walls.
    Walls are expected to be sorted by proximity (distance to spot).
    """
    if not walls or len(walls) < 2:
        return None

    # Use the two nearest walls
    w1 = walls[0]  # nearest wall (buy strike)
    w2 = walls[1]  # second nearest (sell strike)

    # Ensure minimum 5% spread between legs
    spread_pct = abs(w1['strike'] - w2['strike']) / spot
    if spread_pct < 0.05:
        # Force spread to at least 5% by adjusting buy strike downward
        buy_strike = np.floor(spot * 0.95 / 5) * 5  # round down to nearest $5
        sell_strike = np.ceil(spot * 1.05 / 5) * 5  # round up to nearest $5
    else:
        # Use actual wall strikes rounded to nearest $5
        buy_strike = round(w1['strike'] / 5) * 5
        sell_strike = round(w2['strike'] / 5) * 5
        if sell_strike <= buy_strike:
            sell_strike = buy_strike + 5

    gex_ratio = w1['gex'] / (w1['gex'] + w2['gex'] + 1e-9)

    if prob > 0.5:
        strategy = f"BUY ${int(buy_strike)} CALL, SELL ${int(sell_strike)} CALL debit spread (bullish)"
    else:
        strategy = f"SELL ${int(sell_strike)} CALL, BUY ${int(buy_strike)} CALL credit spread (bearish)"
    return strategy

def scan_ticker(ticker: str) -> Optional[dict]:
    """
    Full scan for a single ticker. Returns a dict of signals or None if rejected.
    """
    # ---- DIAGNOSTIC: flag first ticker ----
    DIAGNOSTIC = (ticker == "AAPL")

    # 1) Get spot price
    S = get_spot_price(ticker)
    if S is None:
        logger.info(f"⚠️ Spot price invalid for {ticker}")
        return None
    if DIAGNOSTIC:
        print(f"[DIAG] {ticker} spot = {S:.2f}")

    # 2) Get options data (pass spot)
    opt_df = get_options_data(ticker, S)
    if opt_df is None:
        logger.info(f"⚠️ No options data for {ticker}")
        return None
    if DIAGNOSTIC:
        print(f"[DIAG] {ticker} options data shape: {opt_df.shape}")
        print(f"[DIAG] Columns: {opt_df.columns.tolist()}")
        print(f"[DIAG] First 5 rows:\n{opt_df.head()}")

    # 3) Compute average IV
    avg_iv = opt_df['iv'].mean()
    if DIAGNOSTIC:
        print(f"[DIAG] {ticker} avg IV = {avg_iv:.6f} (from {len(opt_df)} options)")

    if avg_iv < MIN_IV_FILTER:
        logger.info(f"⚠️ IV too low for {ticker}: {avg_iv}")
        # Additional diagnostic: show IV distribution
        if DIAGNOSTIC:
            print(f"[DIAG] IV distribution (min,25,50,75,max): {opt_df['iv'].describe()[['min','25%','50%','75%','max']].values}")
        return None

    # 4) Find gamma walls
    walls = find_gamma_walls(opt_df, S)
    if not walls:
        logger.info(f"⚠️ No gamma walls for {ticker}")
        return None
    if DIAGNOSTIC:
        print(f"[DIAG] Found {len(walls)} walls: {[(w['strike'], w['gex']) for w in walls[:3]]}")

    # 5) Monte Carlo cascade
    mc = monte_carlo_cascade(opt_df, S)
    if mc['prob_squeeze'] == 0:
        logger.info(f"⚠️ Monte Carlo zero probability for {ticker}")
        return None

    # 6) Trade suggestion
    trade = trade_suggestion(walls, S, mc['prob_squeeze'])
    if trade is None:
        logger.info(f"⚠️ No trade suggestion for {ticker}")
        return None

    # 7) Build signal
    signal = {
        'ticker': ticker,
        'spot': S,
        'avg_iv': avg_iv,
        'walls': walls,
        'mc': mc,
        'trade': trade
    }
    return signal

# ---------- Main Scanner Runner ----------

def run_scanner():
    """Main orchestration function."""
    logger.info(f"Starting gamma scanner on {len(SP500_TICKERS)} tickers...")
    all_signals = []

    # Collect all signals first (to control Telegram order)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scan_ticker, ticker): ticker for ticker in SP500_TICKERS}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result(timeout=60)
                if result is not None:
                    all_signals.append(result)
                    logger.info(f"✅ Signal found: {ticker}")
                else:
                    logger.info(f"⚠️ {ticker} – no signal")
            except Exception as e:
                logger.error(f"❌ Error scanning {ticker}: {e}")
            # Rate limiter
            time.sleep(OPTION_DELAY)

    # Send daily summary BEFORE individual alerts (bug fix #2)
    summary = f"*Gamma Squeeze Scanner Summary*\n"
    summary += f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    summary += f"Scanned: {len(SP500_TICKERS)} tickers\n"
    summary += f"Signals found: {len(all_signals)}\n"
    summary += f"Top picks:\n"
    sorted_signals = sorted(all_signals, key=lambda x: x['mc']['prob_squeeze'], reverse=True)
    for s in sorted_signals[:5]:
        summary += f"  • {s['ticker']} @ ${s['spot']:.2f} | squeeze prob: {s['mc']['prob_squeeze']:.1%}\n"
    send_telegram_message(summary)

    # Now send each individual alert
    for sig in sorted_signals:
        msg = f"*Gamma Signal*: {sig['ticker']}\n"
        msg += f"Spot: ${sig['spot']:.2f}\n"
        msg += f"Avg IV: {sig['avg_iv']:.1%}\n"
        msg += f"Squeeze Probability: {sig['mc']['prob_squeeze']:.1%}\n"
        msg += f"Expected Move: {sig['mc']['expected_move']:.1%}\n"
        nearest_wall = sig['walls'][0]
        msg += f"Nearest Gamma Wall: ${nearest_wall['strike']:.2f} (GEX: {nearest_wall['gex']:.0f})\n"
        msg += f"Trade: {sig['trade']}\n"
        send_telegram_message(msg)

    logger.info(f"Scanner finished. {len(all_signals)} signals found.")

# ---------- Entry Point ----------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler("gamma_scanner.log"),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)
    run_scanner()
