from __future__ import annotations

import io
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf


logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_URL = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
DEFAULT_UNIVERSE_CACHE = Path("config/earnings_universe_cache.csv")
FALLBACK_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO"]


def normalise_ticker(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.replace(".", "-").replace("/", "-")


def _to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def average_dollar_volume(history: pd.DataFrame, *, window: int = 30) -> float | None:
    if history.empty or not {"Close", "Volume"}.issubset(history.columns):
        return None
    sample = history.tail(window)
    if sample.empty:
        return None
    values = sample["Close"].astype(float) * sample["Volume"].astype(float)
    if values.empty:
        return None
    median_value = float(values.median())
    return median_value if math.isfinite(median_value) else None


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class UniverseLoadResult:
    tickers: list[str]
    source: str


class YahooEarningsDataSource:
    def __init__(self, *, request_delay_seconds: float = 0.25, session: requests.Session | None = None) -> None:
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.session = session

    def _sleep(self) -> None:
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)

    def _ticker(self, ticker: str):
        return yf.Ticker(ticker, session=self.session)

    def load_universe(
        self,
        *,
        configured_tickers: list[str] | None,
        max_tickers: int,
        universe_url: str = DEFAULT_UNIVERSE_URL,
        cache_path: Path = DEFAULT_UNIVERSE_CACHE,
    ) -> UniverseLoadResult:
        if configured_tickers:
            unique = []
            seen: set[str] = set()
            for ticker in configured_tickers:
                normalised = normalise_ticker(ticker)
                if normalised and normalised not in seen:
                    seen.add(normalised)
                    unique.append(normalised)
            return UniverseLoadResult(tickers=unique[:max_tickers], source="configured")

        try:
            response = requests.get(universe_url, timeout=30)
            response.raise_for_status()
            frame = pd.read_csv(io.StringIO(response.text))
            _ensure_parent(cache_path)
            cache_path.write_text(response.text, encoding="utf-8")
            return UniverseLoadResult(
                tickers=self._frame_to_tickers(frame, max_tickers=max_tickers),
                source="remote",
            )
        except Exception as exc:
            logger.warning("Earnings universe download failed: %r", exc)

        if cache_path.exists():
            frame = pd.read_csv(cache_path)
            return UniverseLoadResult(
                tickers=self._frame_to_tickers(frame, max_tickers=max_tickers),
                source="cache",
            )

        return UniverseLoadResult(tickers=FALLBACK_TICKERS[:max_tickers], source="fallback")

    def _frame_to_tickers(self, frame: pd.DataFrame, *, max_tickers: int) -> list[str]:
        symbol_column = None
        for candidate in ("symbol", "Symbol", "ticker", "Ticker"):
            if candidate in frame.columns:
                symbol_column = candidate
                break
        if symbol_column is None:
            raise ValueError("Universe source is missing a ticker symbol column.")

        filtered = frame.copy()
        volume_column = next((column for column in frame.columns if column.lower() in {"volume", "avgvol"}), None)
        if volume_column:
            numeric = pd.to_numeric(filtered[volume_column], errors="coerce")
            filtered = filtered.assign(_volume=numeric)
            filtered = filtered[filtered["_volume"].fillna(0) > 0]

        ordered: list[str] = []
        seen: set[str] = set()
        for raw in filtered[symbol_column].tolist():
            ticker = normalise_ticker(raw)
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            ordered.append(ticker)
            if len(ordered) >= max_tickers:
                break
        return ordered

    def history(self, ticker: str, *, period: str = "2y") -> pd.DataFrame:
        self._sleep()
        history = self._ticker(ticker).history(period=period, auto_adjust=False)
        if history is None:
            return pd.DataFrame()
        return history.dropna(how="all").copy()

    def earnings_dates(self, ticker: str, *, limit: int = 40) -> pd.DataFrame:
        self._sleep()
        yf_ticker = self._ticker(ticker)
        try:
            frame = yf_ticker.get_earnings_dates(limit=limit)
        except Exception:
            frame = getattr(yf_ticker, "earnings_dates", pd.DataFrame())
        if frame is None or isinstance(frame, list):
            return pd.DataFrame()
        return frame.copy()

    def calendar(self, ticker: str) -> Any:
        self._sleep()
        try:
            return self._ticker(ticker).calendar
        except Exception:
            return None

    def info(self, ticker: str) -> dict[str, Any]:
        self._sleep()
        try:
            return dict(self._ticker(ticker).info or {})
        except Exception:
            return {}

    def option_expirations(self, ticker: str) -> list[date]:
        self._sleep()
        try:
            expirations = list(self._ticker(ticker).options or [])
        except Exception:
            return []
        values: list[date] = []
        for raw in expirations:
            try:
                values.append(date.fromisoformat(str(raw)))
            except ValueError:
                continue
        return sorted(values)

    def option_chain(self, ticker: str, expiry: date) -> tuple[pd.DataFrame, pd.DataFrame]:
        self._sleep()
        chain = self._ticker(ticker).option_chain(expiry.isoformat())
        return chain.calls.copy(), chain.puts.copy()

    def spot_price(self, ticker: str) -> float | None:
        history = self.history(ticker, period="10d")
        if history.empty or "Close" not in history.columns:
            return None
        return _to_float(history["Close"].iloc[-1])


def load_json_file(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
