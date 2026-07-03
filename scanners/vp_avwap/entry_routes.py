from __future__ import annotations

import math
from typing import Any

import pandas as pd

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.models import LevelReference, RouteEvaluation


ROUTE_LABELS = {
    "VAH_DEFENDED_PULLBACK": "Best balance of price and trend",
    "POC_AVWAP_RECOVERY": "Best technical value",
    "BREAKOUT_RETEST": "Best confirmation, highest purchase price",
    "VAL_RECLAIM": "Lowest price, highest technical risk",
}


def profile_state(close: float | None, *, avwap: float | None, poc: float | None, vah: float | None, val: float | None) -> tuple[int | None, str, dict[str, float | None]]:
    metrics = {
        "close_vs_avwap_pct": pct_distance(close, avwap),
        "close_vs_poc_pct": pct_distance(close, poc),
        "close_vs_vah_pct": pct_distance(close, vah),
        "close_vs_val_pct": pct_distance(close, val),
    }
    if close is None or vah is None or val is None or poc is None:
        return None, "DATA_UNAVAILABLE", metrics
    if close > vah:
        return 3, "ABOVE_VAH", metrics
    if poc < close <= vah:
        return 2, "UPPER_VALUE_AREA", metrics
    if val <= close <= poc:
        return 1, "LOWER_VALUE_AREA", metrics
    return 0, "BELOW_VAL", metrics


def pct_distance(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    if not all(math.isfinite(value) for value in (float(numerator), float(denominator))):
        return None
    return ((float(numerator) / float(denominator)) - 1.0) * 100.0


def _is_in_zone(low: float, high: float, *, zone_low: float, zone_high: float) -> bool:
    return low <= zone_high and high >= zone_low


def _distance_to_zone(close: float, zone_low: float, zone_high: float) -> float:
    if zone_low <= close <= zone_high:
        return 0.0
    if close > zone_high:
        return ((close / zone_high) - 1.0) * 100.0
    return ((zone_low / close) - 1.0) * 100.0 if close > 0 else math.inf


def _risk_pct(entry_trigger: float | None, invalidation: float | None) -> float | None:
    if entry_trigger in (None, 0) or invalidation is None:
        return None
    return ((entry_trigger - invalidation) / entry_trigger) * 100.0


def _next_support(
    *,
    zone_low: float | None,
    levels: list[LevelReference],
    excluded_names: set[str],
) -> tuple[str | None, float | None]:
    if zone_low is None:
        return None, None
    candidates = [
        level for level in levels
        if level.name not in excluded_names and math.isfinite(level.price) and level.price < zone_low
    ]
    if not candidates:
        return None, None
    selected = max(candidates, key=lambda level: level.price)
    return selected.name, selected.price


def _build_route(
    *,
    route_code: str,
    eligible: bool,
    status: str,
    zone_low: float | None,
    zone_high: float | None,
    entry_trigger_price: float | None,
    entry_trigger_condition: str,
    route_invalidation: float | None,
    latest_close: float | None,
    supporting_levels: list[str],
    level_basis: list[str],
    reason: str,
    levels: list[LevelReference],
    metadata: dict[str, Any] | None = None,
) -> RouteEvaluation:
    distance_to_zone_pct = None
    if latest_close is not None and zone_low is not None and zone_high is not None:
        distance_to_zone_pct = _distance_to_zone(latest_close, zone_low, zone_high)
    next_support_name, next_support_price = _next_support(zone_low=zone_low, levels=levels, excluded_names=set(level_basis))
    advance_alert_price = zone_high * 1.02 if zone_high is not None else None
    return RouteEvaluation(
        route_code=route_code,
        route_label=ROUTE_LABELS[route_code],
        eligible=eligible,
        status=status,
        zone_low=zone_low,
        zone_high=zone_high,
        advance_alert_price=advance_alert_price,
        entry_trigger_price=entry_trigger_price,
        entry_trigger_condition=entry_trigger_condition,
        route_invalidation=route_invalidation,
        next_support_name=next_support_name,
        next_support_price=next_support_price,
        distance_to_zone_pct=distance_to_zone_pct,
        risk_pct=_risk_pct(entry_trigger_price, route_invalidation),
        reason=reason,
        supporting_levels=supporting_levels,
        level_basis=level_basis,
        metadata=metadata or {},
    )


def _status_from_zone(
    *,
    eligible: bool,
    latest_low: float,
    latest_high: float,
    latest_close: float,
    zone_low: float,
    zone_high: float,
    invalidation: float,
    approach_pct: float,
    extension_pct: float,
    confirmed: bool,
    testing: bool,
) -> str:
    if latest_close < invalidation:
        return "FAILED"
    if confirmed:
        return "CONFIRMED"
    if testing:
        return "TESTING"
    if latest_close > zone_high:
        distance = ((latest_close / zone_high) - 1.0) * 100.0
        if distance <= approach_pct:
            return "APPROACHING"
        if distance > extension_pct:
            return "EXTENDED"
    if eligible:
        return "WAITING"
    if _is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high):
        return "TESTING"
    return "INVALID"


