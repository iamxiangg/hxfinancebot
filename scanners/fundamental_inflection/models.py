from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any


MODEL_VERSION = "2026-06-28-fundamental-inflection-v1"

BUSINESS_MODEL_EXCLUDE_SIC = {
    "60", "61", "62", "63", "64", "65", "66", "67",
}
BUSINESS_MODEL_EXCLUDE_TOKENS = (
    " bank ", " bancorp ", " banc ", "bancshares", " financial ", "reit",
    " real estate investment trust", " insurance ", " insurer ",
    " fund ", " etf ", " spac ", " acquisition corp", " holdings",
)
COMMODITY_EXPLORE_TOKENS = (
    " exploration ", " mining ", " minerals ", " oil & gas",
    " petroleum ", " drilling ", " gold ", " silver ",
    " copper ", " resources ",
)
BIOTECH_PRE_REVENUE_TOKENS = (
    " therapeutics ", " biopharma ", " bioscience ", " biotech ",
)


@dataclass(frozen=True)
class FundamentalInflectionConfig:
    enable: bool = True
    lookback_days: int = 7
    valid_days: int = 30
    min_price: float = 3.0
    min_market_cap: float = 300_000_000.0
    min_median_dollar_volume: float = 5_000_000.0
    min_revenue_growth: float = 0.20
    min_quarters: int = 6
    max_results: int = 20
    early_inflection_threshold: float = 55.0
    validated_inflection_threshold: float = 70.0
    strong_inflection_threshold: float = 82.0
    severe_dilution_threshold: float = 0.12
    high_dilution_threshold: float = 0.07
    cash_runway_severe_months: int = 12
    cash_runway_risk_months: int = 24
    severe_balance_sheet_veto: bool = True

    @classmethod
    def from_env(cls) -> "FundamentalInflectionConfig":
        return cls(
            enable=_env_bool("FUNDAMENTAL_INFLECTION_ENABLE", True),
            lookback_days=_env_int("FUNDAMENTAL_INFLECTION_LOOKBACK_DAYS", 7),
            valid_days=_env_int("FUNDAMENTAL_INFLECTION_VALID_DAYS", 30),
            min_price=_env_float("FUNDAMENTAL_INFLECTION_MIN_PRICE", 3.0),
            min_market_cap=_env_float("FUNDAMENTAL_INFLECTION_MIN_MARKET_CAP", 300_000_000.0),
            min_median_dollar_volume=_env_float("FUNDAMENTAL_INFLECTION_MIN_MEDIAN_DOLLAR_VOLUME", 5_000_000.0),
            min_revenue_growth=_env_float("FUNDAMENTAL_INFLECTION_MIN_REVENUE_GROWTH", 0.20),
            min_quarters=_env_int("FUNDAMENTAL_INFLECTION_MIN_QUARTERS", 6),
            max_results=_env_int("FUNDAMENTAL_INFLECTION_MAX_RESULTS", 20),
        )


@dataclass
class QuarterlySnapshot:
    quarter_label: str
    period_end: date
    fiscal_year: int
    fiscal_period: str
    accession: str
    filed_at: date
    revenue: float | None = None
    cost_of_revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    accounts_receivable: float | None = None
    inventory: float | None = None
    diluted_shares: float | None = None
    stock_based_comp: float | None = None
    is_derived_q4: bool = False


@dataclass
class FinancialSeries:
    ticker: str
    cik: str
    quarters: list[QuarterlySnapshot] = field(default_factory=list)
    data_confidence: str = "low"
    errors: list[str] = field(default_factory=list)

    @property
    def usable_quarters(self) -> list[QuarterlySnapshot]:
        return [q for q in self.quarters if q.revenue is not None]


@dataclass
class RevenueGrowthMetrics:
    latest_revenue: float
    revenue_four_quarters_ago: float
    yoy_growth: float
    prior_quarter_growth: float | None
    growth_acceleration: float | None
    growth_consistency: str
    quarters_above_20pct: int
    trend: str


@dataclass
class GrossEconomicsMetrics:
    gross_profit_growth: float | None
    gross_margin_latest: float | None
    gross_margin_prior: float | None
    gross_margin_change_bps: float | None
    gross_confirmation: str
    flags: list[str]


@dataclass
class OperatingLeverageMetrics:
    operating_margin_latest: float | None
    operating_margin_prior: float | None
    operating_margin_change_bps: float | None
    incremental_operating_margin: float | None
    operating_loss_narrowing: bool
    operating_confirmation: str
    flags: list[str]


@dataclass
class CashFlowMetrics:
    ttm_operating_cash_flow: float | None
    ttm_capital_expenditure: float | None
    ttm_free_cash_flow: float | None
    ttm_fcf_margin: float | None
    prior_ttm_fcf_margin: float | None
    ttm_fcf_margin_change_bps: float | None
    fcf_classification: str
    cash_confirmation: str
    flags: list[str]


@dataclass
class PerShareMetrics:
    diluted_share_growth: float | None
    revenue_per_share_latest: float | None
    revenue_per_share_prior: float | None
    revenue_per_share_growth: float | None
    sbc_to_revenue: float | None
    dilution_classification: str
    per_share_confirmation: str
    flags: list[str]


@dataclass
class BalanceSheetMetrics:
    cash: float | None
    total_debt: float | None
    net_cash: float | None
    cash_runway_months: float | None
    balance_sheet_classification: str
    flags: list[str]


@dataclass
class WorkingCapitalMetrics:
    ar_growth: float | None
    inventory_growth: float | None
    revenue_growth: float
    ar_divergence: float | None
    inventory_divergence: float | None
    flags: list[str]


@dataclass
class InflectionResult:
    ticker: str
    classification: str
    total_score: float
    latest_filing_accession: str
    filing_date: date | None
    latest_quarterly_revenue: float
    revenue_growth_yoy: float
    prior_quarter_growth: float | None
    growth_acceleration: float | None
    gross_profit_growth: float | None
    gross_margin_change_bps: float | None
    operating_margin_change_bps: float | None
    incremental_operating_margin: float | None
    ttm_fcf_margin: float | None
    ttm_fcf_margin_change_bps: float | None
    diluted_share_growth: float | None
    revenue_per_share_growth: float | None
    cash: float | None
    debt: float | None
    cash_runway_months: float | None
    positive_pillars: list[str]
    pilllar_count: int
    economic_confirmation: bool
    risk_flags: list[str]
    data_confidence: str
    valid_for_days: int
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
