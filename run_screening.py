#!/usr/bin/env python3
import os
import time
import logging
import pandas as pd
import numpy as np
import yfinance as yf
import google.generativeai as genai
import requests
from datetime import datetime

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SMA_PERIOD = 60
VWAP_STD_MULT = 1.0
VOLUME_SURGE_MULT = 1.5
SCORE_GREEN_THRESHOLD = 15

# --- Data Fetching ---
def get_universe():
    try:
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]["Symbol"].tolist()
        nasdaq100 = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]["Ticker"].tolist()
        return list(set(sp500 + nasdaq100))
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA"] # Emergency fallback

def get_historical_data(ticker, period="3mo"):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        return hist if not hist.empty and len(hist) >= 65 else None
    except:
        return None

# --- Analysis Logic ---
def technical_score(ticker):
    hist = get_historical_data(ticker)
    if hist is None: return None
    
    close = hist['Close']
    latest = hist.iloc[-1]
    prev = hist.iloc[-2]

    # 1. SMA60 Score
    sma60 = close.rolling(window=SMA_PERIOD).mean().iloc[-1]
    sma_score = min(10, ((latest['Close'] / sma60) - 1) * 200) if latest['Close'] > sma60 else 0

    # 2. VWAP Score
    vwap = (hist['Close'] * hist['Volume']).cumsum() / hist['Volume'].cumsum()
    std_diff = (hist['Close'] - vwap).std()
    threshold = vwap.iloc[-1] - (VWAP_STD_MULT * std_diff)
    
    if latest['Close'] <= threshold: vwap_score = 10
    elif latest['Close'] >= vwap.iloc[-1]: vwap_score = 0
    else: vwap_score = ((vwap.iloc[-1] - latest['Close']) / (vwap.iloc[-1] - threshold)) * 10

    # 3. Volume Reversal
    avg_vol = hist['Volume'].rolling(window=20).mean().iloc[-1]
    vol_surge = latest['Volume'] / avg_vol
    vol_score = min(10, (vol_surge - 1) * 20) if (latest['Close'] > prev['Close'] and vol_surge > VOLUME_SURGE_MULT) else 0

    return round(sum([sma_score, vwap_score, vol_score]), 1)

def fetch_fundamentals(ticker):
    try:
        info = yf.Ticker(ticker).info
        return {
            "rev_growth": round(info.get("revenueGrowth", 0) * 100, 1),
            "de": round(info.get("debtToEquity", 0) / 100, 2),
            "fcf": round(info.get("freeCashflow", 0) / 1e6, 1)
        }
    except:
        return {"rev_growth": "N/A", "de": "N/A", "fcf": "N/A"}

def call_gemini(ticker, fundamentals, score):
    if not GEMINI_API_KEY: return "AI Analysis Skipped (No Key)"
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash-001")
    prompt = f"Forensic analysis for {ticker}. Tech Score: {score}. Growth: {fundamentals['rev_growth']}%, D/E: {fundamentals['de']}, FCF: ${fundamentals['fcf']}M. Provide: Summary, Technicals, Fundamentals, Risks, and Recommendation."
    try:
        return model.generate_content(prompt).text
    except Exception as e:
        return f"Gemini Error: {e}"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN: print(message); return
