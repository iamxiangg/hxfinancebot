# NEWEST COPY — 2026-06-21 CALL-CAPITAL V6 — CLEAR CALL PREMIUM COUNTS FULLY AS BULLISH CAPITAL

import asyncio, hashlib, json, logging, math, os, re, time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from telegram import Bot
from urllib3.util.retry import Retry

MODEL_VERSION = "2026-06-21-call-capital-v6"
RAW_KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TZ = ZoneInfo("Asia/Singapore")

PURCHASE_DAYS, SALE_DAYS, OPTION_MATCH_DAYS, CLUSTER_DAYS = 45, 90, 365, 14
ACTIONABLE_C, ACTIONABLE_E = 60.0, 60.0
WAIT_C, WAIT_CAPITAL = 70.0, 500_000.0
RISK_C, RISK_CAPITAL, SEVERE_DRAWDOWN = 40.0, 250_000.0, -15.0
MAX_ACTIONABLE, MAX_WAIT, MAX_RISK, MAX_NEAREST = 8, 6, 6, 5
MAX_SALE_PENALTY, MAX_CALL_BONUS, MAX_PUT_PENALTY = 20.0, 10.0, 10.0
YF_BATCH_SIZE, YF_ATTEMPTS, YF_FALLBACK_LIMIT, YF_TIMEOUT = 20, 2, 10, 30
YF_CACHE = Path(os.getenv("YF_CACHE_DIRECTORY", "./yfinance_cache"))
LOCK_FILE, LOG_FILE = Path("congress_bot.lock"), "congress_bot.log"
TG_LIMIT = 3800
YF_OVERRIDES = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

logger = logging.getLogger("congress_bot")
logger.setLevel(logging.INFO)
logger.handlers.clear()
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for handler in (logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")):
    handler.setFormatter(fmt)
    logger.addHandler(handler)


def today() -> date:
    return datetime.now(TZ).date()


def fnum(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def pdate(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt_ in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%d/%m/%Y", "%d/%m/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt_).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def money(value):
    value = fnum(value) or 0.0
    sign, value = ("-", -value) if value < 0 else ("", value)
    if value >= 1e9: return f"{sign}${value / 1e9:.1f}b"
    if value >= 1e6: return f"{sign}${value / 1e6:.1f}m"
    if value >= 1e3: return f"{sign}${value / 1e3:.0f}k"
    return f"{sign}${value:.0f}"


def surname(name):
    parts = str(name or "").strip().split()
    while parts and parts[-1].lower() in {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}:
        parts.pop()
    return parts[-1] if parts else "Unknown"


def ticker_code(value):
    ticker = str(value or "").strip().upper()
    if ticker.lower() in {"", "null", "none", "--", "n/a", "nan"}:
        return None
    return ticker if re.fullmatch(r"[A-Z0-9.^=\-]+", ticker) else None


def yf_ticker(ticker):
    return YF_OVERRIDES.get(ticker, ticker)


def script_hash():
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception:
        return "unavailable"


def lock():
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd); return True
    except FileExistsError:
        try:
            if time.time() - LOCK_FILE.stat().st_mtime > 3600:
                LOCK_FILE.unlink(); return lock()
        except Exception:
            pass
        logger.error("Another run appears active: %s", LOCK_FILE); return False


def unlock():
    try: LOCK_FILE.unlink(missing_ok=True)
    except Exception as exc: logger.warning("Could not remove lock: %s", exc)


def session():
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}), raise_on_status=False)
    out = requests.Session(); adapter = HTTPAdapter(max_retries=retry)
    out.mount("https://", adapter); out.mount("http://", adapter)
    out.headers.update({"User-Agent": "CongressTradeMonitor/6.0"}); return out


def action(value):
    text = str(value or "").lower()
    if "purchase" in text or re.search(r"\bbuy\b", text): return "purchase"
    if "sale" in text and "partial" in text: return "sale_partial"
    if "sale" in text and "full" in text: return "sale_full"
    if "sale" in text or re.search(r"\bsell\b", text): return "sale_unknown"
    return "other"


