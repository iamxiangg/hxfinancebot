from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable


InfoFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class BtdMetrics:
    ticker: str
    company_name: str = ""
    next_earnings_date: str = ""
    enterprise_value: Any = ""
    total_revenue: Any = ""
    ebitda_margin: Any = ""
    revenue_growth: Any = ""
    gross_margin: Any = ""
    employees: Any = ""


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percent_text(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number * 100:.1f}%"


def compact_number(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return ""
    abs_number = abs(number)
    if abs_number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if abs_number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    return f"{number:.0f}"


def _score_thresholds(
    value: float | None,
    thresholds: list[tuple[float, int]],
) -> int:
    if value is None:
        return 0
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 0


def calculate_btd_score(metrics: BtdMetrics) -> int:
    revenue_growth = to_float(metrics.revenue_growth)
    gross_margin = to_float(metrics.gross_margin)
    ebitda_margin = to_float(metrics.ebitda_margin)
    total_revenue = to_float(metrics.total_revenue)
    enterprise_value = to_float(metrics.enterprise_value)

    score = 0
    score += _score_thresholds(
        revenue_growth,
        [(0.30, 30), (0.15, 23), (0.05, 15), (0.0, 7)],
    )
    score += _score_thresholds(
        gross_margin,
        [(0.70, 25), (0.50, 18), (0.35, 10), (0.20, 5)],
    )
    score += _score_thresholds(
        ebitda_margin,
        [(0.25, 20), (0.15, 14), (0.05, 8), (0.0, 3)],
    )
    score += _score_thresholds(
        total_revenue,
        [(10_000_000_000, 10), (1_000_000_000, 7), (250_000_000, 4)],
    )

    if enterprise_value and total_revenue and total_revenue > 0:
        ev_to_sales = enterprise_value / total_revenue
        if ev_to_sales <= 5:
            score += 10
        elif ev_to_sales <= 10:
            score += 6
        elif ev_to_sales <= 20:
            score += 3

    return max(0, min(100, int(score)))


def build_btd_summary(metrics: BtdMetrics, score: int) -> str:
    parts = [f"BTD {score}/100"]
    if metrics.revenue_growth not in ("", None):
        parts.append(f"Revenue growth {percent_text(metrics.revenue_growth)}")
    if metrics.gross_margin not in ("", None):
        parts.append(f"Gross margin {percent_text(metrics.gross_margin)}")
    if metrics.ebitda_margin not in ("", None):
        parts.append(f"EBITDA margin {percent_text(metrics.ebitda_margin)}")
    if metrics.total_revenue not in ("", None):
        parts.append(f"Revenue {compact_number(metrics.total_revenue)}")
    return " | ".join(part for part in parts if part)


def _format_earnings_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (list, tuple)) and value:
        return _format_earnings_date(value[0])
    return str(value)


def fetch_yfinance_metrics(ticker: str) -> BtdMetrics:
    import yfinance as yf

    yf_ticker = yf.Ticker(ticker)
    info = yf_ticker.info or {}

    next_earnings = ""
    try:
        calendar = yf_ticker.calendar
        if calendar is not None and "Earnings Date" in calendar:
            next_earnings = _format_earnings_date(calendar["Earnings Date"])
    except Exception:
        next_earnings = ""

    return BtdMetrics(
        ticker=ticker.upper(),
        company_name=str(info.get("shortName") or info.get("longName") or ""),
        next_earnings_date=next_earnings,
        enterprise_value=info.get("enterpriseValue", ""),
        total_revenue=info.get("totalRevenue", ""),
        ebitda_margin=info.get("ebitdaMargins", ""),
        revenue_growth=info.get("revenueGrowth", ""),
        gross_margin=info.get("grossMargins", ""),
        employees=info.get("fullTimeEmployees", ""),
    )


def metrics_to_candidate_updates(metrics: BtdMetrics) -> dict[str, Any]:
    score = calculate_btd_score(metrics)
    return {
        "Company Name": metrics.company_name,
        "Google Ticker": metrics.ticker,
        "BTD Score": score,
        "BTD Summary": build_btd_summary(metrics, score),
        "Next Earnings Date": metrics.next_earnings_date,
        "Enterprise Value": metrics.enterprise_value,
        "Total Revenue": metrics.total_revenue,
        "EBITDA Margin": metrics.ebitda_margin,
        "Revenue Growth": metrics.revenue_growth,
        "Gross Margin": metrics.gross_margin,
        "Employees": metrics.employees,
    }
