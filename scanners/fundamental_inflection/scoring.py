from __future__ import annotations

import math
from typing import Any

from scanners.fundamental_inflection.models import (
    BalanceSheetMetrics,
    CashFlowMetrics,
    FundamentalInflectionConfig,
    GrossEconomicsMetrics,
    InflectionResult,
    OperatingLeverageMetrics,
    PerShareMetrics,
    QuarterlySnapshot,
    RevenueGrowthMetrics,
    WorkingCapitalMetrics,
)


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def evaluate_revenue_growth(
    quarters: list[QuarterlySnapshot],
    config: FundamentalInflectionConfig,
) -> RevenueGrowthMetrics:
    usable = [q for q in quarters if q.revenue is not None]
    if len(usable) < 5:
        return RevenueGrowthMetrics(
            latest_revenue=0.0,
            revenue_four_quarters_ago=0.0,
            yoy_growth=0.0,
            prior_quarter_growth=None,
            growth_acceleration=None,
            growth_consistency="insufficient_data",
            quarters_above_20pct=0,
            trend="insufficient_data",
        )

    latest = usable[-1]
    four_back = usable[-5]
    yoy_growth = (latest.revenue - four_back.revenue) / four_back.revenue if four_back.revenue and four_back.revenue != 0 else 0.0

    prior_growth = None
    if len(usable) >= 6:
        prior_latest = usable[-2]
        prior_four_back = usable[-6]
        if prior_four_back.revenue and prior_four_back.revenue != 0:
            prior_growth = (prior_latest.revenue - prior_four_back.revenue) / prior_four_back.revenue

    acceleration = None
    if prior_growth is not None:
        acceleration = yoy_growth - prior_growth

    above_20 = sum(
        1 for i in range(max(0, len(usable) - 1), max(0, len(usable) - 9), -1)
        if i >= 4 and usable[i].revenue and usable[i - 4].revenue and usable[i - 4].revenue != 0
        and (usable[i].revenue - usable[i - 4].revenue) / usable[i - 4].revenue >= 0.20
    )

    if yoy_growth >= 0.30 and acceleration is not None and acceleration >= 0.05:
        trend = "strong_growth_accelerating"
    elif yoy_growth >= 0.20 and acceleration is not None and acceleration >= -0.03:
        trend = "stable_growth"
    elif yoy_growth >= 0.20:
        trend = "growth_mildly_decelerating"
    elif yoy_growth >= 0.10:
        trend = "moderate_growth"
    else:
        trend = "low_growth"

    growth_consistency = "consistent" if above_20 >= 2 else "sporadic"

    return RevenueGrowthMetrics(
        latest_revenue=latest.revenue,
        revenue_four_quarters_ago=four_back.revenue,
        yoy_growth=yoy_growth,
        prior_quarter_growth=prior_growth,
        growth_acceleration=acceleration,
        growth_consistency=growth_consistency,
        quarters_above_20pct=above_20,
        trend=trend,
    )


def evaluate_gross_economics(
    quarters: list[QuarterlySnapshot],
    revenue_metrics: RevenueGrowthMetrics,
) -> GrossEconomicsMetrics:
    usable = [q for q in quarters if q.revenue is not None and q.gross_profit is not None]
    if len(usable) < 5:
        return GrossEconomicsMetrics(
            gross_profit_growth=None,
            gross_margin_latest=None,
            gross_margin_prior=None,
            gross_margin_change_bps=None,
            gross_confirmation="UNAVAILABLE",
            flags=[],
        )

    latest = usable[-1]
    four_back = usable[-5]
    gp_growth = (latest.gross_profit - four_back.gross_profit) / four_back.gross_profit if four_back.gross_profit and four_back.gross_profit != 0 else None

    gm_latest = latest.gross_profit / latest.revenue if latest.revenue else None
    gm_prior = four_back.gross_profit / four_back.revenue if four_back.revenue else None
    gm_change = (gm_latest - gm_prior) * 10000 if gm_latest is not None and gm_prior is not None else None

    flags: list[str] = []
    confirmation = "NEUTRAL"

    if gm_change is not None and gm_change >= 100:
        confirmation = "POSITIVE"
    elif gp_growth is not None and revenue_metrics.yoy_growth is not None and gp_growth > revenue_metrics.yoy_growth:
        confirmation = "POSITIVE"
    elif gp_growth is not None and gp_growth >= 0.15 and gm_latest is not None and gm_prior is not None and gm_change is not None and gm_change >= -50:
        confirmation = "POSITIVE"

    if gm_change is not None and gm_change <= -300:
        flags.append("severe_margin_deterioration")
        confirmation = "NEGATIVE"

    return GrossEconomicsMetrics(
        gross_profit_growth=gp_growth,
        gross_margin_latest=gm_latest,
        gross_margin_prior=gm_prior,
        gross_margin_change_bps=gm_change,
        gross_confirmation=confirmation,
        flags=flags,
    )


