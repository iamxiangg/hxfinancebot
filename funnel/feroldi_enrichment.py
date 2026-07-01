from __future__ import annotations

"""Feroldi first-cut data enrichment using free sources (yfinance, SEC).

Collects all raw inputs needed for F01–S03 scoring from:
1. yfinance (financial statements, price history, market data)
2. SEC EDGAR (filing texts for M01, M02, M03)
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from funnel.feroldi_config import DEFAULT_REQUEST_TIMEOUT
from funnel.feroldi_fmp import fetch_fmp_quarterly
from funnel.feroldi_models import FeroldiDetailResult
from funnel.feroldi_sec import extract_ceo_evidence, extract_filing_text, extract_mission_evidence

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat() + "Z"


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fiscal_year_end(info: dict) -> str:
    """Best-effort fiscal period string."""
    return str(info.get("lastFiscalYearEnd", "") or info.get("nextFiscalYearEnd", "") or "")


# ---------------------------------------------------------------------------
# yfinance data collection
# ---------------------------------------------------------------------------


def collect_yfinance_metrics(ticker: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> dict[str, Any]:
    """Collect all financial and stock data from yfinance for Feroldi scoring.

    Returns a flat dict of raw inputs ready for scoring functions.
    """
    import yfinance as yf

    try:
        yt = yf.Ticker(ticker)
        info = yt.info or {}
    except Exception as exc:
        logger.warning("yfinance Ticker(%s) failed: %s", ticker, exc.__class__.__name__)
        return {}

    # Financial statement data for F01–F05
    metrics: dict[str, Any] = {
        "ticker": ticker,
        "company_name": str(info.get("shortName") or info.get("longName") or ""),
        "cik": str(info.get("CIK", "")),
        "sector": str(info.get("sector", "")),
        "industry": str(info.get("industry", "")),
        "quote_type": str(info.get("quoteType", "")),
        "currency": str(info.get("currency", "USD")),
        "as_of_date": _now_iso(),
        "source_period": _fiscal_year_end(info),
        "source_date": _now_iso(),
    }

    # F01: Cash and long-term debt
    metrics["cash"] = _safe_float(info.get("totalCash") or info.get("cash"))
    metrics["long_term_debt"] = _safe_float(info.get("longTermDebt"))

    # F02: Gross margin
    metrics["revenue_ttm"] = _safe_float(info.get("totalRevenue"))
    metrics["cost_of_revenue_ttm"] = _safe_float(info.get("costOfRevenue"))
    metrics["gross_profit_ttm"] = _safe_float(info.get("grossProfits"))

    # F03: ROE
    metrics["net_income_ttm"] = _safe_float(info.get("netIncomeToCommon"))
    metrics["book_value"] = _safe_float(info.get("bookValue"))
    metrics["shares_outstanding"] = _safe_float(info.get("sharesOutstanding"))
    if metrics["book_value"] is not None and metrics["shares_outstanding"] is not None and metrics["shares_outstanding"] > 0:
        metrics["total_equity"] = metrics["book_value"] * metrics["shares_outstanding"]
    else:
        metrics["total_equity"] = None

    # F04: FCF components
    metrics["operating_cf"] = _safe_float(info.get("operatingCashflow"))
    metrics["capex"] = _safe_float(info.get("capitalExpenditure"))

    # F05: Diluted EPS
    metrics["diluted_eps"] = _safe_float(info.get("dilutedEPS"))

    # M02: Ownership (partial — insider group % from yfinance, CEO-specific is manual)
    metrics["current_price"] = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    metrics["market_cap"] = _safe_float(info.get("marketCap"))
    metrics["insider_pct"] = _safe_float(info.get("heldPercentInsiders"))

    # S02: Shareholder actions
    metrics["share_repurchases"] = _safe_float(info.get("shareIssuanceShares") or info.get("repurchaseOfStock"))
    metrics["dividend_rate"] = _safe_float(info.get("dividendRate"))
    metrics["total_debt"] = _safe_float(info.get("totalDebt"))
    metrics["total_assets"] = _safe_float(info.get("totalAssets"))
    metrics["diluted_shares"] = _safe_float(info.get("sharesOutstanding"))

    logger.info("Collected yfinance metrics for %s: %d fields", ticker, len(metrics))
    return metrics


# ---------------------------------------------------------------------------
# Quarterly financials collection (F03/F04/F05 prior period)
# ---------------------------------------------------------------------------


def collect_quarterly_financials(ticker: str) -> dict[str, Any]:
    """Collect quarterly financial statements and compute current vs prior TTM.

    Uses yfinance quarterly_income_stmt, quarterly_cashflow, quarterly_balance_sheet.
    Computes: current TTM = sum of last 4 quarters, prior TTM = sum of quarters 5-8.
    Returns a dict with current/prior values for net income, operating CF, capex,
    diluted EPS, and shareholders equity.
    """
    import yfinance as yf

    result: dict[str, Any] = {}

    try:
        yt = yf.Ticker(ticker)
    except Exception as exc:
        logger.warning("yfinance Ticker(%s) failed for quarterly data: %s", ticker, exc.__class__.__name__)
        return result

    # --- Income Statement ---
    try:
        income = yt.quarterly_income_stmt
        if income is not None and not income.empty:
            income = income.fillna(0)
            # Net Income TTM
            result["ni_current"] = _sum_ttm(income, "Net Income", 0, 4)
            result["ni_prior"] = _sum_ttm(income, "Net Income", 4, 8)
            # Diluted EPS TTM
            result["eps_current"] = _sum_ttm(income, "Diluted EPS", 0, 4)
            result["eps_prior"] = _sum_ttm(income, "Diluted EPS", 4, 8)
    except Exception as exc:
        logger.warning("Quarterly income for %s failed: %s", ticker, exc.__class__.__name__)

    # --- Cash Flow ---
    try:
        cashflow = yt.quarterly_cashflow
        if cashflow is not None and not cashflow.empty:
            cashflow = cashflow.fillna(0)
            # Operating Cash Flow TTM
            result["ocf_current"] = _sum_ttm(cashflow, "Operating Cash Flow", 0, 4)
            result["ocf_prior"] = _sum_ttm(cashflow, "Operating Cash Flow", 4, 8)
            # Capital Expenditure TTM (reported as negative in yfinance)
            result["capex_current"] = _sum_ttm(cashflow, "Capital Expenditure", 0, 4)
            result["capex_prior"] = _sum_ttm(cashflow, "Capital Expenditure", 4, 8)
            # S02: Share repurchases TTM (absolute value, reported as negative)
            repurchases = _sum_ttm(cashflow, "Repurchase Of Capital Stock", 0, 4)
            result["repurchases_current"] = abs(repurchases) if repurchases is not None else None
            # S02: Dividends paid TTM (absolute value, reported as negative)
            dividends = _sum_ttm(cashflow, "Common Stock Dividend Paid", 0, 4)
            result["dividends_current"] = abs(dividends) if dividends is not None else None
    except Exception as exc:
        logger.warning("Quarterly cashflow for %s failed: %s", ticker, exc.__class__.__name__)

    # --- Balance Sheet ---
    try:
        balance = yt.quarterly_balance_sheet
        if balance is not None and not balance.empty:
            balance = balance.fillna(0)
            # Total Equity
            result["equity_current"] = _snapshot(balance, "Stockholders Equity")
            result["equity_prior"] = _snapshot(balance, "Stockholders Equity", offset=4)
            # Long-term debt (F01 fallback)
            for label in ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]:
                lt_debt = _snapshot(balance, label)
                if lt_debt is not None:
                    result["lt_debt_current"] = lt_debt
                    break
    except Exception as exc:
        logger.warning("Quarterly balance sheet for %s failed: %s", ticker, exc.__class__.__name__)

    logger.info("Quarterly financials for %s: %d fields collected", ticker, len(result))

    # If yfinance has insufficient quarterly data, try FMP as fallback.
    # FMP provides annual data (full-year TTM) — only use it for keys that
    # yfinance couldn't populate, preserving yfinance current-TTM where available.
    if result.get("ni_prior") is None and result.get("eps_prior") is None:
        try:
            fmp_data = fetch_fmp_quarterly(ticker)
            if fmp_data:
                # Only fill keys that yfinance left as None
                filled = 0
                for key, value in fmp_data.items():
                    if value is not None and result.get(key) is None:
                        result[key] = value
                        filled += 1
                logger.info("FMP fallback for %s: %d additional fields", ticker, filled)
        except Exception as exc:
            logger.debug("FMP fallback failed for %s: %s", ticker, exc)

    return result


def _sum_ttm(df, label: str, start: int, end: int) -> float | None:
    """Sum columns [start:end] for a given row label. Returns None if row missing."""
    try:
        row = df.loc[label]
        if len(row) < end:
            return None
        total = row.iloc[start:end].sum()
        if total == 0 and row.iloc[start:end].eq(0).all():
            return None  # All zeros likely means no data
        return float(total)
    except (KeyError, IndexError):
        return None


def _snapshot(df, label: str, offset: int = 0) -> float | None:
    """Get a single column value at `offset` rows back from the latest."""
    try:
        row = df.loc[label]
        if len(row) <= offset:
            return None
        val = row.iloc[offset]
        if val == 0:
            return None
        return float(val)
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Earnings surprise collection (S03)
# ---------------------------------------------------------------------------


def collect_earnings_surprise(ticker: str) -> dict[str, Any]:
    """Collect last 4 quarters of earnings estimates vs reported EPS from yfinance.

    Returns a dict with q1–q4 reported EPS, estimated EPS, fiscal period, and report date.
    """
    import yfinance as yf

    result: dict[str, Any] = {}

    try:
        yt = yf.Ticker(ticker)
        earnings = yt.earnings_dates
        if earnings is None or earnings.empty:
            return result

        # earnings_dates returns a DataFrame with columns like:
        # 'EPS Estimate', 'Reported EPS', 'Surprise(%)'
        # Index is the earnings date
        for idx in range(min(4, len(earnings))):
            row = earnings.iloc[idx]
            q_num = idx + 1
            result[f"q{q_num}_reported"] = _safe_float(row.get("Reported EPS"))
            result[f"q{q_num}_estimated"] = _safe_float(row.get("EPS Estimate"))
            result[f"q{q_num}_report_date"] = str(earnings.index[idx])[:10]
    except ImportError as exc:
        logger.debug("Earnings surprise unavailable for %s: import error", ticker)
    except AttributeError as exc:
        logger.debug("Earnings surprise unavailable for %s: %s", ticker, exc)
    except Exception as exc:
        logger.warning("Earnings surprise for %s failed: %s", ticker, exc.__class__.__name__)

    logger.info("Earnings surprise for %s: %d quarters collected", ticker, len(result) // 3)
    return result


def _to_series(df, column: str):
    """Extract a column as a 1-D Series regardless of DataFrame structure.

    Handles both flat-column DataFrames (yfinance <0.2) and
    MultiIndex-column DataFrames (yfinance 0.2+) that may return
    a sub-DataFrame instead of a Series for single-ticker downloads.
    """
    col_data = df[column]
    if hasattr(col_data, 'columns'):
        # Multi-level columns: squeeze to first column
        col_data = col_data.iloc[:, 0]
    return col_data


def _safe_scalar(value) -> float | None:
    """Convert a pandas scalar/Series element to float, or return None."""
    try:
        if hasattr(value, 'item'):
            return float(value.item())
        return float(value)
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Price history collection (S01)
# ---------------------------------------------------------------------------


def collect_price_history(ticker: str, years: int = 5) -> dict[str, Any]:
    """Collect 5-year price history for S01."""
    import yfinance as yf

    result: dict[str, Any] = {
        "start_date": "",
        "end_date": "",
        "stock_start_price": None,
        "stock_end_price": None,
        "spy_start_price": None,
        "spy_end_price": None,
        "trading_days": 0,
    }

    end_date = date.today()
    start_date = end_date - timedelta(days=years * 365 + 10)  # Buffer for weekends

    try:
        stock = yf.download(ticker, start=start_date.isoformat(), end=end_date.isoformat(),
                            auto_adjust=True, progress=False, threads=False)
        spy = yf.download("SPY", start=start_date.isoformat(), end=end_date.isoformat(),
                         auto_adjust=True, progress=False, threads=False)
    except Exception as exc:
        logger.warning("Price history for %s failed: %s", ticker, exc.__class__.__name__)
        return result

    if stock.empty or spy.empty:
        return result

    result["trading_days"] = len(stock)

    if "Close" in stock.columns:
        stock_close = _to_series(stock, "Close")
        if stock_close is not None and len(stock_close) > 0:
            result["stock_start_price"] = _safe_scalar(stock_close.iloc[0])
            result["stock_end_price"] = _safe_scalar(stock_close.iloc[-1])
            result["start_date"] = str(stock.index[0].date())
            result["end_date"] = str(stock.index[-1].date())

    if "Close" in spy.columns:
        spy_close = _to_series(spy, "Close")
        if spy_close is not None and len(spy_close) > 0:
            result["spy_start_price"] = _safe_scalar(spy_close.iloc[0])
            result["spy_end_price"] = _safe_scalar(spy_close.iloc[-1])

    return result


# ---------------------------------------------------------------------------
# Full enrichment entry point
# ---------------------------------------------------------------------------


def enrich_feroldi_detail(
    ticker: str,
    *,
    candidate_id: str = "",
    yfinance_metrics: dict[str, Any] | None = None,
    filing_texts: dict[str, str] | None = None,
) -> FeroldiDetailResult:
    """Collect all data and return a populated (but unscored) FeroldiDetailResult.

    The caller is responsible for running scoring via feroldi_scoring.
    """
    import os

    from providers.sec import get_sec_provider

    now = _now_iso()
    detail = FeroldiDetailResult(
        candidate_id=candidate_id,
        ticker=ticker,
        rubric_version="FEROLDI-38-V1",
        as_of_date=now,
        created_at=now,
        last_updated=now,
    )

    # Collect yfinance metrics
    metrics = yfinance_metrics or collect_yfinance_metrics(ticker)
    if not metrics:
        detail.extraction_status = "YFINANCE_FAILED"
        detail.last_error = "yfinance data collection failed"
        return detail

    detail.company_name = str(metrics.get("company_name", ""))
    detail.google_ticker = ticker
    detail.reporting_currency = str(metrics.get("currency", "USD"))
    detail.cik = str(metrics.get("cik", ""))

    # Populate raw financial inputs from yfinance metrics into question-result fields.
    # Accept both transformed metric keys (from collect_yfinance_metrics) and
    # raw yfinance info keys (from tests or alternative sources) as fallback.
    def _get(transformed_key: str, yf_fallback: str = "") -> Any:
        val = metrics.get(transformed_key)
        if val is not None:
            return val
        return metrics.get(yf_fallback) if yf_fallback else None

    src = metrics.get("source", "yfinance")
    period = metrics.get("source_period", "")
    src_date = metrics.get("source_date", now)

    # F01: Cash and long-term debt
    detail.f01.cash_and_equivalents = _get("cash", "totalCash")
    detail.f01.long_term_debt = _get("long_term_debt", "longTermDebt")
    detail.f01.source = src
    detail.f01.source_period = period
    detail.f01.source_date = src_date

    # F02: Gross margin
    detail.f02.revenue_ttm = _get("revenue_ttm", "totalRevenue")
    detail.f02.cost_of_revenue_ttm = _get("cost_of_revenue_ttm", "costOfRevenue")
    detail.f02.gross_profit_ttm = _get("gross_profit_ttm", "grossProfits")
    detail.f02.source = src
    detail.f02.source_period = period
    detail.f02.source_date = src_date

    # F03: ROE (current only from info; quarterly overrides in scoring)
    detail.f03.current_net_income_ttm = _get("net_income_ttm", "netIncomeToCommon")
    detail.f03.source = src
    detail.f03.source_period = period
    detail.f03.source_date = src_date

    # F04: FCF
    detail.f04.current_operating_cf_ttm = _get("operating_cf", "operatingCashflow")
    detail.f04.current_capex_ttm = _get("capex", "capitalExpenditure")
    detail.f04.source = src
    detail.f04.source_period = period
    detail.f04.source_date = src_date

    # F05: EPS
    detail.f05.current_diluted_eps_ttm = _get("diluted_eps", "dilutedEPS")
    detail.f05.source = src
    detail.f05.source_period = period
    detail.f05.source_date = src_date

    # M02: Ownership (partial — insider group %)
    detail.m02.current_share_price = _get("current_price", "currentPrice")
    detail.m02.basic_shares_outstanding = _get("shares_outstanding", "sharesOutstanding")
    detail.m02.directors_officers_group_pct = _get("insider_pct")
    if detail.m02.directors_officers_group_pct is not None and detail.m02.directors_officers_group_pct > 1:
        detail.m02.directors_officers_group_pct = detail.m02.directors_officers_group_pct / 100.0
    detail.m02.source = src
    detail.m02.source_date = src_date

    # S02: Shareholder actions
    detail.s02.share_repurchases_ttm = _get("share_repurchases")
    detail.s02.market_capitalisation = _get("market_cap", "marketCap")
    detail.s02.diluted_shares_current = _get("diluted_shares", "sharesOutstanding")
    detail.s02.dividend_per_share_ttm = _get("dividend_rate", "dividendRate")
    detail.s02.total_debt_current = _get("total_debt", "totalDebt")
    detail.s02.total_assets = _get("total_assets", "totalAssets")
    detail.s02.source = src
    detail.s02.source_date = src_date

    # Collect quarterly financials for prior-period comparison (F03/F04/F05)
    # Also provides fallback data for F01 (long-term debt) and S02 (share repurchases)
    try:
        quarterly = collect_quarterly_financials(ticker)
    except Exception as exc:
        logger.warning("Quarterly financials for %s failed: %s", ticker, exc.__class__.__name__)
        quarterly = {}

    # Apply quarterly fallbacks for F01 (long-term debt), S02 (share repurchases, total assets)
    if detail.f01.long_term_debt is None and quarterly:
        detail.f01.long_term_debt = quarterly.get("lt_debt_current")
        if detail.f01.long_term_debt is not None:
            logger.info("F01 long_term_debt from quarterly balance sheet: %s", detail.f01.long_term_debt)

    if detail.s02.share_repurchases_ttm is None and quarterly:
        detail.s02.share_repurchases_ttm = quarterly.get("repurchases_current")

    if detail.s02.total_assets is None and quarterly:
        detail.s02.total_assets = quarterly.get("total_assets")

    # Collect earnings surprise data (S03)
    try:
        earnings_data = collect_earnings_surprise(ticker)
    except Exception as exc:
        logger.warning("Earnings surprise for %s failed: %s", ticker, exc.__class__.__name__)
        earnings_data = {}

    # Store quarterly + earnings data on the detail for use by scoring
    detail.quarterly = quarterly
    detail.earnings = earnings_data

    # Collect price history
    price_history = collect_price_history(ticker)
    detail.s01.measurement_start_date = price_history.get("start_date", "")
    detail.s01.measurement_end_date = price_history.get("end_date", "")
    detail.s01.trading_days = price_history.get("trading_days", 0)
    detail.s01.stock_start_adjusted_price = price_history.get("stock_start_price")
    detail.s01.stock_end_adjusted_price = price_history.get("stock_end_price")
    detail.s01.spy_start_adjusted_price = price_history.get("spy_start_price")
    detail.s01.spy_end_adjusted_price = price_history.get("spy_end_price")

    # Collect SEC filing evidence for M01 and M03
    if filing_texts is None:
        try:
            filing_texts = extract_filing_text(ticker=ticker)
        except Exception as exc:
            logger.warning("SEC filing text extraction failed for %s: %s", ticker, exc.__class__.__name__)
            filing_texts = {}

    # M01: CEO evidence
    if filing_texts:
        ceo_evidence = extract_ceo_evidence(filing_texts)
        detail.m01.evidence_text = ceo_evidence.get("evidence_text", "")
        detail.m01.primary_source_type = ceo_evidence.get("source_type", "")
        detail.m01.extraction_confidence = ceo_evidence.get("extraction_confidence", "UNKNOWN")
        detail.m01.last_updated = now

        # M03: Mission evidence
        mission_evidence = extract_mission_evidence(filing_texts)
        detail.m03.mission_text = mission_evidence.get("mission_text", "")
        detail.m03.source = mission_evidence.get("source_type", "")
        detail.m03.extraction_phrase = mission_evidence.get("extraction_phrase", "")
        detail.m03.extraction_confidence = mission_evidence.get("extraction_confidence", "UNKNOWN")

    detail.extraction_status = "COMPLETE"
    return detail
