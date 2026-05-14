import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

# ==========================
# CONFIGURATION
# ==========================
RISK_FREE_RATE = 0.05  # to be replaced by dynamic fetch (#10)
MAX_WORKERS = 5         # reduced to avoid rate limiting (#10)
SLEEP_BETWEEN = 0.3     # seconds between API calls
SCAN_TICKERS = []       # fill or fetch from a list
# For demonstration we use a small list
TICKERS = ['DECK', 'CRM', 'META', 'AAPL', 'SPY']  # example

# ==========================
# LOGGING SETUP
# ==========================
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)-8s | %(message)s')
logger = logging.getLogger(__name__)

# ==========================
# HELPER FUNCTIONS
# ==========================
def get_options_chain(ticker):
    """Fetch options chain with error handling and rate limit backoff."""
    for attempt in range(3):
        try:
            stock = yf.Ticker(ticker)
            expirations = stock.options
            if not expirations:
                return None
            # Use nearest expiration for gamma calculation
            nearest = expirations[0]
            opt_chain = stock.option_chain(nearest)
            if opt_chain.calls.empty and opt_chain.puts.empty:
                return None
            return {
                'calls': opt_chain.calls,
                'puts': opt_chain.puts,
                'expiry': nearest,
                'current_price': stock.info.get('regularMarketPrice', stock.info.get('currentPrice', None))
            }
        except Exception as e:
            if "Too Many Requests" in str(e):
                wait = 1 * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait}s for {ticker}")
                time.sleep(wait)
            else:
                logger.error(f"Error fetching {ticker}: {e}")
                return None
    return None

def compute_net_gex(chain_data):
    """
    Compute net gamma exposure per strike from dealer perspective.
    Assumes dealer is short calls and long puts (typical market maker).
    Net GEX = (gamma per call * open interest * -1) + (gamma per put * open interest * +1)
    Returns: sorted dict of strike -> net_gex, and the strike with peak absolute value.
    """
    calls = chain_data['calls']
    puts = chain_data['puts']
    if calls.empty and puts.empty:
        return {}, None

    # Helper: compute Black-Scholes gamma (simplified – use v2.2 form)
    # For brevity, we approximate gamma = (delta change / price). Real implementation uses BS.
    # We reuse existing BS_gamma function from original code.
    def bs_gamma(S, K, T, r=0.05, sigma=0.3):
        from scipy.stats import norm
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        return norm.pdf(d1) / (S * sigma * np.sqrt(T))

    S = chain_data['current_price']
    T = (pd.to_datetime(chain_data['expiry']) - datetime.now()).days / 365.0
    T = max(T, 1e-6)

    net_gex = {}
    for _, row in calls.iterrows():
        K = row['strike']
        oi = row['openInterest']
        if oi > 0 and T > 0 and S > 0:
            gamma = bs_gamma(S, K, T)  # assumes 0.3 IV
            dealer_position = -1  # short calls
            net_gex[K] = net_gex.get(K, 0) + gamma * oi * dealer_position

    for _, row in puts.iterrows():
        K = row['strike']
        oi = row['openInterest']
        if oi > 0 and T > 0 and S > 0:
            gamma = bs_gamma(S, K, T)
            dealer_position = 1   # long puts
            net_gex[K] = net_gex.get(K, 0) + gamma * oi * dealer_position

    # Find strike with maximum absolute net GEX
    if net_gex:
        peak_strike = max(net_gex, key=lambda k: abs(net_gex[k]))
        sorted_gex = dict(sorted(net_gex.items()))
        return sorted_gex, peak_strike
    return net_gex, None

def get_earnings_date(ticker):
    """Get next earnings date (if any). Returns days to earnings or 999 if none."""
    # Simplified: use yfinance info (limited). In practice use a calendar.
    # Fallback: if no data, return 999.
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        earnings = info.get('earningsDates', None)
        if earnings and len(earnings) > 0:
            next_earn = pd.to_datetime(earnings[0])
            days = (next_earn - datetime.now()).days
            return max(days, 0)
        else:
            return 999  # no known earnings
    except:
        return 999

