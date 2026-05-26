import os
import json
import base64
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
        print("Initializing FinBERT Brain...")
        # 1. Initialize Transformers for FinBERT Sentiment
        self.tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        
        # 2. Industry Standard SPDR Sector ETF Maps (Long-Term Benchmarks)
        self.SECTOR_MAP = {
            'Technology': 'XLK', 'Financial Services': 'XLF', 'Healthcare': 'XLV',
            'Consumer Cyclical': 'XLY', 'Communication Services': 'XLC', 'Industrials': 'XLI',
            'Consumer Defensive': 'XLP', 'Energy': 'XLE', 'Real Estate': 'XLRE',
            'Utilities': 'XLU', 'Basic Materials': 'XLB'
        }
        
        # 3. Environment Secrets Setup
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID")

    def get_google_sheets_client(self):
        """Decodes the Base64 GCP Key string and authenticates Google Sheets API"""
        encoded_json = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
        if not encoded_json:
            raise ValueError("GCP Credentials missing from Environment Secrets.")
        
        decoded_json = base64.b64decode(encoded_json).decode('utf-8')
        creds_dict = json.loads(decoded_json)
        
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return googleapiclient.discovery.build('sheets', 'v4', credentials=creds)

    def fetch_tickers_from_sheet(self):
        """Reads Column A from the target Google Sheet, filtering out headers"""
        try:
            service = self.get_google_sheets_client()
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range="Sheet1!A2:A"
            ).execute()
            values = result.get('values', [])
            return [row[0].strip() for row in values if row and row[0]]
        except Exception as e:
            print(f"Error accessing Google Sheet: {e}")
            return [] # Returns an empty list safely if unauthorized

    def write_scores_to_sheet(self, ticker_symbol, score, alpha):
        """Finds the ticker row in Google Sheets and writes the calculated quant metrics"""
        try:
            service = self.get_google_sheets_client()
            
            # Fetch all current tickers in Column A to find the exact matching row index
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range="Sheet1!A1:A"
            ).execute()
            rows = result.get('values', [])
            
            row_index = None
            for idx, row in enumerate(rows):
                if row and row[0].strip() == ticker_symbol:
                    row_index = idx + 1  # Google Sheets is 1-indexed
                    break
            
            if row_index is None:
                print(f"⚠️ Could not find {ticker_symbol} in Google Sheet to update metrics.")
                return

            # Prepare payload: Score (Col B), Alpha % (Col C), Today's Date (Col D)
            current_date = datetime.now().strftime("%Y-%m-%d")
            values = [[round(score, 1), f"{alpha:+.2%}", current_date]]
            body = {'values': values}
            
            # Target range dynamically based on found row index (B{row}:D{row})
            target_range = f"Sheet1!B{row_index}:D{row_index}"
            
            service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, 
                range=target_range, 
                valueInputOption="USER_ENTERED", 
                body=body
            ).execute()
            print(f"💾 Successfully saved {ticker_symbol} metrics to Sheet row {row_index}.")
            
        except Exception as e:
            print(f"❌ Failed to write metrics to Google Sheet for {ticker_symbol}: {e}")

    def get_finbert_sentiment(self, headlines):
        """Runs batched inference across top news headers, extracting core bias"""
        if not headlines: 
            return 0.5
        inputs = self.tokenizer(headlines, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = self.model(**inputs)
        predictions = torch.nn.functional.softmax(outputs.logits, dim=-1)
        
        # Weight structure: Positive score minus Negative score
        score = predictions[:, 0].mean().item() - predictions[:, 1].mean().item()
        return (score + 1) / 2 # Normalized to standard 0.0 - 1.0 scale

    def calculate_volume_profile(self, df_slice):
        """Extracts point-of-control (POC) and value area markers out of input data"""
        if df_slice.empty:
            return 0, 0, 0
        price_bins = pd.cut(df_slice['Close'], bins=50)
        volume_profile = df_slice.groupby(price_bins, observed=False)['Volume'].sum()
        poc = volume_profile.idxmax().mid
        
        sorted_vol = volume_profile.sort_values(ascending=False)
        cumulative_vol = sorted_vol.cumsum() / sorted_vol.sum()
        value_area = sorted_vol[cumulative_vol <= 0.70]
        
        if value_area.empty:
            return poc, poc, poc
        return poc, value_area.index.min().left, value_area.index.max().right

    def get_pead_metrics(self, stock_obj):
        """Calculates earnings surprise performance and the explicit days elapsed"""
        try:
            calendar = stock_obj.get_calendar()
            if not calendar or 'Earnings Date' not in calendar:
                return 0.0, 999
            
            last_earnings = calendar['Earnings Date'][0]
            if isinstance(last_earnings, datetime):
                last_earnings = last_earnings.date()
                
            days_spent = (datetime.now().date() - last_earnings).days
            
            earnings_history = stock_obj.get_earnings_history()
            surprise = 0.0
            if not earnings_history.empty and 'Surprise(%)' in earnings_history.columns:
                surprise = earnings_history['Surprise(%)'].iloc[0] or 0.0
                
            return float(surprise), int(days_spent)
        except:
            return 0.0, 999

    def send_telegram_alert(self, text):
        """Dispatches automated notifications directly to your custom Telegram instance"""
        # FIX: Corrected SyntaxError by separating boolean states entirely
        if not self.telegram_token or not self.telegram_chat_id:
            print(text)
            return
            
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Telegram dispatch failed: {e}")

    def scan_pipeline(self):
        """Main orchestrator for evaluation pipelines"""
        tickers = self.fetch_tickers_from_sheet()
        if not tickers:
            print("No tickers found to scan. Exiting.")
            return
            
        print(f"Loaded {len(tickers)} assets for structural analysis...")
        
        for ticker in tickers:
            try:
                stock = yf.Ticker(ticker)
                info = stock.info
                sector = info.get('sector', 'Technology')
                sector_etf = self.SECTOR_MAP.get(sector, 'SPY')
                
                # Fetch rolling macro price action window mapping context 
                raw_data = yf.download([ticker, sector_etf], period="1y", interval="1d")
                if raw_data.empty: 
                    continue
                
                prices = raw_data['Adj Close']
                hist_ticker = stock.history(period="1y")
                
                # 1. Regime Checks
                price = float(prices[ticker].iloc[-1])
                ma50 = float(prices[ticker].rolling(50).mean().iloc[-1])
                ma200 = float(prices[ticker].rolling(200).mean().iloc[-1])
                regime_bullish = price > ma50 > ma200
                
                # 2. Sector Alpha Check (6-Month Return vs Sector ETF)
                stock_6m_ret = (prices[ticker].iloc[-1] / prices[ticker].iloc[-126]) - 1
                sector_6m_ret = (prices[sector_etf].iloc[-1] / prices[sector_etf].iloc[-126]) - 1
                alpha_score = stock_6m_ret - sector_6m_ret
                
                # 3. Structural Volume Profiles
                poc_252, _, _ = self.calculate_volume_profile(hist_ticker)
                poc_65, val_65, vah_65 = self.calculate_volume_profile(hist_ticker.tail(65))
                
                # 4. Fundamental PEAD Profiling
                surprise, days_since_earnings = self.get_pead_metrics(stock)
                
                # 5. Natural Language Processing (NLP Insight Scoring via FinBERT)
                headlines = [item['title'] for item in stock.news[:5]] if stock.news else []
                sentiment = self.get_finbert_sentiment(headlines)
                
                # --- FACTOR MATRIX SCORING ENGINE ---
                score = 0.0
                if alpha_score > 0:       score += 3.0  
                if regime_bullish:        score += 3.0  
                if price > vah_65:        score += 2.0  
                if sentiment > 0.65:      score += 2.0  
                
                # PEAD Window Decay/Bonus Logic
                pead_active = (0 < days_since_earnings <= 45) and (surprise >= 2.0)
                if pead_active:
                    score = min(score + 1.0, 10.0) 
                elif days_since_earnings > 60:
                    score = max(score - 1.0, 0.0)  

                # --- ALWAYS WRITE RESULTS TO GOOGLE SHEET ---
                self.write_scores_to_sheet(ticker, score, alpha_score)

                # --- DISPATCH TELEGRAM ALERT IF CONVICTION MEETS THRESHOLD ---
                if score >= 7.5:
                    alert_msg = (
                        f"💎 *LONG-TERM ALPHA: {ticker}*\n"
                        f"*Overall Score: {score:.1f}/10*\n\n"
                        f"• *Alpha:* {'✅' if alpha_score > 0 else '❌'} *{alpha_score:+.2%}* vs {sector_etf}\n"
                        f"• *Regime:* {'🟢 Bullish' if regime_bullish else '🔴 Bearish'}\n"
                        f"• *Structure:* {'🚀 Breakout' if price > vah_65 else '🔒 Range'}\n"
                        f"• *Sentiment:* {'🐂' if sentiment > 0.6 else '😐'} (Score: {sentiment:.2f})\n"
                        f"• *PEAD Status:* Surprise {surprise:+.1f}% (Day {days_since_earnings})\n\n"
                        f"📍 *Target Entry Floor:* `${poc_65:.2f}` (65D POC Support)"
                    )
                    self.send_telegram_alert(alert_msg)
                    
            except Exception as e:
                print(f"Skipping evaluation loop execution for {ticker}: {e}")

if __name__ == "__main__":
    scanner = UnifiedPositioningScanner()
    scanner.scan_pipeline()