def all_text(item):
    return " ".join(str(item.get(k) or "") for k in ("asset_type", "asset_name", "asset_description", "description", "comment")).lower()


def option_record(item):
    text, asset_type = all_text(item), str(item.get("asset_type") or "").lower()
    return "option" in asset_type or "option" in text or (re.search(r"\b(call|put)\b", text) and re.search(r"\b(strike|expiry|expiration|expires|maturity)\b", text))


def stock_record(item):
    text = f" {all_text(item)} "; asset_type = str(item.get("asset_type") or "").strip().lower()
    if any(term in text for term in (" option", "bond", "debenture", "treasury", "municipal", "fixed income", "structured note", " note ", "mutual fund", "exchange traded fund", " etf", " fund", "warrant", "preferred stock", "preferred share", "annuity", "certificate of deposit", "cryptocurrency", "crypto asset")):
        return False
    if asset_type in {"st", "stock", "common stock", "equity", "ordinary share", "ordinary shares"}: return True
    return any(term in text for term in ("common stock", "class a common", "class b common", "ordinary share", "american depositary share", "american depositary receipt", "depositary receipt", " adr "))


def opt_side(item):
    explicit = str(item.get("option_type") or item.get("put_call") or item.get("call_put") or "").strip().lower()
    if explicit in {"call", "put"}: return explicit
    match = re.search(r"\b(call|put)\b", all_text(item)); return match.group(1).lower() if match else None


def opt_strike(item):
    for key in ("strike", "strike_price", "option_strike"):
        value = fnum(item.get(key))
        if value and value > 0: return value
    text = all_text(item)
    for pattern in (r"(?:strike(?:\s+price)?|strk)\s*[:=@\-]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:strike|call|put)\b", r"\b(?:call|put)\s*(?:at|@)?\s*\$\s*([0-9]+(?:\.[0-9]+)?)"):
        match = re.search(pattern, text)
        if match:
            value = fnum(match.group(1))
            if value and value > 0: return value
    return None


def opt_expiry(item):
    for key in ("expiration_date", "expiry_date", "option_expiry", "maturity_date"):
        parsed = pdate(item.get(key))
        if parsed: return parsed
    text = all_text(item)
    for pattern in (r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{4}-\d{1,2}-\d{1,2})", r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*(\d{1,2}-\d{1,2}-\d{2,4})", r"(?:expiry|expiration|expires?|maturity)\s*[:=@\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", r"\b(20\d{2}-\d{1,2}-\d{1,2})\b", r"\b(\d{1,2}/\d{1,2}/20\d{2})\b"):
        match = re.search(pattern, text)
        if match:
            parsed = pdate(match.group(1))
            if parsed: return parsed
    return None


def amounts(item):
    low, high = fnum(item.get("amount_range_low")), fnum(item.get("amount_range_high"))
    if low is None or high is None or low < 0 or high < low: return 0.0, 0.0, 0.0
    return low, (low + high) / 2.0, high


def low_signal_weight(trade):
    text = f"{trade.get('asset_name', '')} {trade.get('comment', '')}".lower()
    return 0.25 if any(term in text for term in ("inherited", "inheritance", "estate", "mandatory divestment", "required divestment", "issuer called", "called by issuer")) else 1.0


def option_key(trade):
    if trade.get("side") not in {"call", "put"} or trade.get("strike") is None or trade.get("expiry") is None: return None
    return (trade["filer_id"], trade["ticker"], trade["owner"], trade["side"], round(trade["strike"], 4), trade["expiry"])


