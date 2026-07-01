from __future__ import annotations

"""Feroldi first-cut sheet row serialization.

Converts scored FeroldiDetailResult objects into flat dicts
suitable for Google Sheets upsert.
"""

from typing import Any

from funnel.feroldi_scoring import detail_to_candidate_updates


def _nfrow(val: Any) -> str | float:
    """Format value for sheet row."""
    if val is None:
        return ""
    if isinstance(val, float):
        # ``int(float('nan'))`` raises ValueError. Treat NaN/Inf as missing so
        # that missing yfinance metrics do not crash the Feroldi enrichment.
        if val != val or val in {float("inf"), float("-inf")}:
            return ""
        return round(val, 4) if val != int(val) else int(val)
    return val


def detail_to_sheet_row(detail, now: str) -> dict[str, Any]:
    """Convert a scored FeroldiDetailResult into a flat dict for Google Sheets."""
    d = detail
    row: dict[str, Any] = {
        "Candidate ID": d.candidate_id or "",
        "Ticker": d.ticker or "",
        "Company Name": d.company_name or "",
        "Google Ticker": d.google_ticker or "",
        "Feroldi Rubric Version": d.rubric_version or "",
        "Feroldi As Of Date": d.as_of_date or "",
        "Reporting Currency": d.reporting_currency or "",
        "Data Jurisdiction": d.data_jurisdiction or "",
        "CIK": d.cik or "",
        "Extraction Status": d.extraction_status or "",
        "Missing Inputs": ", ".join(d.missing_inputs) if d.missing_inputs else "",
        "Manual Review Required": str(d.manual_review_required) if d.manual_review_required else "",
        "Last Error": d.last_error or "",
        "Created At": d.created_at or now,
        "Last Updated": d.last_updated or now,
    }
    # F01-F05
    row.update(_f01_sheet_row(d))
    row.update(_f02_sheet_row(d))
    row.update(_f03_sheet_row(d))
    row.update(_f04_sheet_row(d))
    row.update(_f05_sheet_row(d))
    # M01-M03
    row.update(_m01_sheet_row(d))
    row.update(_m02_sheet_row(d))
    row.update(_m03_sheet_row(d))
    # S01-S03
    row.update(_s01_sheet_row(d))
    row.update(_s02_sheet_row(d))
    row.update(_s03_sheet_row(d))
    # Aggregate — use shared function to avoid DRY violation
    row.update(detail_to_candidate_updates(d))
    return row


# ---------------------------------------------------------------------------
# Financials (F01–F05)
# ---------------------------------------------------------------------------


def _f01_sheet_row(d) -> dict:
    return {
        "F01 Cash And Cash Equivalents": _nfrow(d.f01.cash_and_equivalents),
        "F01 Long Term Debt": _nfrow(d.f01.long_term_debt),
        "F01 Cash To Long Term Debt Ratio": _nfrow(d.f01.cash_to_lt_debt_ratio),
        "F01 No Long Term Debt Flag": d.f01.no_long_term_debt_flag,
        "F01 Score": _nfrow(d.f01.score),
        "F01 Available": _nfrow(d.f01.available),
        "F01 Reason": d.f01.reason or "",
        "F01 Source": d.f01.source or "",
        "F01 Source Period": d.f01.source_period or "",
        "F01 Source Date": d.f01.source_date or "",
    }


def _f02_sheet_row(d) -> dict:
    return {
        "F02 Revenue TTM": _nfrow(d.f02.revenue_ttm),
        "F02 Cost Of Revenue TTM": _nfrow(d.f02.cost_of_revenue_ttm),
        "F02 Gross Profit TTM": _nfrow(d.f02.gross_profit_ttm),
        "F02 Gross Margin %": _nfrow(d.f02.gross_margin_pct),
        "F02 Score": _nfrow(d.f02.score),
        "F02 Available": _nfrow(d.f02.available),
        "F02 Reason": d.f02.reason or "",
        "F02 Source": d.f02.source or "",
        "F02 Source Period": d.f02.source_period or "",
        "F02 Source Date": d.f02.source_date or "",
    }


