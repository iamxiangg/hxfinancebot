import os
import json
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
import google.auth
from google.oauth2.service_account import Credentials
import googleapiclient.discovery

class UnifiedPositioningScanner:
    def __init__(self):
        print("🚀 Initializing Quant Scanner & FinBERT Model...")
        # Load NLP Brain
        self.tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        
        # Sector Benchmarks
        self.SECTOR_MAP = {
            'Technology': 'XLK', 'Financial Services': 'XLF', 'Healthcare': 'XLV',
            'Consumer Cyclical': 'XLY', 'Communication Services': 'XLC', 'Industrials': 'XLI',
            'Consumer Defensive': 'XLP', 'Energy': 'XLE', 'Real Estate': 'XLRE',
            'Utilities': 'XLU', 'Basic Materials': 'XLB'
        }
        
        # Env Variables
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID")

    def get_google_sheets_client(self):
        """Authenticates using raw JSON string (Old BTD Method)."""
        creds_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        if not creds_json:
            raise ValueError("Secret GCP_SERVICE_ACCOUNT_FILE is missing")
        
        try:
            creds_dict = json.loads(creds_json)
            # Standard V4 Sheets Scope
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            return googleapiclient.discovery.build('sheets', 'v4', credentials=creds)
        except Exception as e:
            print(f"❌ Auth Failed: {e}")
            raise

    def fetch_tickers_from_sheet(self):
        """Reads Column A from Sheet1"""
        try:
            service = self.get_google_sheets_client()
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range="Sheet1!A2:A"
            ).execute()
            values = result.get('values', [])
            return [row[0].strip().upper() for row in values if row and row[0]]
        except Exception as e:
            print(f"❌ Error fetching tickers: {e}")
            return []

    def write_scores_to_sheet(self, ticker, score, alpha):
        """Updates Col B (Score), C (Alpha), D (Date)"""
        try:
            service = self.get_google_sheets_client()
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range="Sheet1!A1:A"
            ).execute()
            rows = result.get('values', [])
            
            row_idx = next((i+1 for i, r in enumerate(rows) if r and r[0].strip().upper() == ticker), None)
            if not row_idx: return

            current_date = datetime.now().strftime("%Y-%m-%d")
            values = [[round(score, 1), f"{alpha:+.2%}", current_date]]
            service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, 
                range=f"Sheet1!B{row_idx}:D{row_idx}", 
                valueInputOption="USER_ENTERED", 
                body={'values': values}
            ).execute()
            print(f"✅ Saved {ticker} to row {row_idx}")
        except Exception as e:
            print(f"❌ Save error for {ticker}: {e}")

    def get_finbert_sentiment(self, headlines):
        if not headlines: return 0.5
        inputs = self.tokenizer(headlines, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = self.model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        score = probs[:, 0].mean().item() - probs[:, 1].mean().item()
        return (score + 1) / 2

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
                stock = yf.Ticker(ticker)
                sector_etf = self.SECTOR_MAP.get(stock.info.get('sector'), 'SPY')
                data = yf.download([ticker, sector_etf], period="1y", interval="1d", progress=False)['Adj Close']
                
                # 1. Regime
                px, m50, m200 = data[ticker].iloc[-1], data[ticker].rolling(50).mean().iloc[-1], data[ticker].rolling(200).mean().iloc[-1]
                bull = px > m50 > m200
                
                # 2. Alpha
                alpha = (data[ticker].iloc[-1]/data[ticker].iloc[-126] - 1) - (data[sector_etf].iloc[-1]/data[sector_etf].iloc[-126] - 1)
                
                # 3. Volume
                hist = stock.history(period="1y")
                poc65, val65, vah65 = self.calculate_volume_profile(hist.tail(65))
                
                # 4. PEAD & News
                surp, days = self.get_pead_metrics(stock)
                sent = self.get_finbert_sentiment([n['title'] for n in stock.news[:5]])
                
                # Scoring
                score = (3.0 if alpha > 0 else 0) + (3.0 if bull else 0) + (2.0 if px > vah65 else 0) + (2.0 if sent > 0.65 else 0)
                if 0 < days <= 45 and surp >= 2: score = min(score + 1, 10)
                elif days > 60: score = max(score - 1, 0)

                self.write_scores_to_sheet(ticker, score, alpha)

                if score >= 7.5:
                    msg = f"💎 *ALPHA ALERT: {ticker}* ({score:.1f}/10)\nAlpha: {alpha:+.2%} vs {sector_etf}\nRegime: {'🟢' if bull else '🔴'}\nSentiment: {sent:.2f}\nFloor: ${poc65:.2f}"
                    self.send_telegram(msg)
            except Exception as e: print(f"Error {ticker}: {e}")

if __name__ == "__main__":
    UnifiedPositioningScanner().run()