def fetch_trades():
    try:
        response = session().get(RAW_KADOA_URL, timeout=30); response.raise_for_status()
        raw, payload = response.content, response.json()
    except Exception as exc:
        logger.error("Trade feed failed: %s", exc); return None
    if not isinstance(payload, list): logger.error("Unexpected payload type"); return None
    logger.info("Kadoa payload: records=%d | bytes=%d | SHA256=%s", len(payload), len(raw), hashlib.sha256(raw).hexdigest())

    out, seen, rejected, now = [], set(), Counter(), today()
    for item in payload:
        if not isinstance(item, dict): rejected["not_dict"] += 1; continue
        tx_action = action(item.get("transaction_type", item.get("type")))
        if tx_action not in {"purchase", "sale_partial", "sale_full", "sale_unknown"}: rejected["unsupported_action"] += 1; continue
        if option_record(item): asset_class = "option"
        elif stock_record(item): asset_class = "stock"
        else: rejected["ineligible_asset"] += 1; continue
        ticker, tx_date = ticker_code(item.get("ticker")), pdate(item.get("transaction_date"))
        if not ticker: rejected["invalid_ticker"] += 1; continue
        if not tx_date: rejected["invalid_date"] += 1; continue
        age = (now - tx_date).days
        if age < 0: rejected["future_date"] += 1; continue
        if age > (OPTION_MATCH_DAYS if asset_class == "option" else SALE_DAYS): rejected["outside_context_window"] += 1; continue

        low, mid, high = amounts(item)
        filer_name = str(item.get("filer_name") or item.get("representative") or "Unknown").strip()
        filer_id, owner = str(item.get("filer_id") or filer_name).strip(), str(item.get("owner") or "Unknown").strip()
        side = opt_side(item) if asset_class == "option" else None
        strike = opt_strike(item) if asset_class == "option" else None
        expiry = opt_expiry(item) if asset_class == "option" else None
        trade_id = str(item.get("id") or "").strip()
        key = ("id", trade_id) if trade_id else ("x", filer_id, ticker, tx_date.isoformat(), tx_action, asset_class, low, high, owner, side, strike, expiry.isoformat() if expiry else "")
        if key in seen: rejected["duplicate"] += 1; continue
        seen.add(key)
        out.append({"trade_id": trade_id or hashlib.sha1(repr(key).encode()).hexdigest(), "ticker": ticker, "yf": yf_ticker(ticker), "date": tx_date, "age": age, "action": tx_action, "asset": asset_class, "low": low, "mid": mid, "high": high, "filer": filer_name, "filer_id": filer_id, "owner": owner, "asset_name": str(item.get("asset_name") or ""), "comment": str(item.get("comment") or ""), "side": side, "strike": strike, "expiry": expiry})
    logger.info("Retained %d transactions: %s", len(out), dict(Counter((t["asset"], t["action"]) for t in out)))
    logger.info("Trade rejection counts: %s", dict(rejected)); return out


def init_yf():
    YF_CACHE.mkdir(parents=True, exist_ok=True)
    try: yf.set_tz_cache_location(str(YF_CACHE))
    except Exception as exc: logger.warning("Could not set yfinance cache: %s", exc)


def series(value):
    if not isinstance(value, pd.Series): return pd.Series(dtype="float64")
    out = pd.to_numeric(value, errors="coerce").dropna(); out.index = pd.to_datetime(out.index).tz_localize(None); return out.sort_index()


def batch_prices(symbols, start):
    for attempt in range(1, YF_ATTEMPTS + 1):
        try:
            logger.info("Yahoo history batch: %d tickers | attempt %d/%d", len(symbols), attempt, YF_ATTEMPTS)
            frame = yf.download(tickers=symbols, start=start.isoformat(), end=(today() + timedelta(days=1)).isoformat(), interval="1d", auto_adjust=True, actions=False, repair=False, keepna=False, group_by="ticker", threads=False, progress=False, timeout=YF_TIMEOUT, multi_level_index=True)
            found, one = {}, len(symbols) == 1
            for symbol in symbols:
                if frame is None or frame.empty: continue
                if one: close, volume = series(frame.get("Close")), series(frame.get("Volume"))
                elif isinstance(frame.columns, pd.MultiIndex) and symbol in frame.columns.get_level_values(0): close, volume = series(frame[symbol].get("Close")), series(frame[symbol].get("Volume"))
                else: continue
                if not close.empty: found[symbol] = {"close": close, "volume": volume}
            if found: return found
        except Exception as exc: logger.warning("Yahoo batch failed: %s", exc)
        if attempt < YF_ATTEMPTS: time.sleep(4 * attempt)
    return {}


