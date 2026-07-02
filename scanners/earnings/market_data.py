from __future__ import annotations

import io
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

from providers.yahoo_throttle import yahoo_call


logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_URL = "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/sp500.csv"
DEFAULT_UNIVERSE_CACHE = Path("config/earnings_universe_cache.csv")
DEFAULT_DELISTED_TICKERS_PATH = Path("config/earnings_delisted_tickers.json")
WARMUP_TICKER = "SPY"
FALLBACK_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO"]

# Substrings in exception text that signal a Yahoo rate-limit (429/503).
# Deliberately specific — matching bare ``"rate"`` would also fire on
# unrelated errors like ``ValueError("rate not found")`` or
# ``KeyError("interest_rate")``.
_RATE_LIMIT_SIGNALS = ("429", "503", "too many", "rate limit")


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


def _is_warmup_disabled() -> bool:
    return str(os.getenv("EARNINGS_SKIP_WARMUP", "")).strip().lower() in {"1", "true", "yes"}


def _load_delisted_set(path: Path) -> set[str]:
    """Load the delisted tickers denylist from JSON; return an uppercase set."""
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, list):
        return set()
    return {
        str(t).strip().upper()
        for t in payload
        if isinstance(t, str) and str(t).strip()
    }


def _is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(signal in text for signal in _RATE_LIMIT_SIGNALS)


class RateLimiter:
    """Thread-safe token-bucket-ish rate limiter for yfinance calls.

    Yahoo's unauthenticated API is observed to allow ~100 req/min before
    returning 429/503 bans. We default to half that (50 req/min) for
    safety. The lock is only held to check/update state; the actual
    sleep happens outside the lock so concurrent callers can compute
    their wait times in parallel.

    A ``max_per_minute`` of 0 disables the limiter.
    """

    def __init__(self, max_per_minute: int = 50) -> None:
        if max_per_minute < 0:
            raise ValueError("max_per_minute must be non-negative")
        self._enabled = max_per_minute > 0
        self.interval = 60.0 / max_per_minute if self._enabled else 0.0
        self.last_call = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self._enabled:
            return
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_call
            wait_time = max(0.0, self.interval - elapsed)
            self.last_call = now + wait_time
        if wait_time > 0:
            time.sleep(wait_time)


@dataclass(frozen=True)
class UniverseLoadResult:
    tickers: list[str]
    source: str


