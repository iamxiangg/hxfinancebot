from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Callable, TypeVar


logger = logging.getLogger(__name__)

T = TypeVar("T")

_REQUEST_GATE = threading.Semaphore(max(1, int(float(os.getenv("YAHOO_MAX_CONCURRENT_REQUESTS", 1)))))
_REQUEST_TIMING_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


def _min_interval_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("YAHOO_MIN_INTERVAL_SECONDS", "0.4")))
    except (TypeError, ValueError):
        return 0.4


def _retry_limit() -> int:
    try:
        return max(0, int(float(os.getenv("YAHOO_RETRY_LIMIT", "2"))))
    except (TypeError, ValueError):
        return 2


def _pace_requests() -> None:
    global _LAST_REQUEST_AT
    min_interval = _min_interval_seconds()
    if min_interval <= 0:
        return
    with _REQUEST_TIMING_LOCK:
        now = time.monotonic()
        wait = max(0.0, (_LAST_REQUEST_AT + min_interval) - now)
        scheduled = now + wait
        _LAST_REQUEST_AT = scheduled
    if wait > 0:
        time.sleep(wait)


def _is_retryable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "invalid crumb",
            "unauthorized",
            "too many requests",
            "temporarily unavailable",
            "connection",
            "timeout",
            "read timed out",
            "rate limit",
        )
    )


def yahoo_call(
    fn: Callable[[], T],
    *,
    label: str = "yahoo",
    retries: int | None = None,
    pace: bool = True,
) -> T:
    retries = _retry_limit() if retries is None else max(0, int(retries))
    for attempt in range(retries + 1):
        with _REQUEST_GATE:
            if pace:
                _pace_requests()
            try:
                return fn()
            except Exception as exc:
                if attempt >= retries or not _is_retryable(exc):
                    raise
                backoff = min(2.0, 0.6 * (attempt + 1)) + random.uniform(0.0, 0.25)
                logger.warning(
                    "Yahoo call retry: label=%s attempt=%d/%d error=%s backoff=%.2fs",
                    label,
                    attempt + 1,
                    retries + 1,
                    exc.__class__.__name__,
                    backoff,
                )
        time.sleep(backoff)
    raise RuntimeError("Yahoo call retry loop exhausted unexpectedly")


def create_ticker(ticker: str, *, session: Any | None = None):
    import yfinance as yf

    if session is None:
        return yf.Ticker(ticker)
    return yf.Ticker(ticker, session=session)


def yahoo_download(
    *args: Any,
    _yahoo_retries: int | None = None,
    _yahoo_pace: bool = True,
    **kwargs: Any,
):
    import yfinance as yf

    return yahoo_call(
        lambda: yf.download(*args, **kwargs),
        label=f"download:{kwargs.get('tickers') or kwargs.get('ticker') or args[0] if args else 'unknown'}",
        retries=_yahoo_retries,
        pace=_yahoo_pace,
    )