def evaluate_operating_leverage(
    quarters: list[QuarterlySnapshot],
    revenue_metrics: RevenueGrowthMetrics,
) -> OperatingLeverageMetrics:
    usable = [q for q in quarters if q.revenue is not None and q.operating_income is not None]
    if len(usable) < 5:
        return OperatingLeverageMetrics(
            operating_margin_latest=None,
            operating_margin_prior=None,
            operating_margin_change_bps=None,
            incremental_operating_margin=None,
            operating_loss_narrowing=False,
            operating_confirmation="UNAVAILABLE",
            flags=[],
        )

    latest = usable[-1]
    four_back = usable[-5]

    om_latest = latest.operating_income / latest.revenue if latest.revenue else None
    om_prior = four_back.operating_income / four_back.revenue if four_back.revenue else None
    om_change = (om_latest - om_prior) * 10000 if om_latest is not None and om_prior is not None else None

    rev_delta = latest.revenue - four_back.revenue
    oi_delta = latest.operating_income - four_back.operating_income
    incremental_om = oi_delta / rev_delta if rev_delta and rev_delta != 0 else None

    flags: list[str] = []
    confirmation = "NEUTRAL"
    loss_narrowing = False

    if om_prior is not None and om_prior < 0 and om_latest is not None and om_latest > om_prior:
        loss_narrowing = True
        if om_latest < 0:
            confirmation = "WEAK_POSITIVE"
        else:
            confirmation = "POSITIVE"

    if om_change is not None and om_change >= 200:
        if not loss_narrowing:
            confirmation = "POSITIVE"
    elif incremental_om is not None and incremental_om > 0.10:
        if not loss_narrowing:
            confirmation = "POSITIVE"

    if om_change is not None and om_change <= -200:
        flags.append("operating_margin_deterioration")

    return OperatingLeverageMetrics(
        operating_margin_latest=om_latest,
        operating_margin_prior=om_prior,
        operating_margin_change_bps=om_change,
        incremental_operating_margin=incremental_om,
        operating_loss_narrowing=loss_narrowing,
        operating_confirmation=confirmation,
        flags=flags,
    )


