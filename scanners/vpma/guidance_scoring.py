from __future__ import annotations

from scanners.vpma.guidance_models import EarningsFundamentalConfirmation


def score_economic_event(confirmation: EarningsFundamentalConfirmation) -> float:
    growth_score = _score_reported_growth(confirmation)
    guidance_score = _score_guidance(confirmation)
    kpi_score = _score_kpis(confirmation)
    total = round(growth_score + guidance_score + kpi_score, 2)
    return max(0.0, min(30.0, total))


def _score_reported_growth(confirmation: EarningsFundamentalConfirmation) -> float:
    points = 0.0

    rev_growth = confirmation.revenue_growth_yoy
    if rev_growth is not None:
        if rev_growth >= 30.0:
            points += 4.0
        elif rev_growth >= 15.0:
            points += 3.0
        elif rev_growth >= 5.0:
            points += 2.0
        elif rev_growth >= 0.0:
            points += 1.0

    gross_margin = confirmation.gross_margin_pct
    gross_change = confirmation.gross_margin_change_bps
    if gross_margin is not None:
        if gross_margin >= 70.0:
            points += 2.0
        elif gross_margin >= 50.0:
            points += 1.0
    if gross_change is not None:
        if gross_change >= 200.0:
            points += 2.0
        elif gross_change > 0.0:
            points += 1.0
        elif gross_change < -200.0:
            points -= 1.0

    op_margin = confirmation.operating_margin_pct
    if op_margin is not None:
        if op_margin >= 25.0:
            points += 2.0
        elif op_margin >= 10.0:
            points += 1.0

    if confirmation.free_cash_flow is not None and confirmation.reported_revenue is not None:
        if confirmation.reported_revenue > 0 and confirmation.free_cash_flow > 0:
            fcf_margin = confirmation.free_cash_flow / confirmation.reported_revenue
            if fcf_margin >= 0.20:
                points += 2.0
            elif fcf_margin >= 0.10:
                points += 1.0

    return max(0.0, min(10.0, points))


def _score_guidance(confirmation: EarningsFundamentalConfirmation) -> float:
    points = 0.0

    rev_action = confirmation.revenue_guidance_action
    rev_change = confirmation.revenue_guidance_change_pct

    if rev_action == "WITHDRAWN":
        return 0.0

    action_scores = {
        "RAISED": 6.0,
        "MODESTLY_RAISED": 4.0,
        "MAINTAINED": 3.0,
        "INITIATED": 2.0,
        "NOT_PROVIDED": 1.0,
        "UNAVAILABLE": 0.0,
        "MODESTLY_LOWERED": 1.0,
        "LOWERED": 0.0,
    }
    points += action_scores.get(rev_action, 0.0)

    if rev_change is not None:
        if rev_change >= 5.0:
            points += 4.0
        elif rev_change >= 2.0:
            points += 3.0
        elif rev_change >= 0.0:
            points += 1.0
        elif rev_change >= -2.0:
            points += 0.5
        else:
            points += 0.0

    margin_action = confirmation.margin_guidance_action
    margin_scores = {
        "RAISED": 3.0,
        "MAINTAINED": 2.0,
        "LOWERED": 0.0,
    }
    points += margin_scores.get(margin_action, 0.0)

    return max(0.0, min(12.0, points))


def _score_kpis(confirmation: EarningsFundamentalConfirmation) -> float:
    kpis = confirmation.business_kpis
    if not kpis:
        return 0.0
    points = min(8.0, len(kpis) * 1.5)
    return round(points, 2)


