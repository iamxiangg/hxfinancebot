from __future__ import annotations

"""Financial Modeling Prep (FMP) free-tier API client for quarterly financials.

Used as a fallback when yfinance has fewer than 8 quarters of data.
FMP free tier: 250 requests/day, 5+ years of quarterly history.
"""

import logging
import os
from typing import Any

import requests

from funnel.feroldi_config import DEFAULT_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"

# Module-level cache: {ticker: {...}} — in-memory only (CI runs are single-process)
_fmp_cache: dict[str, dict[str, Any]] = {}


def clear_fmp_cache() -> None:
    """Clear the FMP cache (useful for testing)."""
    _fmp_cache.clear()


def _get_api_key() -> str | None:
    """Return the FMP API key from environment, or None if not configured."""
    key = os.getenv("FMP_API_KEY", "").strip()
    return key or None


def _fmp_get(endpoint: str, ticker: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> list[dict[str, Any]]:
    """Call an FMP API endpoint and return the JSON list."""
    api_key = _get_api_key()
    if not api_key:
        logger.debug("FMP_API_KEY not configured — skipping FMP")
        return []

    url = f"{FMP_BASE}/{endpoint}"
    params = {"symbol": ticker, "period": "annual", "limit": 5, "apikey": api_key}

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        # FMP returns an error object on failures
        if isinstance(data, dict) and data.get("Error Message"):
            logger.warning("FMP error for %s %s: %s", ticker, endpoint, data.get("Error Message"))
        return []
    except requests.RequestException as exc:
        logger.warning("FMP request failed for %s %s: %s", ticker, endpoint, exc)
        return []


def fetch_fmp_quarterly(ticker: str) -> dict[str, Any]:
    """Fetch annual financial statements from FMP (free tier limit=5 years).

    Returns a dict with the same keys as collect_quarterly_financials():
        ni_current, ni_prior, ocf_current, ocf_prior, capex_current, capex_prior,
        eps_current, eps_prior, equity_current, equity_prior,
        lt_debt_current, repurchases_current, total_assets

    Only called as fallback when yfinance has insufficient quarterly data.
    Uses module-level in-memory cache.
    """
    ticker_key = ticker.strip().upper()

    # Check cache
    if ticker_key in _fmp_cache:
        logger.debug("FMP cache hit for %s", ticker)
        return dict(_fmp_cache[ticker_key])

    api_key = _get_api_key()
    if not api_key:
        return {}

    logger.info("FMP: fetching quarterly data for %s", ticker)

    result: dict[str, Any] = {}

    # Fetch all three statements in parallel (sequential to be kind to free tier)
    income = _fmp_get("income-statement", ticker)
    cashflow = _fmp_get("cash-flow-statement", ticker)
    balance_sheet = _fmp_get("balance-sheet-statement", ticker)

    if not income or not cashflow or not balance_sheet:
        logger.warning("FMP incomplete data for %s (income=%d, cf=%d, bs=%d)",
                       ticker, len(income), len(cashflow), len(balance_sheet))
        # Even partial data is better than nothing — store what we have
        _fmp_cache[ticker_key] = dict(result)
        return result

    # FMP returns most-recent-first annual data (free tier: limit=5 years).
    # Annual statements represent full-year TTM — no quarterly summing needed.
    income = income[:5]
    cashflow = cashflow[:5]
    balance_sheet = balance_sheet[:5]

    def _snap(rows: list[dict], field: str, offset: int = 0) -> float | None:
        """Get a single row value at offset (0=current year, 1=prior year)."""
        if len(rows) <= offset:
            return None
        val = rows[offset].get(field)
        if val is None or val == "":
            return None
        try:
            fv = float(val)
            return fv if fv != 0 else None
        except (TypeError, ValueError):
            return None

    # Income statement — annual snapshots (0=current, 1=prior, 2=two-years-ago)
    result["ni_current"] = _snap(income, "netIncome", 0)
    result["ni_prior"] = _snap(income, "netIncome", 1)
    result["ni_2y"] = _snap(income, "netIncome", 2)
    result["eps_current"] = _snap(income, "epsDiluted", 0)
    result["eps_prior"] = _snap(income, "epsDiluted", 1)
    result["eps_2y"] = _snap(income, "epsDiluted", 2)

    # Cash flow -- annual snapshots
    result["ocf_current"] = _snap(cashflow, "operatingCashFlow", 0)
    result["ocf_prior"] = _snap(cashflow, "operatingCashFlow", 1)
    result["ocf_2y"] = _snap(cashflow, "operatingCashFlow", 2)
    result["capex_current"] = _snap(cashflow, "capitalExpenditure", 0)
    result["capex_prior"] = _snap(cashflow, "capitalExpenditure", 1)
    result["capex_2y"] = _snap(cashflow, "capitalExpenditure", 2)

    # Repurchases and dividends from cash flow (annual totals)
    rep = _snap(cashflow, "commonStockRepurchased", 0)
    result["repurchases_current"] = abs(rep) if rep is not None else None
    div = _snap(cashflow, "commonDividendsPaid", 0)
    result["dividends_current"] = abs(div) if div is not None else None

    # Balance sheet — annual snapshots
    result["equity_current"] = _snap(balance_sheet, "totalStockholdersEquity", 0)
    result["equity_prior"] = _snap(balance_sheet, "totalStockholdersEquity", 1)
    result["equity_2y"] = _snap(balance_sheet, "totalStockholdersEquity", 2)
    result["lt_debt_current"] = _snap(balance_sheet, "longTermDebt", 0)
    result["total_assets"] = _snap(balance_sheet, "totalAssets", 0)

    # Cache and return
    _fmp_cache[ticker_key] = dict(result)
    logger.info("FMP: %d fields collected for %s", len(result), ticker)
    return result