def _f03_sheet_row(d) -> dict:
    return {
        "F03 Current Net Income TTM": _nfrow(d.f03.current_net_income_ttm),
        "F03 Current Opening Equity": _nfrow(d.f03.current_opening_equity),
        "F03 Current Closing Equity": _nfrow(d.f03.current_closing_equity),
        "F03 Current Average Equity": _nfrow(d.f03.current_average_equity),
        "F03 Current ROE %": _nfrow(d.f03.current_roe_pct),
        "F03 Prior Net Income TTM": _nfrow(d.f03.prior_net_income_ttm),
        "F03 Prior Opening Equity": _nfrow(d.f03.prior_opening_equity),
        "F03 Prior Closing Equity": _nfrow(d.f03.prior_closing_equity),
        "F03 Prior Average Equity": _nfrow(d.f03.prior_average_equity),
        "F03 Prior ROE %": _nfrow(d.f03.prior_roe_pct),
        "F03 Two Year Net Income TTM": _nfrow(d.f03.two_year_net_income_ttm),
        "F03 Two Year ROE %": _nfrow(d.f03.two_year_roe_pct),
        "F03 ROE Growth %": _nfrow(d.f03.roe_growth_pct),
        "F03 Weighted ROE Growth %": _nfrow(d.f03.weighted_roe_growth_pct),
        "F03 Trajectory Label": d.f03.trajectory_label or "",
        "F03 Turnaround Flag": d.f03.turnaround_flag,
        "F03 Valid Equity Flag": d.f03.valid_equity_flag,
        "F03 Score": _nfrow(d.f03.score),
        "F03 Available": _nfrow(d.f03.available),
        "F03 Reason": d.f03.reason or "",
        "F03 Source": d.f03.source or "",
        "F03 Source Period": d.f03.source_period or "",
        "F03 Source Date": d.f03.source_date or "",
    }


def _f04_sheet_row(d) -> dict:
    return {
        "F04 Current Operating Cash Flow TTM": _nfrow(d.f04.current_operating_cf_ttm),
        "F04 Current Capital Expenditure TTM": _nfrow(d.f04.current_capex_ttm),
        "F04 Current Free Cash Flow TTM": _nfrow(d.f04.current_fcf_ttm),
        "F04 Prior Operating Cash Flow TTM": _nfrow(d.f04.prior_operating_cf_ttm),
        "F04 Prior Capital Expenditure TTM": _nfrow(d.f04.prior_capex_ttm),
        "F04 Prior Free Cash Flow TTM": _nfrow(d.f04.prior_fcf_ttm),
        "F04 Two Year Operating Cash Flow TTM": _nfrow(d.f04.two_year_operating_cf_ttm),
        "F04 Two Year Capital Expenditure TTM": _nfrow(d.f04.two_year_capex_ttm),
        "F04 Two Year Free Cash Flow TTM": _nfrow(d.f04.two_year_fcf_ttm),
        "F04 FCF Growth %": _nfrow(d.f04.fcf_growth_pct),
        "F04 Weighted FCF Growth %": _nfrow(d.f04.weighted_fcf_growth_pct),
        "F04 Trajectory Label": d.f04.trajectory_label or "",
        "F04 Turnaround Flag": d.f04.turnaround_flag,
        "F04 Score": _nfrow(d.f04.score),
        "F04 Available": _nfrow(d.f04.available),
        "F04 Reason": d.f04.reason or "",
        "F04 Source": d.f04.source or "",
        "F04 Source Period": d.f04.source_period or "",
        "F04 Source Date": d.f04.source_date or "",
    }