def classify_economic_event(score: float, confirmation: EarningsFundamentalConfirmation) -> str:
    if confirmation.source_accession is None and confirmation.conflict_flags:
        return "ECONOMIC_UNAVAILABLE"

    has_withdrawn = confirmation.revenue_guidance_action == "WITHDRAWN"
    has_material_cut = (
        confirmation.revenue_guidance_action in {"LOWERED", "MODESTLY_LOWERED"}
        and confirmation.revenue_guidance_change_pct is not None
        and confirmation.revenue_guidance_change_pct <= -2.0
    )
    rev_growth = confirmation.revenue_growth_yoy
    gross_change = confirmation.gross_margin_change_bps

    severe_operating_conflict = (
        rev_growth is not None
        and rev_growth < 0
        and gross_change is not None
        and gross_change < 0
    )

    if has_withdrawn or severe_operating_conflict:
        return "ECONOMIC_WEAK"
    if has_material_cut:
        return "ECONOMIC_WEAK"

    if score >= 21.0 and not has_material_cut and not severe_operating_conflict:
        return "ECONOMIC_STRONG"
    if score >= 12.0:
        return "ECONOMIC_MIXED"
    return "ECONOMIC_WEAK"


def determine_conflict_type(
    vpma_classification: str,
    economic_classification: str,
) -> str:
    if economic_classification == "ECONOMIC_UNAVAILABLE":
        return "FUNDAMENTALS_UNAVAILABLE"

    price_strong = vpma_classification in {"actionable", "wait"}
    econ_strong = economic_classification == "ECONOMIC_STRONG"
    econ_mixed = economic_classification == "ECONOMIC_MIXED"
    econ_weak = economic_classification == "ECONOMIC_WEAK"

    if price_strong and econ_strong:
        return "PRICE_STRONG_FUNDAMENTALS_STRONG"
    if price_strong and econ_mixed:
        return "PRICE_STRONG_FUNDAMENTALS_MIXED"
    if price_strong and econ_weak:
        return "PRICE_STRONG_FUNDAMENTALS_WEAK"
    if not price_strong and econ_strong:
        return "PRICE_WEAK_FUNDAMENTALS_STRONG"
    if not price_strong and econ_weak:
        return "PRICE_WEAK_FUNDAMENTALS_WEAK"
    if price_strong:
        return "PRICE_STRONG_FUNDAMENTALS_MIXED"
    return "PRICE_WEAK_FUNDAMENTALS_WEAK"


def apply_economic_overlay(
    classification: str,
    conflict_type: str,
    economic_classification: str,
    rev_guidance_action: str,
    reason: str,
) -> tuple[str, str, list[str]]:
    downgrade_flags: list[str] = []

    if conflict_type == "FUNDAMENTALS_UNAVAILABLE":
        return classification, reason, ["fundamentals_unavailable"]

    if rev_guidance_action == "WITHDRAWN":
        downgrade_flags.append("guidance_withdrawn")
        if classification == "actionable":
            return "wait", f"{reason} | guidance withdrawn", downgrade_flags
        return classification, reason, downgrade_flags

    if rev_guidance_action == "LOWERED":
        downgrade_flags.append("material_guidance_cut")
        if classification == "actionable":
            return "wait", f"{reason} | material revenue guidance cut", downgrade_flags
        if classification == "wait":
            return "risk", f"{reason} | guidance lowered", downgrade_flags
        return classification, reason, downgrade_flags

    if conflict_type == "PRICE_STRONG_FUNDAMENTALS_WEAK":
        downgrade_flags.append("weak_fundamentals")
        if classification == "actionable":
            return "wait", f"{reason} | price strong but fundamentals weak", downgrade_flags
        if classification == "wait":
            return "near_miss", f"{reason} | fundamentals weak", downgrade_flags
        return classification, reason, downgrade_flags

    if conflict_type == "PRICE_STRONG_FUNDAMENTALS_MIXED":
        if classification == "actionable":
            return "wait", f"{reason} | mixed fundamental confirmation", downgrade_flags
        return classification, reason, downgrade_flags

    if conflict_type == "PRICE_WEAK_FUNDAMENTALS_STRONG":
        if classification in {"actionable", "wait"}:
            return classification, reason, downgrade_flags
        return classification, reason, downgrade_flags

    return classification, reason, downgrade_flags