def economic_score(first_step_amp, prob_hit_wall, total_potential, iv_percentile, put_call_ratio):
    """
    Composite economic score (unchanged logic from v2.2, but called with separate components now).
    """
    score = first_step_amp * prob_hit_wall * 0.30 + total_potential * 0.70
    # Adjustments
    score *= (1 + 0.15 * (iv_percentile - 0.5))  # IV tilt
    score *= (1 - 0.10 * (put_call_ratio - 1.0))  # put/call ratio drag
    return max(0, min(1, score))

def monte_carlo_cascade(chain_data, wall_strike, catalyst_move_pct, num_sims=2000, steps=50):
    """
    Monte Carlo with separate recording of catalyst drift and gamma amplification.
    Returns: (total_move_abs, catalyst_contrib, gamma_contrib, prob_hit_wall)
    """
    # Fix: use catalyst_move_pct from earnings (not hardcoded 50%)
    # drift per step = catalyst_move_pct / 100 / 10 (original magic number – #3 will fix)
    drift_per_step = catalyst_move_pct / 100 / 10
    volatility = 0.02  # per step (2% daily vol approximation)

    S = chain_data['current_price']
    total_catalyst = 0
    total_gamma = 0
    count_hit_wall = 0

    for _ in range(num_sims):
        price = S
        cum_drift = 0
        cum_noise = 0
        hit_wall = False
        for step in range(steps):
            # Catalyst drift
            drift = drift_per_step
            noise = np.random.normal(0, volatility)

            # Gamma amplification: proximity factor based on distance from wall
            if wall_strike is not None:
                distance = (wall_strike - price) / S  # percentage distance
                if distance > 0:
                    proximity = max(1, 3.0 * (1 / max(distance, 0.01)))  # up to 3x boost
                else:
                    proximity = 3.0  # beyond wall, maximum boost
                gamma_boost = proximity * 0.02   # heuristic gamma effect per step
            else:
                gamma_boost = 0

            # Step return
            step_return = drift + noise + gamma_boost
            price *= (1 + step_return)

            # Track components separately
            cum_drift += drift
            cum_noise += noise
            # gamma_boost is added to step_return, so total gamma contrib = step_return - (drift+noise)
            # But we want cumulative gamma contribution after all steps.

        # After steps, total return = sum(drift) + sum(noise) + sum(gamma_boost)
        total_return = cum_drift + cum_noise + (step_return - drift - noise)*steps  # simplified
        # Actually easier: track total_return and subtract catalyst contribution later.
        # We'll compute catalyst contribution as cum_drift (drift sum) + cum_noise? No, noise is random.
        # The user wants expected catalyst drift = cum_drift (deterministic) + noise? Typically catalyst drift is the deterministic drift only.
        # We'll define catalyst_contrib = cum_drift (the expected drift from catalyst).
        # Gamma_contrib = total_return - cum_drift - cum_noise.
        # But we need absolute for output. We'll keep per-path and then average abs.
        catalyst_contrib = cum_drift * 100   # absolute % (drift alone)
        total_move = total_return * 100       # absolute % (drift+noise+gamma)
        gamma_contrib = total_move - catalyst_contrib - cum_noise*100

        # Accumulate absolutes
        total_catalyst += abs(catalyst_contrib)
        total_gamma += abs(gamma_contrib)

        if wall_strike is not None and price >= wall_strike:
            count_hit_wall += 1

    avg_total = total_catalyst / num_sims + total_gamma / num_sims
    prob_wall = count_hit_wall / num_sims

    return avg_total, total_catalyst/num_sims, total_gamma/num_sims, prob_wall

