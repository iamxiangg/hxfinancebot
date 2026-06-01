import os
import json
import time
import requests
import logging
import threading
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from google.oauth2.service_account import Credentials
import googleapiclient.discovery
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── NLP IMPORTS ──
try:
    from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class NeoQuantScannerPro:
    def __init__(self):
        logging.info("🚀 Deploying Production Neo Quant Scanner Pro (v4.0 - All Bugs Patched)...")
        
        self.SHEET_NAME = "Xiang Stock Analysis"
        self.WORKSHEET_NAME = "Stock Summary USD"
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

        # ── INITIALIZE FINBERT (Thread-Safe via Lock) ──
        if HAS_TRANSFORMERS:
            try:
                logging.info("🧠 Initializing FinBERT Sentiment Architecture...")
                tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
                model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
                self.nlp_engine = pipeline("sentiment-analysis", model=model, tokenizer=tokenizer, device=-1)
            except Exception as e:
                logging.error(f"FinBERT initialization failed: {e}. Falling back.")
                self.nlp_engine = None
        else:
            logging.warning("⚠️ 'transformers' not found. Run: pip install transformers torch")
            self.nlp_engine = None

    # ───────────────────────────────────────────────
    # GOOGLE SHEETS PERSISTENCE
    # ───────────────────────────────────────────────
    def get_service(self, api_name, version):
        creds_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive.metadata.readonly']
        )
        return googleapiclient.discovery.build(api_name, version, credentials=creds)

    def init_sheet_mapping(self):
        try:
            drive_service = self.get_service('drive', 'v3')
            query = f"name = '{self.SHEET_NAME}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
            results = drive_service.files().list(q=query, fields='files(id)').execute()
            if not results.get('files'):
                raise Exception("Spreadsheet file targets not found.")
            self._sheet_id = results['files'][0]['id']

            service = self.get_service('sheets', 'v4')
            result = service.spreadsheets().values().get(
                spreadsheetId=self._sheet_id,
                range=f"'{self.WORKSHEET_NAME}'!A:A"
            ).execute()
            rows = result.get('values', [])
            for i, row in enumerate(rows):
                if row and row[0]:
                    clean_ticker = row[0].strip().upper().replace('.', '-')
                    self._ticker_row_map[clean_ticker] = i + 1
        except Exception as e:
            logging.error(f"Failed to build sheet row map: {e}")
            raise

    def batch_write_results(self, update_data):
        if not update_data:
            return
        try:
            service = self.get_service('sheets', 'v4')
            data = [
                {
                    'range': f"'{self.WORKSHEET_NAME}'!AL{self._ticker_row_map[t]}:AN{self._ticker_row_map[t]}",
                    'values': [v]
                }
                for t, v in update_data.items() if t in self._ticker_row_map
            ]
            for i in range(0, len(data), 100):
                chunk = data[i:i+100]
                body = {'valueInputOption': 'USER_ENTERED', 'data': chunk}
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self._sheet_id, body=body
                ).execute()
        except Exception as e:
            logging.error(f"Batch write failed: {e}")

    # ───────────────────────────────────────────────
    # FIXED BUG #1 & #5: PEAD METRICS (Ordering-Safe)
    # ───────────────────────────────────────────────
    def get_pead_metrics(self, stock_obj):
        """
        FIXED: Uses get_earnings_dates() exclusively (no stock.calendar).
        FIXED: Explicitly sorts index to avoid ordering-dependent .iloc[0] vs [-1].
        """
        try:
            earnings_df = stock_obj.get_earnings_dates(limit=4)
            if earnings_df is None or earnings_df.empty:
                return 0.0, 999

            past = earnings_df[earnings_df.index <= pd.Timestamp.now()]
            if past.empty:
                return 0.0, 999

            # FIX BUG #5: Sort ascending, take last = most recent
            past_sorted = past.sort_index()
            most_recent_date = past_sorted.index[-1]
            days = (datetime.now().date() - most_recent_date.date()).days

            surp_col = [c for c in past_sorted.columns if 'Surprise' in c]
            if surp_col:
                surp_series = past_sorted[surp_col[0]].dropna()
                if not surp_series.empty:
                    surp = float(surp_series.iloc[-1])
                    return surp, days
            return 0.0, days
        except Exception as e:
            logging.debug(f"PEAD metrics lookup failed: {e}")
            return 0.0, 999

    # ───────────────────────────────────────────────
    # FIXED BUG #6: FINBERT (Thread-Safe via Lock)
    # ───────────────────────────────────────────────
    def get_finbert_sentiment(self, stock_obj):
        """Thread-safe FinBERT inference using self._nlp_lock."""
        if not self.nlp_engine:
            return 0.0, "BERT Inactive"
        try:
            news_items = stock_obj.news
            if not news_items:
                return 0.0, "Neutral (No News)"

            titles = [item['title'] for item in news_items[:6] if 'title' in item]
            if not titles:
                return 0.0, "Neutral (Blank Titles)"

            # FIX BUG #6: Thread-safe inference
            with self._nlp_lock:
                predictions = self.nlp_engine(titles)

            net_sentiment_score = 0.0
            pos_count, neg_count = 0, 0

            for p in predictions:
                label = p['label'].upper()
                score = p['score']
                if 'POS' in label:
                    net_sentiment_score += score
                    pos_count += 1
                elif 'NEG' in label:
                    net_sentiment_score -= score
                    neg_count += 1

            avg_score = net_sentiment_score / len(titles)

            if avg_score > 0.15:
                status_str = f"🚀 Bullish NLP ({pos_count}/{len(titles)} Pos)"
            elif avg_score < -0.15:
                status_str = f"⚠️ Bearish NLP ({neg_count}/{len(titles)} Neg)"
            else:
                status_str = "💤 Neutral Sentiment"

            return float(avg_score), status_str
        except Exception as e:
            logging.debug(f"FinBERT inference failed: {e}")
            return 0.0, "Inference Errored"

    # ───────────────────────────────────────────────
    # VOLUME PROFILE
    # ───────────────────────────────────────────────
    def calculate_vp_nuanced(self, df):
        if df.empty or len(df) < 15:
            return 0, 0
        n_bins = min(30, max(8, int(np.sqrt(len(df)) * 1.5)))
        bins = pd.cut(df['Close'], bins=n_bins)
        vol_prof = df.groupby(bins, observed=False)['Volume'].sum()
        sorted_vol = vol_prof.sort_values(ascending=False)
        cum_vol = sorted_vol.cumsum() / sorted_vol.sum()
        va = sorted_vol[cum_vol <= 0.70]
        poc = vol_prof.idxmax().mid
        vah = va.index.max().right if not va.empty else poc
        return float(poc), float(vah)

    # ───────────────────────────────────────────────
    # TELEGRAM
    # ───────────────────────────────────────────────
    def send_telegram_chunked(self, html_text):
        if not self.telegram_token:
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        if len(html_text) <= 4000:
            try:
                requests.post(
                    url,
                    json={"chat_id": self.telegram_chat_id, "text": html_text, "parse_mode": "HTML"},
                    timeout=10
                )
            except Exception:
                pass
            return
        lines = html_text.split('\n')
        current_chunk = ""
        for line in lines:
            if len(current_chunk) + len(line) + 1 > 4000:
                requests.post(
                    url,
                    json={"chat_id": self.telegram_chat_id, "text": current_chunk, "parse_mode": "HTML"},
                    timeout=10
                )
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        if current_chunk:
            requests.post(
                url,
                json={"chat_id": self.telegram_chat_id, "text": current_chunk, "parse_mode": "HTML"},
                timeout=10
            )

    # ───────────────────────────────────────────────
    # PER-TICKER PROCESSING (ALL BUGS FIXED)
    # ───────────────────────────────────────────────
    def process_ticker(self, ticker):
        clean_tk = ticker.replace('.', '-')
        try:
            stock = yf.Ticker(clean_tk)
            info = stock.info
            sector = info.get('sector', 'Technology') if info else 'Technology'
            sector_etf = self.SECTOR_MAP.get(sector, 'SPY')

            data = yf.download([clean_tk, sector_etf], period="1y", auto_adjust=True, progress=False)
            if data.empty or 'Close' not in data:
                return None

            lvl = data.columns.nlevels > 1
            try:
                t_prices = data['Close'][clean_tk].dropna() if lvl else data['Close'].dropna()
                t_highs = data['High'][clean_tk].dropna() if lvl else data['High'].dropna()
                t_lows = data['Low'][clean_tk].dropna() if lvl else data['Low'].dropna()
                t_vols = data['Volume'][clean_tk].dropna() if lvl else data['Volume'].dropna()
                s_prices = data['Close'][sector_etf].dropna() if lvl else data['Close'].dropna()
            except KeyError:
                return None

            if len(t_prices) < 25:
                return None
            px = float(t_prices.iloc[-1])
            if px <= 0:
                return None
            
            m10 = t_prices.rolling(10).mean().iloc[-1]
            m20 = t_prices.rolling(20).mean().iloc[-1]
            m50 = t_prices.rolling(50).mean().iloc[-1]
            m200 = t_prices.rolling(200).mean().iloc[-1] if len(t_prices) >= 200 else None
            bull_regime = (px > m50 > m200) if m200 is not None else (px > m50)

            lookback = min(126, len(t_prices)-1)
            asset_ret = (t_prices.iloc[-1] / t_prices.iloc[-lookback]) - 1
            sector_ret = (s_prices.iloc[-1] / s_prices.iloc[-lookback]) - 1
            alpha = asset_ret - sector_ret

            # FIX BUG #3: MANUAL VOLUME PROFILE SPLIT ADJUSTMENT IS MULTIPLICATIVE
            adjusted_vols = t_vols.copy()
            try:
                splits = stock.splits
                if splits is not None and not splits.empty:
                    horizon_start = t_prices.index[-65]
                    active_splits = splits[splits.index >= horizon_start]
                    for split_date, ratio in active_splits.items():
                        if ratio > 0:
                            adjusted_vols.loc[adjusted_vols.index < split_date] *= ratio
            except Exception:
                pass

            vol_df = pd.DataFrame({'Close': t_prices, 'Volume': adjusted_vols}).tail(65)
            poc, vah = self.calculate_vp_nuanced(vol_df)

            surp, days = self.get_pead_metrics(stock)
            nlp_score, nlp_desc = self.get_finbert_sentiment(stock)

            base_score = 0.0
            is_pead_active = False
            status = "COILING (Standard Technical Setup)"
            exit_logic_note = "Standard 20% Technical Target Frame"

            historical_base = t_highs.iloc[-25:-2]
            resistance_20d = float(historical_base.max()) if not historical_base.empty else px

            if surp >= 15.0 and 0 <= days <= 90:
                is_pead_active = True
                
                if days < 5:
                    score = 0.1
                    status = "⛔ PEAD PENALTY BOX (Evaluating Follow-Through & Washouts)"
                else:
                    # FIX BUG #5: Use ordering-safe ascending sort to determine correct date anchor
                    earnings_df = stock.get_earnings_dates(limit=4)
                    if earnings_df is not None and not earnings_df.empty:
                        past_earnings = earnings_df[earnings_df.index <= pd.Timestamp.now()]
                        if not past_earnings.empty:
                            earn_dt = past_earnings.sort_index().index[-1]
                            earn_bar_pos = abs(t_prices.index - earn_dt).argmin()
                            earn_idx = int(earn_bar_pos - len(t_prices))
                        else:
                            earn_idx = max(-len(t_prices), -int(days))
                    else:
                        earn_idx = max(-len(t_prices), -int(days))

                    if abs(earn_idx) <= len(t_prices):
                        e_close = t_prices.iloc[earn_idx]
                        e_high = t_highs.iloc[earn_idx]
                        e_low = t_lows.iloc[earn_idx]
                        earn_day_range = e_high - e_low
                        close_pos = (e_close - e_low) / earn_day_range if earn_day_range != 0 else 0.5
                    else:
                        close_pos = 0.5

                    if close_pos < 0.40:
                        score = 0.0
                        status = "⚠️ PEAD DISTRIBUTION (Weak Earnings Day Close Position)"
                    else:
                        # FIX BUG #4: Convert calendar days into accurate trading bar historical lookbacks
                        bars_since_earnings = -earn_idx
                        if bars_since_earnings <= 0:
                            bars_since_earnings = 1

                        post_earnings_history = t_highs.tail(bars_since_earnings)
                        pe_max_high = float(post_earnings_history.max()) if not post_earnings_history.empty else px
                        correction_depth = (pe_max_high - px) / pe_max_high if pe_max_high > 0 else 0
                        
                        depth_bonus = 0.0
                        depth_valid = False
                        if correction_depth <= 0.10:
                            depth_bonus = 1.5   
                            depth_valid = True
                        elif 0.10 < correction_depth <= 0.25:
                            depth_bonus = 0.5   
                            depth_valid = True
                        
                        closes_series = t_prices.tail(bars_since_earnings)
                        ma20_series = t_prices.rolling(20).mean().tail(bars_since_earnings)
                        ma20_violations = (closes_series < ma20_series).sum() if not ma20_series.empty else 0
                        respects_ma_floor = (ma20_violations <= 3) and (px > m50)
                        
                        recent_vol_avg = adjusted_vols.tail(4).mean()
                        baseline_vol_avg = adjusted_vols.tail(50).mean()
                        volume_dried = recent_vol_avg < baseline_vol_avg
                        respects_poc = px >= (poc * 0.97)

                        if depth_valid and respects_ma_floor and volume_dried and respects_poc:
                            alpha_modifier = min(2.0, max(0.0, alpha * 10)) 
                            time_decay = 1.0 if days <= 45 else 0.6
                            
                            nlp_modifier = 0.5 if nlp_score > 0.25 else (-1.5 if nlp_score < -0.25 else 0.0)

                            base_score = (7.0 + depth_bonus + alpha_modifier + nlp_modifier) * time_decay
                            score = min(base_score, 10.0)
                            
                            exit_logic_note = f"🎯 1/2 Trim Target: ${px * 1.10:.2f} (3-5 Days) | 🛡️ Tail Remaining Slot on 20-DMA (${m20:.2f})"

                            if px >= resistance_20d:
                                status = f"🔥 PEAD BREAKOUT | {nlp_desc}"
                                if score < 8.0: score = 8.5
                            else:
                                status = f"⏳ PEAD FLAG COILING | {nlp_desc}"
                        else:
                            score = 2.0
                            # FIX ISSUE #7: Appended NLP context to degraded flag paths
                            status = f"⚠️ PEAD STRUCTURE DEGRADED | {nlp_desc}"
            
            if not is_pead_active:
                alpha_score = 3.0 if alpha > 0.05 else (1.5 if alpha > 0 else 0)
                regime_score = 3.0 if bull_regime else 0
                profile_score = 2.0 if px > vah else 0
                
                nlp_modifier = 0.5 if nlp_score > 0.3 else 0.0
                score = alpha_score + regime_score + profile_score + nlp_modifier
                
                if px >= resistance_20d:
                    status = f"🔥 TECHNICAL BREAKOUT ACTIVE | {nlp_desc}"
                elif px > (poc * 1.05):
                    status = "EXTENDED (Wait for Pullback)"
                else:
                    status = f"COILING | {nlp_desc}"

            return {
                "ticker": clean_tk, "score": round(score, 1), "alpha_str": f"{alpha:+.2%}",
                "px": px, "poc": poc, "resistance": resistance_20d, "status": status, "exits": exit_logic_note
            }
        except Exception as e:
            logging.error(f"Execution failed for ticker {ticker}: {e}")
            return None

    # ───────────────────────────────────────────────
    # ENTRANCE & EXECUTION DISPATCH
    # ───────────────────────────────────────────────
    def run(self):
        self.init_sheet_mapping()
        tickers = [t for t in self._ticker_row_map.keys() if t not in ["TICKER", "SYMBOL"]]
        all_updates, t1, t2, t3, neutral = {}, [], [], [], []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self.process_ticker, tk): tk for tk in tickers}
            for future in as_completed(futures):
                res = future.result()
                if res is None:
                    continue
                ticker = res["ticker"]
                score = res["score"]
                all_updates[ticker] = [score, res["alpha_str"], datetime.now().strftime("%Y-%m-%d")]

                if score >= 8.0: t1.append(res)
                elif 6.5 <= score < 8.0: t2.append(res)
                elif 1.0 <= score < 6.5: neutral.append(res)
                else: t3.append(res)

        self.batch_write_results(all_updates)
        self.send_report(t1, t2, neutral, t3)

    def send_report(self, t1, t2, neutral, t3):
        msg = f"<b>🚀 NEO QUANT DUO ENGINE REPORT - {datetime.now().strftime('%Y-%m-%d')}</b>\n\n"
        msg += "<b>🔥 TIER 1: ACTIONABLE BUYS (In Play / Breakouts + FinBERT Verified)</b>\n"
        if not t1:
            msg += "<i>No dual-conviction setups discovered today.</i>\n"
        for i in t1:
            msg += f"• <b>{i['ticker']}</b> (Score: <b>{i['score']:.1f}</b>) | Price: <b>${i['px']:.2f}</b>\n"
            msg += f"  📊 Status: <i>{i['status']}</i>\n"
            msg += f"  🚧 Exit Protocol: <i>{i['exits']}</i>\n\n"

        msg += "\n<b>👀 TIER 2: HIGH-ALPHA WATCH (Coiling / Technical Monitoring)</b>\n"
        if not t2:
            msg += "<i>No pending setups.</i>\n"
        for i in t2:
            msg += f"• <b>{i['ticker']}</b> (Score: {i['score']:.1f}) | Price: ${i['px']:.2f}\n"
            msg += f"  📊 Status: <i>{i['status']}</i>\n\n"

        msg += "\n<b>💤 NEUTRAL / ⚠️ TRAPS (No-Allocation Zone)</b>\n"
        no_trade = [i['ticker'] for i in neutral[:12]] + [i['ticker'] for i in t3[:12]]
        msg += (f"• " + " | ".join(no_trade) + "\n") if no_trade else "<i>Empty</i>\n"

        self.send_telegram_chunked(msg)

if __name__ == "__main__":
    NeoQuantScannerPro().run()
