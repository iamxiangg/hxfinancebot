from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import yfinance as yf

from research.regulatory.identifiers import stable_hash
from research.regulatory.models import AnnouncementTiming, MarketSnapshot, TimingConfidence


HistoryFetcher = Callable[[str, str, str], Any]


def _default_history_fetcher(symbol: str, start: str, end: str):
    return yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)


def _safe_close(history, offset: int) -> float | None:
    if history is None or getattr(history, "empty", True):
        return None
    if len(history.index) <= offset:
        return None
    try:
        return float(history["Close"].iloc[offset])
    except Exception:
        return None


def build_market_snapshot(
    *,
    ticker: str,
    event_date: str,
    announcement_timing: AnnouncementTiming = AnnouncementTiming.UNKNOWN,
    timing_confidence: TimingConfidence = TimingConfidence.LOW,
    history_fetcher: HistoryFetcher | None = None,
) -> MarketSnapshot:
    fetcher = history_fetcher or _default_history_fetcher
    event_dt = datetime.fromisoformat(str(event_date)[:10])
    start = (event_dt - timedelta(days=10)).date().isoformat()
    end = (event_dt + timedelta(days=40)).date().isoformat()
    stock = fetcher(ticker, start, end)
    spy = fetcher("SPY", start, end)
    xbi = fetcher("XBI", start, end)
    previous_close = _safe_close(stock, 0)
    event_close = _safe_close(stock, 1)
    next_close = _safe_close(stock, 2)
    five_close = _safe_close(stock, 5)
    twenty_close = _safe_close(stock, 20)
    current_close = None
    if stock is not None and not getattr(stock, "empty", True):
        try:
            current_close = float(stock["Close"].iloc[-1])
        except Exception:
            current_close = None
    stock_return = None
    spy_return = None
    xbi_return = None
    if previous_close and event_close:
        stock_return = (event_close / previous_close) - 1.0
    spy_prev = _safe_close(spy, 0)
    spy_event = _safe_close(spy, 1)
    if spy_prev and spy_event and stock_return is not None:
        spy_return = stock_return - ((spy_event / spy_prev) - 1.0)
    xbi_prev = _safe_close(xbi, 0)
    xbi_event = _safe_close(xbi, 1)
    if xbi_prev and xbi_event and stock_return is not None:
        xbi_return = stock_return - ((xbi_event / xbi_prev) - 1.0)
    direction = ""
    if stock_return is not None:
        direction = "POSITIVE" if stock_return > 0 else "NEGATIVE" if stock_return < 0 else "FLAT"
    return MarketSnapshot(
        snapshot_id=stable_hash([ticker, event_date], prefix="mkt"),
        ticker=ticker,
        event_date=event_date,
        previous_close=previous_close,
        event_close=event_close,
        next_close=next_close,
        five_session_close=five_close,
        twenty_session_close=twenty_close,
        current_close=current_close,
        spy_relative_return=spy_return,
        xbi_relative_return=xbi_return,
        observed_price_direction=direction,
        announcement_timing=announcement_timing,
        timing_confidence=timing_confidence,
    )

