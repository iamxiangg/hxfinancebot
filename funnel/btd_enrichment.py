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
    btd_ratio: float | None = None


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


def calculate_btd_score(metrics: BtdMetrics) -> float:
    ratio = calculate_btd_ratio(metrics)
    if ratio is None:
        return 0.0
    return round(ratio, 2)


def calculate_btd_ratio(metrics: BtdMetrics) -> float | None:
    enterprise_value = to_float(metrics.enterprise_value)
    total_revenue = to_float(metrics.total_revenue)
    gross_margin = to_float(metrics.gross_margin)
    revenue_growth = to_float(metrics.revenue_growth)

    if (
        enterprise_value is None
        or total_revenue is None
        or gross_margin is None
        or revenue_growth is None
        or enterprise_value <= 0
        or total_revenue <= 0
        or gross_margin <= 0
        or revenue_growth <= 0
    ):
        return None

    ev_b = enterprise_value / 1_000_000_000
    revenue_b = total_revenue / 1_000_000_000
    ratio = ev_b / (revenue_b * gross_margin * (revenue_growth * 100))
    return ratio


def build_btd_summary(metrics: BtdMetrics, score: float) -> str:
    parts = [f"BTD {score}"]
    ratio = calculate_btd_ratio(metrics)
    if ratio is not None:
        parts.append("Lower is better")
        parts.append(f"Efficiency ratio {ratio:.2f}")
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
        "BTD Ratio": calculate_btd_ratio(metrics) or "",
        "BTD Summary": build_btd_summary(metrics, score),
        "Next Earnings Date": metrics.next_earnings_date,
        "Enterprise Value": metrics.enterprise_value,
        "Total Revenue": metrics.total_revenue,
        "EBITDA Margin": metrics.ebitda_margin,
        "Revenue Growth": metrics.revenue_growth,
        "Gross Margin": metrics.gross_margin,
        "Employees": metrics.employees,
    }
