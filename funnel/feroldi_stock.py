from __future__ import annotations

"""S01–S03 deterministic stock scoring functions."""

from funnel.feroldi_config import (
    BUYBACK_DILUTED_SHARE_DECLINE_MIN,
    BUYBACK_NET_REPURCHASES_TO_MC_MIN,
    DEBT_DECLINE_MIN,
    DEBT_TO_ASSETS_DEBT_FREE_MAX,
    DIVIDEND_GROWTH_MIN,
    SURPRISE_LARGE_BEAT_ABS,
    SURPRISE_LARGE_BEAT_PCT,
)
from funnel.feroldi_models import (
    S01PerformanceResult,
    S02ShareholderResult,
    S03EarningsSurpriseResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# S01 — Five-year performance vs SPY (max 4)
# ---------------------------------------------------------------------------


def score_s01(
    *,
    stock_start_price: float | None = None,
    stock_end_price: float | None = None,
    spy_start_price: float | None = None,
    spy_end_price: float | None = None,
    trading_days: int = 0,
    start_date: str = "",
    end_date: str = "",
    source: str = "",
) -> S01PerformanceResult:
    result = S01PerformanceResult(
        stock_start_adjusted_price=stock_start_price,
        stock_end_adjusted_price=stock_end_price,
        spy_start_adjusted_price=spy_start_price,
        spy_end_adjusted_price=spy_end_price,
        measurement_start_date=start_date,
        measurement_end_date=end_date,
        trading_days=trading_days,
        source=source,
    )

    # Require at least 252 trading days
    if trading_days < 252:
        if trading_days > 0:
            result.short_listing_flag = True
            result.reason = f"Insufficient trading days: {trading_days} (< 252)"
        else:
            result.reason = "No trading data available"
        return result

    # All four prices required
    if any(
        p is None
        for p in [stock_start_price, stock_end_price, spy_start_price, spy_end_price]
    ):
        result.reason = "Missing price data for comparison"
        return result

    # Calculate returns
    stock_return = stock_end_price / stock_start_price - 1.0
    spy_return = spy_end_price / spy_start_price - 1.0
    excess_points = (stock_return - spy_return) * 100.0

    result.stock_total_return_pct = stock_return * 100.0
    result.spy_total_return_pct = spy_return * 100.0
    result.excess_return_points = excess_points
    result.available = 4.0

    if excess_points <= 0:
        result.score = 0.0
        result.reason = f"Excess return {excess_points:.1f}pp <= 0"
    elif excess_points < 25:
        result.score = 1.0
        result.reason = f"Excess return {excess_points:.1f}pp > 0 and < 25"
    elif excess_points < 50:
        result.score = 2.0
        result.reason = f"Excess return {excess_points:.1f}pp >= 25 and < 50"
    elif excess_points < 100:
        result.score = 3.0
        result.reason = f"Excess return {excess_points:.1f}pp >= 50 and < 100"
    else:
        result.score = 4.0
        result.reason = f"Excess return {excess_points:.1f}pp >= 100"

    return result


# ---------------------------------------------------------------------------
# S02 — Shareholder-friendly actions (max 3)
# ---------------------------------------------------------------------------


def score_s02(
    *,
    # Buyback
    share_repurchases_ttm: float | None = None,
    share_issuance_ttm: float | None = None,
    market_cap: float | None = None,
    diluted_shares_current: float | None = None,
    diluted_shares_prior: float | None = None,
    # Dividend
    dividend_per_share_ttm: float | None = None,
    dividend_per_share_prior: float | None = None,
    dividend_cut_flag: bool = False,
    dividend_data_valid: bool = False,
    # Debt
    total_debt_current: float | None = None,
    total_debt_prior: float | None = None,
    total_assets: float | None = None,
    # Meta
    source: str = "",
    source_date: str = "",
) -> S02ShareholderResult:
    result = S02ShareholderResult(
        share_repurchases_ttm=share_repurchases_ttm,
        share_issuance_ttm=share_issuance_ttm,
        market_capitalisation=market_cap,
        diluted_shares_current=diluted_shares_current,
        diluted_shares_prior=diluted_shares_prior,
        dividend_per_share_ttm=dividend_per_share_ttm,
        dividend_per_share_prior=dividend_per_share_prior,
        dividend_cut_flag=dividend_cut_flag,
        dividend_data_valid_flag=dividend_data_valid,
        total_debt_current=total_debt_current,
        total_debt_prior=total_debt_prior,
        total_assets=total_assets,
        source=source,
        source_date=source_date,
    )

    # Track which subtests have data available
    buyback_data_available = False
    dividend_data_available = False
    debt_data_available = False
    
    # --- Buyback point ---
    buyback_scored = False
    # Method 1: Diluted share reduction
    if diluted_shares_current is not None and diluted_shares_prior is not None and diluted_shares_prior > 0:
        buyback_data_available = True
        result.diluted_share_change_pct = (diluted_shares_current - diluted_shares_prior) / diluted_shares_prior
        if result.diluted_share_change_pct <= -BUYBACK_DILUTED_SHARE_DECLINE_MIN:
            result.buyback_point = 1
            buyback_scored = True

    # Method 2: Net repurchases to market cap
    if not buyback_scored:
        if share_repurchases_ttm is not None or share_issuance_ttm is not None:
            buyback_data_available = True
            rep = share_repurchases_ttm or 0.0
            iss = share_issuance_ttm or 0.0
            result.net_repurchases_ttm = rep - iss
            if result.net_repurchases_ttm > 0 and market_cap is not None and market_cap > 0:
                result.net_repurchases_to_mc_pct = result.net_repurchases_ttm / market_cap
                if result.net_repurchases_to_mc_pct >= BUYBACK_NET_REPURCHASES_TO_MC_MIN:
                    result.buyback_point = 1

    # --- Dividend point ---
    if result.dividend_data_valid_flag:
        dividend_data_available = True
        if dividend_per_share_ttm is not None and dividend_per_share_prior is not None and dividend_per_share_prior > 0:
            result.dividend_growth_pct = (dividend_per_share_ttm - dividend_per_share_prior) / dividend_per_share_prior
            if result.dividend_growth_pct >= DIVIDEND_GROWTH_MIN and not result.dividend_cut_flag:
                result.dividend_point = 1
        elif dividend_per_share_ttm is not None and dividend_per_share_ttm > 0:
            # Prior unavailable — can't prove growth, no point
            pass
    else:
        # Non-dividend payer or data invalid — zero score, not unavailable
        result.dividend_point = 0

    # --- Debt reduction point ---
    if total_debt_current is not None and total_assets is not None and total_assets > 0:
        debt_data_available = True
        debt_to_assets = total_debt_current / total_assets
        if debt_to_assets <= DEBT_TO_ASSETS_DEBT_FREE_MAX:
            result.effectively_debt_free_flag = True
            result.debt_reduction_point = 1

    if not result.effectively_debt_free_flag and total_debt_current is not None and total_debt_prior is not None and total_debt_prior > 0:
        debt_data_available = True
        result.debt_change_pct = (total_debt_current - total_debt_prior) / total_debt_prior
        if result.debt_change_pct <= -DEBT_DECLINE_MIN:
            result.debt_reduction_point = 1

    # Set availability based on actual data presence
    result.buyback_available = 1 if buyback_data_available else 0
    result.dividend_available = 1 if dividend_data_available else 0
    result.debt_reduction_available = 1 if debt_data_available else 0
    
    # Aggregate
    result.score = float(result.buyback_point + result.dividend_point + result.debt_reduction_point)
    result.available = float(result.buyback_available + result.dividend_available + result.debt_reduction_available)

    reasons = []
    if result.buyback_point:
        reasons.append("buyback")
    if result.dividend_point:
        reasons.append("dividend growth")
    if result.debt_reduction_point:
        reasons.append("debt reduction")
    result.reason = f"Points earned: {' + '.join(reasons)}" if reasons else "No points earned"

    return result


# ---------------------------------------------------------------------------
# S03 — Earnings-expectation record (max 4)
# ---------------------------------------------------------------------------


def _quarter_point(reported: float | None, estimated: float | None) -> float:
    """Score a single quarter's earnings surprise."""
    if reported is None or estimated is None:
        return 0.0  # Not available for this quarter

    absolute_surprise = reported - estimated

    if estimated == 0:
        # Zero estimate case
        if absolute_surprise >= SURPRISE_LARGE_BEAT_ABS:
            return 1.0
        elif absolute_surprise > 0:
            return 0.5
        return 0.0

    surprise_pct = absolute_surprise / abs(estimated)

    if surprise_pct >= SURPRISE_LARGE_BEAT_PCT and absolute_surprise >= SURPRISE_LARGE_BEAT_ABS:
        return 1.0
    elif reported > estimated:
        return 0.5

    return 0.0


def score_s03(
    *,
    q1_reported: float | None = None,
    q1_estimated: float | None = None,
    q1_fiscal_period: str = "",
    q1_report_date: str = "",
    q2_reported: float | None = None,
    q2_estimated: float | None = None,
    q2_fiscal_period: str = "",
    q2_report_date: str = "",
    q3_reported: float | None = None,
    q3_estimated: float | None = None,
    q3_fiscal_period: str = "",
    q3_report_date: str = "",
    q4_reported: float | None = None,
    q4_estimated: float | None = None,
    q4_fiscal_period: str = "",
    q4_report_date: str = "",
    source: str = "",
    source_date: str = "",
) -> S03EarningsSurpriseResult:
    result = S03EarningsSurpriseResult(
        q1_fiscal_period=q1_fiscal_period,
        q1_report_date=q1_report_date,
        q1_reported_eps=q1_reported,
        q1_estimated_eps=q1_estimated,
        q2_fiscal_period=q2_fiscal_period,
        q2_report_date=q2_report_date,
        q2_reported_eps=q2_reported,
        q2_estimated_eps=q2_estimated,
        q3_fiscal_period=q3_fiscal_period,
        q3_report_date=q3_report_date,
        q3_reported_eps=q3_reported,
        q3_estimated_eps=q3_estimated,
        q4_fiscal_period=q4_fiscal_period,
        q4_report_date=q4_report_date,
        q4_reported_eps=q4_reported,
        q4_estimated_eps=q4_estimated,
        source=source,
        source_date=source_date,
    )

    quarters = [
        ("Q1", q1_reported, q1_estimated),
        ("Q2", q2_reported, q2_estimated),
        ("Q3", q3_reported, q3_estimated),
        ("Q4", q4_reported, q4_estimated),
    ]

    points = []
    for idx, (label, reported, estimated) in enumerate(quarters):
        pt = _quarter_point(reported, estimated)
        points.append(pt)
        # Store per-quarter results
        q_num = idx + 1
        if reported is not None and estimated is not None:
            abs_surprise = reported - estimated
            surprise_pct = abs_surprise / abs(estimated) if estimated != 0 else None
            setattr(result, f"q{q_num}_absolute_surprise", abs_surprise)
            setattr(result, f"q{q_num}_surprise_pct", surprise_pct)
        setattr(result, f"q{q_num}_point", pt)

    result.score = sum(points)

    # Availability: each quarter with valid reported+estimated = 1 available point
    valid_quarters = sum(1 for _, r, e in quarters if r is not None and e is not None)
    result.available = float(valid_quarters)
    result.reason = (
        f"{valid_quarters}/4 quarters available, "
        f"points: {', '.join(f'Q{i+1}={p}' for i, p in enumerate(points))}"
    )

    return result
