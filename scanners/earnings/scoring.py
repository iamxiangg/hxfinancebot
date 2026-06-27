from __future__ import annotations

import math
from typing import Any

from scanners.earnings.models import EarningsOpportunity, HistoricalMoveSummary


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def calculate_richness_metrics(
    implied_move_pct: float | None,
    summary: HistoricalMoveSummary,
    realised_move_percentile: float | None,
) -> dict[str, float | None]:
    return {
        "move_richness_median": _safe_ratio(implied_move_pct, summary.median_absolute_move),
        "move_richness_mean": _safe_ratio(implied_move_pct, summary.mean_absolute_move),
        "p75_coverage": _safe_ratio(implied_move_pct, summary.p75_move),
        "p90_coverage": _safe_ratio(implied_move_pct, summary.p90_move),
        "realised_move_percentile": realised_move_percentile,
    }


def event_richness_score(metrics: dict[str, float | None]) -> float:
    ratio = metrics.get("move_richness_median")
    if ratio is None:
        return 0.0
    if ratio < 1.10:
        score = 0.0
    elif ratio < 1.20:
        score = 8.0
    elif ratio < 1.35:
        score = 18.0
    elif ratio < 1.50:
        score = 28.0
    else:
        score = 35.0
    if (metrics.get("p75_coverage") or 0.0) >= 1.0 and (metrics.get("realised_move_percentile") or 0.0) >= 80.0:
        score += 5.0
    return min(40.0, score)


def historical_reliability_score(
    summary: HistoricalMoveSummary,
    *,
    implied_move_pct: float | None,
    breach_rate: float | None,
) -> float:
    score = 0.0
    if summary.usable_event_count >= 12:
        score += 7.0
    elif summary.usable_event_count >= 8:
        score += 4.0

    if breach_rate is not None:
        if breach_rate <= 0.10:
            score += 8.0
        elif breach_rate <= 0.20:
            score += 6.0
        elif breach_rate <= 0.33:
            score += 4.0
        elif breach_rate <= 0.50:
            score += 2.0

    if implied_move_pct is not None and summary.p75_move is not None and implied_move_pct >= summary.p75_move:
        score += 5.0
    if implied_move_pct is not None and summary.p90_move is not None:
        if implied_move_pct >= summary.p90_move:
            score += 3.0
        elif implied_move_pct >= summary.p90_move * 0.9:
            score += 1.0

    if summary.mean_absolute_move and summary.standard_deviation is not None:
        stability = summary.standard_deviation / summary.mean_absolute_move
        if stability <= 0.5:
            score += 2.0
    return min(25.0, score)


def execution_quality_score(*, liquidity_status: str, event_purity: str, structure_valid: bool) -> float:
    score = 0.0
    if liquidity_status == "GOOD":
        score += 10.0
    elif liquidity_status == "ACCEPTABLE":
        score += 7.0
    if event_purity == "HIGH":
        score += 5.0
    elif event_purity == "MEDIUM":
        score += 3.0
    if structure_valid:
        score += 5.0
    return min(20.0, score)


