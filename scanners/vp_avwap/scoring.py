from __future__ import annotations

import math

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.models import ROUTE_PRIORITY, STATUS_PRIORITY, RouteEvaluation


def _structure_points(profile_state_code: int | None, slope_pct: float | None) -> float:
    profile_points = {
        3: 20.0,
        2: 16.0,
        1: 8.0,
        0: 0.0,
        None: 0.0,
    }[profile_state_code]
    if slope_pct is None:
        slope_points = 0.0
    elif slope_pct > 0.25:
        slope_points = 5.0
    elif slope_pct >= -0.25:
        slope_points = 3.0
    else:
        slope_points = 0.0
    return profile_points + slope_points


def _confluence_points(route: RouteEvaluation) -> float:
    if route.route_code == "POC_AVWAP_RECOVERY" and bool(route.metadata.get("confluence")):
        return 25.0
    if len(route.level_basis) >= 2:
        return 20.0
    if route.route_code == "BREAKOUT_RETEST":
        return 12.0
    if route.level_basis:
        return 14.0
    return 0.0


def _readiness_points(status: str) -> float:
    return {
        "CONFIRMED": 20.0,
        "TESTING": 15.0,
        "APPROACHING": 12.0,
        "WAITING": 6.0,
        "EXTENDED": 2.0,
        "FAILED": 0.0,
        "INVALID": 0.0,
        "DATA_UNAVAILABLE": 0.0,
    }.get(status, 0.0)


def _price_points(route: RouteEvaluation, latest_close: float | None) -> float:
    if latest_close is None or route.zone_low is None or route.zone_high is None:
        return 0.0
    if latest_close < route.zone_low and route.status != "CONFIRMED":
        return 0.0
    if route.status == "CONFIRMED" or route.zone_low <= latest_close <= route.zone_high:
        return 15.0
    pct_above = ((latest_close / route.zone_high) - 1.0) * 100.0 if route.zone_high > 0 else math.inf
    if pct_above <= 2.0:
        return 13.0
    if pct_above <= 5.0:
        return 10.0
    if pct_above <= 8.0:
        return 6.0
    if pct_above <= 12.0:
        return 3.0
    return 0.0


def _risk_points(risk_pct: float | None) -> float:
    if risk_pct is None:
        return 0.0
    if risk_pct <= 3.0:
        return 15.0
    if risk_pct <= 5.0:
        return 12.0
    if risk_pct <= 8.0:
        return 8.0
    if risk_pct <= 12.0:
        return 4.0
    return 0.0


def score_routes(
    routes: list[RouteEvaluation],
    *,
    profile_state_code: int | None,
    avwap_slope_pct: float | None,
    latest_close: float | None,
) -> list[RouteEvaluation]:
    scored: list[RouteEvaluation] = []
    for route in routes:
        if not route.eligible:
            route.route_score = 0.0
            scored.append(route)
            continue
        route.structure_points = _structure_points(profile_state_code, avwap_slope_pct)
        route.confluence_points = _confluence_points(route)
        route.readiness_points = _readiness_points(route.status)
        route.price_points = _price_points(route, latest_close)
        route.risk_points = _risk_points(route.risk_pct)
        route.route_score = max(0.0, min(100.0, route.structure_points + route.confluence_points + route.readiness_points + route.price_points + route.risk_points))
        scored.append(route)
    return scored


def choose_preferred_route(routes: list[RouteEvaluation]) -> RouteEvaluation:
    eligible = [route for route in routes if route.eligible]
    if not eligible:
        return max(routes, key=lambda route: (-route.route_score, -STATUS_PRIORITY.get(route.status, 99), -ROUTE_PRIORITY.get(route.route_code, 99)))
    return max(
        eligible,
        key=lambda route: (
            route.route_score,
            -ROUTE_PRIORITY.get(route.route_code, 99),
        ),
    )


def raw_score_tier(score: float) -> int:
    if score >= 75.0:
        return 1
    if score >= 55.0:
        return 2
    if score >= 35.0:
        return 3
    return 4


def apply_tier_overrides(
    *,
    preferred_route: RouteEvaluation,
    routes: list[RouteEvaluation],
    raw_tier: int,
    data_quality: str,
    latest_close: float | None,
    val: float | None,
    avwap: float | None,
    poc: float | None,
    avwap_slope_pct: float | None,
    status: str,
    missing_anchor: bool,
    config: VpAvwapConfig,
) -> tuple[int, bool, str]:
    final_tier = raw_tier
    if missing_anchor:
        return 4, True, "Missing usable earnings anchor."
    if status == "DATA_UNAVAILABLE":
        return 4, True, "Ticker data is unavailable."
    if data_quality == "UNAVAILABLE":
        return 4, True, "Market data quality is unavailable."
    val_reclaim = next((route for route in routes if route.route_code == "VAL_RECLAIM"), None)
    if latest_close is not None and val is not None and latest_close < val and (val_reclaim is None or val_reclaim.status != "CONFIRMED"):
        return 4, True, "Latest close is below VAL without a confirmed VAL reclaim."
    if preferred_route.status == "FAILED":
        return 4, True, "Preferred route has failed its invalidation level."
    if data_quality == "LOW":
        final_tier = max(final_tier, 2)
    if avwap_slope_pct is not None and avwap_slope_pct <= config.falling_override_pct and latest_close is not None and avwap is not None and poc is not None and latest_close < avwap and latest_close < poc:
        final_tier = max(final_tier, 3)
    nearest_zone_distance = min(
        (
            route.distance_to_zone_pct
            for route in routes
            if route.eligible and route.distance_to_zone_pct is not None
        ),
        default=None,
    )
    breakout_confirmed = any(route.route_code == "BREAKOUT_RETEST" and route.status == "CONFIRMED" for route in routes)
    if nearest_zone_distance is not None and nearest_zone_distance > config.extension_pct and not breakout_confirmed:
        final_tier = max(final_tier, 3)
    if final_tier == 1 and preferred_route.status not in {"CONFIRMED", "TESTING", "APPROACHING"}:
        final_tier = 2
        return final_tier, True, "Technical Tier 1 requires CONFIRMED, TESTING, or APPROACHING status."
    return final_tier, final_tier != raw_tier, "" if final_tier == raw_tier else "Hard tier override applied."