def prices(symbols, earliest):
    symbols, start, found = sorted(set(symbols)), earliest - timedelta(days=100), {}
    for index in range(0, len(symbols), YF_BATCH_SIZE): found.update(batch_prices(symbols[index:index + YF_BATCH_SIZE], start))
    for symbol in [s for s in symbols if s not in found][:YF_FALLBACK_LIMIT]:
        try:
            history = yf.Ticker(symbol).history(start=start.isoformat(), end=(today() + timedelta(days=1)).isoformat(), interval="1d", auto_adjust=True, actions=False, repair=False, keepna=False, timeout=YF_TIMEOUT, raise_errors=True)
            close, volume = series(history.get("Close")), series(history.get("Volume"))
            if not close.empty: found[symbol] = {"close": close, "volume": volume}
        except Exception as exc: logger.warning("Yahoo fallback failed for %s: %s", symbol, exc)
    return found


def amount_score(value):
    for threshold, score in ((1e6, 55), (750e3, 50), (500e3, 45), (250e3, 35), (100e3, 25), (50e3, 15), (15e3, 8)): 
        if value >= threshold: return float(score)
    return 2.0


def floor_score(value):
    for threshold, score in ((1e6, 10), (500e3, 8), (250e3, 6), (100e3, 4), (50e3, 2)):
        if value >= threshold: return float(score)
    return 0.0


def size_score(value):
    for threshold, score in ((1e6, 15), (500e3, 12), (250e3, 10), (100e3, 8), (50e3, 6), (15e3, 4)):
        if value >= threshold: return float(score)
    return 2.0


def call_bonus(value):
    for threshold, score in ((1e6, 10), (500e3, 8), (250e3, 6), (100e3, 4), (50e3, 2)):
        if value >= threshold: return float(score)
    return 0.0


def fresh(age, maximum): return maximum * max(0.0, 1.0 - age / PURCHASE_DAYS)

def cluster_score(count): return 15.0 if count >= 5 else {4: 12.0, 3: 9.0, 2: 5.0}.get(count, 0.0)

def repeat_score(transactions, buyers): return 10.0 if transactions - buyers >= 2 else 5.0 if transactions - buyers == 1 else 0.0

def price_score(ret): return 5.0 if ret <= -15 else 25.0 if ret <= -10 else 35.0 if ret <= -5 else 45.0 if ret <= 2 else 35.0 if ret <= 8 else 20.0 if ret <= 15 else 5.0


def trend_score(close):
    current, ma20, ma50 = fnum(close.iloc[-1]), fnum(close.tail(20).mean()) if len(close) >= 20 else None, fnum(close.tail(50).mean()) if len(close) >= 50 else None
    if current is None: return 0.0
    if ma20 is not None and ma50 is not None and current > ma20 > ma50: return 20.0
    if ma20 is not None and current > ma20: return 15.0
    if ma50 is not None and current > ma50: return 10.0
    return 3.0


def liquidity_score(close, volume):
    common = close.index.intersection(volume.index)
    if common.empty: return 4.0
    value = (close.loc[common] * volume.loc[common]).tail(20).mean()
    return 15.0 if value >= 50e6 else 12.0 if value >= 10e6 else 8.0 if value >= 2e6 else 4.0


def active_options(trades):
    states, by_key, unclear, matched, matched_full = [], defaultdict(list), 0, 0, 0
    for trade in sorted(trades, key=lambda t: (t["date"], t["trade_id"])):
        if trade["action"] == "purchase":
            state = [trade, 1.0]; states.append(state); key = option_key(trade)
            if key is not None: by_key[key].append(state)
            continue
        if trade["action"] not in {"sale_partial", "sale_full", "sale_unknown"} or trade["age"] > SALE_DAYS: continue
        key = option_key(trade); prior = [] if key is None else [s for s in by_key.get(key, []) if s[0]["date"] < trade["date"] and s[1] > 0]
        if not prior or trade["action"] == "sale_unknown": unclear += 1; continue
        matched += 1
        if trade["action"] == "sale_full":
            matched_full += 1
            for state in prior: state[1] = 0.0
        else:
            for state in prior: state[1] *= 0.5
    calls, puts = [], []
    for trade, fraction in states:
        if trade["age"] > PURCHASE_DAYS or fraction <= 0: continue
        scaled = dict(trade); scaled["low"] *= fraction; scaled["mid"] *= fraction; scaled["high"] *= fraction
        if trade.get("side") == "call": calls.append(scaled)
        elif trade.get("side") == "put": puts.append(scaled)
    return calls, puts, unclear, matched, matched_full


