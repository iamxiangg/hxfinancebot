import os
import json
import time
import requests
import logging
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
from google.oauth2.service_account import Credentials
import googleapiclient.discovery

# Setup Logging to monitor CI/CD execution pipeline
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class NeoQuantScannerPro:
    def __init__(self):
        logging.info("🚀 Initializing Final Neo Quant Scanner Pro...")
        # NLP Engine Initialization (FinBERT Transformer model)
        self.tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        
        # Configuration & Environment Matrix
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

    # --- DATA PERSISTENCE: GOOGLE SHEETS (BATCH OPTIMIZED) ---
    def get_service(self, api_name, version):
        creds_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, 
            scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.metadata.readonly'])
        return googleapiclient.discovery.build(api_name, version, credentials=creds)

    def init_sheet_mapping(self):
        """Builds a flat index map of Ticker -> Spreadsheet Row Index at execution startup."""
        try:
            drive_service = self.get_service('drive', 'v3')
            query = f"name = '{self.SHEET_NAME}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
            results = drive_service.files().list(q=query, fields='files(id)').execute()
            if not results.get('files'): raise Exception("Spreadsheet file targets not found.")
            self._sheet_id = results['files'][0]['id']

            service = self.get_service('sheets', 'v4')
            result = service.spreadsheets().values().get(
                spreadsheetId=self._sheet_id, range=f"'{self.WORKSHEET_NAME}'!A:A").execute()
            rows = result.get('values', [])
            for i, row in enumerate(rows):
                if row: self._ticker_row_map[row[0].strip().upper()] = i + 1
        except Exception as e:
            logging.error(f"Failed to cleanly build Google Sheet row index map: {e}")
            raise

    def batch_write_results(self, update_data):
        """Pushes computed metrics to Google Sheets using a single batch payload."""
        if not update_data: return
        try:
            service = self.get_service('sheets', 'v4')
            data = [{'range': f"'{self.WORKSHEET_NAME}'!AL{self._ticker_row_map[t]}:AN{self._ticker_row_map[t]}", 'values': [v]} 
                    for t, v in update_data.items() if t in self._ticker_row_map]
            body = {'valueInputOption': 'USER_ENTERED', 'data': data}
            service.spreadsheets().values().batchUpdate(spreadsheetId=self._sheet_id, body=body).execute()
        except Exception as e:
            logging.error(f"Batch payload database write failed: {e}")

    # --- QUANT & NLP ENGINE LOGIC ---
    def get_sentiment(self, headlines):
        """Calculates positive probability distribution scores over aggregated public news items."""
        if not headlines: return 0.5
        inputs = self.tokenizer(headlines, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = self.model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        return probs[:, 0].mean().item()

    def get_pead_metrics(self, stock_obj):
        """Calculates Post-Earnings Announcement Drift window and surprises."""
        try:
            cal = stock_obj.get_calendar()
            e_date = pd.to_datetime(cal['Earnings Date'][0])
            days = (datetime.now().date() - e_date.date()).days
            hist = stock_obj.get_earnings_history()
            surp = float(hist['Surprise(%)'].iloc[0]) if not hist.empty else 0.0
            return surp, days
        except: return 0.0, 999

    def calculate_vp_nuanced(self, df):
        """Calculates Point of Control (PoC) and Value Area High (VAH) over volume distributions."""
        if df.empty: return 0, 0
        bins = pd.cut(df['Close'], bins=30)
        vol_prof = df.groupby(bins, observed=False)['Volume'].sum()
        sorted_vol = vol_prof.sort_values(ascending=False)
        cum_vol = sorted_vol.cumsum() / sorted_vol.sum()
        va = sorted_vol[cum_vol <= 0.70]
        
        poc = vol_prof.idxmax().mid
        vah = va.index.max().right if not va.empty else poc
        return float(poc), float(vah)

    def send_telegram(self, html_text):
        """Transmits fully structured HTML alerts directly into your Telegram channel."""
        if not self.telegram_token: return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.telegram_chat_id, "text": html_text, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            logging.error(f"Telegram broadcast gateway connection timed out: {e}")

    # --- MAIN SCANNER DISPATCH ROUTINE ---
    def run(self):
        self.init_sheet_mapping()
        tickers = list(self._ticker_row_map.keys())
        all_updates, t1, t2, t3, neutral = {}, [], [], [], []

        for ticker in tickers:
            if ticker in ["TICKER", "SYMBOL"]: continue
            try:
                logging.info(f"🔍 Analyzing {ticker}")
                stock = yf.Ticker(ticker)
                sector_etf = self.SECTOR_MAP.get(stock.info.get('sector'), 'SPY')
                
                # Structural Price Horizon Data
                hist_1y = stock.history(period="1y")
                if hist_1y.empty: continue
                
                data = yf.download([ticker, sector_etf], period="1y", auto_adjust=True, progress=False)
                if data.empty: continue
                
                lvl = 1 if data.columns.nlevels > 1 else 0
                t_prices = data['Close'][ticker].dropna() if lvl else data['Close'].dropna()
                s_prices = data['Close'][sector_etf].dropna() if lvl else data['Close'].dropna()
                
                # Mitigate deprecated Pandas sorting mechanics (Pandas 4 specification warning patch)
                combined = pd.concat([t_prices, s_prices], axis=1, sort=True).dropna()
                if len(combined) < 20: continue

                # Core Technical Aggregates
                px = float(t_prices.iloc[-1])
                m50 = t_prices.rolling(50).mean().iloc[-1]
                m200 = t_prices.rolling(200).mean().iloc[-1]
                bull = (px > m50 > m200) if not pd.isna(m200) else False
                
                # The Trigger Patch: Calculate local rolling resistance to form an actionable breakout ceiling
                resistance_20d = float(hist_1y['High'].tail(20).max())
                
                # Relative Performance Generation (Alpha vs Sector Engine)
                lookback = min(126, len(combined)-1)
                alpha = (combined.iloc[-1,0]/combined.iloc[-lookback,0]) - (combined.iloc[-1,1]/combined.iloc[-lookback,1])

                # Institutional Volume Profile Profile Generation (65 Trading Days)
                poc, vah = self.calculate_vp_nuanced(hist_1y.tail(65))
                
                # Natural Language News Ingest Framework
                news = stock.news[:5] if stock.news else []
                headlines = [n.get('title') or n.get('headline') or n.get('content',{}).get('title') for n in news]
                sent = self.get_sentiment([h for h in headlines if h])

                # Unified Scoring Matrix Engine Configuration
                score = (3.0 if alpha > 0 else 0) + (3.0 if bull else 0) + (2.0 if px > vah else 0) + (2.0 if sent > 0.60 else 0)
                surp, days = self.get_pead_metrics(stock)
                if 0 < days <= 45 and surp >= 2: score = min(score + 1, 10)
                elif days > 60: score = max(score - 1, 0)

                # Queue Google Spreadsheet Synchronization Payloads
                all_updates[ticker] = [round(score, 1), f"{alpha:+.2%}", datetime.now().strftime("%Y-%m-%d")]
                
                # Dynamic Structural Evaluation Logic
                # If price is extended greater than 5% past the institutional accumulation shelf, label as Extended.
                if px > (poc * 1.05):
                    status = "EXTENDED (Wait for Pullback / Tight Flag)"
                else:
                    status = "COILING (Watch Breakout Target)"

                item = {
                    "ticker": ticker, "score": score, "alpha": alpha, 
                    "px": px, "poc": poc, "resistance": resistance_20d, "status": status,
                    "target": px * 1.20  # Standard risk-ratio benchmark target mapping
                }
                
                # Tier Threshold Segregation
                if score >= 7.5: t1.append(item)
                elif 6.0 <= score < 7.5: t2.append(item)
                elif 0.1 <= score < 6.0: neutral.append(item)
                else: t3.append(item)

                # Rate limiter to safely navigate high-throughput pipeline restrictions
                time.sleep(0.5) 
            except Exception as e:
                logging.error(f"Processing error context on tracker asset allocation {ticker}: {e}")

        # Final Execution Outputs
        self.batch_write_results(all_updates)
        self.send_report(t1, t2, neutral, t3)

    # --- REPORT CONSTRUTION & DELIVERY ---
    def send_report(self, t1, t2, neutral, t3):
        msg = f"<b>🚀 NEO QUANT PRO REPORT - {datetime.now().strftime('%Y-%m-%d')}</b>\n\n"
        
        # TIER 1 OUTBOUND RENDERING
        msg += "<b>🔥 TIER 1: ACTIONABLE BUYS (In Play)</b>\n"
        if not t1: msg += "<i>No high-conviction setups discovered</i>\n"
        for i in t1:
            msg += f"• <b>{i['ticker']}</b> | Price: <b>${i['px']:.2f}</b> | 🛡️ Support (PoC): ${i['poc']:.2f} | 🎯 Target: ${i['target']:.2f}\n"

        # TIER 2 OUTBOUND STRUCTURAL RENDERING
        msg += "\n<b>👀 TIER 2: HIGH-ALPHA WATCH (Tactical Setups)</b>\n"
        if not t2: msg += "<i>Empty</i>\n"
        for i in t2:
            msg += f"• <b>{i['ticker']}</b> ({i['score']:.1f}) | Price: ${i['px']:.2f}\n"
            msg += f"  🛡️ Support (PoC): ${i['poc']:.2f} | 🚀 Breakout Target: &gt;${i['resistance']:.2f}\n"
            msg += f"  📊 Status: <i>{i['status']}</i>\n\n"

        # COMPACT NO-TRADE LOGGING OVER NEUTRAL AND TRAP BUCKETS
        msg += "<b>💤 NEUTRAL / ⚠️ TRAPS (No-Allocation Zone)</b>\n"
        no_trade = [i['ticker'] for i in neutral[:8]] + [i['ticker'] for i in t3[:8]]
        if no_trade:
            msg += f"• " + " | ".join(no_trade) + "\n"
        else:
            msg += "<i>Empty</i>\n"
            
        msg += "\n<i>💡 Strategy Architecture: Tier 1 = Manage Implemented Trade Risk. Tier 2 = Place Pending Alert Targets on Breakout.</i>"

        self.send_telegram(msg)

if __name__ == "__main__":
    NeoQuantScannerPro().run()
