import os
import json
import time
import requests
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
from google.oauth2.service_account import Credentials
import googleapiclient.discovery

class UnifiedPositioningScanner:
    def __init__(self):
        print("🚀 Initializing Validated Quant Scanner (YF 2026 Compliant)...")
        self.tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        
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
        self._cached_sheet_id = None 

    def get_service(self, api_name, version):
        creds_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        if not creds_json: raise ValueError("Secret GCP_SERVICE_ACCOUNT_FILE missing")
        creds_dict = json.loads(creds_json)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.metadata.readonly']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return googleapiclient.discovery.build(api_name, version, credentials=creds)

    def find_sheet_id_by_name(self):
        if self._cached_sheet_id: return self._cached_sheet_id
        drive_service = self.get_service('drive', 'v3')
        query = f"name = '{self.SHEET_NAME}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        items = results.get('files', [])
        if not items: raise Exception(f"Spreadsheet '{self.SHEET_NAME}' not found.")
        self._cached_sheet_id = items[0]['id']
        return self._cached_sheet_id

    def fetch_tickers_from_sheet(self):
        try:
            sheet_id = self.find_sheet_id_by_name()
            service = self.get_service('sheets', 'v4')
            result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{self.WORKSHEET_NAME}'!A2:A").execute()
            return [row[0].strip().upper() for row in result.get('values', []) if row and row[0]]
        except Exception as e:
            print(f"❌ Error fetching tickers: {e}")
            return []

    def write_scores_to_sheet(self, ticker, score, alpha):
        try:
            sheet_id = self.find_sheet_id_by_name()
            service = self.get_service('sheets', 'v4')
            rows = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{self.WORKSHEET_NAME}'!A:A").execute().get('values', [])
            row_idx = next((i+1 for i, r in enumerate(rows) if r and r[0].strip().upper() == ticker), None)
            if row_idx:
                values = [[round(score, 1), f"{alpha:+.2%}", datetime.now().strftime("%Y-%m-%d")]]
                service.spreadsheets().values().update(spreadsheetId=sheet_id, range=f"'{self.WORKSHEET_NAME}'!AL{row_idx}:AN{row_idx}", valueInputOption="USER_ENTERED", body={'values': values}).execute()
        except Exception as e: print(f"❌ Save error: {e}")

    def get_finbert_sentiment(self, headlines):
        if not headlines: return 0.5
        inputs = self.tokenizer(headlines, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = self.model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        return (probs[:, 0].mean().item() - probs[:, 1].mean().item() + 1) / 2

    def calculate_volume_profile(self, df):
        if df.empty: return 0, 0, 0
        price_bins = pd.cut(df['Close'], bins=50)
        vol_prof = df.groupby(price_bins, observed=False)['Volume'].sum()
        poc = vol_prof.idxmax().mid
        sorted_vol = vol_prof.sort_values(ascending=False)
        cum_vol = sorted_vol.cumsum() / sorted_vol.sum()
        va = sorted_vol[cum_vol <= 0.70]
        return poc, va.index.min().left if not va.empty else poc, va.index.max().right if not va.empty else poc

    def get_pead_metrics(self, stock_obj):
        try:
            cal = stock_obj.get_calendar()
            e_date = cal['Earnings Date'][0]
            days = (datetime.now().date() - (e_date.date() if isinstance(e_date, datetime) else e_date)).days
            hist = stock_obj.get_earnings_history()
            surp = hist['Surprise(%)'].iloc[0] if not hist.empty else 0.0
            return float(surp or 0), int(days)
        except: return 0.0, 999

    def send_telegram(self, text):
        if not self.telegram_token or not self.telegram_chat_id: return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        requests.post(url, json={"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

    def run(self):
        tickers = self.fetch_tickers_from_sheet()
        for ticker in tickers:
            try:
                print(f"🔍 Analyzing {ticker}...")
                stock = yf.Ticker(ticker)
                sector_etf = self.SECTOR_MAP.get(stock.info.get('sector'), 'SPY')
                
                # Validated yfinance 2026 download
                data = yf.download([ticker, sector_etf], period="1y", interval="1d", progress=False, auto_adjust=True)
                if data.empty or 'Close' not in data: continue

                ticker_prices = data['Close'][ticker].dropna()
                sector_prices = data['Close'][sector_etf].dropna()
                if ticker_prices.empty: continue
                
                # Quant Calcs
                px, m50, m200 = ticker_prices.iloc[-1], ticker_prices.rolling(50).mean().iloc[-1], ticker_prices.rolling(200).mean().iloc[-1]
                bull = px > m50 > m200
                lookback = min(126, len(ticker_prices)-1)
                alpha = (ticker_prices.iloc[-1]/ticker_prices.iloc[-lookback]) - (sector_prices.iloc[-1]/sector_prices.iloc[-lookback])
                
                hist = stock.history(period="1y") # history() already auto-adjusts
                poc65, val65, vah65 = self.calculate_volume_profile(hist.tail(65))
                
                # Resilient News Validation
                news_items = stock.news[:5] if stock.news else []
                headlines = [n.get('title') or n.get('headline') or n.get('content', {}).get('title') for n in news_items]
                headlines = [h for h in headlines if h]
                sent = self.get_finbert_sentiment(headlines)

                surp, days = self.get_pead_metrics(stock)
                
                score = (3.0 if alpha > 0 else 0) + (3.0 if bull else 0) + (2.0 if px > vah65 else 0) + (2.0 if sent > 0.65 else 0)
                if 0 < days <= 45 and surp >= 2: score = min(score + 1, 10)
                elif days > 60: score = max(score - 1, 0)

                self.write_scores_to_sheet(ticker, score, alpha)
                if score >= 7.5:
                    self.send_telegram(f"💎 *ALPHA ALERT: {ticker}* ({score:.1f}/10)\nAlpha: {alpha:+.2%} vs {sector_etf}\nRegime: {'🟢' if bull else '🔴'}\nSentiment: {sent:.2f}\nFloor: ${poc65:.2f}")
                
                time.sleep(1) # Prevent rate limiting
            except Exception as e: print(f"❌ Error {ticker}: {e}")

if __name__ == "__main__":
    UnifiedPositioningScanner().run()