def sale_metrics(trades):
    prior_buys, penalty, total, partial, full, same_full = defaultdict(list), 0.0, 0.0, 0, 0, False
    for trade in trades:
        if trade["action"] == "purchase": prior_buys[trade["filer_id"]].append(trade["date"])
    for trade in trades:
        if trade["action"] not in {"sale_partial", "sale_full", "sale_unknown"} or trade["age"] > SALE_DAYS: continue
        total += trade["mid"]; partial += trade["action"] == "sale_partial"; full += trade["action"] == "sale_full"
        same = any(d < trade["date"] for d in prior_buys.get(trade["filer_id"], []))
        penalty += size_score(trade["mid"]) * (1.0 if trade["action"] == "sale_full" else 0.5) * (1.0 if same else 0.5) * max(0.0, 1.0 - trade["age"] / SALE_DAYS) * low_signal_weight(trade)
        if trade["action"] == "sale_full" and same: same_full = True
    return min(MAX_SALE_PENALTY, penalty), total, partial, full, same_full


def close_after(close, tx_date):
    eligible = close[close.index.date >= tx_date]
    return fnum(eligible.iloc[0]) if not eligible.empty else None


def analyse(ticker, trades, market):
    close, volume = market["close"], market["volume"]
    if close.empty or fnum(close.iloc[-1]) is None: return None
    current = fnum(close.iloc[-1])
    stock = [t for t in trades if t["asset"] == "stock"]
    stock_buys = [t for t in stock if t["action"] == "purchase" and t["age"] <= PURCHASE_DAYS]
    calls, puts, unclear_sales, matched_sales, matched_full = active_options([t for t in trades if t["asset"] == "option"])
    bullish = stock_buys + calls
    if not bullish: return None

    low, mid, high = sum(t["low"] for t in bullish), sum(t["mid"] for t in bullish), sum(t["high"] for t in bullish)
    effective = 0.6 * mid + 0.4 * low
    call_low, call_mid = sum(t["low"] for t in calls), sum(t["mid"] for t in calls)
    leverage_bonus = call_bonus(0.6 * call_mid + 0.4 * call_low)
    put_mid = sum(t["mid"] for t in puts)
    put_penalty = min(MAX_PUT_PENALTY, 0.5 * size_score(put_mid)) if put_mid else 0.0
    option_adjustment = leverage_bonus - put_penalty

    priced = []
    for trade in bullish:
        ref = close_after(close, trade["date"])
        if ref and ref > 0:
            weight = trade["mid"] if trade["mid"] > 0 else 1.0
            priced.append((trade, (current - ref) / ref * 100.0, weight))
    if not priced: return None
    coverage = sum(t[0]["mid"] for t in priced) / mid if mid else 0.0
    if coverage < 0.75: logger.info("%s rejected: price coverage %.1f%%", ticker, coverage * 100); return None

    weight_total = sum(item[2] for item in priced)
    weighted_return = sum(item[1] * item[2] for item in priced) / weight_total
    weighted_age = sum(item[0]["age"] * item[2] for item in priced) / weight_total
    buyers, cluster_buyers = {t["filer_id"] for t in bullish}, {t["filer_id"] for t in bullish if t["age"] <= CLUSTER_DAYS}
    base = amount_score(effective) + floor_score(low) + cluster_score(len(cluster_buyers)) + repeat_score(len(bullish), len(buyers)) + fresh(weighted_age, 10.0)
    sale_penalty, sale_mid, partial_sales, full_sales, same_full = sale_metrics(stock)
    conviction = max(0.0, min(100.0, base + option_adjustment - sale_penalty))
    entry = max(0.0, min(100.0, price_score(weighted_return) + fresh(weighted_age, 20.0) + trend_score(close) + liquidity_score(close, volume)))

    strong_distribution = same_full or sale_penalty >= 12 or option_adjustment <= -8
    if strong_distribution and (base >= RISK_C or effective >= RISK_CAPITAL): category = "risk"
    elif conviction >= ACTIONABLE_C and entry >= ACTIONABLE_E and weighted_return > SEVERE_DRAWDOWN: category = "actionable"
    elif weighted_return <= -10 and (conviction >= RISK_C or effective >= RISK_CAPITAL): category = "risk"
    elif conviction >= WAIT_C or effective >= WAIT_CAPITAL: category = "wait"
    else: category = "other"

    stock_mid = sum(t["mid"] for t in stock_buys)
    if same_full: flow = "🔴 Full stock sale disclosed"
    elif sale_penalty >= 12 or option_adjustment <= -8: flow = "🔴 Distribution"
    elif sale_penalty > 3 or option_adjustment < 0 or matched_full: flow = "🟡 Mixed / trimming"
    elif call_mid >= 250_000 and stock_mid < 50_000: flow = "🎯 Options-led"
    else: flow = "🟢 Accumulation"

    result = {"ticker": ticker, "category": category, "conviction": conviction, "entry": entry, "base": base, "sale_penalty": sale_penalty, "call_bonus": leverage_bonus, "put_penalty": put_penalty, "low": low, "mid": mid, "high": high, "effective": effective, "call_mid": call_mid, "put_mid": put_mid, "buyers": len(buyers), "cluster_buyers": len(cluster_buyers), "weighted_age": weighted_age, "weighted_return": weighted_return, "flow": flow, "names": sorted({surname(t["filer"]) for t in bullish}), "unclear_sales": unclear_sales, "matched_sales": matched_sales}
    logger.info("%s score audit | base=%.1f | sale=-%.1f | option=%+.1f (call bonus=+%.1f, put=-%.1f) | final=%.1f | entry=%.1f | capital=%s | calls=%s | buyers=%d | category=%s", ticker, base, sale_penalty, option_adjustment, leverage_bonus, put_penalty, conviction, entry, money(mid), money(call_mid), len(buyers), category)
    return result