def evaluate_cash_flow(
    quarters: list[QuarterlySnapshot],
) -> CashFlowMetrics:
    ttm_ocf, ttm_capex, prior_ttm_ocf, prior_ttm_capex = None, None, None, None
    rev_ttm, prior_rev = None, None

    usable = [q for q in quarters if q.revenue is not None]
    if len(usable) >= 4:
        latest4 = usable[-4:]
        prior4 = usable[-8:-4] if len(usable) >= 8 else []
        rev_ttm = sum(q.revenue for q in latest4 if q.revenue)
        ocf_vals = [q.operating_cash_flow for q in latest4 if q.operating_cash_flow is not None]
        capex_vals = [q.capital_expenditure for q in latest4 if q.capital_expenditure is not None]
        if len(ocf_vals) == 4:
            ttm_ocf = sum(ocf_vals)
        if len(capex_vals) == 4:
            ttm_capex = sum(capex_vals)

        if prior4:
            prior_rev = sum(q.revenue for q in prior4 if q.revenue)
            prior_ocf = [q.operating_cash_flow for q in prior4 if q.operating_cash_flow is not None]
            prior_cx = [q.capital_expenditure for q in prior4 if q.capital_expenditure is not None]
            if len(prior_ocf) == 4:
                prior_ttm_ocf = sum(prior_ocf)
            if len(prior_cx) == 4:
                prior_ttm_capex = sum(prior_cx)

    ttm_fcf = (ttm_ocf + ttm_capex) if ttm_ocf is not None and ttm_capex is not None else None
    ttm_fcf_margin = ttm_fcf / rev_ttm if ttm_fcf is not None and rev_ttm and rev_ttm != 0 else None

    prior_fcf = (prior_ttm_ocf + prior_ttm_capex) if prior_ttm_ocf is not None and prior_ttm_capex is not None else None
    prior_margin = prior_fcf / prior_rev if prior_fcf is not None and prior_rev and prior_rev != 0 else None
    margin_change = (ttm_fcf_margin - prior_margin) * 10000 if ttm_fcf_margin is not None and prior_margin is not None else None

    fcf_class = "FCF_UNAVAILABLE"
    if ttm_fcf is not None and ttm_fcf > 0:
        if margin_change is not None and margin_change > 0:
            fcf_class = "FCF_POSITIVE_AND_EXPANDING"
        else:
            fcf_class = "FCF_POSITIVE_AND_EXPANDING"
    elif ttm_fcf is not None:
        if prior_fcf is not None and ttm_fcf > prior_fcf:
            fcf_class = "FCF_IMPROVING_BUT_NEGATIVE"
        else:
            fcf_class = "FCF_DETERIORATING"

    flags: list[str] = []
    confirmation = "NEUTRAL"
    if fcf_class in ("FCF_POSITIVE_AND_EXPANDING",):
        confirmation = "POSITIVE"
    elif fcf_class in ("FCF_IMPROVING_BUT_NEGATIVE",):
        confirmation = "WEAK_POSITIVE"

    return CashFlowMetrics(
        ttm_operating_cash_flow=ttm_ocf,
        ttm_capital_expenditure=ttm_capex,
        ttm_free_cash_flow=ttm_fcf,
        ttm_fcf_margin=ttm_fcf_margin,
        prior_ttm_fcf_margin=prior_margin,
        ttm_fcf_margin_change_bps=margin_change,
        fcf_classification=fcf_class,
        cash_confirmation=confirmation,
        flags=flags,
    )


def evaluate_per_share(
    quarters: list[QuarterlySnapshot],
    revenue_metrics: RevenueGrowthMetrics,
) -> PerShareMetrics:
    usable = [q for q in quarters if q.revenue is not None and q.diluted_shares is not None]
    if len(usable) < 5:
        return PerShareMetrics(
            diluted_share_growth=None,
            revenue_per_share_latest=None,
            revenue_per_share_prior=None,
            revenue_per_share_growth=None,
            sbc_to_revenue=None,
            dilution_classification="UNAVAILABLE",
            per_share_confirmation="UNAVAILABLE",
            flags=[],
        )

    latest = usable[-1]
    four_back = usable[-5]

    share_growth = (latest.diluted_shares - four_back.diluted_shares) / four_back.diluted_shares if four_back.diluted_shares and four_back.diluted_shares != 0 else None

    rps_latest = latest.revenue / latest.diluted_shares if latest.diluted_shares else None
    rps_prior = four_back.revenue / four_back.diluted_shares if four_back.diluted_shares else None
    rps_growth = (rps_latest - rps_prior) / rps_prior if rps_latest is not None and rps_prior is not None and rps_prior != 0 else None

    sbc_to_rev = None
    if latest.stock_based_comp is not None and latest.revenue:
        sbc_to_rev = latest.stock_based_comp / latest.revenue

    flags: list[str] = []
    dil_class = "HEALTHY"
    conf = "NEUTRAL"

    if share_growth is not None:
        if share_growth <= 0.03:
            dil_class = "HEALTHY"
        elif share_growth <= 0.07:
            dil_class = "MONITOR"
            flags.append("elevated_dilution")
        elif share_growth <= 0.12:
            dil_class = "HIGH_DILUTION"
            flags.append("high_dilution")
        else:
            dil_class = "SEVERE_DILUTION"
            flags.append("severe_dilution")

    if rps_growth is not None and rps_growth > 0:
        conf = "POSITIVE"

    return PerShareMetrics(
        diluted_share_growth=share_growth,
        revenue_per_share_latest=rps_latest,
        revenue_per_share_prior=rps_prior,
        revenue_per_share_growth=rps_growth,
        sbc_to_revenue=sbc_to_rev,
        dilution_classification=dil_class,
        per_share_confirmation=conf,
        flags=flags,
    )


