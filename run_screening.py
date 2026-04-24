import pandas as pd
from finvizfinance.screener.overview import Overview

def create_csv():
    print("Fetching 8,000+ tickers from Finviz...")
    foverview = Overview()
    # No filters = Everything (Full Universe)
    # limit=-1 = All pages
    all_df = foverview.screener_view(limit=-1, verbose=1)
    
    # Clean and Save
    tickers = all_df[['Ticker']].copy()
    tickers['Ticker'] = tickers['Ticker'].str.replace('.', '-', regex=False)
    tickers.to_csv('stock_universe.csv', index=False)
    print(f"Done! Created stock_universe.csv with {len(tickers)} stocks.")

if __name__ == "__main__":
    create_csv()