def _f05_sheet_row(d) -> dict:
    return {
        "F05 Current Diluted EPS TTM": _nfrow(d.f05.current_diluted_eps_ttm),
        "F05 Prior Diluted EPS TTM": _nfrow(d.f05.prior_diluted_eps_ttm),
        "F05 Two Year Diluted EPS TTM": _nfrow(d.f05.two_year_diluted_eps_ttm),
        "F05 EPS Growth %": _nfrow(d.f05.eps_growth_pct),
        "F05 Weighted EPS Growth %": _nfrow(d.f05.weighted_eps_growth_pct),
        "F05 Trajectory Label": d.f05.trajectory_label or "",
        "F05 Turnaround Flag": d.f05.turnaround_flag,
        "F05 Score": _nfrow(d.f05.score),
        "F05 Available": _nfrow(d.f05.available),
        "F05 Reason": d.f05.reason or "",
        "F05 Source": d.f05.source or "",
        "F05 Source Period": d.f05.source_period or "",
        "F05 Source Date": d.f05.source_date or "",
    }


# ---------------------------------------------------------------------------
# Management & Culture (M01–M03)
# ---------------------------------------------------------------------------


def _m01_sheet_row(d) -> dict:
    return {
        "M01 Current CEO Name": d.m01.ceo_name or "",
        "M01 CEO Appointment Date": d.m01.ceo_appointment_date or "",
        "M01 CEO Appointment Year": _nfrow(d.m01.ceo_appointment_year),
        "M01 CEO Date Precision": d.m01.ceo_date_precision or "",
        "M01 CEO Tenure Years": _nfrow(d.m01.ceo_tenure_years),
        "M01 Founder Flag": d.m01.founder_flag,
        "M01 Cofounder Flag": d.m01.cofounder_flag,
        "M01 Founding Family Flag": d.m01.founding_family_flag,
        "M01 Interim CEO Flag": d.m01.interim_ceo_flag,
        "M01 External Hire Flag": d.m01.external_hire_flag,
        "M01 Evidence Text": d.m01.evidence_text or "",
        "M01 Primary Source Type": d.m01.primary_source_type or "",
        "M01 Primary Source Filing Date": d.m01.primary_source_filing_date or "",
        "M01 Primary Source Accession": d.m01.primary_source_accession or "",
        "M01 Primary Source URL": d.m01.primary_source_url or "",
        "M01 Conflict Flag": d.m01.conflict_flag,
        "M01 Extraction Confidence": d.m01.extraction_confidence or "",
        "M01 Score": _nfrow(d.m01.score),
        "M01 Available": _nfrow(d.m01.available),
        "M01 Reason": d.m01.reason or "",
        "M01 Last Updated": d.m01.last_updated or "",
    }


def _m02_sheet_row(d) -> dict:
    return {
        "M02 CEO Beneficial Shares": _nfrow(d.m02.ceo_beneficial_shares),
        "M02 Basic Shares Outstanding": _nfrow(d.m02.basic_shares_outstanding),
        "M02 CEO Ownership %": _nfrow(d.m02.ceo_ownership_pct),
        "M02 Current Share Price": _nfrow(d.m02.current_share_price),
        "M02 CEO Stake Value USD": _nfrow(d.m02.ceo_stake_value_usd),
        "M02 Directors And Officers Group %": _nfrow(d.m02.directors_officers_group_pct),
        "M02 Ownership Basis": d.m02.ownership_basis or "",
        "M02 Score": _nfrow(d.m02.score),
        "M02 Available": _nfrow(d.m02.available),
        "M02 Reason": d.m02.reason or "",
        "M02 Source Type": d.m02.source or "",
        "M02 Source Date": d.m02.source_date or "",
        "M02 Source URL": "",
        "M02 Extraction Confidence": d.m02.extraction_confidence or "",
    }