def evaluate_routes(
    current_period_daily: pd.DataFrame,
    *,
    latest_close: float | None,
    avwap: float | None,
    poc: float | None,
    vah: float | None,
    val: float | None,
    previous_anchor_vwap_close: float | None,
    avwap_slope_pct: float | None,
    config: VpAvwapConfig,
) -> list[RouteEvaluation]:
    levels = [
        LevelReference("VAH", vah) if vah is not None else None,
        LevelReference("POC", poc) if poc is not None else None,
        LevelReference("AVWAP", avwap) if avwap is not None else None,
        LevelReference("VAL", val) if val is not None else None,
        LevelReference("Previous Anchor VWAP Close", previous_anchor_vwap_close) if previous_anchor_vwap_close is not None else None,
    ]
    structural_levels = [level for level in levels if level is not None]
    if current_period_daily.empty or latest_close is None:
        return [
            RouteEvaluation(route_code=code, route_label=label, eligible=False, status="DATA_UNAVAILABLE", zone_low=None, zone_high=None, advance_alert_price=None, entry_trigger_price=None, entry_trigger_condition="Unavailable", route_invalidation=None, next_support_name=None, next_support_price=None, distance_to_zone_pct=None, risk_pct=None, reason="Missing completed daily bars.", supporting_levels=[], error="Missing completed daily bars.", level_basis=[])
            for code, label in ROUTE_LABELS.items()
        ]

    latest = current_period_daily.iloc[-1]
    previous = current_period_daily.iloc[-2] if len(current_period_daily) > 1 else latest
    latest_low = float(latest["Low"])
    latest_high = float(latest["High"])
    previous_close = float(previous["Close"])
    zone_buffer = config.zone_buffer_pct / 100.0
    invalidation_buffer = config.invalidation_buffer_pct / 100.0
    approach_pct = config.approach_pct
    extension_pct = config.extension_pct

    routes: list[RouteEvaluation] = []

    if vah is None or val is None:
        routes.append(RouteEvaluation("VAH_DEFENDED_PULLBACK", ROUTE_LABELS["VAH_DEFENDED_PULLBACK"], False, "DATA_UNAVAILABLE", None, None, None, None, "Unavailable", None, None, None, None, None, reason="VAH or VAL is unavailable.", error="VAH or VAL is unavailable."))
    else:
        zone_low = vah * (1.0 - zone_buffer)
        zone_high = vah * (1.0 + zone_buffer)
        invalidation = zone_low * (1.0 - invalidation_buffer)
        earlier_close_above_vah = bool((current_period_daily["Close"].iloc[:-1] > vah).any()) if len(current_period_daily) > 1 else False
        slope_ok = avwap_slope_pct is not None and avwap_slope_pct >= -config.avwap_flat_threshold_pct
        eligible = earlier_close_above_vah and slope_ok and latest_close >= val
        confirmed = _is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high) and latest_close > zone_high
        testing = _is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high) and zone_low <= latest_close <= zone_high
        status = _status_from_zone(
            eligible=eligible,
            latest_low=latest_low,
            latest_high=latest_high,
            latest_close=latest_close,
            zone_low=zone_low,
            zone_high=zone_high,
            invalidation=invalidation,
            approach_pct=approach_pct,
            extension_pct=extension_pct,
            confirmed=confirmed,
            testing=testing,
        )
        routes.append(
            _build_route(
                route_code="VAH_DEFENDED_PULLBACK",
                eligible=eligible,
                status=status,
                zone_low=zone_low,
                zone_high=zone_high,
                entry_trigger_price=zone_high,
                entry_trigger_condition=f"Completed daily candle closes above {zone_high:.2f} after testing the VAH zone.",
                route_invalidation=invalidation,
                latest_close=latest_close,
                supporting_levels=["VAH", "AVWAP"] if avwap is not None else ["VAH"],
                level_basis=["VAH"],
                reason="Earlier post-earnings closes established value above VAH and price is being judged against a defended VAH pullback zone.",
                levels=structural_levels,
            )
        )

    if poc is None or avwap is None:
        routes.append(RouteEvaluation("POC_AVWAP_RECOVERY", ROUTE_LABELS["POC_AVWAP_RECOVERY"], False, "DATA_UNAVAILABLE", None, None, None, None, "Unavailable", None, None, None, None, None, reason="POC or AVWAP is unavailable.", error="POC or AVWAP is unavailable."))
    else:
        confluence_distance = abs(poc - avwap) / ((poc + avwap) / 2.0) * 100.0 if (poc + avwap) > 0 else math.inf
        confluence = confluence_distance <= config.confluence_pct
        if confluence:
            zone_low = min(poc, avwap) * (1.0 - zone_buffer)
            zone_high = max(poc, avwap) * (1.0 + zone_buffer)
            basis = ["POC", "AVWAP"]
            support = ["POC", "AVWAP"]
            reason = "POC and earnings AVWAP are inside the configured confluence threshold."
        else:
            selected = "POC" if abs(latest_close - poc) <= abs(latest_close - avwap) else "AVWAP"
            selected_level = poc if selected == "POC" else avwap
            zone_low = selected_level * (1.0 - zone_buffer)
            zone_high = selected_level * (1.0 + zone_buffer)
            basis = [selected]
            support = [selected]
            reason = f"POC and AVWAP are {confluence_distance:.2f}% apart, so the route falls back to the stronger single level: {selected}."
        invalidation = zone_low * (1.0 - invalidation_buffer)
        eligible = latest_close >= (val if val is not None else zone_low)
        confirmed_defence = previous_close > zone_high and _is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high) and latest_close > zone_high
        confirmed_reclaim = previous_close < zone_low and latest_close > zone_high
        confirmed = confirmed_defence or confirmed_reclaim
        testing = _is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high) and zone_low <= latest_close <= zone_high
        status = _status_from_zone(
            eligible=eligible,
            latest_low=latest_low,
            latest_high=latest_high,
            latest_close=latest_close,
            zone_low=zone_low,
            zone_high=zone_high,
            invalidation=invalidation,
            approach_pct=approach_pct,
            extension_pct=extension_pct,
            confirmed=confirmed,
            testing=testing,
        )
        routes.append(
            _build_route(
                route_code="POC_AVWAP_RECOVERY",
                eligible=eligible,
                status=status,
                zone_low=zone_low,
                zone_high=zone_high,
                entry_trigger_price=zone_high,
                entry_trigger_condition=f"Completed daily candle closes above {zone_high:.2f} after recovering the POC/AVWAP zone.",
                route_invalidation=invalidation,
                latest_close=latest_close,
                supporting_levels=support,
                level_basis=basis,
                reason=reason,
                levels=structural_levels,
                metadata={"confluence_pct_distance": confluence_distance, "confluence": confluence},
            )
        )

    if len(current_period_daily) < 2:
        routes.append(RouteEvaluation("BREAKOUT_RETEST", ROUTE_LABELS["BREAKOUT_RETEST"], False, "DATA_UNAVAILABLE", None, None, None, None, "Unavailable", None, None, None, None, None, reason="Breakout retest needs at least two completed sessions.", error="Breakout retest needs at least two completed sessions."))
    else:
        breakout_buffer = config.breakout_buffer_pct / 100.0
        breakout_level = None
        breakout_index = None
        for idx in range(1, len(current_period_daily)):
            prior_high = float(current_period_daily["High"].iloc[:idx].max())
            close = float(current_period_daily["Close"].iloc[idx])
            if close > prior_high * (1.0 + breakout_buffer):
                breakout_level = prior_high
                breakout_index = idx
        if breakout_level is None or breakout_index is None:
            routes.append(RouteEvaluation("BREAKOUT_RETEST", ROUTE_LABELS["BREAKOUT_RETEST"], False, "INVALID", None, None, None, None, "No breakout has been confirmed yet.", None, None, None, None, None, reason="No post-earnings breakout above the prior range high has occurred.", level_basis=["Breakout Level"]))
        else:
            zone_low = breakout_level * (1.0 - zone_buffer)
            zone_high = breakout_level * (1.0 + zone_buffer)
            invalidation = zone_low * (1.0 - invalidation_buffer)
            retest_frame = current_period_daily.iloc[breakout_index + 1 : breakout_index + 1 + config.breakout_retest_window]
            confirmed_retest = False
            testing = False
            for bar in retest_frame.itertuples():
                bar_low = float(bar.Low)
                bar_high = float(bar.High)
                bar_close = float(bar.Close)
                if _is_in_zone(bar_low, bar_high, zone_low=zone_low, zone_high=zone_high):
                    if bar_close > breakout_level:
                        confirmed_retest = True
                    else:
                        testing = True
            if confirmed_retest:
                status = "CONFIRMED" if latest_close >= breakout_level else "FAILED"
            else:
                status = _status_from_zone(
                    eligible=True,
                    latest_low=latest_low,
                    latest_high=latest_high,
                    latest_close=latest_close,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    invalidation=invalidation,
                    approach_pct=approach_pct,
                    extension_pct=extension_pct,
                    confirmed=False,
                    testing=testing or (_is_in_zone(latest_low, latest_high, zone_low=zone_low, zone_high=zone_high) and latest_close <= breakout_level),
                )
            routes.append(
                _build_route(
                    route_code="BREAKOUT_RETEST",
                    eligible=True,
                    status=status,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    entry_trigger_price=zone_high,
                    entry_trigger_condition=f"Later completed bar closes above stored breakout level {breakout_level:.2f} after retesting the breakout zone.",
                    route_invalidation=invalidation,
                    latest_close=latest_close,
                    supporting_levels=["Breakout Level"],
                    level_basis=["Breakout Level"],
                    reason="Breakout level is stored from the prior post-earnings range high with no look-ahead.",
                    levels=structural_levels,
                    metadata={"breakout_level": breakout_level, "breakout_index": breakout_index, "confirmed_retest": confirmed_retest},
                )
            )

    if val is None:
        routes.append(RouteEvaluation("VAL_RECLAIM", ROUTE_LABELS["VAL_RECLAIM"], False, "DATA_UNAVAILABLE", None, None, None, None, "Unavailable", None, None, None, None, None, reason="VAL is unavailable.", error="VAL is unavailable."))
    else:
        zone_low = val * (1.0 - zone_buffer)
        zone_high = val * (1.0 + zone_buffer)
        reclaim_trigger = (previous_close < val) or (latest_low < zone_low)
        confirmed = reclaim_trigger and latest_close > zone_high
        testing = latest_low <= zone_high and latest_close <= zone_high
        invalidation_base = min(latest_low, zone_low) if confirmed else zone_low
        invalidation = invalidation_base * (1.0 - invalidation_buffer)
        status = _status_from_zone(
            eligible=True,
            latest_low=latest_low,
            latest_high=latest_high,
            latest_close=latest_close,
            zone_low=zone_low,
            zone_high=zone_high,
            invalidation=invalidation,
            approach_pct=approach_pct,
            extension_pct=extension_pct,
            confirmed=confirmed,
            testing=testing,
        )
        routes.append(
            _build_route(
                route_code="VAL_RECLAIM",
                eligible=True,
                status=status,
                zone_low=zone_low,
                zone_high=zone_high,
                entry_trigger_price=zone_high,
                entry_trigger_condition=f"Completed daily candle closes above {zone_high:.2f} after reclaiming the VAL zone.",
                route_invalidation=invalidation,
                latest_close=latest_close,
                supporting_levels=["VAL"],
                level_basis=["VAL"],
                reason="VAL reclaim is the lowest-price route and requires a completed daily reclaim, not a touch alone.",
                levels=structural_levels,
            )
        )

    return routes
