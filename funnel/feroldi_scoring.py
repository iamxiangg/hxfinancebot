from __future__ import annotations

"""Feroldi first-cut scoring orchestrator.

Takes a FeroldiDetailResult with raw inputs populated (from enrichment)
and runs all 11 deterministic scoring functions.
"""

import logging
from typing import Any

from funnel.feroldi_config import (
    FINANCIALS_MAX,
    MANAGEMENT_MAX,
    RUBRIC_VERSION,
    STOCK_MAX,
)
from funnel.feroldi_enrichment import enrich_feroldi_detail
from funnel.feroldi_financials import (
    score_f01,
    score_f02,
    score_f03,
    score_f04,
    score_f05,
)
from funnel.feroldi_management import score_m01, score_m02, score_m03
from funnel.feroldi_models import FeroldiDetailResult
from funnel.feroldi_stock import score_s01, score_s02, score_s03

logger = logging.getLogger(__name__)


def score_feroldi_detail(
    detail: FeroldiDetailResult,
    *,
    metrics: dict[str, Any] | None = None,
) -> FeroldiDetailResult:
    """Run all 11 scoring functions on a populated FeroldiDetailResult.

    The detail must already have raw inputs. The metrics dict is an
    optional flat dict of yfinance fields for fallback data.
    """
    m = metrics or {}
    src = m.get("source", "yfinance")
    period = m.get("source_period", "")
    src_date = m.get("source_date", "") or detail.as_of_date

    # --- Financials ---

    # F01
    cash = detail.f01.cash_and_equivalents or _nf(m, "cash")
    lt_debt = detail.f01.long_term_debt or _nf(m, "long_term_debt")
    detail.f01 = score_f01(cash=cash, long_term_debt=lt_debt, source=src, source_period=period, source_date=src_date)

    # F02
    rev = detail.f02.revenue_ttm or _nf(m, "revenue_ttm")
    cor = detail.f02.cost_of_revenue_ttm or _nf(m, "cost_of_revenue_ttm")
    gp = detail.f02.gross_profit_ttm or _nf(m, "gross_profit_ttm")
    detail.f02 = score_f02(revenue_ttm=rev, cost_of_revenue_ttm=cor, gross_profit_ttm=gp, source=src, source_period=period, source_date=src_date)

    # F03 — prefer quarterly TTM, fall back to yfinance info via detail fields
    q = detail.quarterly or {}
    ni_cur = _nf(q, "ni_current") or _nf(m, "net_income_ttm") or detail.f03.current_net_income_ttm
    ni_prior = _nf(q, "ni_prior") or detail.f03.prior_net_income_ttm
    ni_2y = _nf(q, "ni_2y")
    eq_cur = _nf(q, "equity_current") or _nf(m, "total_equity")
    eq_prior = _nf(q, "equity_prior")
    eq_2y = _nf(q, "equity_2y")
    # Store raw year-2 inputs before scoring (so sheet columns are populated)
    detail.f03.two_year_net_income_ttm = ni_2y
    detail.f03.two_year_opening_equity = eq_2y
    detail.f03.two_year_closing_equity = eq_2y
    detail.f03 = score_f03(
        current_net_income=ni_cur,
        current_opening_equity=eq_cur,
        current_closing_equity=eq_cur,
        prior_net_income=ni_prior,
        prior_opening_equity=eq_prior,
        prior_closing_equity=eq_prior,
        two_year_net_income=ni_2y,
        two_year_opening_equity=eq_2y,
        two_year_closing_equity=eq_2y,
        source=src, source_period=period, source_date=src_date,
    )

    # F04 — prefer quarterly TTM, fall back to yfinance info via detail fields
    ocf_cur = _nf(q, "ocf_current") or _nf(m, "operating_cf") or detail.f04.current_operating_cf_ttm
    capex_cur = _nf(q, "capex_current") or _nf(m, "capex") or detail.f04.current_capex_ttm
    ocf_prior = _nf(q, "ocf_prior") or detail.f04.prior_operating_cf_ttm
    capex_prior = _nf(q, "capex_prior") or detail.f04.prior_capex_ttm
    ocf_2y = _nf(q, "ocf_2y")
    capex_2y = _nf(q, "capex_2y")
    # Store raw year-2 inputs before scoring (so sheet columns are populated)
    detail.f04.two_year_operating_cf_ttm = ocf_2y
    detail.f04.two_year_capex_ttm = capex_2y
    detail.f04 = score_f04(
        current_operating_cf=ocf_cur, current_capex=capex_cur,
        prior_operating_cf=ocf_prior, prior_capex=capex_prior,
        two_year_operating_cf=ocf_2y, two_year_capex=capex_2y,
        source=src, source_period=period, source_date=src_date,
    )

    # F05 — prefer quarterly TTM, fall back to yfinance info via detail fields
    eps_cur = _nf(q, "eps_current") or _nf(m, "diluted_eps") or detail.f05.current_diluted_eps_ttm
    eps_prior = _nf(q, "eps_prior") or detail.f05.prior_diluted_eps_ttm
    eps_2y = _nf(q, "eps_2y")
    # Store raw year-2 input before scoring (so sheet column is populated)
    detail.f05.two_year_diluted_eps_ttm = eps_2y
    detail.f05 = score_f05(
        current_diluted_eps=eps_cur, prior_diluted_eps=eps_prior,
        two_year_diluted_eps=eps_2y,
        source=src, source_period=period, source_date=src_date,
    )

    # --- Management ---

    # M01
    detail.m01 = score_m01(
        evidence_text=detail.m01.evidence_text or "",
        source_type=detail.m01.primary_source_type or "",
        extraction_confidence=detail.m01.extraction_confidence or "UNKNOWN",
        last_updated=detail.as_of_date,
    )

    # M02 — read insider ownership from detail fields populated by enrichment
    insider_pct = detail.m02.directors_officers_group_pct
    if insider_pct is not None:
        detail.m02 = score_m02(
            directors_officers_group_pct=insider_pct,
            basic_shares_outstanding=detail.m02.basic_shares_outstanding,
            current_share_price=detail.m02.current_share_price,
            extraction_confidence="MEDIUM",
        )
    else:
        detail.m02 = score_m02(
            basic_shares_outstanding=detail.m02.basic_shares_outstanding,
            current_share_price=detail.m02.current_share_price,
            extraction_confidence="LOW",
        )

    # M03
    detail.m03 = score_m03(
        mission_text=detail.m03.mission_text or "",
        source_type=detail.m03.source or "",
        extraction_confidence=detail.m03.extraction_confidence or "UNKNOWN",
        last_updated=detail.as_of_date,
    )

    # --- Stock ---

    # S01
    detail.s01 = score_s01(
        stock_start_price=detail.s01.stock_start_adjusted_price,
        stock_end_price=detail.s01.stock_end_adjusted_price,
        spy_start_price=detail.s01.spy_start_adjusted_price,
        spy_end_price=detail.s01.spy_end_adjusted_price,
        trading_days=detail.s01.trading_days,
        start_date=detail.s01.measurement_start_date,
        end_date=detail.s01.measurement_end_date,
        source="yfinance",
    )

    # S02 — read from detail fields populated by enrichment
    detail.s02 = score_s02(
        share_repurchases_ttm=detail.s02.share_repurchases_ttm,
        share_issuance_ttm=None,
        market_cap=detail.s02.market_capitalisation,
        diluted_shares_current=detail.s02.diluted_shares_current,
        diluted_shares_prior=None,
        dividend_per_share_ttm=detail.s02.dividend_per_share_ttm,
        dividend_per_share_prior=None,
        total_debt_current=detail.s02.total_debt_current,
        total_debt_prior=None,
        total_assets=detail.s02.total_assets,
        source=src, source_date=src_date,
    )

    # S03 — earnings surprise from quarterly earnings_dates
    e = detail.earnings or {}
    detail.s03 = score_s03(
        q1_reported=_nf(e, "q1_reported"), q1_estimated=_nf(e, "q1_estimated"),
        q1_fiscal_period=str(e.get("q1_report_date", "")),
        q2_reported=_nf(e, "q2_reported"), q2_estimated=_nf(e, "q2_estimated"),
        q2_fiscal_period=str(e.get("q2_report_date", "")),
        q3_reported=_nf(e, "q3_reported"), q3_estimated=_nf(e, "q3_estimated"),
        q3_fiscal_period=str(e.get("q3_report_date", "")),
        q4_reported=_nf(e, "q4_reported"), q4_estimated=_nf(e, "q4_estimated"),
        q4_fiscal_period=str(e.get("q4_report_date", "")),
        source=src, source_date=src_date,
    )

    # Aggregate
    detail.aggregate()
    detail.extraction_status = "SCORED"
    detail.last_updated = detail.as_of_date

    return detail


