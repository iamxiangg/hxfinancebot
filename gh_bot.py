#!/usr/bin/env python3
"""
gh_bot.py — Streamlined GitHub Actions Version
===============================================
Monitors portfolio drift and sends Telegram alerts.
No IBKR connection needed.

How it works:
1. Reads current holdings from holdings.json (checked into repo)
2. Fetches market data from Yahoo Finance
3. Runs the same 3-layer strategy (EWMA vol → Trend → Breadth)
4. Compares current allocation vs target
5. Sends Telegram alert if drift > ±5%
6. You update holdings.json after each manual Pi trade run

Files needed in repo:
  - gh_bot.py           (this file)
  - holdings.json       (your current positions — you update this)
  - .github/workflows/daily_check.yml  (GH Actions schedule)

Secrets needed in GitHub:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID
"""

import os
import sys
import json
import math
import time
import logging
import argparse
import pandas as pd
import yfinance as yf
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

NASDAQ_PCT = 0.85
TARGET_VOL = 0.20
VOL_LOOKBACK = 20
EWMA_LAMBDA = 0.95
MAX_LEVERAGE = 3.0
TREND_CAP = 2.0
MIN_VOL_FLOOR = 0.001
MANAGED_FUTURES_PCT = 0.15
BREADTH_TICKERS = ['SPY', 'QQQ', 'IWM', 'EFA']
BREADTH_THRESHOLD = 2
REBALANCE_BAND = 0.05
USD_CASH_TARGET = 7.50

# Tickers
QQQ_TICKER = 'QQQ'
SPY_TICKER = 'SPY'
IWM_TICKER = 'IWM'
EFA_TICKER = 'EFA'
KMLM_TICKER = 'KMLM'
SGOV_TICKER = 'SGOV'
TQQQ_TICKER = 'TQQQ'
QLD_TICKER = 'QLD'
VIX_TICKER = '^VIX'
IAU_TICKER = 'IAU'

# All possible holdings (strategy + orphan candidates)
ALL_TICKERS = ['QQQ', 'TQQQ', 'QLD', 'KMLM', 'SGOV', 'SPY', 'IWM', 'EFA', 'IAU']

HOLDINGS_FILE = 'holdings.json'

# ─── Helpers ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Send notification via Telegram bot using env vars."""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return
    try:
        import requests
        if len(message) > 4000:
            message = message[:3997] + "..."
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

def load_holdings() -> Dict:
    """Load current holdings from holdings.json with validation."""
    try:
        with open(HOLDINGS_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("holdings.json must be a dict")
        if 'positions' not in data:
            data['positions'] = {}
        if 'usd_cash' not in data:
            data['usd_cash'] = 0.0
        # Validate numeric types
        for ticker, shares in data['positions'].items():
            if not isinstance(shares, (int, float)):
                raise ValueError(f"Position {ticker}: shares must be numeric, got {type(shares).__name__}")
            data['positions'][ticker] = float(shares)
        if not isinstance(data['usd_cash'], (int, float)):
            raise ValueError(f"usd_cash must be numeric, got {type(data['usd_cash']).__name__}")
        data['usd_cash'] = float(data['usd_cash'])
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"{HOLDINGS_FILE} error: {e}")
        return {'positions': {}, 'usd_cash': 0.0, 'last_updated': None}