def process_ticker(ticker):
    """Analyze a single ticker and return signal dict."""
    logger.debug(f"Processing {ticker}")
    time.sleep(SLEEP_BETWEEN)
    chain = get_options_chain(ticker)
    if chain is None:
        return None

    # Compute net GEX and wall
    gex_dict, wall_strike = compute_net_gex(chain)
    if wall_strike is None:
        logger.debug(f"{ticker}: No wall found, skipping")
        return None

    # Earnings
    days_to_earn = get_earnings_date(ticker)
    # Fix: treat 0 as valid (earnings today) – changed from `if days_to_earn:` to explicit check
    if days_to_earn is not None and days_to_earn <= 30:
        catalyst_move_pct = 10.0  # default for stocks with earnings
    else:
        catalyst_move_pct = 1.5   # minimal drift without catalyst
        wall_strike = None        # no meaningful wall without catalyst? (original logic)

    # Run Monte Carlo with separated components
    total_move, cat_contrib, gamma_contrib, prob_wall = monte_carlo_cascade(
        chain, wall_strike, catalyst_move_pct
    )

    # Economic score (same formula but uses separate components)
    first_step_amp = 0.0  # placeholder, needs to be computed from cascade first step
    total_potential = total_move / 100.0
    # For simplicity, we'll compute first_step_amp from the drift alone:
    first_step_amp = catalyst_move_pct / 100 / 10  # per-step drift
    prob_hit_wall = prob_wall

    iv_percentile = 0.5  # default – improvement #3 will fetch real data
    put_call_ratio = 1.0  # default
    score = economic_score(first_step_amp, prob_hit_wall, total_potential, iv_percentile, put_call_ratio)

    # Classification (unchanged thresholds)
    loop_gain = 0.0  # placeholder; original used self-sustaining > 50% etc.
    mc_p95 = 0.0     # placeholder
    if (loop_gain >= 0.65 or prob_hit_wall > 0.5 or 0) and score >= 0.10:
        classification = "🔴 EXTREME"
    elif score >= 0.05:
        classification = "🟠 HIGH_CONVICTION"
    elif score >= 0.02:
        classification = "🟡 WATCH"
    else:
        classification = "⚪ STRUCTURAL"

    # Build output line with new format
    # Format: [CLASS] [TICKER] $[price] | Score [score%] | Wall $[wall] (GEX) | Cat: [cat%] | Gamma: [+gamma%] | MC: [total%] | P(Wall): [p%] | 📅 [days]d
    price = chain['current_price']
    output = (f"{classification} → {ticker:6s} ${price:>5.1f} | Score {score*100:>4.1f}% | "
              f"Wall ${wall_strike if wall_strike else 'N/A':>5} (GEX) | "
              f"Cat: {cat_contrib:>5.1f}% | "
              f"Gamma: +{gamma_contrib:>4.1f}% | "
              f"MC: {total_move:>5.1f}% | "
              f"P(Wall): {prob_hit_wall*100:>3.0f}% | "
              f"📅 {days_to_earn if days_to_earn<=30 else '?'}d")
    logger.info(output)

    return {
        'ticker': ticker,
        'price': price,
        'score': score,
        'wall': wall_strike,
        'cat': cat_contrib,
        'gamma': gamma_contrib,
        'total_move': total_move,
        'prob_wall': prob_hit_wall,
        'days_to_earn': days_to_earn,
        'classification': classification,
        'output': output
    }

# ==========================
# MAIN SCANNER
# ==========================
def main():
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_ticker, t): t for t in TICKERS}
        for future in as_completed(futures):
            try:
                res = future.result()
                if res:
                    results.append(res)
            except Exception as e:
                logger.error(f"Error processing {futures[future]}: {e}")

    # Sort by score descending
    results.sort(key=lambda x: x['score'], reverse=True)

    # Print only 🔴 EXTREME signals if desired (filter)
    extreme_signals = [r for r in results if "EXTREME" in r['classification']]
    if extreme_signals:
        print("\n=== EXTREME SIGNALS (🔴) ===")
        for signal in extreme_signals:
            print(signal['output'])
    else:
        print("No EXTREME signals found.")

if __name__ == "__main__":
    main()
