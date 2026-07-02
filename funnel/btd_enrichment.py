from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from providers.yahoo_throttle import create_ticker, yahoo_call


InfoFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class BtdMetrics:
    ticker: str
    company_name: str = ""
    next_earnings_date: str = ""
    sector: str = ""
    industry: str = ""
    quote_type: str = ""
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
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


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


def calculate_btd_score(metrics: BtdMetrics) -> float | None:
    ratio = calculate_btd_ratio(metrics)
    if ratio is None:
        return None
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
    if ratio != ratio or ratio in {float("inf"), float("-inf")}:
        return None
    return ratio


def determine_btd_applicability(metrics: BtdMetrics) -> str:
    quote_type = str(metrics.quote_type or "").strip().lower()
    sector = str(metrics.sector or "").strip().lower()
    industry = str(metrics.industry or "").strip().lower()
    company_name = str(metrics.company_name or "").strip().lower()

    haystack = " | ".join(part for part in (quote_type, sector, industry, company_name) if part)
    if not haystack:
        return "UNAVAILABLE"

    not_applicable_tokens = (
        "bank",
        "insurance",
        "reit",
        "mortgage reit",
        "closed-end fund",
        "closed end fund",
        "etf",
        "etn",
        "shell",
        "blank check",
        "spac",
        "royalty",
        "trust",
        "fund",
    )
    if any(token in haystack for token in not_applicable_tokens):
        return "NOT_APPLICABLE"

    if "biotech" in haystack:
        total_revenue = to_float(metrics.total_revenue)
        if total_revenue is None or total_revenue <= 0:
            return "NOT_APPLICABLE"

    required_values = (
        to_float(metrics.enterprise_value),
        to_float(metrics.total_revenue),
        to_float(metrics.gross_margin),
        to_float(metrics.revenue_growth),
    )
    if any(value is None for value in required_values):
        return "UNAVAILABLE"
    return "APPLICABLE"


def calculate_btd_components(metrics: BtdMetrics) -> dict[str, Any]:
    enterprise_value = to_float(metrics.enterprise_value)
    total_revenue = to_float(metrics.total_revenue)
    gross_margin = to_float(metrics.gross_margin)
    revenue_growth = to_float(metrics.revenue_growth)
    ratio = calculate_btd_ratio(metrics)

    ev_b = enterprise_value / 1_000_000_000 if enterprise_value is not None else None
    revenue_b = total_revenue / 1_000_000_000 if total_revenue is not None else None
    gross_margin_pct = gross_margin * 100 if gross_margin is not None else None
    revenue_growth_pct = revenue_growth * 100 if revenue_growth is not None else None

    formula = ""
    if (
        ratio is not None
        and ev_b is not None
        and revenue_b is not None
        and gross_margin is not None
        and revenue_growth_pct is not None
    ):
        formula = (
            "EV(B) / (Revenue(B) * Gross Margin(decimal) * Revenue Growth(%pts)) = "
            f"{ev_b:.2f} / ({revenue_b:.2f} * {gross_margin:.4f} * {revenue_growth_pct:.1f})"
        )

    return {
        "EV (B)": round(ev_b, 2) if ev_b is not None else "",
        "Revenue TTM (B)": round(revenue_b, 2) if revenue_b is not None else "",
        "Gross Margin %": round(gross_margin_pct, 1) if gross_margin_pct is not None else "",
        "Revenue Growth %": round(revenue_growth_pct, 1) if revenue_growth_pct is not None else "",
        "BTD Formula": formula,
    }


def build_btd_summary(metrics: BtdMetrics, score: float | None) -> str:
    parts = [f"BTD {score}" if score is not None else "BTD unavailable"]
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
    yf_ticker = create_ticker(ticker)
    info = yahoo_call(lambda: yf_ticker.info or {}, label=f"btd-info:{ticker}") or {}

    next_earnings = ""
    try:
        calendar = yahoo_call(lambda: yf_ticker.calendar, label=f"btd-calendar:{ticker}")
        if calendar is not None and "Earnings Date" in calendar:
            next_earnings = _format_earnings_date(calendar["Earnings Date"])
    except Exception:
        next_earnings = ""

    return BtdMetrics(
        ticker=ticker.upper(),
        company_name=str(info.get("shortName") or info.get("longName") or ""),
        next_earnings_date=next_earnings,
        sector=str(info.get("sector") or ""),
        industry=str(info.get("industry") or ""),
        quote_type=str(info.get("quoteType") or ""),
        enterprise_value=info.get("enterpriseValue", ""),
        total_revenue=info.get("totalRevenue", ""),
        ebitda_margin=info.get("ebitdaMargins", ""),
        revenue_growth=info.get("revenueGrowth", ""),
        gross_margin=info.get("grossMargins", ""),
        employees=info.get("fullTimeEmployees", ""),
    )


def metrics_to_candidate_updates(metrics: BtdMetrics) -> dict[str, Any]:
    score = calculate_btd_score(metrics)
    ratio = calculate_btd_ratio(metrics)
    updates = {
        "Company Name": metrics.company_name,
        "Google Ticker": metrics.ticker,
        "BTD Score": "" if score is None else score,
        "BTD Ratio": "" if ratio is None else ratio,
        "BTD Summary": build_btd_summary(metrics, score),
        "BTD Applicability": determine_btd_applicability(metrics),
        "Next Earnings Date": metrics.next_earnings_date,
        "Enterprise Value": metrics.enterprise_value,
        "Total Revenue": metrics.total_revenue,
        "EBITDA Margin": metrics.ebitda_margin,
        "Revenue Growth": metrics.revenue_growth,
        "Gross Margin": metrics.gross_margin,
        "Employees": metrics.employees,
    }
    updates.update(calculate_btd_components(metrics))
    return updates
