import os
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime

# ─────────────────────────────────────────────
# Telegram Notification (unchanged)
# ─────────────────────────────────────────────
def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Telegram Error: {e}")

# ─────────────────────────────────────────────
# Composite Score Calculation (NEW)
# ─────────────────────────────────────────────
def compute_squeeze_score(candidate_data):
    """
    candidate_data: dict with keys:
        si           : float (short % of float, e.g. 20.5 for 20.5%)
        dtc          : float (days to cover)
        volume_ratio : float (current volume / 10-day avg volume)
        rsi          : float (14‑period RSI)
        si_change    : float or None (percentage change in shares short)
    Returns a normalised score 0–100.
    """
    # Base weights
    weights = {
        'si': 0.30,
        'dtc': 0.15,
        'volume': 0.20,
        'rsi': 0.15,
        'si_change': 0.20
    }

    # Compute individual component scores (0‑100)
    scores = {}
    scores['si'] = min(candidate_data['si'] * 2, 100)                # 50% SI ➜ 100
    scores['dtc'] = min(candidate_data['dtc'] * 10, 100)             # 10 days ➜ 100
    scores['volume'] = min(candidate_data['volume_ratio'] * 50, 100) # 2x volume ➜ 100
    scores['rsi'] = (100 - candidate_data['rsi'])                     # Low RSI = high score

    # Short interest change – if missing, skip this component
    if candidate_data.get('si_change') is not None:
        scores['si_change'] = min(candidate_data['si_change'] * 2, 100)  # 50% increase ➜ 100
    else:
        scores['si_change'] = None

    # Remove missing components and re‑normalise weights
    active_keys = [k for k in weights if scores.get(k) is not None]
    if not active_keys:
        return 0.0

    total_weight = sum(weights[k] for k in active_keys)
    normalised_weights = {k: weights[k] / total_weight for k in active_keys}

    final_score = sum(scores[k] * normalised_weights[k] for k in active_keys)
    return round(final_score, 2)

# ─────────────────────────────────────────────
# Helper: Compute RSI(14)
# ─────────────────────────────────────────────
def compute_rsi(close_series, period=14):
    delta = close_series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1] if len(rsi) > 0 else 50.0  # fallback

# ─────────────────────────────────────────────
# Main Scan
# ─────────────────────────────────────────────
def main():
    # S&P 500 tickers from verified repo
    url = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/all.csv"
    try:
        tickers = pd.read_csv(url)['symbol'].tolist()
    except Exception as e:
        print(f"Error loading ticker list: {e}")
        return

    candidates = []
    print(f"--- Squeeze Scan Started: {datetime.now()} ---")
    print(f"Scanning {len(tickers)} stocks...")

    for t in tickers:
        try:
            stock = yf.Ticker(t)
            info = stock.info

            # 1. SHORT INTEREST BASE
            short_pct = info.get("shortPercentOfFloat", 0)
            days_to_cover = info.get("shortRatio", 0)

            # Minimum SI filter
            if short_pct is None or short_pct < 0.15:
                continue

            # 2. PRICE DATA (1 month for SMA10 and indicators)
            hist = stock.history(period="1mo")
            if hist.empty or len(hist) < 15:
                continue

            current_price = hist['Close'].iloc[-1]
            sma10 = hist['Close'].rolling(window=10).mean().iloc[-1]

            # Momentum trigger
            if current_price <= sma10:
                continue

            # 3. VOLUME SURGE
            volume = hist['Volume']
            current_volume = volume.iloc[-1]
            avg_volume_10 = volume.rolling(window=10).mean().iloc[-1]
            volume_ratio = current_volume / avg_volume_10 if avg_volume_10 > 0 else 0
            if volume_ratio < 1.5:
                continue

            # 4. RSI FILTER (14‑period, between 30 and 50)
            rsi = compute_rsi(hist['Close'])
            if not (30 <= rsi <= 50):
                continue

            # 5. SHORT INTEREST CHANGE (optional, do not skip if missing)
            shares_short_current = info.get("sharesShort")
            shares_short_prior = info.get("sharesShortPriorMonth")
            if shares_short_prior and shares_short_prior > 0:
                si_change = ((shares_short_current - shares_short_prior) / shares_short_prior) * 100
                if si_change <= 0:      # only stocks where SI increased
                    continue
            else:
                # If prior month data missing, we still allow the candidate
                # The composite score will adaptively ignore this component
                si_change = None

            # 6. BUILD CANDIDATE DATA
            si_value = short_pct * 100  # convert to percentage
            candidate_data = {
                'si': si_value,
                'dtc': days_to_cover,
                'volume_ratio': volume_ratio,
                'rsi': rsi,
                'si_change': si_change
            }
            score = compute_squeeze_score(candidate_data)

            candidates.append({
                "ticker": t,
                "score": score,
                "si": round(si_value, 2),
                "dtc": round(days_to_cover, 2),
                "volume_ratio": round(volume_ratio, 2),
                "rsi": round(rsi, 1),
                "si_change": round(si_change, 1) if si_change is not None else "N/A",
                "price": round(current_price, 2)
            })
            print(f"MATCH: {t} (SI: {si_value:.1f}% | Score: {score})")

        except Exception:
            # silent skip for individual ticker errors
            continue

    # Sort by composite score descending
    sorted_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)

    # ── Output ──
    if sorted_candidates:
        msg = "🔥 **Short Squeeze Screen (Enhanced)** 🔥\n"
        msg += "_>15% SI · Price > SMA10 · Volume > 1.5x avg · RSI 30-50 · Rising SI_\n\n"
        for c in sorted_candidates[:10]:
            msg += (
                f"• **${c['ticker']}** (Score: `{c['score']}`)\n"
                f"  SI: `{c['si']}%` | DTC: `{c['dtc']}` | Vol: `{c['volume_ratio']}x` | "
                f"RSI: `{c['rsi']}` | SIΔ: `{c['si_change']}` | Price: `${c['price']}`\n"
            )
        send_telegram(msg)
    else:
        send_telegram("✅ **Squeeze Scan Complete**: No S&P 500 stocks met all criteria today.")

    print("--- Scan Finished ---")

if __name__ == "__main__":
    main()