def process(trades):
    grouped = defaultdict(list)
    for trade in trades: grouped[trade["ticker"]].append(trade)
    active = {ticker: items for ticker, items in grouped.items() if any(t["age"] <= PURCHASE_DAYS and t["action"] == "purchase" and (t["asset"] == "stock" or (t["asset"] == "option" and t.get("side") == "call")) for t in items)}
    if not active: return []
    earliest = min(t["date"] for items in active.values() for t in items if t["age"] <= PURCHASE_DAYS)
    market = prices([yf_ticker(ticker) for ticker in active], earliest)
    out = []
    for ticker, items in active.items():
        if yf_ticker(ticker) not in market: logger.info("%s rejected: no Yahoo data", ticker); continue
        result = analyse(ticker, items, market[yf_ticker(ticker)])
        if result: out.append(result)
    return out


def rank(result): return result["conviction"], result["entry"], result["effective"]


def note(result):
    parts = []
    if result["call_mid"]: parts.append(f"Calls {money(result['call_mid'])}, call bonus +{result['call_bonus']:.0f}")
    if result["put_mid"]: parts.append(f"Puts {money(result['put_mid'])}, penalty -{result['put_penalty']:.0f}")
    if result["matched_sales"]: parts.append(f"{result['matched_sales']} matched option sale")
    if result["unclear_sales"]: parts.append(f"{result['unclear_sales']} unclear option sale")
    return ", ".join(parts)