def evaluate_balance_sheet(
    quarters: list[QuarterlySnapshot],
    cash_flow: CashFlowMetrics,
    config: FundamentalInflectionConfig,
) -> BalanceSheetMetrics:
    latest = quarters[-1] if quarters else None
    if latest is None or latest.cash is None:
        return BalanceSheetMetrics(
            cash=None, total_debt=None, net_cash=None,
            cash_runway_months=None,
            balance_sheet_classification="UNAVAILABLE",
            flags=[],
        )

    net_cash = latest.cash - (latest.total_debt or 0)

    flags: list[str] = []
    bs_class = "STRONG"
    runway = None

    if cash_flow.ttm_free_cash_flow is not None and cash_flow.ttm_free_cash_flow < 0:
        annual_burn = abs(cash_flow.ttm_free_cash_flow)
        if annual_burn > 0:
            runway = latest.cash / annual_burn
    elif cash_flow.ttm_free_cash_flow is not None and cash_flow.ttm_free_cash_flow > 0:
        bs_class = "STRONG"

    if runway is not None:
        if runway > 36:
            bs_class = "STRONG"
        elif runway > config.cash_runway_risk_months:
            bs_class = "ACCEPTABLE"
        elif runway > config.cash_runway_severe_months:
            bs_class = "RISK"
            flags.append("cash_runway_risk")
        else:
            bs_class = "SEVERE"
            flags.append("severe_cash_runway")

    if net_cash is not None and net_cash < 0 and bs_class == "SEVERE":
        flags.append("negative_net_cash")

    return BalanceSheetMetrics(
        cash=latest.cash,
        total_debt=latest.total_debt,
        net_cash=net_cash,
        cash_runway_months=runway,
        balance_sheet_classification=bs_class,
        flags=flags,
    )


def evaluate_working_capital(
    quarters: list[QuarterlySnapshot],
    revenue_metrics: RevenueGrowthMetrics,
) -> WorkingCapitalMetrics:
    usable = [q for q in quarters if q.revenue is not None]
    if len(usable) < 5:
        return WorkingCapitalMetrics(
            ar_growth=None, inventory_growth=None,
            revenue_growth=revenue_metrics.yoy_growth,
            ar_divergence=None, inventory_divergence=None,
            flags=[],
        )

    latest = usable[-1]
    four_back = usable[-5]

    flags: list[str] = []
    ar_growth = None
    inv_growth = None
    ar_div = None
    inv_div = None

    if latest.accounts_receivable is not None and four_back.accounts_receivable is not None and four_back.accounts_receivable != 0:
        ar_growth = (latest.accounts_receivable - four_back.accounts_receivable) / four_back.accounts_receivable
        ar_div = ar_growth - revenue_metrics.yoy_growth
        if ar_div > 0.25:
            flags.append("RECEIVABLES_DIVERGENCE")

    if latest.inventory is not None and four_back.inventory is not None and four_back.inventory != 0:
        inv_growth = (latest.inventory - four_back.inventory) / four_back.inventory
        inv_div = inv_growth - revenue_metrics.yoy_growth
        if inv_div > 0.25:
            flags.append("INVENTORY_DIVERGENCE")

    if ar_div is not None and ar_div > 0.15 and inv_div is not None and inv_div > 0.15:
        flags.append("AGGRESSIVE_WORKING_CAPITAL")

    return WorkingCapitalMetrics(
        ar_growth=ar_growth,
        inventory_growth=inv_growth,
        revenue_growth=revenue_metrics.yoy_growth,
        ar_divergence=ar_div,
        inventory_divergence=inv_div,
        flags=flags,
    )


