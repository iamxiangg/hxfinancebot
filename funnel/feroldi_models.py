from __future__ import annotations

"""Feroldi 38-point first-cut dataclasses.

Every question-level raw input, intermediate calculation, score and
available-point value is represented as a typed dataclass.
"""

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Base result
# ---------------------------------------------------------------------------


@dataclass
class QuestionResult:
    """Single Feroldi question score with evidence."""

    question_id: str = ""
    score: float = 0.0
    available: float = 0.0
    max_points: float = 0.0
    reason: str = ""
    source: str = ""
    source_period: str = ""
    source_date: str = ""


# ---------------------------------------------------------------------------
# Financials (F01–F05)
# ---------------------------------------------------------------------------


@dataclass
class F01CashToDebtResult(QuestionResult):
    question_id: str = "F01"
    max_points: float = 5.0

    cash_and_equivalents: float | None = None
    long_term_debt: float | None = None
    cash_to_lt_debt_ratio: float | None = None
    no_long_term_debt_flag: bool = False


@dataclass
class F02GrossMarginResult(QuestionResult):
    question_id: str = "F02"
    max_points: float = 3.0

    revenue_ttm: float | None = None
    cost_of_revenue_ttm: float | None = None
    gross_profit_ttm: float | None = None
    gross_margin_pct: float | None = None


@dataclass
class F03ROEResult(QuestionResult):
    question_id: str = "F03"
    max_points: float = 3.0

    current_net_income_ttm: float | None = None
    current_opening_equity: float | None = None
    current_closing_equity: float | None = None
    current_average_equity: float | None = None
    current_roe_pct: float | None = None
    prior_net_income_ttm: float | None = None
    prior_opening_equity: float | None = None
    prior_closing_equity: float | None = None
    prior_average_equity: float | None = None
    prior_roe_pct: float | None = None
    two_year_net_income_ttm: float | None = None
    two_year_opening_equity: float | None = None
    two_year_closing_equity: float | None = None
    two_year_roe_pct: float | None = None
    roe_growth_pct: float | None = None
    weighted_roe_growth_pct: float | None = None
    trajectory_label: str = ""
    turnaround_flag: bool = False
    valid_equity_flag: bool = False


@dataclass
class F04FCFResult(QuestionResult):
    question_id: str = "F04"
    max_points: float = 3.0

    current_operating_cf_ttm: float | None = None
    current_capex_ttm: float | None = None
    current_fcf_ttm: float | None = None
    prior_operating_cf_ttm: float | None = None
    prior_capex_ttm: float | None = None
    prior_fcf_ttm: float | None = None
    two_year_operating_cf_ttm: float | None = None
    two_year_capex_ttm: float | None = None
    two_year_fcf_ttm: float | None = None
    fcf_growth_pct: float | None = None
    weighted_fcf_growth_pct: float | None = None
    trajectory_label: str = ""
    turnaround_flag: bool = False


@dataclass
class F05EPSResult(QuestionResult):
    question_id: str = "F05"
    max_points: float = 3.0

    current_diluted_eps_ttm: float | None = None
    prior_diluted_eps_ttm: float | None = None
    two_year_diluted_eps_ttm: float | None = None
    eps_growth_pct: float | None = None
    weighted_eps_growth_pct: float | None = None
    trajectory_label: str = ""
    turnaround_flag: bool = False


# ---------------------------------------------------------------------------
# Management & Culture (M01–M03)
# ---------------------------------------------------------------------------


@dataclass
class M01SoulInGameResult(QuestionResult):
    question_id: str = "M01"
    max_points: float = 4.0

    ceo_name: str = ""
    ceo_appointment_date: str = ""
    ceo_appointment_year: int | None = None
    ceo_date_precision: str = ""
    ceo_tenure_years: float | None = None
    founder_flag: bool = False
    cofounder_flag: bool = False
    founding_family_flag: bool = False
    interim_ceo_flag: bool = False
    external_hire_flag: bool = False
    evidence_text: str = ""
    primary_source_type: str = ""
    primary_source_filing_date: str = ""
    primary_source_accession: str = ""
    primary_source_url: str = ""
    conflict_flag: bool = False
    extraction_confidence: str = ""
    last_updated: str = ""


