import os
from google import genai  # <--- New Import Syntax
import yfinance as yf
import requests

# ... (other imports)

def call_gemini(ticker, score):
    # New Client setup for 2026
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    prompt = f"Brief analysis for {ticker} with tech score {score}."
    
    try:
        # New response syntax
        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"AI Error: {e}"