def score_and_classify(
    revenue: RevenueGrowthMetrics,
    gross: GrossEconomicsMetrics,
    operating: OperatingLeverageMetrics,
    cash_flow: CashFlowMetrics,
    per_share: PerShareMetrics,
    balance: BalanceSheetMetrics,
    working_cap: WorkingCapitalMetrics,
    config: FundamentalInflectionConfig,
) -> tuple[str, float, list[str], bool, dict[str, Any]]:
    growth_positive = revenue.yoy_growth >= config.min_revenue_growth
    if not growth_positive:
        return "REJECTED", 0.0, ["revenue_growth_below_20pct"], False, {"reason": f"Revenue growth {revenue.yoy_growth:.1%} below {config.min_revenue_growth:.0%}"}

    gross_positive = gross.gross_confirmation == "POSITIVE"
    operating_positive = operating.operating_confirmation in ("POSITIVE", "WEAK_POSITIVE")
    cash_positive = cash_flow.cash_confirmation in ("POSITIVE", "WEAK_POSITIVE")
    per_share_pos = per_share.per_share_confirmation == "POSITIVE"
    kpi_positive = False

    economic_confirmation = gross_positive or operating_positive or cash_positive

    growth_score = 0.0
    if revenue.yoy_growth >= 0.50:
        growth_score = 25.0
    elif revenue.yoy_growth >= 0.35:
        growth_score = 20.0
    elif revenue.yoy_growth >= 0.25:
        growth_score = 17.0
    elif revenue.yoy_growth >= 0.20:
        growth_score = 14.0
    else:
        growth_score = 10.0

    if revenue.growth_acceleration is not None:
        if revenue.growth_acceleration >= 0.10:
            growth_score += 3.0
        elif revenue.growth_acceleration >= 0.03:
            growth_score += 1.0
        elif revenue.growth_acceleration <= -0.10:
            growth_score -= 3.0
        elif revenue.growth_acceleration <= -0.03:
            growth_score -= 1.0

    if revenue.growth_consistency == "consistent":
        growth_score += 2.0
    growth_score = max(0.0, min(25.0, growth_score))

    gross_score = 0.0
    if gross.gross_confirmation == "POSITIVE":
        gross_score = 15.0
        if gross.gross_margin_change_bps is not None and gross.gross_margin_change_bps >= 200:
            gross_score += 3.0
        elif gross.gross_margin_change_bps is not None and gross.gross_margin_change_bps >= 100:
            gross_score += 1.0
    elif gross.gross_confirmation == "NEUTRAL":
        gross_score = 7.0
    elif gross.gross_confirmation == "NEGATIVE":
        gross_score = 3.0
    gross_score = max(0.0, min(20.0, gross_score))

    operating_score = 0.0
    if operating.operating_confirmation == "POSITIVE":
        operating_score = 15.0
        if operating.operating_margin_change_bps is not None and operating.operating_margin_change_bps >= 400:
            operating_score += 3.0
        elif operating.operating_margin_change_bps is not None and operating.operating_margin_change_bps >= 200:
            operating_score += 1.0
    elif operating.operating_confirmation == "WEAK_POSITIVE":
        operating_score = 10.0
    elif operating.operating_confirmation == "NEUTRAL":
        operating_score = 5.0
    operating_score = max(0.0, min(20.0, operating_score))

    cash_score = 0.0
    if cash_flow.fcf_classification in ("FCF_POSITIVE_AND_EXPANDING",):
        cash_score = 12.0
        if cash_flow.ttm_fcf_margin_change_bps is not None and cash_flow.ttm_fcf_margin_change_bps >= 200:
            cash_score += 2.0
    elif cash_flow.fcf_classification in ("FCF_IMPROVING_BUT_NEGATIVE",):
        cash_score = 7.0
    elif cash_flow.fcf_classification in ("FCF_DETERIORATING",):
        cash_score = 3.0
    cash_score = max(0.0, min(15.0, cash_score))

    per_share_score = 0.0
    if per_share.dilution_classification == "HEALTHY":
        per_share_score = 8.0
    elif per_share.dilution_classification == "MONITOR":
        per_share_score = 5.0
    elif per_share.dilution_classification == "HIGH_DILUTION":
        per_share_score = 2.0
    elif per_share.dilution_classification == "SEVERE_DILUTION":
        per_share_score = 0.0
    if per_share.revenue_per_share_growth is not None and per_share.revenue_per_share_growth > revenue.yoy_growth:
        per_share_score += 1.0
    per_share_score = max(0.0, min(10.0, per_share_score))

    balance_score = 0.0
    if balance.balance_sheet_classification == "STRONG":
        balance_score = 10.0
    elif balance.balance_sheet_classification == "ACCEPTABLE":
        balance_score = 6.0
    elif balance.balance_sheet_classification == "RISK":
        balance_score = 3.0
    else:
        balance_score = 1.0
    balance_score = max(0.0, min(10.0, balance_score))

    total_score = round(growth_score + gross_score + operating_score + cash_score + per_share_score + balance_score, 2)

    positive_pillars: list[str] = []
    if growth_positive:
        positive_pillars.append("growth")
    if gross_positive:
        positive_pillars.append("gross_economics")
    if operating_positive:
        positive_pillars.append("operating_leverage")
    if cash_positive:
        positive_pillars.append("cash_flow")
    if per_share_pos:
        positive_pillars.append("per_share")
    if kpi_positive:
        positive_pillars.append("business_kpi")

    pillar_count = len(positive_pillars)
    all_flags = (
        gross.flags + operating.flags + cash_flow.flags +
        per_share.flags + balance.flags + working_cap.flags
    )

    severe_dilution = per_share.dilution_classification == "SEVERE_DILUTION"
    severe_bs = balance.balance_sheet_classification == "SEVERE"

    classification = "REJECTED"
    reason = ""

    if not economic_confirmation:
        classification = "GROWTH_WITHOUT_INFLECTION"
        reason = f"Revenue growth {revenue.yoy_growth:.0%} but no economic confirmation"
    elif severe_bs and config.severe_balance_sheet_veto:
        classification = "REJECTED"
        reason = f"Severe balance sheet failure; {balance.flags}"
    elif pillar_count >= 3 and economic_confirmation and not severe_dilution and not severe_bs:
        if total_score >= config.strong_inflection_threshold:
            classification = "STRONG_INFLECTION"
            reason = f"Strong inflection: {pillar_count} pillars, score {total_score:.0f}"
        else:
            classification = "VALIDATED_INFLECTION"
            reason = f"Validated inflection: {pillar_count} pillars, score {total_score:.0f}"
    elif pillar_count >= 2 and economic_confirmation and not severe_dilution and not severe_bs:
        classification = "VALIDATED_INFLECTION"
        reason = f"Validated inflection: {pillar_count} pillars, score {total_score:.0f}"
    elif pillar_count >= 1 and economic_confirmation and not severe_bs:
        classification = "EARLY_INFLECTION"
        reason = f"Early inflection: {pillar_count} pillar, score {total_score:.0f}"
    else:
        classification = "GROWTH_WITHOUT_INFLECTION"
        reason = f"Growth {revenue.yoy_growth:.0%} without sufficient confirmation"

    if severe_dilution and classification in ("STRONG_INFLECTION", "VALIDATED_INFLECTION"):
        classification = "EARLY_INFLECTION"
        reason = f"{reason} | severe dilution"
        all_flags.append("severe_dilution_veto")

    details = {
        "growth_score": growth_score,
        "gross_score": gross_score,
        "operating_score": operating_score,
        "cash_score": cash_score,
        "per_share_score": per_share_score,
        "balance_score": balance_score,
    }

    return classification, total_score, all_flags, economic_confirmation, {
        **details, "reason": reason,
    }