@dataclass
class M02OwnershipResult(QuestionResult):
    question_id: str = "M02"
    max_points: float = 3.0

    ceo_beneficial_shares: float | None = None
    basic_shares_outstanding: float | None = None
    ceo_ownership_pct: float | None = None
    current_share_price: float | None = None
    ceo_stake_value_usd: float | None = None
    directors_officers_group_pct: float | None = None
    ownership_basis: str = ""
    extraction_confidence: str = ""


@dataclass
class M03MissionResult(QuestionResult):
    question_id: str = "M03"
    max_points: float = 3.0

    mission_text: str = ""
    source_url: str = ""
    word_count: int = 0
    sentence_count: int = 0
    structural_punctuation_count: int = 0
    parenthetical_count: int = 0
    action_verb_found: bool = False
    object_or_offering_found: bool = False
    beneficiary_found: bool = False
    outcome_found: bool = False
    undefined_acronym_count: int = 0
    vague_term_count: int = 0
    financial_only_flag: bool = False
    simple_point: int = 0
    clear_point: int = 0
    inspirational_point: int = 0
    extraction_phrase: str = ""
    extraction_confidence: str = ""


# ---------------------------------------------------------------------------
# Stock (S01–S03)
# ---------------------------------------------------------------------------


@dataclass
class S01PerformanceResult(QuestionResult):
    question_id: str = "S01"
    max_points: float = 4.0

    measurement_start_date: str = ""
    measurement_end_date: str = ""
    stock_start_adjusted_price: float | None = None
    stock_end_adjusted_price: float | None = None
    spy_start_adjusted_price: float | None = None
    spy_end_adjusted_price: float | None = None
    stock_total_return_pct: float | None = None
    spy_total_return_pct: float | None = None
    excess_return_points: float | None = None
    trading_days: int = 0
    short_listing_flag: bool = False


@dataclass
class S02ShareholderResult(QuestionResult):
    question_id: str = "S02"
    max_points: float = 3.0

    share_repurchases_ttm: float | None = None
    share_issuance_ttm: float | None = None
    net_repurchases_ttm: float | None = None
    market_capitalisation: float | None = None
    net_repurchases_to_mc_pct: float | None = None
    diluted_shares_current: float | None = None
    diluted_shares_prior: float | None = None
    diluted_share_change_pct: float | None = None

    buyback_point: int = 0
    buyback_available: int = 0

    dividend_per_share_ttm: float | None = None
    dividend_per_share_prior: float | None = None
    dividend_growth_pct: float | None = None
    dividend_cut_flag: bool = False
    dividend_data_valid_flag: bool = False

    dividend_point: int = 0
    dividend_available: int = 0

    total_debt_current: float | None = None
    total_debt_prior: float | None = None
    debt_change_pct: float | None = None
    total_assets: float | None = None
    effectively_debt_free_flag: bool = False

    debt_reduction_point: int = 0
    debt_reduction_available: int = 0