def _m03_sheet_row(d) -> dict:
    return {
        "M03 Mission Text": d.m03.mission_text or "",
        "M03 Source Type": d.m03.source or "",
        "M03 Source URL": d.m03.source_url or "",
        "M03 Source Date": d.m03.source_date or "",
        "M03 Extraction Phrase": d.m03.extraction_phrase or "",
        "M03 Extraction Confidence": d.m03.extraction_confidence or "",
        "M03 Word Count": _nfrow(d.m03.word_count),
        "M03 Sentence Count": _nfrow(d.m03.sentence_count),
        "M03 Structural Punctuation Count": _nfrow(d.m03.structural_punctuation_count),
        "M03 Parenthetical Count": _nfrow(d.m03.parenthetical_count),
        "M03 Action Verb Found": d.m03.action_verb_found,
        "M03 Object Or Offering Found": d.m03.object_or_offering_found,
        "M03 Beneficiary Found": d.m03.beneficiary_found,
        "M03 Outcome Found": d.m03.outcome_found,
        "M03 Undefined Acronym Count": _nfrow(d.m03.undefined_acronym_count),
        "M03 Vague Term Count": _nfrow(d.m03.vague_term_count),
        "M03 Financial Only Flag": d.m03.financial_only_flag,
        "M03 Simple Point": _nfrow(d.m03.simple_point),
        "M03 Clear Point": _nfrow(d.m03.clear_point),
        "M03 Inspirational Point": _nfrow(d.m03.inspirational_point),
        "M03 Score": _nfrow(d.m03.score),
        "M03 Available": _nfrow(d.m03.available),
        "M03 Reason": d.m03.reason or "",
        "M03 Last Updated": "",
    }


# ---------------------------------------------------------------------------
# Stock (S01–S03)
# ---------------------------------------------------------------------------


def _s01_sheet_row(d) -> dict:
    return {
        "S01 Measurement Start Date": d.s01.measurement_start_date or "",
        "S01 Measurement End Date": d.s01.measurement_end_date or "",
        "S01 Stock Start Adjusted Price": _nfrow(d.s01.stock_start_adjusted_price),
        "S01 Stock End Adjusted Price": _nfrow(d.s01.stock_end_adjusted_price),
        "S01 SPY Start Adjusted Price": _nfrow(d.s01.spy_start_adjusted_price),
        "S01 SPY End Adjusted Price": _nfrow(d.s01.spy_end_adjusted_price),
        "S01 Stock Total Return %": _nfrow(d.s01.stock_total_return_pct),
        "S01 SPY Total Return %": _nfrow(d.s01.spy_total_return_pct),
        "S01 Excess Return Percentage Points": _nfrow(d.s01.excess_return_points),
        "S01 Trading Days": _nfrow(d.s01.trading_days),
        "S01 Short Listing Flag": d.s01.short_listing_flag,
        "S01 Score": _nfrow(d.s01.score),
        "S01 Available": _nfrow(d.s01.available),
        "S01 Reason": d.s01.reason or "",
        "S01 Source": d.s01.source or "",
    }


def _s02_sheet_row(d) -> dict:
    return {
        "S02 Share Repurchases TTM": _nfrow(d.s02.share_repurchases_ttm),
        "S02 Share Issuance TTM": _nfrow(d.s02.share_issuance_ttm),
        "S02 Net Repurchases TTM": _nfrow(d.s02.net_repurchases_ttm),
        "S02 Market Capitalisation": _nfrow(d.s02.market_capitalisation),
        "S02 Net Repurchases To Market Cap %": _nfrow(d.s02.net_repurchases_to_mc_pct),
        "S02 Diluted Shares Current": _nfrow(d.s02.diluted_shares_current),
        "S02 Diluted Shares Prior": _nfrow(d.s02.diluted_shares_prior),
        "S02 Diluted Share Change %": _nfrow(d.s02.diluted_share_change_pct),
        "S02 Dividend Per Share TTM": _nfrow(d.s02.dividend_per_share_ttm),
        "S02 Dividend Per Share Prior": _nfrow(d.s02.dividend_per_share_prior),
        "S02 Dividend Growth %": _nfrow(d.s02.dividend_growth_pct),
        "S02 Dividend Cut Flag": d.s02.dividend_cut_flag,
        "S02 Dividend Data Valid Flag": d.s02.dividend_data_valid_flag,
        "S02 Total Debt Current": _nfrow(d.s02.total_debt_current),
        "S02 Total Debt Prior": _nfrow(d.s02.total_debt_prior),
        "S02 Debt Change %": _nfrow(d.s02.debt_change_pct),
        "S02 Total Assets": _nfrow(d.s02.total_assets),
        "S02 Effectively Debt Free Flag": d.s02.effectively_debt_free_flag,
        "S02 Buyback Point": _nfrow(d.s02.buyback_point),
        "S02 Buyback Available": _nfrow(d.s02.buyback_available),
        "S02 Dividend Point": _nfrow(d.s02.dividend_point),
        "S02 Dividend Available": _nfrow(d.s02.dividend_available),
        "S02 Debt Reduction Point": _nfrow(d.s02.debt_reduction_point),
        "S02 Debt Reduction Available": _nfrow(d.s02.debt_reduction_available),
        "S02 Score": _nfrow(d.s02.score),
        "S02 Available": _nfrow(d.s02.available),
        "S02 Reason": d.s02.reason or "",
        "S02 Source": d.s02.source or "",
        "S02 Source Date": d.s02.source_date or "",
    }