def line(result):
    prefix = "👥" if result["cluster_buyers"] >= 2 else ""
    extra = f" | {note(result)}" if note(result) else ""
    return f"{prefix}${result['ticker']} | C{result['conviction']:.0f}/E{result['entry']:.0f} | Bullish capital {money(result['mid'])} [{money(result['low'])}-{money(result['high'])}] | {result['buyers']} buyers ({result['cluster_buyers']}/{CLUSTER_DAYS}d) | Wtd age {result['weighted_age']:.0f}d | Vs activity {result['weighted_return']:+.1f}% | {result['flow']}{extra} | {', '.join(result['names'][:4])}"


def chunks(lines):
    out, current = [], ""
    for item in lines:
        addition = item + "\n"
        if current and len(current) + len(addition) > TG_LIMIT: out.append(current.rstrip()); current = addition
        else: current += addition
    if current.strip(): out.append(current.rstrip())
    return out


def messages(results):
    actionable = sorted((r for r in results if r["category"] == "actionable"), key=rank, reverse=True)[:MAX_ACTIONABLE]
    wait = sorted((r for r in results if r["category"] == "wait"), key=rank, reverse=True)[:MAX_WAIT]
    risk = sorted((r for r in results if r["category"] == "risk"), key=rank, reverse=True)[:MAX_RISK]
    if actionable or wait or risk:
        lines = ["📊 CONGRESS TRADE OPPORTUNITIES", f"Model: {MODEL_VERSION}", f"Analysed: {len(results)} tickers | Shown: {len(actionable)+len(wait)+len(risk)}", "C = conviction after sales/options | E = entry quality", ""]
        for title, items in (("🔥 BEST ACTIONABLE", actionable), ("👀 HIGH CONVICTION — WAIT FOR ENTRY", wait), ("⚠️ CONFLICTING / HIGHER RISK", risk)):
            if items: lines += [title] + [line(item) for item in items] + [""]
        lines += ["Bullish capital = stock purchases + clear call-premium purchases.", "Call premium is capital at risk, not estimated stock-equivalent exposure.", "Unmatched option sales remain neutral. Screening signal only."]
        return chunks(lines)
    nearest = sorted((r for r in results if r["conviction"] >= 15), key=rank, reverse=True)[:MAX_NEAREST]
    lines = ["📊 CONGRESS TRADE MONITOR", f"Model: {MODEL_VERSION}", "No ticker met the strict actionable, wait or risk thresholds.", f"Analysed: {len(results)} tickers | Qualified: 0", ""]
    if nearest: lines += ["🔎 NEAREST SIGNALS — NOT QUALIFIED"] + [line(item) for item in nearest] + [""]
    lines += ["Current thresholds:", f"Actionable: C≥{ACTIONABLE_C:.0f} and E≥{ACTIONABLE_E:.0f}", f"Wait: C≥{WAIT_C:.0f} or bullish capital ≥{money(WAIT_CAPITAL)}", "Risk: meaningful bullish activity plus severe drawdown or strong distribution", "", "Near-miss entries are monitoring context, not purchase signals."]
    return chunks(lines)


async def send(items):
    bot = Bot(token=TOKEN)
    for index, item in enumerate(items):
        await bot.send_message(chat_id=CHAT_ID, text=item)
        if index < len(items) - 1: await asyncio.sleep(1)


async def failure(text):
    if TOKEN and CHAT_ID:
        try: await Bot(token=TOKEN).send_message(chat_id=CHAT_ID, text=f"⚠️ Congress monitor failure\nModel: {MODEL_VERSION}\n{text}")
        except Exception: logger.exception("Could not send failure alert")


def main():
    if not TOKEN or not CHAT_ID: logger.error("Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID"); return
    if not lock(): return
    try:
        logger.info("Running model %s | script SHA256=%s", MODEL_VERSION, script_hash()); init_yf()
        trades = fetch_trades()
        if trades is None: asyncio.run(failure("Trade feed could not be retrieved.")); return
        results = process(trades)
        if not results: asyncio.run(failure("No ticker produced usable price analytics.")); return
        output = messages(results); asyncio.run(send(output)); logger.info("Sent %d Telegram message(s)", len(output))
    except Exception as exc:
        logger.exception("Unhandled failure: %s", exc); asyncio.run(failure(str(exc)[:500]))
    finally: unlock()


if __name__ == "__main__": main()