@dataclass
class S03EarningsSurpriseResult(QuestionResult):
    question_id: str = "S03"
    max_points: float = 4.0

    q1_fiscal_period: str = ""
    q1_report_date: str = ""
    q1_reported_eps: float | None = None
    q1_estimated_eps: float | None = None
    q1_absolute_surprise: float | None = None
    q1_surprise_pct: float | None = None
    q1_point: float = 0.0
    q2_fiscal_period: str = ""
    q2_report_date: str = ""
    q2_reported_eps: float | None = None
    q2_estimated_eps: float | None = None
    q2_absolute_surprise: float | None = None
    q2_surprise_pct: float | None = None
    q2_point: float = 0.0
    q3_fiscal_period: str = ""
    q3_report_date: str = ""
    q3_reported_eps: float | None = None
    q3_estimated_eps: float | None = None
    q3_absolute_surprise: float | None = None
    q3_surprise_pct: float | None = None
    q3_point: float = 0.0
    q4_fiscal_period: str = ""
    q4_report_date: str = ""
    q4_reported_eps: float | None = None
    q4_estimated_eps: float | None = None
    q4_absolute_surprise: float | None = None
    q4_surprise_pct: float | None = None
    q4_point: float = 0.0


# ---------------------------------------------------------------------------
# Aggregate result
# ---------------------------------------------------------------------------


@dataclass
class FeroldiDetailResult:
    candidate_id: str = ""
    ticker: str = ""
    company_name: str = ""
    google_ticker: str = ""
    rubric_version: str = "FEROLDI-38-V1"
    as_of_date: str = ""
    reporting_currency: str = "USD"
    data_jurisdiction: str = ""
    cik: str = ""
    extraction_status: str = ""
    missing_inputs: list[str] = field(default_factory=list)
    manual_review_required: bool = False
    last_error: str = ""
    created_at: str = ""
    last_updated: str = ""

    f01: F01CashToDebtResult = field(default_factory=F01CashToDebtResult)
    f02: F02GrossMarginResult = field(default_factory=F02GrossMarginResult)
    f03: F03ROEResult = field(default_factory=F03ROEResult)
    f04: F04FCFResult = field(default_factory=F04FCFResult)
    f05: F05EPSResult = field(default_factory=F05EPSResult)
    m01: M01SoulInGameResult = field(default_factory=M01SoulInGameResult)
    m02: M02OwnershipResult = field(default_factory=M02OwnershipResult)
    m03: M03MissionResult = field(default_factory=M03MissionResult)
    s01: S01PerformanceResult = field(default_factory=S01PerformanceResult)
    s02: S02ShareholderResult = field(default_factory=S02ShareholderResult)
    s03: S03EarningsSurpriseResult = field(default_factory=S03EarningsSurpriseResult)

    quarterly: dict[str, Any] = field(default_factory=dict, init=False)
    earnings: dict[str, Any] = field(default_factory=dict, init=False)

    financial_score: float = 0.0
    financial_available: float = 0.0
    management_score: float = 0.0
    management_available: float = 0.0
    stock_score: float = 0.0
    stock_available: float = 0.0
    first_cut_score: float = 0.0
    available_points: float = 0.0
    max_points: float = 38.0
    equivalent_score: float = 0.0
    coverage: float = 0.0

    def aggregate(self) -> None:
        questions = [
            self.f01, self.f02, self.f03, self.f04, self.f05,
            self.m01, self.m02, self.m03,
            self.s01, self.s02, self.s03,
        ]
        self.financial_score = sum(q.score for q in questions if q.question_id.startswith("F"))
        self.financial_available = sum(q.available for q in questions if q.question_id.startswith("F"))
        self.management_score = sum(q.score for q in questions if q.question_id.startswith("M"))
        self.management_available = sum(q.available for q in questions if q.question_id.startswith("M"))
        self.stock_score = sum(q.score for q in questions if q.question_id.startswith("S"))
        self.stock_available = sum(q.available for q in questions if q.question_id.startswith("S"))
        self.first_cut_score = self.financial_score + self.management_score + self.stock_score
        self.available_points = self.financial_available + self.management_available + self.stock_available
        self.max_points = 38.0
        if self.available_points > 0:
            self.equivalent_score = (self.first_cut_score / self.available_points) * 38.0
            self.coverage = self.available_points / 38.0
        missing: list[str] = []
        for q in questions:
            if q.available <= 0 and q.max_points > 0:
                missing.append(q.question_id)
        self.missing_inputs = missing