def build_risk_flags(
    summary: HistoricalMoveSummary,
    *,
    implied_move_pct: float | None,
    earnings_timing: str,
    event_purity: str,
    sector: str,
    industry: str,
    info: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    if summary.usable_event_count < 8:
        flags.append("INSUFFICIENT_HISTORY")
    if implied_move_pct is not None and summary.p75_move is not None and implied_move_pct < summary.p75_move:
        flags.append("CURRENT_IMPLIED_BELOW_P75")
    if implied_move_pct is not None and summary.p90_move is not None and implied_move_pct < summary.p90_move:
        flags.append("CURRENT_IMPLIED_BELOW_P90")
    if implied_move_pct is not None and summary.maximum_move is not None:
        if summary.maximum_move / implied_move_pct > 1.5:
            flags.append("FAT_TAIL_HISTORY")
    if summary.mean_absolute_move and summary.standard_deviation is not None:
        if summary.standard_deviation / summary.mean_absolute_move > 0.8:
            flags.append("UNSTABLE_EVENT_DISTRIBUTION")
    if earnings_timing == "UNKNOWN":
        flags.append("UNKNOWN_TIMING")
    if event_purity == "LOW":
        flags.append("LOW_EVENT_PURITY")

    sector_text = f"{sector} {industry}".lower()
    if "biotech" in sector_text and float(info.get("totalRevenue") or 0.0) <= 0:
        flags.append("PRE_REVENUE_BIOTECH")
    float_shares = info.get("floatShares")
    try:
        if float_shares is not None and float(float_shares) < 50_000_000:
            flags.append("VERY_SMALL_FLOAT")
    except (TypeError, ValueError):
        pass
    short_interest = info.get("shortPercentOfFloat")
    try:
        if short_interest is not None and float(short_interest) >= 0.20:
            flags.append("EXTREME_SHORT_INTEREST")
    except (TypeError, ValueError):
        pass
    return sorted(set(flags))


def risk_adjustment_score(
    flags: list[str],
    *,
    event_purity: str,
    data_confidence: str,
) -> float:
    adjustment = 0.0
    if event_purity == "LOW":
        adjustment -= 6.0
    elif event_purity == "MEDIUM":
        adjustment -= 2.0

    penalties = {
        "FAT_TAIL_HISTORY": -4.0,
        "UNSTABLE_EVENT_DISTRIBUTION": -4.0,
        "UNKNOWN_TIMING": -6.0,
        "PRE_REVENUE_BIOTECH": -4.0,
        "VERY_SMALL_FLOAT": -3.0,
        "EXTREME_SHORT_INTEREST": -2.0,
        "CURRENT_IMPLIED_BELOW_P75": -3.0,
    }
    for flag in flags:
        adjustment += penalties.get(flag, 0.0)

    if data_confidence == "LOW":
        adjustment -= 3.0
    elif data_confidence == "UNKNOWN":
        adjustment -= 2.0

    if "FAT_TAIL_HISTORY" not in flags and "UNSTABLE_EVENT_DISTRIBUTION" not in flags and event_purity in {"HIGH", "MEDIUM"}:
        adjustment += 2.0
    return max(-15.0, min(15.0, adjustment))


def classify_opportunity(
    opportunity: EarningsOpportunity,
    *,
    entry_session_is_today: bool,
    structure_valid: bool,
) -> str:
    if opportunity.earnings_timing == "UNKNOWN":
        return "MANUAL_CONFIRMATION_REQUIRED"

    if not structure_valid or opportunity.historical_event_count < 8:
        return "REJECTED"

    if opportunity.liquidity_status == "POOR":
        return "REJECTED"

    if not entry_session_is_today:
        return "WATCH" if opportunity.total_score >= 50.0 else "REJECTED"

    strong = (
        opportunity.total_score >= 80.0
        and (opportunity.move_richness_median or 0.0) >= 1.40
        and opportunity.historical_p90_move is not None
        and opportunity.implied_move_pct is not None
        and opportunity.implied_move_pct >= opportunity.historical_p90_move
        and opportunity.liquidity_status == "GOOD"
        and opportunity.event_purity in {"HIGH", "MEDIUM"}
        and (opportunity.historical_breach_rate or 1.0) <= 0.20
    )
    if strong:
        return "STRONG_ACTIONABLE"

    actionable = (
        opportunity.total_score >= 70.0
        and opportunity.richness_score >= 25.0
        and opportunity.reliability_score >= 15.0
        and opportunity.execution_score >= 12.0
        and opportunity.historical_event_count >= 8
        and (opportunity.move_richness_median or 0.0) >= 1.25
        and opportunity.implied_move_pct is not None
        and opportunity.historical_p75_move is not None
        and opportunity.implied_move_pct >= opportunity.historical_p75_move
        and opportunity.event_purity in {"HIGH", "MEDIUM"}
        and opportunity.liquidity_status in {"GOOD", "ACCEPTABLE"}
    )
    if actionable:
        return "ACTIONABLE"

    if opportunity.total_score >= 50.0:
        return "WATCH"
    return "REJECTED"
