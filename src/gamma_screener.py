import logging
import pandas as pd
import numpy as np
from finvizfinance.screener.overview import Overview
from finvizfinance.screener.performance import Performance
import yfinance as yf
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 1. GAMMA CALCULATION HELPER
# ──────────────────────────────────────────────
def get_gamma_exposure(ticker, expiration_range='week'):
    """
    Fetch option chain for the nearest expiry and compute gamma exposure.
    Returns total gamma (sum of gamma * open_interest * 100) and max gamma strike.
    """
    try:
        stock = yf.Ticker(ticker)
        exps = stock.options
        if not exps:
            return 0, None, pd.DataFrame()

        # Choose nearest expiry
        if expiration_range == 'week':
            target_days = 7
        elif expiration_range == 'month':
            target_days = 30
        else:
            target_days = 7

        now = datetime.now()
        best_exp = None
        best_diff = 999
        for e in exps:
            exp_date = datetime.strptime(e, '%Y-%m-%d')
            diff = (exp_date - now).days
            if 0 <= diff <= target_days * 2 and abs(diff - target_days) < best_diff:
                best_diff = abs(diff - target_days)
                best_exp = e

        if not best_exp:
            return 0, None, pd.DataFrame()

        opt = stock.option_chain(best_exp)
        calls = opt.calls.copy()
        puts = opt.puts.copy()

        # Calculate gamma exposure per strike: gamma * openInterest * 100
        calls['GammaExposure'] = calls['gamma'] * calls['openInterest'] * 100
        puts['GammaExposure'] = puts['gamma'] * puts['openInterest'] * 100
        calls['Type'] = 'Call'
        puts['Type'] = 'Put'

        combined = pd.concat([calls[['strike', 'gamma', 'openInterest', 'GammaExposure', 'Type']],
                              puts[['strike', 'gamma', 'openInterest', 'GammaExposure', 'Type']]])

        total_gamma = combined['GammaExposure'].sum()
        max_gamma_strike = combined.loc[combined['GammaExposure'].idxmax(), 'strike'] if not combined.empty else None

        return total_gamma, max_gamma_strike, combined

    except Exception as e:
        logger.warning(f"Gamma calc failed for {ticker}: {e}")
        return 0, None, pd.DataFrame()

# ──────────────────────────────────────────────
# 2. FINVIZ SCREENING (fixed)
# ──────────────────────────────────────────────
def gamma_squeeze_screener():
    logger.info("Starting gamma squeeze scanner...")

    try:
        f = Overview()
        filters = {
            'Volume': 'Over 2M',
            'Market Cap.': 'Small',          # adjust as needed
            'Relative Volume': 'Over 1.5',
            'Short Float': 'Over 10%',
            'Price': 'Over $5',
            'Change': 'Over 5%'
        }
        f.set_filter(filters=filters)
        candidates = f.screener_view()
    except Exception as e:
        logger.error(f"Finviz screening failed: {e}")
        logger.error("No tickers from Finviz. Exiting.")
        return pd.DataFrame()

    if candidates.empty:
        logger.info("No tickers matched the filters.")
        return pd.DataFrame()

    logger.info(f"Found {len(candidates)} potential candidates: {candidates['Ticker'].tolist()}")
    return candidates

# ──────────────────────────────────────────────
# 3. MAIN ANALYSIS
# ──────────────────────────────────────────────
def analyze_gamma_squeeze(candidates_df):
    results = []

    for idx, row in candidates_df.iterrows():
        ticker = row['Ticker']
        logger.info(f"Analyzing {ticker}...")

        total_gamma, max_gamma_strike, chain = get_gamma_exposure(ticker)

        # Fetch current price & short interest from Finviz data
        price = row.get('Price', np.nan)
        short_float_pct = row.get('Short Float', '0%')
        volume = row.get('Volume', 0)

        # Clean short float percentage
        try:
            short_float_val = float(short_float_pct.strip('%'))
        except:
            short_float_val = 0.0

        # Relative volume indicator (crude: volume / avg volume)
        rel_vol = row.get('Rel Volume', 1)

        # Gamma squeeze score (heuristic)
        gamma_score = 0
        if total_gamma > 1e6:
            gamma_score += 3
        elif total_gamma > 5e5:
            gamma_score += 2
        elif total_gamma > 1e5:
            gamma_score += 1

        # Short interest score
        short_score = 0
        if short_float_val > 30:
            short_score = 3
        elif short_float_val > 20:
            short_score = 2
        elif short_float_val > 10:
            short_score = 1

        # Volume score
        vol_score = 0
        if volume > 10_000_000:
            vol_score = 3
        elif volume > 5_000_000:
            vol_score = 2
        elif volume > 2_000_000:
            vol_score = 1

        total_score = gamma_score + short_score + vol_score

        results.append({
            'Ticker': ticker,
            'Price': price,
            'Short Float %': short_float_val,
            'Volume': volume,
            'Gamma Exposure': round(total_gamma, 0),
            'Max Gamma Strike': max_gamma_strike,
            'Gamma Score': gamma_score,
            'Short Score': short_score,
            'Vol Score': vol_score,
            'Total Squeeze Score': total_score
        })

        # Avoid hammering Yahoo Finance
        pd.Timestamp('0.5s')

    return pd.DataFrame(results)

# ──────────────────────────────────────────────
# 4. OUTPUT
# ──────────────────────────────────────────────
def main():
    # Step 1: Screen Finviz
    candidates_df = gamma_squeeze_screener()
    if candidates_df.empty:
        return

    # Step 2: Analyze each candidate
    logger.info("Analyzing options gamma for each candidate...")
    results_df = analyze_gamma_squeeze(candidates_df)

    # Step 3: Sort and display
    results_df.sort_values('Total Squeeze Score', ascending=False, inplace=True)

    print("\n" + "="*80)
    print("GAMMA SQUEEZE SCANNER RESULTS")
    print("="*80)
    print(results_df.to_string(index=False))

    # Optional: save to CSV
    results_df.to_csv('gamma_squeeze_candidates.csv', index=False)
    logger.info("Results saved to gamma_squeeze_candidates.csv")

if __name__ == "__main__":
    main()
