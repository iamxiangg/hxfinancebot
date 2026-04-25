import yfinance as yf
test_list = ["GME", "AMC", "BB", "RKT", "SPCE"]  # smaller caps
for t in test_list:
  info = yf.Ticker(t).info
  si = info.get("shortPercentOfFloat", 0)
  print(f"{t}: SI={si*100:.1f}%")