def _s03_sheet_row(d) -> dict:
    return {
        "S03 Q1 Fiscal Period": d.s03.q1_fiscal_period or "",
        "S03 Q1 Report Date": d.s03.q1_report_date or "",
        "S03 Q1 Reported EPS": _nfrow(d.s03.q1_reported_eps),
        "S03 Q1 Estimated EPS": _nfrow(d.s03.q1_estimated_eps),
        "S03 Q1 Absolute Surprise": _nfrow(d.s03.q1_absolute_surprise),
        "S03 Q1 Surprise %": _nfrow(d.s03.q1_surprise_pct),
        "S03 Q1 Point": _nfrow(d.s03.q1_point),
        "S03 Q2 Fiscal Period": d.s03.q2_fiscal_period or "",
        "S03 Q2 Report Date": d.s03.q2_report_date or "",
        "S03 Q2 Reported EPS": _nfrow(d.s03.q2_reported_eps),
        "S03 Q2 Estimated EPS": _nfrow(d.s03.q2_estimated_eps),
        "S03 Q2 Absolute Surprise": _nfrow(d.s03.q2_absolute_surprise),
        "S03 Q2 Surprise %": _nfrow(d.s03.q2_surprise_pct),
        "S03 Q2 Point": _nfrow(d.s03.q2_point),
        "S03 Q3 Fiscal Period": d.s03.q3_fiscal_period or "",
        "S03 Q3 Report Date": d.s03.q3_report_date or "",
        "S03 Q3 Reported EPS": _nfrow(d.s03.q3_reported_eps),
        "S03 Q3 Estimated EPS": _nfrow(d.s03.q3_estimated_eps),
        "S03 Q3 Absolute Surprise": _nfrow(d.s03.q3_absolute_surprise),
        "S03 Q3 Surprise %": _nfrow(d.s03.q3_surprise_pct),
        "S03 Q3 Point": _nfrow(d.s03.q3_point),
        "S03 Q4 Fiscal Period": d.s03.q4_fiscal_period or "",
        "S03 Q4 Report Date": d.s03.q4_report_date or "",
        "S03 Q4 Reported EPS": _nfrow(d.s03.q4_reported_eps),
        "S03 Q4 Estimated EPS": _nfrow(d.s03.q4_estimated_eps),
        "S03 Q4 Absolute Surprise": _nfrow(d.s03.q4_absolute_surprise),
        "S03 Q4 Surprise %": _nfrow(d.s03.q4_surprise_pct),
        "S03 Q4 Point": _nfrow(d.s03.q4_point),
        "S03 Score": _nfrow(d.s03.score),
        "S03 Available": _nfrow(d.s03.available),
        "S03 Reason": d.s03.reason or "",
        "S03 Source": d.s03.source or "",
        "S03 Source Date": d.s03.source_date or "",
    }