def _nf(data: dict[str, Any], key: str) -> Any:
    """Safely get a numeric value from dict."""
    val = data.get(key)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def run_feroldi_first_cut(
    ticker: str,
    *,
    candidate_id: str = "",
) -> FeroldiDetailResult:
    """End-to-end: enrich and score a single ticker for the Feroldi first cut.

    This is the main entry point called by the review funnel.
    """
    logger.info("Feroldi first-cut starting for %s", ticker)

    # Enrich
    detail = enrich_feroldi_detail(ticker, candidate_id=candidate_id)

    # Score
    detail = score_feroldi_detail(detail)

    logger.info(
        "Feroldi first-cut for %s: score=%.1f available=%.1f/%d",
        ticker, detail.first_cut_score, detail.available_points, 38,
    )

    return detail


def detail_to_candidate_updates(detail: FeroldiDetailResult) -> dict[str, Any]:
    """Convert a scored FeroldiDetailResult into BTD_Candidates row updates."""
    return {
        "Feroldi Financial Score": round(detail.financial_score, 2),
        "Feroldi Financial Available": round(detail.financial_available, 2),
        "Feroldi Management Score": round(detail.management_score, 2),
        "Feroldi Management Available": round(detail.management_available, 2),
        "Feroldi Stock Score": round(detail.stock_score, 2),
        "Feroldi Stock Available": round(detail.stock_available, 2),
        "Feroldi First Cut Score": round(detail.first_cut_score, 2),
        "Feroldi Available Points": round(detail.available_points, 2),
        "Feroldi Max Points": 38.0,
        "Feroldi Equivalent Score": round(detail.equivalent_score, 2) if detail.available_points > 0 else "",
        "Feroldi Coverage": round(detail.coverage, 4),
        "Feroldi Missing Inputs": ", ".join(detail.missing_inputs) if detail.missing_inputs else "",
        "Feroldi Last Updated": detail.last_updated,
        # Trajectory labels for filtering/sorting
        "Feroldi F03 Trajectory": detail.f03.trajectory_label or "",
        "Feroldi F04 Trajectory": detail.f04.trajectory_label or "",
        "Feroldi F05 Trajectory": detail.f05.trajectory_label or "",
        # Weighted growth % for F03/F04/F05
        "Feroldi F03 Weighted ROE Growth %": round(detail.f03.weighted_roe_growth_pct, 4) if detail.f03.weighted_roe_growth_pct is not None else "",
        "Feroldi F04 Weighted FCF Growth %": round(detail.f04.weighted_fcf_growth_pct, 4) if detail.f04.weighted_fcf_growth_pct is not None else "",
        "Feroldi F05 Weighted EPS Growth %": round(detail.f05.weighted_eps_growth_pct, 4) if detail.f05.weighted_eps_growth_pct is not None else "",
    }