class YahooEarningsDataSource:
    DEFAULT_RATE_LIMIT_PER_MINUTE = 50

    def __init__(
        self,
        *,
        request_delay_seconds: float = 0.5,
        session: requests.Session | None = None,
        warmup_session: bool = True,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.session = session
        self._rate_limiter = (
            RateLimiter(rate_limit_per_minute) if rate_limit_per_minute > 0 else None
        )
        if warmup_session and not _is_warmup_disabled():
            self._start_session_warmup()

    def _start_session_warmup(self) -> None:
        """Fire-and-forget yfinance session warmup.

        Pays the ~1-2s cold-start cost (cookie fetch, DNS) on a daemon
        thread so the first real ticker query is fast. Failures are
        swallowed silently — the scanner should still work, just slower.
        """
        session = self.session

        def _warm() -> None:
            try:
                yahoo_call(
                    lambda: yf.Ticker(WARMUP_TICKER, session=session).info,
                    label=f"earnings-warmup:{WARMUP_TICKER}",
                    retries=0,
                    pace=False,
                )
            except Exception:
                pass

        thread = threading.Thread(
            target=_warm,
            daemon=True,
            name="yf-session-warmup",
        )
        thread.start()

    def _throttle(self) -> None:
        """Apply per-ticker throttling: rate-limit cap + minimum delay."""
        if self._rate_limiter is not None:
            self._rate_limiter.wait()
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
        delisted_tickers_path: Path = DEFAULT_DELISTED_TICKERS_PATH,
    ) -> UniverseLoadResult:
        delisted = _load_delisted_set(delisted_tickers_path)

        if configured_tickers:
            unique: list[str] = []
            seen: set[str] = set()
            for ticker in configured_tickers:
                normalised = normalise_ticker(ticker)
                if (
                    normalised
                    and normalised not in seen
                    and normalised not in delisted
                ):
                    seen.add(normalised)
                    unique.append(normalised)
            return UniverseLoadResult(
                tickers=unique[:max_tickers],
                source="configured",
            )

        raw_tickers: list[str]
        source: str
        try:
            response = requests.get(universe_url, timeout=30)
            response.raise_for_status()
            frame = pd.read_csv(io.StringIO(response.text))
            _ensure_parent(cache_path)
            cache_path.write_text(response.text, encoding="utf-8")
            raw_tickers = self._frame_to_tickers(frame, max_tickers=max_tickers)
            source = "remote"
        except Exception as exc:
            logger.warning("Earnings universe download failed: %r", exc)
            if cache_path.exists():
                frame = pd.read_csv(cache_path)
                raw_tickers = self._frame_to_tickers(frame, max_tickers=max_tickers)
                source = "cache"
            else:
                raw_tickers = list(FALLBACK_TICKERS[:max_tickers])
                source = "fallback"

        filtered = [t for t in raw_tickers if t not in delisted][:max_tickers]
        if delisted and len(filtered) < len(raw_tickers):
            logger.info(
                "Earnings universe: filtered %d delisted ticker(s) from %s source.",
                len(raw_tickers) - len(filtered),
                source,
            )
        return UniverseLoadResult(tickers=filtered, source=source)

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

    def history(
        self,
        ticker: str,
        *,
        period: str = "2y",
        max_retries: int = 3,
    ) -> pd.DataFrame:
        """Fetch price history with retry on rate-limit-style failures.

        yfinance returns an empty DataFrame on rate limit, which is
        indistinguishable from a delisted ticker at the API level. Since
        the denylist already filters known-delisted tickers upstream,
        an empty result here is treated as a likely rate limit and
        retried with exponential backoff (capped at 8s).
        """
        for attempt in range(max_retries):
            self._throttle()
            try:
                history = yahoo_call(
                    lambda: self._ticker(ticker).history(period=period, auto_adjust=False),
                    label=f"earnings-history:{ticker}",
                    retries=0,
                    pace=False,
                )
            except Exception as exc:
                if _is_rate_limit_error(exc) and attempt < max_retries - 1:
                    logger.warning(
                        "yfinance rate limit for %s (attempt %d/%d): %s",
                        ticker, attempt + 1, max_retries, exc,
                    )
                    time.sleep(min(2 ** attempt, 8))
                    continue
                # Non-rate-limit exceptions are NOT retried — they surface
                # to the engine immediately so a real bug (e.g. yfinance
                # internal error) doesn't get hidden behind a silent
                # retry loop. Rate-limit detection is in
                # ``_is_rate_limit_error``.
                raise
            if history is None:
                history = pd.DataFrame()
            if not history.empty:
                return history.dropna(how="all").copy()
            if attempt < max_retries - 1:
                logger.warning(
                    "yfinance empty result for %s (attempt %d/%d), backing off",
                    ticker, attempt + 1, max_retries,
                )
                time.sleep(min(2 ** attempt, 4))
        return pd.DataFrame()

    def batch_history(
        self,
        tickers: list[str],
        *,
        period: str = "2y",
        max_retries: int = 2,
    ) -> dict[str, pd.DataFrame]:
        """Fetch history for many tickers in one yf.download call.

        Threads is disabled (``threads=False``) so the outer
        ``ThreadPoolExecutor`` in ``engine.run_earnings_scan`` owns
        concurrency. Stacking yfinance's internal ~8-thread pool on
        top of an outer 4-worker pool can hit Yahoo's ~100 req/min
        rate limit within seconds on a 500-ticker scan.

        Returns a dict mapping each ticker to its price-history
        DataFrame. Tickers whose yfinance response has no rows
        (delisted, missing, or rate-limited dropouts) are omitted;
        the engine treats a missing entry as an empty DataFrame.
        """
        if not tickers:
            return {}

        for attempt in range(max_retries + 1):
            self._throttle()
            try:
                raw = yahoo_call(
                    lambda: yf.download(
                        tickers=tickers,
                        period=period,
                        auto_adjust=False,
                        group_by="ticker",
                        threads=False,  # outer pool owns concurrency
                        progress=False,
                        session=self.session,
                    ),
                    label=f"earnings-batch-history:{len(tickers)}",
                    retries=0,
                    pace=False,
                )
            except Exception as exc:
                if _is_rate_limit_error(exc) and attempt < max_retries:
                    logger.warning(
                        "yfinance batch rate limit (attempt %d/%d): %s",
                        attempt + 1, max_retries, exc,
                    )
                    time.sleep(min(2 ** attempt, 8))
                    continue
                logger.warning("Batch history download failed: %r", exc)
                return {}

            if raw is None or raw.empty:
                if attempt < max_retries:
                    logger.warning(
                        "yfinance batch returned empty (attempt %d/%d), backing off",
                        attempt + 1, max_retries,
                    )
                    time.sleep(min(2 ** attempt, 4))
                    continue
                return {}

            out = self._extract_per_ticker(raw, tickers)
            if out:
                return out
            if attempt < max_retries:
                logger.warning(
                    "yfinance batch returned no per-ticker data (attempt %d/%d), backing off",
                    attempt + 1, max_retries,
                )
                time.sleep(min(2 ** attempt, 4))
        return {}

    def _extract_per_ticker(
        self,
        raw: pd.DataFrame,
        tickers: list[str],
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        if len(tickers) == 1:
            sub = raw.dropna(how="all").copy()
            if not sub.empty:
                out[tickers[0]] = sub
            return out

        columns = raw.columns
        if isinstance(columns, pd.MultiIndex):
            level_zero = set(columns.get_level_values(0))
        else:
            level_zero = set()
        for ticker in tickers:
            try:
                if ticker in level_zero:
                    sub = raw[ticker]
                elif ticker in columns:
                    sub = raw[ticker]
                else:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            sub = sub.dropna(how="all").copy()
            if not sub.empty:
                out[ticker] = sub
        return out

    def earnings_dates(self, ticker: str, *, limit: int = 40) -> pd.DataFrame:
        self._throttle()
        yf_ticker = self._ticker(ticker)
        try:
            frame = yahoo_call(
                lambda: yf_ticker.get_earnings_dates(limit=limit),
                label=f"earnings-dates:{ticker}",
                retries=0,
                pace=False,
            )
        except Exception:
            frame = yahoo_call(
                lambda: getattr(yf_ticker, "earnings_dates", pd.DataFrame()),
                label=f"earnings-dates-fallback:{ticker}",
                retries=0,
                pace=False,
            )
        if frame is None or isinstance(frame, list):
            return pd.DataFrame()
        return frame.copy()

    def calendar(self, ticker: str) -> Any:
        self._throttle()
        try:
            return yahoo_call(
                lambda: self._ticker(ticker).calendar,
                label=f"earnings-calendar:{ticker}",
                retries=0,
                pace=False,
            )
        except Exception:
            return None

    def info(self, ticker: str) -> dict[str, Any]:
        self._throttle()
        try:
            return dict(
                yahoo_call(
                    lambda: self._ticker(ticker).info or {},
                    label=f"earnings-info:{ticker}",
                    retries=0,
                    pace=False,
                )
                or {}
            )
        except Exception:
            return {}

    def option_expirations(self, ticker: str) -> list[date]:
        self._throttle()
        try:
            expirations = list(
                yahoo_call(
                    lambda: self._ticker(ticker).options or [],
                    label=f"earnings-options:{ticker}",
                    retries=0,
                    pace=False,
                )
                or []
            )
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
        self._throttle()
        chain = yahoo_call(
            lambda: self._ticker(ticker).option_chain(expiry.isoformat()),
            label=f"earnings-option-chain:{ticker}:{expiry.isoformat()}",
            retries=0,
            pace=False,
        )
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