def build_result(
    ticker: str,
    classification: str,
    total_score: float,
    revenue: RevenueGrowthMetrics,
    gross: GrossEconomicsMetrics,
    operating: OperatingLeverageMetrics,
    cash_flow: CashFlowMetrics,
    per_share: PerShareMetrics,
    balance: BalanceSheetMetrics,
    working_cap: WorkingCapitalMetrics,
    positive_pillars: list[str],
    economic_confirmation: bool,
    risk_flags: list[str],
    accession: str,
    filing_date: str | None,
    data_confidence: str,
    config: FundamentalInflectionConfig,
) -> InflectionResult:
    return InflectionResult(
        ticker=ticker,
        classification=classification,
        total_score=total_score,
        latest_filing_accession=accession,
        filing_date=None,
        latest_quarterly_revenue=revenue.latest_revenue,
        revenue_growth_yoy=revenue.yoy_growth,
        prior_quarter_growth=revenue.prior_quarter_growth,
        growth_acceleration=revenue.growth_acceleration,
        gross_profit_growth=gross.gross_profit_growth,
        gross_margin_change_bps=gross.gross_margin_change_bps,
        operating_margin_change_bps=operating.operating_margin_change_bps,
        incremental_operating_margin=operating.incremental_operating_margin,
        ttm_fcf_margin=cash_flow.ttm_fcf_margin,
        ttm_fcf_margin_change_bps=cash_flow.ttm_fcf_margin_change_bps,
        diluted_share_growth=per_share.diluted_share_growth,
        revenue_per_share_growth=per_share.revenue_per_share_growth,
        cash=balance.cash,
        debt=balance.total_debt,
        cash_runway_months=balance.cash_runway_months,
        positive_pillars=positive_pillars,
        pilllar_count=len(positive_pillars),
        economic_confirmation=economic_confirmation,
        risk_flags=risk_flags,
        data_confidence=data_confidence,
        valid_for_days=config.valid_days,
        reason="",
        details={},
    )
