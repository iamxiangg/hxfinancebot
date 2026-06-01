import os
import json
import time
import random
import requests
import logging
import threading
import html
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from google.oauth2.service_account import Credentials
import googleapiclient.discovery
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── MACHINE LEARNING NLP DEPENDENCY CHECK ──
try:
    from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class NeoQuantScannerPro:
    def __init__(self):
        logging.info("🚀 Deploying Hardened Production Neo Quant Scanner Pro (v4.5-RC1)...")
        
        self.SHEET_NAME = "Xiang Stock Analysis"
        self.WORKSHEET_NAME = "Stock Summary USD"
        self.CACHE_FILE = "vp_cache.json"
        
        self.SECTOR_MAP = {
            'Technology': 'XLK', 'Financial Services': 'XLF', 'Healthcare': 'XLV',
            'Consumer Cyclical': 'XLY', 'Communication Services': 'XLC', 'Industrials': 'XLI',
            'Consumer Defensive': 'XLP', 'Energy': 'XLE', 'Real Estate': 'XLRE',
            'Utilities': 'XLU', 'Basic Materials': 'XLB'
        }
        
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self._sheet_id = None
        self._ticker_row_map = {}
        self._nlp_lock = threading.Lock()

        # ── CONFIGURABLE ENGINE THROUGHPUT TUNING ──
        self.MAX_WORKERS = 6
        self.THROTTLE_MIN = 0.1
        self.THROTTLE_MAX = 0.4

        # Load Local Volume Profile Caching
        self.vp_cache = {}
        if os.path.exists(self.CACHE_FILE):
            try:
                with open(self.CACHE_FILE, 'r') as f:
                    self.vp_cache = json.load(f)
                logging.info(f"💾 Loaded cached volume profiles for {len(self.vp_cache)} assets.")
            except Exception as e:
                logging.warning(f"⚠️ Cache read error, resetting profile register: {e}")

        # Hardened Network Frame
        self.custom_session = requests.Session()
        self.custom_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive"
        })

        if HAS_TRANSFORMERS:
            try:
                logging.info("🧠 Initializing FinBERT Sentiment Architecture...")
                tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
                model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
                self.nlp_engine = pipeline("sentiment-analysis", model=model, tokenizer=tokenizer, device=-1)
                logging.info("✅ FinBERT Core Loaded Successfully.")
            except Exception as e:
                logging.error(f"❌ FinBERT initialization failed: {e}")
                self.nlp_engine = None
        else:
            logging.warning("⚠️ Transformers not detected. Running NLP-disabled baseline mode.")
            self.nlp_engine = None

    # ─────────────────────────────────────────────────────────────────────────
    # PRODUCTION CORE INTERFACES & PRE-FLIGHT
    # ─────────────────────────────────────────────────────────────────────────
    def pre_flight_validation(self):
        """Rule G: Enforces absolute validation constraints before launching scan cycles."""
        missing = []
        if not os.getenv("GCP_SERVICE_ACCOUNT_FILE"): missing.append("GCP_SERVICE_ACCOUNT_FILE")
        if not self.telegram_token: missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id: missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise SystemExit(f"🚨 CRITICAL BOOT ERROR: Missing environment configurations: {', '.join(missing)}")
        logging.info("🎯 Pre-Flight Core Checks Passed. Initiating connection protocols.")

    def get_service(self, api_name, version):
        creds_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.metadata.readonly']
        )
        return googleapiclient.discovery.build(api_name, version, credentials=creds)

    def init_sheet_mapping(self):
        """Rule D: Discovers grid row structures with runtime testing pipeline overrides."""
        test_tickers = os.getenv("TEST_TICKERS")
        if test_tickers:
            logging.info(f"🧪 TEST MODE ENABLED: Filtering scan solely for: {test_tickers}")
            for i, t in enumerate(test_tickers.split(','), start=2):
                self._ticker_row_map[t.strip().upper().replace('.', '-')] = i
            return

        try:
            drive_service = self.get_service('drive', 'v3')
            query = f"name = '{self.SHEET_NAME}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
            results = drive_service.files().list(q=query, fields='files(id)').execute()
            if not results.get('files'):
                raise Exception(f"Google Sheet named '{self.SHEET_NAME}' was not located.")
            self._sheet_id = results['files'][0]['id']

            service = self.get_service('sheets', 'v4')
            result = service.spreadsheets().values().get(
                spreadsheetId=self._sheet_id, range=f"'{self.WORKSHEET_NAME}'!A:A"
            ).execute()
            rows = result.get('values', [])
            for i, row in enumerate(rows):
                if row and row[0]:
                    clean_ticker = row[0].strip().upper().replace('.', '-')
                    if clean_ticker not in ["TICKER", "SYMBOL"]:
                        self._ticker_row_map[clean_ticker] = i + 1
            logging.info(f"📋 Mapped {len(self._ticker_row_map)} live tickers directly from sheet definitions.")
        except Exception as e:
            logging.error(f"❌ Failed initializing sheet layout architecture: {e}")
            raise

    def batch_write_results(self, update_data):
        if not update_data or not self._sheet_id: return
        try:
            service = self.get_service('sheets', 'v4')
            data = [
                {'range': f"'{self.WORKSHEET_NAME}'!AL{self._ticker_row_map[t]}:AN{self._ticker_row_map[t]}", 'values': [v]}
                for t, v in update_data.items() if t in self._ticker_row_map
            ]
            for i in range(0, len(data), 100):
                body = {'valueInputOption': 'USER_ENTERED', 'data': data[i:i+100]}
                service.spreadsheets().values().batchUpdate(spreadsheetId=self._sheet_id, body=body).execute()
        except Exception as e:
            logging.error(f"❌ Batch optimization matrix sync failure: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # REFACTORED CORE TECHNICAL & SEMANTIC DATA PIPELINES
    # ─────────────────────────────────────────────────────────────────────────
    def calculate_vp_nuanced(self, clean_tk, df):
        """Rule 1 & E: Static price profile engine containing automated cache optimization mechanics."""
        if df.empty or len(df) < 15: return 0.0, 0.0
        
        # Check Execution Cache Constraints first to bypass duplicate math processing loops
        today_str = str(datetime.now().date())
        if clean_tk in self.vp_cache and self.vp_cache[clean_tk].get("date") == today_str:
            return float(self.vp_cache[clean_tk]["poc"]), float(self.vp_cache[clean_tk]["vah"])

        try:
            p_min, p_max = df['Close'].min(), df['Close'].max()
            price_range = p_max - p_min
            if price_range == 0: return float(p_min), float(p_min)

            round_base = 2.0 if p_max > 500 else (0.50 if p_max > 50 else 0.10)
            
            # Rule 1 Fallback Extension: Protect low range asset shelf visibility boundaries
            min_shelves = 8
            while (price_range / round_base) < min_shelves:
                round_base /= 2.0

            df_copy = df.copy()
            df_copy['PriceShelf'] = (df_copy['Close'] / round_base).round() * round_base
            vol_prof = df_copy.groupby('PriceShelf', observed=False)['Volume'].sum()
            
            if vol_prof.empty: return float(df['Close'].iloc[-1]), float(df['Close'].iloc[-1])
            
            poc = float(vol_prof.idxmax())
            sorted_shelves = vol_prof.sort_values(ascending=False)
            total_vol, cum_vol, val_area = sorted_shelves.sum(), 0.0, []

            if total_vol == 0: return poc, poc

            for shelf, vol in sorted_shelves.items():
                cum_vol += vol
                val_area.append(shelf)
                if cum_vol / total_vol >= 0.70: break

            vah = float(max(val_area)) if val_area else poc
            
            # Commit newly mapped structures to local file dictionary memory registers
            self.vp_cache[clean_tk] = {"poc": poc, "vah": vah, "date": today_str}
            return poc, vah
        except Exception as e:
            logging.debug(f"Volume profile parsing error fallback on {clean_tk}: {e}")
            return float(df['Close'].iloc[-1]), float(df['Close'].iloc[-1])

    def get_pead_metrics(self, stock_obj, t_prices, t_highs, t_lows):
        """Rule 2 & 9: Timezone localized vector search with automated retries and explicit structure parsing."""
        try:
            if not t_prices.index.is_monotonic_increasing:
                t_prices = t_prices.sort_index()
                t_highs = t_highs.sort_index()
                t_lows = t_lows.sort_index()

            earnings_df = None
            for _ in range(2):
                try:
                    earnings_df = stock_obj.get_earnings_dates(limit=4)
                    if earnings_df is not None and not earnings_df.empty and not isinstance(earnings_df, list):
                        break
                except Exception:
                    time.sleep(0.3)

            if earnings_df is None or earnings_df.empty or isinstance(earnings_df, list):
                return 0.0, 999, {}

            if earnings_df.index.tz is not None:
                earnings_df.index = earnings_df.index.tz_localize(None)
            
            t_prices_naive = t_prices.index.tz_localize(None) if t_prices.index.tz is not None else t_prices.index
            now_naive = pd.Timestamp.now().tz_localize(None)
            
            past_earnings = earnings_df[earnings_df.index <= now_naive]
            if past_earnings.empty: return 0.0, 999, {}

            past_sorted = past_earnings.sort_index()
            earn_dt = past_sorted.index[-1]
            days_since_earnings = (now_naive.date() - earn_dt.date()).days

            past_sorted.columns = [str(c).strip().lower() for c in past_sorted.columns]
            surp_col = [c for c in past_sorted.columns if 'surprise' in c]
            if not surp_col: return 0.0, days_since_earnings, {}

            raw_surprise = past_sorted[surp_col[0]].dropna()
            if raw_surprise.empty: return 0.0, days_since_earnings, {}
            surprise_margin = float(raw_surprise.iloc[-1])

            # Vector Search for Absolute Proximity Positioning Alignment
            price_dates = np.array([dt.to_pydatetime() for dt in t_prices_naive])
            time_deltas = np.abs(price_dates - earn_dt.to_pydatetime())
            earn_bar_pos = int(np.argmin(time_deltas))
            earn_idx_offset = earn_bar_pos - len(t_prices)

            if abs(earn_idx_offset) <= len(t_prices):
                e_close, e_high, e_low = float(t_prices.iloc[earn_idx_offset]), float(t_highs.iloc[earn_idx_offset]), float(t_lows.iloc[earn_idx_offset])
                day_range = e_high - e_low
                close_position = (e_close - e_low) / day_range if day_range != 0 else 0.5
            else:
                close_position = 0.5

            return surprise_margin, days_since_earnings, {
                "earn_idx_offset": earn_idx_offset, "close_position": close_position, "bars_since_earnings": max(1, -earn_idx_offset)
            }
        except Exception as e:
            logging.debug(f"PEAD data processing matrix error deflection: {e}")
            return 0.0, 999, {}

    def get_finbert_sentiment(self, stock_obj):
        """Rule 3: Live news tracker running parsing fallbacks to shield engine visibility metrics."""
        if not self.nlp_engine: return 0.0, "BERT Inactive"
        titles = []
        try:
            news_items = stock_obj.news
            if isinstance(news_items, list) and news_items:
                for item in news_items[:6]:
                    if isinstance(item, dict) and 'title' in item:
                        titles.append(str(item['title']))
        except Exception:
            pass

        # Rule 3 Fallback Extension: Query RSS pipelines if primary corporate data maps are empty
        if not titles:
            try:
                ticker = stock_obj.ticker
                rss_url = f"https://news.google.com/rss/search?q={ticker}+stock+when:5d&hl=en-US&gl=US&ceid=US:en"
                response = self.custom_session.get(rss_url, timeout=4)
                if response.status_code == 200:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(response.content)
                    titles = [item.find('title').text for item in root.findall('.//item')[:5] if item.find('title') is not None]
            except Exception:
                pass

        if not titles: return 0.0, "💤 Neutral Sentiment"

        try:
            with self._nlp_lock: predictions = self.nlp_engine(titles)
            net_score, pos_count, neg_count = 0.0, 0, 0
            for p in predictions:
                label, score = p['label'].upper(), p['score']
                if 'POS' in label: net_score += score; pos_count += 1
                elif 'NEG' in label: net_score -= score; neg_count += 1
            
            avg_score = net_score / len(titles)
            if avg_score > 0.15: status_str = f"🚀 Bullish Sentiment ({pos_count}/{len(titles)} High)"
            elif avg_score < -0.15: status_str = f"⚠️ Bearish Pressure ({neg_count}/{len(titles)} Disp)"
            else: status_str = "💤 Neutral Sentiment"
            return float(avg_score), status_str
        except Exception:
            return 0.0, "Inference Deflected"

    # ─────────────────────────────────────────────────────────────────────────
    # REFACTORED WORKER ROUTINE MATRIX
    # ─────────────────────────────────────────────────────────────────────────
    def process_ticker(self, ticker):
        """Rule C & 🔴 Signature Correction: Complete validation workflow containing structural logging loops."""
        clean_tk = ticker.replace('.', '-')
        time.sleep(random.uniform(self.THROTTLE_MIN, self.THROTTLE_MAX))
        
        try:
            stock = yf.Ticker(clean_tk, session=self.custom_session)
            try:
                info = stock.info
                if info is None or not isinstance(info, dict):
                    logging.debug(f"Skipping {clean_tk}: yfinance returned an empty profile payload.")
                    return None
                sector = info.get('sector', 'Technology')
            except Exception as e:
                logging.debug(f"Skipping {clean_tk} info fetch failure: {e}")
                return None

            sector_etf = self.SECTOR_MAP.get(sector, 'SPY')
            data = yf.download([clean_tk, sector_etf], period="1y", auto_adjust=True, progress=False, session=self.custom_session)
            if data is None or data.empty or 'Close' not in data:
                logging.debug(f"Skipping {clean_tk}: historical raw data query empty.")
                return None

            lvl = data.columns.nlevels > 1
            try:
                t_prices = data['Close'][clean_tk].dropna() if lvl else data['Close'].dropna()
                t_highs = data['High'][clean_tk].dropna() if lvl else data['High'].dropna()
                t_lows = data['Low'][clean_tk].dropna() if lvl else data['Low'].dropna()
                t_vols = data['Volume'][clean_tk].dropna() if lvl else data['Volume'].dropna()
                s_prices = data['Close'][sector_etf].dropna() if lvl else data['Close'].dropna()
            except KeyError as ke:
                logging.debug(f"Skipping {clean_tk} due to dataframe columns key mismatch: {ke}")
                return None

            if len(t_prices) < 25: return None
            px = float(t_prices.iloc[-1])
            if px <= 0: return None
            
            m20 = t_prices.rolling(20).mean().iloc[-1]
            m50 = t_prices.rolling(50).mean().iloc[-1]
            m200 = t_prices.rolling(200).mean().iloc[-1] if len(t_prices) >= 200 else None
            bull_regime = (px > m50 > m200) if m200 is not None else (px > m50)

            # Extract 6-Month Relative Alpha
            lookback = min(126, len(t_prices)-1)
            alpha = (t_prices.iloc[-1] / t_prices.iloc[-lookback]) - (s_prices.iloc[-1] / s_prices.iloc[-lookback])

            # Rule 3 Correction: Handle macro split adjustments across comprehensive history windows
            adjusted_vols = t_vols.copy()
            try:
                splits = stock.splits
                if splits is not None and not splits.empty and not isinstance(splits, list):
                    for split_date, ratio in splits.items():
                        if ratio > 0: adjusted_vols.loc[adjusted_vols.index < split_date] *= ratio
            except Exception:
                pass

            poc, vah = self.calculate_vp_nuanced(clean_tk, pd.DataFrame({'Close': t_prices, 'Volume': adjusted_vols}).tail(65))
            
            # Correct Call Signature Implementation Layer mapping structured variable tracking dicts
            surp, days, struct_meta = self.get_pead_metrics(stock, t_prices, t_highs, t_lows)
            nlp_score, nlp_desc = self.get_finbert_sentiment(stock)

            score = 0.0
            is_pead_active = False
            status = "COILING (Standard Technical Setup)"
            exit_logic_note = "Standard 20% Technical Target Frame"

            historical_base = t_highs.iloc[-25:-2]
            resistance_20d = float(historical_base.max()) if not historical_base.empty else px

            # PEAD ALLOCATION MATRIX
            if surp >= 15.0 and 0 <= days <= 90 and struct_meta:
                is_pead_active = True
                if days < 5:
                    score = 1.0
                    status = "⛔ PEAD PENALTY BOX (Evaluating Post-Earnings Action)"
                else:
                    earn_idx = struct_meta["earn_idx_offset"]
                    close_pos = struct_meta["close_position"]
                    bars_since_earnings = struct_meta["bars_since_earnings"]

                    if close_pos < 0.40:
                        score = 0.0
                        status = "⚠️ PEAD DISTRIBUTION (Weak Earnings Day Close)"
                    else:
                        post_earnings_history = t_highs.tail(bars_since_earnings)
                        pe_max_high = float(post_earnings_history.max()) if not post_earnings_history.empty else px
                        correction_depth = (pe_max_high - px) / pe_max_high if pe_max_high > 0 else 0
                        
                        depth_bonus = 1.5 if correction_depth <= 0.10 else (0.5 if correction_depth <= 0.25 else -99)
                        
                        closes_series = t_prices.tail(bars_since_earnings)
                        ma20_series = t_prices.rolling(20).mean().tail(bars_since_earnings)
                        ma20_violations = (closes_series < ma20_series).sum() if not ma20_series.empty else 0
                        
                        respects_ma_floor = (ma20_violations <= 3) and (px > m50)
                        volume_dried = adjusted_vols.tail(4).mean() < adjusted_vols.tail(50).mean()
                        respects_poc = px >= (poc * 0.97)

                        if depth_bonus >= 0 and respects_ma_floor and volume_dried and respects_poc:
                            alpha_mod = min(2.0, max(0.0, alpha * 10)) 
                            time_decay = 1.0 if days <= 45 else 0.6
                            nlp_mod = 0.5 if nlp_score > 0.25 else (-1.5 if nlp_score < -0.25 else 0.0)

                            score = min((7.0 + depth_bonus + alpha_mod + nlp_mod) * time_decay, 10.0)
                            exit_logic_note = f"🎯 Trim: ${px * 1.10:.2f} | 🛡️ Stop: 20-DMA (${m20:.2f})"
                            status = f"🔥 PEAD BREAKOUT | {nlp_desc}" if px >= resistance_20d else f"⏳ PEAD FLAG COILING | {nlp_desc}"
                        else:
                            score = 2.0
                            status = f"⚠️ PEAD STRUCTURE DEGRADED | {nlp_desc}"
            
            # Rule A Fallback Implementation Matrix: Clamping system scores tightly
            if not is_pead_active:
                alpha_score = 3.0 if alpha > 0.05 else (1.5 if alpha > 0 else 0)
                regime_score = 3.0 if bull_regime else 0
                profile_score = 2.0 if px > vah else 0
                nlp_mod = 0.5 if nlp_score > 0.3 else 0.0
                
                score = min(alpha_score + regime_score + profile_score + nlp_mod, 8.5)
                
                if px >= resistance_20d: status = f"🔥 TECHNICAL BREAKOUT ACTIVE | {nlp_desc}"
                elif px > (poc * 1.05): status = "EXTENDED (Wait for Pullback)"
                else: status = f"COILING | {nlp_desc}"

            return {
                "ticker": clean_tk, "score": round(score, 1), "alpha_str": f"{alpha:+.2%}",
                "px": px, "poc": poc, "resistance": resistance_20d, "status": status, "exits": exit_logic_note
            }
        except Exception as e:
            logging.error(f"❌ Structural core exception for {ticker} runtime track: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # ENGINE RUNTIME ENTRANCE LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def run(self):
        self.pre_flight_validation()
        self.init_sheet_mapping()
        tickers = list(self._ticker_row_map.keys())
        all_updates, t1, t2, neutral, t3 = {}, [], [], [], []

        logging.info(f"⏳ Spawning high-throughput worker routines across {len(tickers)} targets...")

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = {executor.submit(self.process_ticker, tk): tk for tk in tickers}
            for future in as_completed(futures):
                res = future.result()
                if res is None: continue
                ticker = res["ticker"]
                score = res["score"]
                all_updates[ticker] = [score, res["alpha_str"], datetime.now().strftime("%Y-%m-%d")]

                if score >= 8.5: t1.append(res)
                elif 6.5 <= score < 8.5: t2.append(res)
                elif 2.5 <= score < 6.5: neutral.append(res)
                else: t3.append(res)

        self.batch_write_results(all_updates)
        
        # Save Consolidated Local Profile Memory Caching States down cleanly
        try:
            with open(self.CACHE_FILE, 'w') as f: json.dump(self.vp_cache, f)
            logging.info("💾 Cached profiles committed safely to disk memory.")
        except Exception as e:
            logging.error(f"Failed to cache profile structures down to disk storage registers: {e}")

        self.send_report(t1, t2, neutral, t3)

    def send_report(self, t1, t2, neutral, t3):
        """Rule B: Universal absolute HTML character escaping framework across all metrics fields."""
        msg = f"<b>🚀 NEO QUANT ENGINE SUMMARY REPORT - {datetime.now().strftime('%Y-%m-%d')}</b>\n\n"
        
        msg += "<b>🔥 TIER 1: CONVICTION SELECTION (PEAD Validated Breakouts)</b>\n"
        if not t1: msg += "<i>No high-conviction PEAD setups matching your strategy criteria today.</i>\n"
        for i in t1:
            msg += f"• <b>{html.escape(str(i['ticker']))}</b> (Score: <b>{html.escape(str(i['score']))}</b>) | Spot: <b>${i['px']:.2f}</b>\n"
            msg += f"  📊 Condition: <i>{html.escape(str(i['status']))}</i>\n"
            msg += f"  🚧 Risk Anchoring: <i>{html.escape(str(i['exits']))}</i>\n\n"

        msg += "\n<b>👀 TIER 2: TECHNICAL WATCH (Coiling Patterns / Trend Breakouts)</b>\n"
        if not t2: msg += "<i>No auxiliary setups spotted.</i>\n"
        for i in t2:
            msg += f"• <b>{html.escape(str(i['ticker']))}</b> (Score: {html.escape(str(i['score']))}) | Spot: ${i['px']:.2f}\n"
            msg += f"  📊 Condition: <i>{html.escape(str(i['status']))}</i>\n\n"

        msg += "\n<b>💤 NO-ALLOCATION TRAP ZONE (Failed Setups or Distribution)</b>\n"
        no_trade = [html.escape(str(i['ticker'])) for i in neutral[:15]] + [html.escape(str(i['ticker'])) for i in t3[:15]]
        msg += (f"• " + " | ".join(no_trade) + "\n") if no_trade else "<i>Empty</i>\n"

        self.send_telegram_chunked(msg)
        logging.info("📢 Safe structural text-escaped report dispatched successfully.")

    def send_telegram_chunked(self, html_text):
        if not self.telegram_token: return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        if len(html_text) <= 4000:
            try: requests.post(url, json={"chat_id": self.telegram_chat_id, "text": html_text, "parse_mode": "HTML"}, timeout=10)
            except Exception: pass
            return
        lines = html_text.split('\n')
        curr = ""
        for line in lines:
            if len(curr) + len(line) + 1 > 4000:
                try: requests.post(url, json={"chat_id": self.telegram_chat_id, "text": curr, "parse_mode": "HTML"}, timeout=10)
                except Exception: pass
                curr = line + '\n'
            else: curr += line + '\n'
        if curr:
            try: requests.post(url, json={"chat_id": self.telegram_chat_id, "text": curr, "parse_mode": "HTML"}, timeout=10)
            except Exception: pass

if __name__ == "__main__":
    NeoQuantScannerPro().run()
