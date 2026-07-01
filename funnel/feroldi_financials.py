from __future__ import annotations

"""F01–F05 deterministic scoring functions.

Every score is derived from raw numeric inputs using explicit formulas.
No LLM, no qualitative inference, no paid API.
"""

from funnel.feroldi_models import (
    F01CashToDebtResult,
    F02GrossMarginResult,
    F03ROEResult,
    F04FCFResult,
    F05EPSResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value) -> float | None:
    """Convert to float, returning None for invalid/missing values."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _growth_pct(current: float | None, prior: float | None) -> float | None:
    """Percentage growth: (current / prior) - 1.  Returns None if inputs invalid."""
    if current is None or prior is None:
        return None
    if prior == 0:
        return None  # Cannot calculate growth from zero
    return (current / prior) - 1.0


# ---------------------------------------------------------------------------
# F01 — Cash to long-term-debt resilience (max 5)
# ---------------------------------------------------------------------------


def score_f01(
    *,
    cash: float | None = None,
    long_term_debt: float | None = None,
    source: str = "",
    source_period: str = "",
    source_date: str = "",
) -> F01CashToDebtResult:
    result = F01CashToDebtResult(
        cash_and_equivalents=cash,
        long_term_debt=long_term_debt,
        source=source,
        source_period=source_period,
        source_date=source_date,
    )

    # Both must be present
    if cash is None or long_term_debt is None:
        result.reason = f"Missing data: cash={'present' if cash is not None else 'missing'}, debt={'present' if long_term_debt is not None else 'missing'}"
        return result

    # Negative LT debt is invalid
    if long_term_debt < 0:
        result.reason = f"Invalid negative long-term debt: {long_term_debt}"
        return result

    # Zero LT debt with valid data = no debt flag
    if long_term_debt == 0:
        result.no_long_term_debt_flag = True
        result.cash_to_lt_debt_ratio = None
        result.score = 5.0
        result.available = 5.0
        result.reason = "No long-term debt; maximum resilience score"
        return result

    # Calculate ratio
    result.cash_to_lt_debt_ratio = cash / long_term_debt
    ratio = result.cash_to_lt_debt_ratio
    result.available = 5.0

    if ratio <= 0:
        result.score = 0.0
        result.reason = f"Ratio {ratio:.2f} <= 0"
    elif ratio < 1:
        result.score = 1.0
        result.reason = f"Ratio {ratio:.2f} is > 0 and < 1"
    elif ratio < 2:
        result.score = 3.0
        result.reason = f"Ratio {ratio:.2f} is >= 1 and < 2"
    else:
        result.score = 5.0
        result.reason = f"Ratio {ratio:.2f} >= 2"

    return result


# ---------------------------------------------------------------------------
# F02 — Gross margin (max 3)
# ---------------------------------------------------------------------------


def score_f02(
    *,
    revenue_ttm: float | None = None,
    cost_of_revenue_ttm: float | None = None,
    gross_profit_ttm: float | None = None,
    source: str = "",
    source_period: str = "",
    source_date: str = "",
) -> F02GrossMarginResult:
    result = F02GrossMarginResult(
        revenue_ttm=revenue_ttm,
        cost_of_revenue_ttm=cost_of_revenue_ttm,
        gross_profit_ttm=gross_profit_ttm,
        source=source,
        source_period=source_period,
        source_date=source_date,
    )

    # Derive gross profit if not provided
    if result.gross_profit_ttm is None and result.revenue_ttm is not None and result.cost_of_revenue_ttm is not None:
        result.gross_profit_ttm = result.revenue_ttm - result.cost_of_revenue_ttm

    if result.revenue_ttm is None or result.gross_profit_ttm is None:
        result.reason = "Missing revenue or gross profit data"
        return result

    if result.revenue_ttm <= 0:
        result.reason = f"Non-positive revenue: {result.revenue_ttm}"
        return result

    result.gross_margin_pct = result.gross_profit_ttm / result.revenue_ttm
    margin = result.gross_margin_pct
    result.available = 3.0

    if margin < 0.50:
        result.score = 0.0
        result.reason = f"Gross margin {margin * 100:.1f}% below 50%"
    elif margin < 0.65:
        result.score = 1.0
        result.reason = f"Gross margin {margin * 100:.1f}% in 50–65% range"
    elif margin < 0.80:
        result.score = 2.0
        result.reason = f"Gross margin {margin * 100:.1f}% in 65–80% range"
    else:
        result.score = 3.0
        result.reason = f"Gross margin {margin * 100:.1f}% at or above 80%"

    return result


# ---------------------------------------------------------------------------
# F03 — Positive and growing ROE (max 3)
# ---------------------------------------------------------------------------


def score_f03(
    *,
    current_net_income: float | None = None,
    current_opening_equity: float | None = None,
    current_closing_equity: float | None = None,
    prior_net_income: float | None = None,
    prior_opening_equity: float | None = None,
    prior_closing_equity: float | None = None,
    two_year_net_income: float | None = None,
    two_year_opening_equity: float | None = None,
    two_year_closing_equity: float | None = None,
    source: str = "",
    source_period: str = "",
    source_date: str = "",
) -> F03ROEResult:
    result = F03ROEResult(
        current_net_income_ttm=current_net_income,
        current_opening_equity=current_opening_equity,
        current_closing_equity=current_closing_equity,
        prior_net_income_ttm=prior_net_income,
        prior_opening_equity=prior_opening_equity,
        prior_closing_equity=prior_closing_equity,
        source=source,
        source_period=source_period,
        source_date=source_date,
    )

    # Compute current average equity
    if result.current_opening_equity is not None and result.current_closing_equity is not None:
        result.current_average_equity = (result.current_opening_equity + result.current_closing_equity) / 2.0
        if result.current_average_equity > 0:
            result.valid_equity_flag = True

    # Current ROE
    current_roe: float | None = None
    if result.current_net_income_ttm is not None and result.current_average_equity is not None and result.current_average_equity > 0:
        current_roe = result.current_net_income_ttm / result.current_average_equity
        result.current_roe_pct = current_roe

    if current_roe is None or result.current_average_equity is None or result.current_average_equity <= 0:
        result.reason = "Cannot compute current ROE (missing or non-positive equity)"
        return result

    # Prior ROE
    prior_roe: float | None = None
    if result.prior_opening_equity is not None and result.prior_closing_equity is not None:
        result.prior_average_equity = (result.prior_opening_equity + result.prior_closing_equity) / 2.0
    if result.prior_net_income_ttm is not None and result.prior_average_equity is not None and result.prior_average_equity > 0:
        prior_roe = result.prior_net_income_ttm / result.prior_average_equity
        result.prior_roe_pct = prior_roe

    # Turnaround: prior ROE <= 0, current ROE positive
    if prior_roe is not None and prior_roe <= 0 and current_roe > 0:
        result.turnaround_flag = True
        result.score = 2.0
        result.available = 3.0
        result.reason = f"Turnaround: prior ROE {prior_roe * 100:.1f}% <= 0 to current {current_roe * 100:.1f}% > 0"
        return result

    # Current-only: no prior data
    if prior_roe is None:
        if current_roe > 0:
            result.score = 1.0
            result.available = 1.0
            result.reason = f"Current ROE {current_roe * 100:.1f}% positive; prior unavailable"
        else:
            result.score = 0.0
            result.available = 1.0
            result.reason = f"Current ROE {current_roe * 100:.1f}% <= 0; prior unavailable"
        return result

    # Compute 2-year-ago ROE for trajectory analysis
    two_year_roe: float | None = None
    if two_year_net_income is not None and two_year_opening_equity is not None and two_year_closing_equity is not None:
        two_year_avg_equity = (two_year_opening_equity + two_year_closing_equity) / 2.0
        if two_year_avg_equity > 0:
            two_year_roe = two_year_net_income / two_year_avg_equity
            result.two_year_roe_pct = two_year_roe

    # Full comparison with trajectory
    result.available = 3.0
    result.roe_growth_pct = _growth_pct(current_roe, prior_roe)

    # Weighted growth: 60% recent YoY + 40% prior YoY (smooths volatility)
    weighted_growth = result.roe_growth_pct
    trajectory = ""
    if two_year_roe is not None and prior_roe is not None and prior_roe > 0:
        prior_yoy = (prior_roe / two_year_roe) - 1.0
        if result.roe_growth_pct is not None and prior_yoy is not None:
            weighted_growth = 0.60 * result.roe_growth_pct + 0.40 * prior_yoy
            result.weighted_roe_growth_pct = weighted_growth
            # Trajectory classification
            if result.roe_growth_pct > 0 and prior_yoy > 0:
                if result.roe_growth_pct > prior_yoy * 1.2:
                    trajectory = "accelerating"
                elif abs(result.roe_growth_pct - prior_yoy) < 0.03:
                    trajectory = "stable"
                elif result.roe_growth_pct < prior_yoy * 0.8:
                    trajectory = "decelerating"
                else:
                    trajectory = "moderate"
            elif result.roe_growth_pct > 0 and prior_yoy <= 0:
                trajectory = "recovering"
            elif result.roe_growth_pct <= 0 and prior_yoy > 0:
                trajectory = "declining"
    result.trajectory_label = trajectory

    if current_roe <= 0:
        result.score = 0.0
        result.reason = f"Current ROE {current_roe * 100:.1f}% <= 0"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth is None or weighted_growth <= 0:
        result.score = 1.0
        result.reason = f"ROE positive but growth {weighted_growth or 'N/A'} <= 0%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth < 0.15:
        result.score = 2.0
        result.reason = f"ROE growth {weighted_growth * 100:.1f}% > 0% and < 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    else:
        result.score = 3.0
        result.reason = f"ROE growth {weighted_growth * 100:.1f}% >= 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"

    return result


# ---------------------------------------------------------------------------
# F04 — Positive and growing free cash flow (max 3)
# ---------------------------------------------------------------------------


def score_f04(
    *,
    current_operating_cf: float | None = None,
    current_capex: float | None = None,
    prior_operating_cf: float | None = None,
    prior_capex: float | None = None,
    two_year_operating_cf: float | None = None,
    two_year_capex: float | None = None,
    source: str = "",
    source_period: str = "",
    source_date: str = "",
) -> F04FCFResult:
    result = F04FCFResult(
        current_operating_cf_ttm=current_operating_cf,
        current_capex_ttm=current_capex,
        prior_operating_cf_ttm=prior_operating_cf,
        prior_capex_ttm=prior_capex,
        source=source,
        source_period=source_period,
        source_date=source_date,
    )

    # CapEx is an absolute outflow — use absolute value
    current_capex_abs: float | None = None
    if result.current_capex_ttm is not None:
        current_capex_abs = abs(result.current_capex_ttm)

    # Current FCF
    current_fcf: float | None = None
    if result.current_operating_cf_ttm is not None and current_capex_abs is not None:
        current_fcf = result.current_operating_cf_ttm - current_capex_abs
        result.current_fcf_ttm = current_fcf

    if current_fcf is None:
        result.reason = "Missing operating cash flow or capital expenditure"
        return result

    # Prior FCF
    prior_capex_abs: float | None = None
    if result.prior_capex_ttm is not None:
        prior_capex_abs = abs(result.prior_capex_ttm)

    prior_fcf: float | None = None
    if result.prior_operating_cf_ttm is not None and prior_capex_abs is not None:
        prior_fcf = result.prior_operating_cf_ttm - prior_capex_abs
        result.prior_fcf_ttm = prior_fcf

    # Turnaround: prior FCF <= 0, current FCF > 0
    if prior_fcf is not None and prior_fcf <= 0 and current_fcf > 0:
        result.turnaround_flag = True
        result.score = 2.0
        result.available = 3.0
        result.reason = f"Turnaround: prior FCF {prior_fcf:,.0f} <= 0 to current {current_fcf:,.0f} > 0"
        return result

    # Current-only
    if prior_fcf is None:
        if current_fcf > 0:
            result.score = 1.0
            result.available = 1.0
            result.reason = f"Current FCF {current_fcf:,.0f} positive; prior unavailable"
        else:
            result.score = 0.0
            result.available = 1.0
            result.reason = f"Current FCF {current_fcf:,.0f} <= 0; prior unavailable"
        return result

    # Compute 2-year-ago FCF for trajectory analysis
    two_year_fcf: float | None = None
    two_year_capex_abs: float | None = None
    if two_year_capex is not None:
        two_year_capex_abs = abs(two_year_capex)
    if two_year_operating_cf is not None and two_year_capex_abs is not None:
        two_year_fcf = two_year_operating_cf - two_year_capex_abs
        result.two_year_fcf_ttm = two_year_fcf

    # Full comparison
    result.available = 3.0
    result.fcf_growth_pct = _growth_pct(current_fcf, prior_fcf)

    # Weighted growth: 60% recent YoY + 40% prior YoY (smooths volatility)
    weighted_growth = result.fcf_growth_pct
    trajectory = ""
    if two_year_fcf is not None and prior_fcf is not None and prior_fcf > 0:
        prior_yoy = (prior_fcf / two_year_fcf) - 1.0
        recent_yoy = result.fcf_growth_pct
        if recent_yoy is not None and prior_yoy is not None:
            weighted_growth = 0.60 * recent_yoy + 0.40 * prior_yoy
            result.weighted_fcf_growth_pct = weighted_growth
            # Trajectory classification
            if recent_yoy > 0 and prior_yoy > 0:
                if recent_yoy > prior_yoy * 1.2:
                    trajectory = "accelerating"
                elif abs(recent_yoy - prior_yoy) < 0.03:
                    trajectory = "stable"
                elif recent_yoy < prior_yoy * 0.8:
                    trajectory = "decelerating"
                else:
                    trajectory = "moderate"
            elif recent_yoy > 0 and prior_yoy <= 0:
                trajectory = "recovering"
            elif recent_yoy <= 0 and prior_yoy > 0:
                trajectory = "declining"
    result.trajectory_label = trajectory

    if current_fcf <= 0:
        result.score = 0.0
        result.reason = f"Current FCF {current_fcf:,.0f} <= 0"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth is None or weighted_growth <= 0:
        result.score = 1.0
        result.reason = f"FCF positive but growth {weighted_growth or 'N/A'} <= 0%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth < 0.15:
        result.score = 2.0
        result.reason = f"FCF growth {weighted_growth * 100:.1f}% > 0% and < 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    else:
        result.score = 3.0
        result.reason = f"FCF growth {weighted_growth * 100:.1f}% >= 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"

    return result


# ---------------------------------------------------------------------------
# F05 — Positive and growing diluted EPS (max 3)
# ---------------------------------------------------------------------------


def score_f05(
    *,
    current_diluted_eps: float | None = None,
    prior_diluted_eps: float | None = None,
    two_year_diluted_eps: float | None = None,
    source: str = "",
    source_period: str = "",
    source_date: str = "",
) -> F05EPSResult:
    result = F05EPSResult(
        current_diluted_eps_ttm=current_diluted_eps,
        prior_diluted_eps_ttm=prior_diluted_eps,
        source=source,
        source_period=source_period,
        source_date=source_date,
    )

    if result.current_diluted_eps_ttm is None:
        result.reason = "Missing current diluted EPS"
        return result

    # Turnaround
    if result.prior_diluted_eps_ttm is not None and result.prior_diluted_eps_ttm <= 0 and result.current_diluted_eps_ttm > 0:
        result.turnaround_flag = True
        result.score = 2.0
        result.available = 3.0
        result.reason = f"Turnaround: prior EPS {result.prior_diluted_eps_ttm:.2f} <= 0 to current {result.current_diluted_eps_ttm:.2f} > 0"
        return result

    # Current-only
    if result.prior_diluted_eps_ttm is None:
        if result.current_diluted_eps_ttm > 0:
            result.score = 1.0
            result.available = 1.0
            result.reason = f"Current EPS {result.current_diluted_eps_ttm:.2f} positive; prior unavailable"
        else:
            result.score = 0.0
            result.available = 1.0
            result.reason = f"Current EPS {result.current_diluted_eps_ttm:.2f} <= 0; prior unavailable"
        return result

    # Full comparison with trajectory
    result.available = 3.0
    result.eps_growth_pct = _growth_pct(result.current_diluted_eps_ttm, result.prior_diluted_eps_ttm)

    # Weighted growth: 60% recent YoY + 40% prior YoY (smooths volatility)
    weighted_growth = result.eps_growth_pct
    trajectory = ""
    if two_year_diluted_eps is not None and result.prior_diluted_eps_ttm is not None and result.prior_diluted_eps_ttm > 0:
        prior_yoy = (result.prior_diluted_eps_ttm / two_year_diluted_eps) - 1.0
        recent_yoy = result.eps_growth_pct
        if recent_yoy is not None and prior_yoy is not None:
            weighted_growth = 0.60 * recent_yoy + 0.40 * prior_yoy
            result.weighted_eps_growth_pct = weighted_growth
            # Trajectory classification
            if recent_yoy > 0 and prior_yoy > 0:
                if recent_yoy > prior_yoy * 1.2:
                    trajectory = "accelerating"
                elif abs(recent_yoy - prior_yoy) < 0.03:
                    trajectory = "stable"
                elif recent_yoy < prior_yoy * 0.8:
                    trajectory = "decelerating"
                else:
                    trajectory = "moderate"
            elif recent_yoy > 0 and prior_yoy <= 0:
                trajectory = "recovering"
            elif recent_yoy <= 0 and prior_yoy > 0:
                trajectory = "declining"
    result.trajectory_label = trajectory

    if result.current_diluted_eps_ttm <= 0:
        result.score = 0.0
        result.reason = f"Current EPS {result.current_diluted_eps_ttm:.2f} <= 0"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth is None or weighted_growth <= 0:
        result.score = 1.0
        result.reason = f"EPS positive but growth {weighted_growth or 'N/A'} <= 0%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    elif weighted_growth < 0.15:
        result.score = 2.0
        result.reason = f"EPS growth {weighted_growth * 100:.1f}% > 0% and < 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"
    else:
        result.score = 3.0
        result.reason = f"EPS growth {weighted_growth * 100:.1f}% >= 15%"
        if trajectory:
            result.reason += f" [{trajectory}]"

    return result