def load_state() -> Dict:
    try:
        with open('gh_bot_state.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(state: Dict):
    with open('gh_bot_state.json', 'w') as f:
        json.dump(state, f, indent=2)

# ─── Yahoo Finance ─────────────────────────────────────────────────────────

def yf_download_with_retry(ticker: str, period: str = '5d', max_retries: int = 3) -> pd.DataFrame:
    last_error = None
    for attempt in range(max_retries):
        try:
            data = yf.download(ticker, period=period, interval='1d')
            if not data.empty:
                return data
            logger.warning(f"{ticker}: empty data on attempt {attempt+1}/{max_retries}")
        except Exception as e:
            last_error = e
            logger.warning(f"{ticker}: error on attempt {attempt+1}/{max_retries}: {e}")
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    if last_error:
        raise last_error
    raise ValueError(f"{ticker}: no data after {max_retries} attempts")

def extract_close(data: pd.DataFrame, label: str) -> float:
    close_col = data['Close']
    if isinstance(close_col, pd.DataFrame):
        return float(close_col.iloc[-1, 0])
    return float(close_col.iloc[-1])

def extract_sma200(data: pd.DataFrame, label: str) -> Optional[float]:
    close_col = data['Close']
    if isinstance(close_col, pd.DataFrame):
        series = close_col.iloc[:, 0]
    else:
        series = close_col
    if len(series) < 200:
        logger.warning(f"{label}: only {len(series)} rows, cannot compute SMA200")
        return None
    sma = series.rolling(window=200).mean().iloc[-1]
    if pd.isna(sma):
        return None
    return float(sma)

def extract_realized_vol(data: pd.DataFrame, label: str, lookback: int = 20) -> float:
    close_col = data['Close']
    if isinstance(close_col, pd.DataFrame):
        series = close_col.iloc[:, 0]
    else:
        series = close_col
    returns = series.pct_change().dropna()
    if len(returns) < lookback:
        logger.warning(f"{label}: only {len(returns)} returns, vol estimate may be unstable")
    ewma_variance = returns.ewm(alpha=1 - EWMA_LAMBDA, min_periods=lookback).var()
    realized_vol = float(pow(ewma_variance.iloc[-1] * 252, 0.5))
    return max(realized_vol, MIN_VOL_FLOOR)

def get_market_data() -> Dict:
    """
    Fetch ALL needed prices from Yahoo Finance with retry logic.
    Includes all tickers that could be held (strategy + orphan candidates).
    """
    logger.info("Fetching market data from Yahoo Finance")

    # Strategy-critical tickers (fail on these)
    qqq = yf_download_with_retry(QQQ_TICKER, period='2y')
    qqq_price = extract_close(qqq, QQQ_TICKER)
    qqq_sma200 = extract_sma200(qqq, QQQ_TICKER)
    qqq_realized_vol = extract_realized_vol(qqq, QQQ_TICKER, VOL_LOOKBACK)

    spy = yf_download_with_retry(SPY_TICKER, period='2y')
    spy_price = extract_close(spy, SPY_TICKER)
    spy_sma200 = extract_sma200(spy, SPY_TICKER)

    iwm = yf_download_with_retry(IWM_TICKER, period='2y')
    iwm_price = extract_close(iwm, IWM_TICKER)
    iwm_sma200 = extract_sma200(iwm, IWM_TICKER)

    efa = yf_download_with_retry(EFA_TICKER, period='2y')
    efa_price = extract_close(efa, EFA_TICKER)
    efa_sma200 = extract_sma200(efa, EFA_TICKER)

    try:
        vix = yf_download_with_retry(VIX_TICKER, period='5d')
        vix_current = extract_close(vix, VIX_TICKER)
    except Exception:
        logger.warning("VIX data unavailable")
        vix_current = 0.0

    tqqq = yf_download_with_retry(TQQQ_TICKER, period='5d')
    tqqq_price = extract_close(tqqq, TQQQ_TICKER)

    qld = yf_download_with_retry(QLD_TICKER, period='5d')
    qld_price = extract_close(qld, QLD_TICKER)

    # Non-critical tickers (use defaults on failure, but warn)
    try:
        kmlm = yf_download_with_retry(KMLM_TICKER, period='5d')
        kmlm_price = extract_close(kmlm, KMLM_TICKER)
    except Exception as e:
        logger.error(f"KMLM data unavailable: {e}. Defaulting to $50. HOLDINGS MAY BE WRONG!")
        kmlm_price = 50.0

    try:
        sgov = yf_download_with_retry(SGOV_TICKER, period='5d')
        sgov_price = extract_close(sgov, SGOV_TICKER)
    except Exception as e:
        logger.error(f"SGOV data unavailable: {e}. Defaulting to $100. HOLDINGS MAY BE WRONG!")
        sgov_price = 100.0

    # Orphan tickers (use defaults on failure, warn)
    try:
        iau = yf_download_with_retry(IAU_TICKER, period='5d')
        iau_price = extract_close(iau, IAU_TICKER)
    except Exception as e:
        logger.warning(f"IAU data unavailable: {e}. Defaulting to $86.")
        iau_price = 86.0

    return {
        'qqq_price': qqq_price,
        'qqq_sma200': qqq_sma200,
        'qqq_realized_vol': qqq_realized_vol,
        'spy_price': spy_price,
        'spy_sma200': spy_sma200,
        'iwm_price': iwm_price,
        'iwm_sma200': iwm_sma200,
        'efa_price': efa_price,
        'efa_sma200': efa_sma200,
        'tqqq_price': tqqq_price,
        'qld_price': qld_price,
        'kmlm_price': kmlm_price,
        'sgov_price': sgov_price,
        'iau_price': iau_price,
        'vix': vix_current,
    }

def build_prices_dict(market_data: Dict) -> Dict[str, float]:
    """
    Build a complete price dict for ALL possible tickers
    that could be held (strategy + orphan candidates).
    """
    prices = {
        'QQQ': market_data['qqq_price'],
        'TQQQ': market_data['tqqq_price'],
        'QLD': market_data['qld_price'],
        'KMLM': market_data['kmlm_price'],
        'SGOV': market_data['sgov_price'],
        'SPY': market_data['spy_price'],
        'IWM': market_data['iwm_price'],
        'EFA': market_data['efa_price'],
        'IAU': market_data['iau_price'],
    }
    return prices

# ─── Strategy (identical to main bot) ─────────────────────────────────────

def determine_allocations(market_data: Dict) -> Tuple[Dict[str, float], Dict]:
    """
    Three-layer strategy — exactly the same logic as main trade_bot.py.
    """
    realized_vol = market_data['qqq_realized_vol']
    qqq_price = market_data['qqq_price']
    qqq_sma200 = market_data.get('qqq_sma200')
    qqq_above_sma = qqq_price > qqq_sma200 if qqq_sma200 is not None else True

    # Layer 1 — Vol Targeting
    raw_leverage = min(MAX_LEVERAGE, max(0.0, TARGET_VOL / realized_vol))

    # Layer 2 — Trend Filter
    trend_capped_leverage = raw_leverage
    if qqq_sma200 is not None and not qqq_above_sma:
        trend_capped_leverage = min(raw_leverage, TREND_CAP)

    # Layer 3 — Breadth Momentum
    valid_count = 0
    breadth_count = 0
    for ticker in BREADTH_TICKERS:
        ticker_lower = ticker.lower()
        sma_key = f'{ticker_lower}_sma200'
        price_key = f'{ticker_lower}_price'
        ticker_sma = market_data.get(sma_key)
        ticker_price = market_data.get(price_key, 0)
        if ticker_sma is None:
            continue
        valid_count += 1
        if ticker_price > ticker_sma:
            breadth_count += 1

    if valid_count == 0:
        breadth_pass = True
    else:
        adjusted_threshold = max(1, math.ceil(valid_count * (BREADTH_THRESHOLD / len(BREADTH_TICKERS))))
        breadth_pass = breadth_count >= adjusted_threshold

    final_leverage = 0.0 if not breadth_pass else trend_capped_leverage

    # Select vehicle + reason string
    if final_leverage >= 2.5:
        nasdaq_ticker = 'TQQQ'
        nasdaq_reason = f"Bull — Vol={realized_vol*100:.1f}%, Leverage={final_leverage:.2f}x, Breadth={breadth_count}/{valid_count or '?'}"
    elif final_leverage >= 1.5:
        nasdaq_ticker = 'QLD'
        nasdaq_reason = f"Moderate — Vol={realized_vol*100:.1f}%, Leverage={final_leverage:.2f}x, Breadth={breadth_count}/{valid_count or '?'}"
    elif final_leverage >= 0.5:
        nasdaq_ticker = 'QQQ'
        nasdaq_reason = f"Low — Vol={realized_vol*100:.1f}%, Leverage={final_leverage:.2f}x, Breadth={breadth_count}/{valid_count or '?'}"
    else:
        nasdaq_ticker = 'SGOV'
        if not breadth_pass:
            nasdaq_reason = f"Panic Cash — Breadth={breadth_count}/{valid_count or '?'} below threshold, Vol={realized_vol*100:.1f}%"
        else:
            nasdaq_reason = f"Cash — Vol={realized_vol*100:.1f}%, Leverage={final_leverage:.2f}x, Breadth={breadth_count}/{valid_count or '?'}"

    targets = {
        nasdaq_ticker: NASDAQ_PCT,
        KMLM_TICKER: MANAGED_FUTURES_PCT,
    }

    decision_trace = {
        'realized_vol': realized_vol,
        'raw_leverage': raw_leverage,
        'qqq_above_sma': qqq_above_sma if qqq_sma200 is not None else None,
        'final_leverage': final_leverage,
        'breadth_count': breadth_count,
        'valid_count': valid_count,
        'breadth_pass': breadth_pass,
        'nasdaq_ticker': nasdaq_ticker,
        'nasdaq_reason': nasdaq_reason,
    }

    return targets, decision_trace

# ─── Report Builder ─────────────────────────────────────────────────────────

def build_report(
    market_data: Dict,
    holdings: Dict,
    total_value: float,
    investable_value: float,
    target_allocations: Dict[str, float],
    decision_trace: Dict,
    holdings_value_pct: Dict[str, float],
    cash_pct: float,
    drift_alert: bool,
    drift_details: List[str],
    prices: Dict[str, float],
) -> str:
    """Build a compact Telegram report."""
    realized_vol = decision_trace.get('realized_vol', 0)
    qqq_price = market_data.get('qqq_price', 0)
    qqq_sma200 = market_data.get('qqq_sma200')
    qqq_above = qqq_price > qqq_sma200 if qqq_sma200 is not None else None
    vix = market_data.get('vix', 0)
    nasdaq_ticker = decision_trace.get('nasdaq_ticker', '')
    nasdaq_reason = decision_trace.get('nasdaq_reason', '')
    leverage = decision_trace.get('final_leverage', 0)
    breadth_count = decision_trace.get('breadth_count', 0)
    valid_count = decision_trace.get('valid_count', 0)
    breadth_pass = decision_trace.get('breadth_pass', True)
    spy_price = market_data.get('spy_price', 0)
    spy_sma200 = market_data.get('spy_sma200')
    spy_above = spy_price > spy_sma200 if spy_sma200 is not None else None
    iwm_price = market_data.get('iwm_price', 0)
    iwm_sma200 = market_data.get('iwm_sma200')
    iwm_above = iwm_price > iwm_sma200 if iwm_sma200 is not None else None
    efa_price = market_data.get('efa_price', 0)
    efa_sma200 = market_data.get('efa_sma200')
    efa_above = efa_price > efa_sma200 if efa_sma200 is not None else None

    lines = []
    lines.append("📊 Portfolio Monitor (GitHub Actions)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("")

    # Market Summary
    lines.append("📈 Market Data:")
    if qqq_above is True:
        lines.append(f"• QQQ ${qqq_price:.2f} — 📈 Above SMA200 (${qqq_sma200:.2f})")
    elif qqq_above is False:
        lines.append(f"• QQQ ${qqq_price:.2f} — 📉 Below SMA200 (${qqq_sma200:.2f})")
    else:
        lines.append(f"• QQQ ${qqq_price:.2f} — ⚠️ SMA200 unavailable")
    lines.append(f"• Realized Vol (EWMA0.95): {realized_vol*100:.1f}%  |  VIX: {vix:.2f}")

    for ticker, label, p, s in [('SPY', 'SPY', spy_price, spy_sma200),
                                  ('IWM', 'IWM', iwm_price, iwm_sma200),
                                  ('EFA', 'EFA', efa_price, efa_sma200)]:
        if s is not None:
            icon = "✅" if p > s else "❌"
            lines.append(f"• {label} ${p:.2f} — {icon} SMA200 ${s:.2f}")
        else:
            lines.append(f"• {label} ${p:.2f} — ⚠️ SMA200 unavailable")

    kmlm_price = prices.get('KMLM', 0)
    lines.append(f"• KMLM ${kmlm_price:.2f}")
    lines.append(f"• Breadth: {breadth_count}/{valid_count} above SMA200 {'✅' if breadth_pass else '❌'}")
    lines.append("")

    # Strategy Decision
    lines.append("🎯 Strategy Target:")
    lines.append(f"• Vehicle: {nasdaq_ticker}  |  Leverage: {leverage:.2f}x")
    lines.append(f"  Reason: {nasdaq_reason}")
    for ticker, pct in target_allocations.items():
        lines.append(f"• {ticker}: {pct*100:.0f}%")
    lines.append("")

    # Current Portfolio
    lines.append("💰 Current Portfolio:")
    lines.append(f"• Total Value: ${total_value:,.2f}")
    lines.append(f"  Investable: ${investable_value:,.2f} (${total_value - investable_value:.2f} buffer)")
    for ticker in sorted(holdings.get('positions', {}).keys()):
        shares = holdings['positions'][ticker]
        price = prices.get(ticker, 0)
        value = shares * price
        pct = holdings_value_pct.get(ticker, 0)
        line = f"  {ticker}: {shares:,.0f} sh × ${price:.2f} = ${value:,.2f} ({pct:.1f}%)"
        # Show target if this is a strategy ticker
        for t_ticker, t_pct in target_allocations.items():
            if t_ticker == ticker:
                line += f"  [target: {t_pct*100:.0f}%]"
                break
        lines.append(line)
    lines.append(f"  Cash: ${holdings.get('usd_cash', 0):,.2f} ({cash_pct:.1f}%)")
    lines.append("")

    # Drift Alert
    if drift_alert:
        lines.append("🚨 ⚠️ DRIFT ALERT ⚠️ 🚨")
        for d in drift_details:
            lines.append(f"  {d}")
        lines.append("")
        lines.append("💡 Action: Run the bot on your Raspberry Pi:")
        lines.append("   cd /home/neo/trading-bot && ./run_bot.sh")
    else:
        lines.append("✅ All allocations within ±5% — No action needed")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Last Pi update: {holdings.get('last_updated', 'unknown')}")

    return "\n".join(lines)

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='GH Actions Portfolio Monitor')
    parser.add_argument('--force', action='store_true', help='Force run even if already ran today')
    args = parser.parse_args()

    logger.info("Starting GitHub Actions portfolio monitor")

    # ── Step 1: Check state (prevent duplicate daily runs) ──
    state = load_state()
    today = date.today().isoformat()
    if state.get('last_run_date') == today and not args.force:
        logger.info(f"Already ran today ({today}) — skipping")
        return
    if args.force:
        logger.info("Force flag set — bypassing daily check")

    # ── Step 2: Load current holdings with validation ──
    holdings = load_holdings()
    positions = holdings.get('positions', {})
    usd_cash = holdings.get('usd_cash', 0.0)
    last_updated = holdings.get('last_updated', 'never')
    logger.info(f"Holdings loaded: {positions} (cash: ${usd_cash:.2f}, updated: {last_updated})")

    if not positions and usd_cash == 0:
        logger.warning("No holdings found — sending alert")
        send_telegram("⚠️ No holdings found in holdings.json. Please update the file.")
        return

    # ── Step 3: Fetch market data ──
    try:
        market_data = get_market_data()
    except Exception as e:
        logger.error(f"Failed to fetch market data: {e}")
        send_telegram(f"❌ Market data fetch failed: {str(e)[:200]}")
        return

    # ── Step 4: Build prices dict for ALL possible tickers ──
    prices = build_prices_dict(market_data)
    logger.debug(f"Prices: {prices}")

    # ── Step 5: Compute target allocations ──
    target_allocations, decision_trace = determine_allocations(market_data)
    logger.info(f"Target: {target_allocations}")

    # ── Step 6: Calculate total portfolio value FIRST ──
    total_value = usd_cash
    for ticker, shares in positions.items():
        price = prices.get(ticker)
        if price and price > 0:
            total_value += shares * price
        else:
            logger.warning(f"{ticker}: price unavailable (${price}), excluding from total")
            # Still add the value using a fallback if we have market data
            price_key = ticker.lower() + '_price'
            fallback_price = market_data.get(price_key, 0)
            if fallback_price > 0:
                total_value += shares * fallback_price
                logger.warning(f"  Used fallback price ${fallback_price:.2f} from market_data")

    investable_value = total_value - USD_CASH_TARGET
    if investable_value < 0:
        investable_value = 0.0
    logger.info(f"Total: ${total_value:.2f}, Investable: ${investable_value:.2f}")

    # ── Step 7: Calculate percentages (SINGLE source of truth) ──
    denom = investable_value if investable_value > 0 else total_value
    holdings_value_pct = {}
    for ticker, shares in positions.items():
        price = prices.get(ticker, 0)
        value = shares * price
        if price == 0:
            logger.warning(f"{ticker}: price is $0, percentage will be 0%")
        holdings_value_pct[ticker] = (value / denom * 100) if denom > 0 else 0

    cash_pct = (usd_cash / denom * 100) if denom > 0 else 0

    # ── Step 8: Check for drift ──
    drift_alert = False
    drift_details = []

    # Check strategy tickers (Nasdaq vehicle + KMLM)
    for ticker, target_pct in target_allocations.items():
        actual_value = positions.get(ticker, 0) * prices.get(ticker, 0)
        actual_pct = (actual_value / denom * 100) if denom > 0 else 0
        deviation = abs(actual_pct - target_pct * 100)
        if deviation > REBALANCE_BAND * 100:
            drift_alert = True
            drift_details.append(
                f"{ticker}: actual {actual_pct:.1f}% vs target {target_pct*100:.0f}% "
                f"(deviation: {deviation:.1f}pp)"
            )

    # Check for orphan tickers (held but not in strategy)
    for ticker in positions:
        if ticker not in target_allocations:
            actual_value = positions[ticker] * prices.get(ticker, 0)
            actual_pct = (actual_value / denom * 100) if denom > 0 else 0
            if actual_pct > 0.5:  # >0.5% is meaningful
                drift_alert = True
                drift_details.append(
                    f"{ticker}: {actual_pct:.1f}% — ORPHAN (not in strategy)"
                )

    # ── Step 9: Build and send report ──
    report = build_report(
        market_data=market_data,
        holdings=holdings,
        total_value=total_value,
        investable_value=investable_value,
        target_allocations=target_allocations,
        decision_trace=decision_trace,
        holdings_value_pct=holdings_value_pct,
        cash_pct=cash_pct,
        drift_alert=drift_alert,
        drift_details=drift_details,
        prices=prices,
    )

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")
    send_telegram(report)

    # ── Step 10: Save state ──
    save_state({'last_run_date': today})
    logger.info("State saved")

if __name__ == '__main__':
    main()

