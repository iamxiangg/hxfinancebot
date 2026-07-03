from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import pandas as pd

from scanners.vp_avwap.avwap import compute_anchored_vwap
from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.earnings_anchor import select_latest_confirmed_earnings_anchor
from scanners.vp_avwap.entry_routes import evaluate_routes, profile_state
from scanners.vp_avwap.market_data import (
    VpAvwapYahooDataSource,
    has_full_session_coverage,
    trim_intraday_to_completed_sessions,
)
from scanners.vp_avwap.models import (
    STATUS_PRIORITY,
    TickerAnalysis,
    TickerRecord,
    VpAvwapScanResult,
)
from scanners.vp_avwap.profile import build_volume_profile
from scanners.vp_avwap.scoring import apply_tier_overrides, choose_preferred_route, raw_score_tier, score_routes


logger = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _slice_period(frame: pd.DataFrame, *, start: pd.Timestamp, end: pd.Timestamp | None = None) -> pd.DataFrame:
    working = frame.copy()
    if end is None:
        return working.loc[working.index >= start].copy()
    return working.loc[(working.index >= start) & (working.index < end)].copy()


def _current_price(frame: pd.DataFrame) -> float | None:
    if frame.empty or "Close" not in frame.columns:
        return None
    return _to_float(frame["Close"].iloc[-1])


def _choose_profile_bars(
    ticker: str,
    *,
    reaction_session: pd.Timestamp,
    latest_completed_session: pd.Timestamp,
    completed_daily: pd.DataFrame,
    data_source: VpAvwapYahooDataSource,
    config: VpAvwapConfig,
    now_utc: datetime,
) -> tuple[pd.DataFrame, str, str, list[str]]:
    warnings: list[str] = []
    start = reaction_session.to_pydatetime() - timedelta(days=2)
    end = now_utc + timedelta(days=1)
    for interval, quality in ((config.primary_interval, "HIGH"), (config.secondary_interval, "MEDIUM")):
        intraday = trim_intraday_to_completed_sessions(
            data_source.intraday_history(ticker, interval=interval, start=start, end=end),
            now_utc=now_utc,
        )
        intraday = intraday.loc[intraday.index >= reaction_session]
        if has_full_session_coverage(intraday, reaction_session=reaction_session, latest_completed_session=latest_completed_session):
            return intraday, interval, quality, warnings
        warnings.append(f"{interval} coverage incomplete from {reaction_session.date().isoformat()}; falling back.")
    current_period_daily = _slice_period(completed_daily, start=reaction_session)
    if current_period_daily.empty:
        return pd.DataFrame(), "daily", "UNAVAILABLE", warnings
    warnings.append("Daily bars used as the volume-profile fallback.")
    return current_period_daily, "daily", "LOW", warnings


def _status_from_profile_and_avwap(profile_status: str, avwap_status: str) -> str:
    if profile_status != "OK":
        return "DATA_UNAVAILABLE"
    if avwap_status != "OK":
        return "DATA_UNAVAILABLE"
    return "OK"


def _technical_reason(preferred_route: object, *, hard_override_reason: str) -> str:
    route = preferred_route
    if hard_override_reason:
        return hard_override_reason
    assert isinstance(route, object)
    return getattr(route, "reason", "")


def _analysis_from_record(
    record: TickerRecord,
    *,
    data_source: VpAvwapYahooDataSource,
    config: VpAvwapConfig,
    now_utc: datetime,
) -> TickerAnalysis:
    completed_daily = data_source.latest_completed_daily(record.ticker, now_utc=now_utc)
    if completed_daily.empty:
        empty_route = choose_preferred_route(score_routes(evaluate_routes(pd.DataFrame(), latest_close=None, avwap=None, poc=None, vah=None, val=None, previous_anchor_vwap_close=None, avwap_slope_pct=None, config=config), profile_state_code=None, avwap_slope_pct=None, latest_close=None))
        return TickerAnalysis(
            ticker=record.ticker,
            google_ticker=record.google_ticker,
            stock_name=record.stock_name,
            current_price=None,
            technical_score=0.0,
            raw_score_tier=4,
            final_tier=4,
            profile_state="DATA_UNAVAILABLE",
            profile_state_code=None,
            earnings_timestamp=None,
            earnings_reaction_session=None,
            earnings_release_timing=None,
            anchor_confidence=None,
            previous_earnings_timestamp=None,
            previous_reaction_session=None,
            avwap=None,
            poc=None,
            vah=None,
            val=None,
            previous_anchor_vwap_close=None,
            avwap_five_session_slope_pct=None,
            close_vs_avwap_pct=None,
            close_vs_poc_pct=None,
            close_vs_vah_pct=None,
            close_vs_val_pct=None,
            profile_high=None,
            profile_low=None,
            number_of_profile_rows=config.rows,
            value_area_target_pct=config.value_area_pct,
            actual_value_area_pct=None,
            source_bars=0,
            data_interval_used="daily",
            data_quality="UNAVAILABLE",
            hard_override=True,
            hard_override_reason="Missing usable market data.",
            preferred_route=empty_route,
            routes=[empty_route],
            technical_reason="Missing usable market data.",
            calculation_version=config.calculation_version,
            status="DATA_UNAVAILABLE",
            error="Missing usable market data.",
        )

    latest_completed_session = pd.Timestamp(completed_daily.index[-1])
    earnings_frame = data_source.earnings_dates(record.ticker)
    anchor_selection = select_latest_confirmed_earnings_anchor(
        earnings_frame,
        pd.DatetimeIndex(completed_daily.index),
        latest_completed_session=latest_completed_session,
    )
    if anchor_selection.current is None:
        routes = score_routes(
            evaluate_routes(pd.DataFrame(), latest_close=None, avwap=None, poc=None, vah=None, val=None, previous_anchor_vwap_close=None, avwap_slope_pct=None, config=config),
            profile_state_code=None,
            avwap_slope_pct=None,
            latest_close=None,
        )
        preferred = choose_preferred_route(routes)
        return TickerAnalysis(
            ticker=record.ticker,
            google_ticker=record.google_ticker,
            stock_name=record.stock_name,
            current_price=_current_price(completed_daily),
            technical_score=0.0,
            raw_score_tier=4,
            final_tier=4,
            profile_state="DATA_UNAVAILABLE",
            profile_state_code=None,
            earnings_timestamp=None,
            earnings_reaction_session=None,
            earnings_release_timing=None,
            anchor_confidence=None,
            previous_earnings_timestamp=None,
            previous_reaction_session=None,
            avwap=None,
            poc=None,
            vah=None,
            val=None,
            previous_anchor_vwap_close=None,
            avwap_five_session_slope_pct=None,
            close_vs_avwap_pct=None,
            close_vs_poc_pct=None,
            close_vs_vah_pct=None,
            close_vs_val_pct=None,
            profile_high=None,
            profile_low=None,
            number_of_profile_rows=config.rows,
            value_area_target_pct=config.value_area_pct,
            actual_value_area_pct=None,
            source_bars=0,
            data_interval_used="daily",
            data_quality="UNAVAILABLE",
            hard_override=True,
            hard_override_reason=anchor_selection.reason or "Missing usable earnings anchor.",
            preferred_route=preferred,
            routes=routes,
            technical_reason=anchor_selection.reason or "Missing usable earnings anchor.",
            calculation_version=config.calculation_version,
            status="DATA_UNAVAILABLE",
            error=anchor_selection.reason or "Missing usable earnings anchor.",
        )

    current_anchor = anchor_selection.current
    previous_anchor = anchor_selection.previous
    current_period_daily = _slice_period(completed_daily, start=current_anchor.reaction_session)
    previous_period_daily = (
        _slice_period(completed_daily, start=previous_anchor.reaction_session, end=current_anchor.reaction_session)
        if previous_anchor is not None
        else pd.DataFrame()
    )
    source_bars, interval_used, data_quality, warnings = _choose_profile_bars(
        record.ticker,
        reaction_session=current_anchor.reaction_session,
        latest_completed_session=latest_completed_session,
        completed_daily=completed_daily,
        data_source=data_source,
        config=config,
        now_utc=now_utc,
    )
    avwap_result = compute_anchored_vwap(
        source_bars,
        slope_lookback_sessions=config.avwap_slope_lookback,
        previous_period_bars=previous_period_daily,
    )
    profile_result = build_volume_profile(
        source_bars,
        rows=config.rows,
        value_area_pct=config.value_area_pct,
        current_avwap=avwap_result.current_avwap,
        interval_used=interval_used,
        data_quality=data_quality,
    )
    current_price = _current_price(current_period_daily)
    state_code, state_label, metrics = profile_state(
        current_price,
        avwap=avwap_result.current_avwap,
        poc=profile_result.poc,
        vah=profile_result.vah,
        val=profile_result.val,
    )
    routes = evaluate_routes(
        current_period_daily,
        latest_close=current_price,
        avwap=avwap_result.current_avwap,
        poc=profile_result.poc,
        vah=profile_result.vah,
        val=profile_result.val,
        previous_anchor_vwap_close=avwap_result.previous_anchor_vwap_close,
        avwap_slope_pct=avwap_result.five_session_slope_pct,
        config=config,
    )
    routes = score_routes(
        routes,
        profile_state_code=state_code,
        avwap_slope_pct=avwap_result.five_session_slope_pct,
        latest_close=current_price,
    )
    preferred_route = choose_preferred_route(routes)
    technical_score = max((route.route_score for route in routes if route.eligible), default=0.0)
    raw_tier = raw_score_tier(technical_score)
    status = _status_from_profile_and_avwap(profile_result.status, avwap_result.status)
    final_tier, hard_override, hard_override_reason = apply_tier_overrides(
        preferred_route=preferred_route,
        routes=routes,
        raw_tier=raw_tier,
        data_quality=data_quality,
        latest_close=current_price,
        val=profile_result.val,
        avwap=avwap_result.current_avwap,
        poc=profile_result.poc,
        avwap_slope_pct=avwap_result.five_session_slope_pct,
        status=status,
        missing_anchor=False,
        config=config,
    )
    calibration = {
        "ticker": record.ticker,
        "earnings_timestamp_selected": current_anchor.earnings_timestamp.isoformat(),
        "release_timing": current_anchor.release_timing,
        "reaction_session_anchor": current_anchor.reaction_session.date().isoformat(),
        "anchor_confidence": current_anchor.reaction_session_confidence,
        "previous_reaction_session_anchor": previous_anchor.reaction_session.date().isoformat() if previous_anchor is not None else None,
        "requested_interval": config.primary_interval,
        "actual_interval_used": interval_used,
        "first_source_timestamp": source_bars.index[0].isoformat() if not source_bars.empty else None,
        "last_source_timestamp": source_bars.index[-1].isoformat() if not source_bars.empty else None,
        "number_of_source_bars": len(source_bars),
        "profile_low": profile_result.profile_low,
        "profile_high": profile_result.profile_high,
        "row_width": profile_result.row_width,
        "poc": profile_result.poc,
        "vah": profile_result.vah,
        "val": profile_result.val,
        "avwap": avwap_result.current_avwap,
        "previous_anchor_vwap_close": avwap_result.previous_anchor_vwap_close,
        "total_source_volume": profile_result.total_source_volume,
        "total_allocated_volume": profile_result.total_allocated_volume,
        "volume_allocation_difference": profile_result.total_source_volume - profile_result.total_allocated_volume,
        "actual_value_area_percentage": profile_result.actual_value_area_percentage,
        "data_quality": data_quality,
        "price_adjustment_convention": f"auto_adjust={config.auto_adjust}",
        "regular_hours_setting": f"prepost={not config.regular_hours_only}",
        "calculation_version": config.calculation_version,
        "warnings": warnings,
    }
    return TickerAnalysis(
        ticker=record.ticker,
        google_ticker=record.google_ticker,
        stock_name=record.stock_name,
        current_price=current_price,
        technical_score=technical_score,
        raw_score_tier=raw_tier,
        final_tier=final_tier,
        profile_state=state_label,
        profile_state_code=state_code,
        earnings_timestamp=current_anchor.earnings_timestamp,
        earnings_reaction_session=current_anchor.reaction_session,
        earnings_release_timing=current_anchor.release_timing,
        anchor_confidence=current_anchor.reaction_session_confidence,
        previous_earnings_timestamp=previous_anchor.earnings_timestamp if previous_anchor is not None else None,
        previous_reaction_session=previous_anchor.reaction_session if previous_anchor is not None else None,
        avwap=avwap_result.current_avwap,
        poc=profile_result.poc,
        vah=profile_result.vah,
        val=profile_result.val,
        previous_anchor_vwap_close=avwap_result.previous_anchor_vwap_close,
        avwap_five_session_slope_pct=avwap_result.five_session_slope_pct,
        close_vs_avwap_pct=metrics["close_vs_avwap_pct"],
        close_vs_poc_pct=metrics["close_vs_poc_pct"],
        close_vs_vah_pct=metrics["close_vs_vah_pct"],
        close_vs_val_pct=metrics["close_vs_val_pct"],
        profile_high=profile_result.profile_high,
        profile_low=profile_result.profile_low,
        number_of_profile_rows=config.rows,
        value_area_target_pct=config.value_area_pct,
        actual_value_area_pct=profile_result.actual_value_area_percentage,
        source_bars=len(source_bars),
        data_interval_used=interval_used,
        data_quality=data_quality,
        hard_override=hard_override,
        hard_override_reason=hard_override_reason,
        preferred_route=preferred_route,
        routes=routes,
        technical_reason=_technical_reason(preferred_route, hard_override_reason=hard_override_reason),
        calculation_version=config.calculation_version,
        status=status,
        error="; ".join(filter(None, [profile_result.reason, avwap_result.reason])) if status != "OK" else "",
        calibration=calibration,
    )


def _assign_ranks(results: list[TickerAnalysis]) -> list[TickerAnalysis]:
    ordered = sorted(
        results,
        key=lambda result: (
            result.final_tier,
            -result.technical_score,
            STATUS_PRIORITY.get(result.preferred_route.status, 99),
            result.preferred_route.distance_to_zone_pct if result.preferred_route.distance_to_zone_pct is not None else math.inf,
            result.ticker,
        ),
    )
    per_tier_counts: dict[int, int] = {}
    for overall_rank, result in enumerate(ordered, start=1):
        per_tier_counts[result.final_tier] = per_tier_counts.get(result.final_tier, 0) + 1
        result.overall_technical_rank = overall_rank
        result.rank_within_tier = per_tier_counts[result.final_tier]
    return ordered


def run_vp_avwap_scan(
    ticker_records: list[dict[str, object]] | list[TickerRecord],
    *,
    config: VpAvwapConfig | None = None,
    data_source: VpAvwapYahooDataSource | None = None,
    observed_at: str | None = None,
) -> VpAvwapScanResult:
    actual_config = config or VpAvwapConfig.from_env()
    now_utc = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else datetime.now(UTC)
    source = data_source or VpAvwapYahooDataSource(config=actual_config)
    records: list[TickerRecord] = []
    seen: set[str] = set()
    for item in ticker_records:
        record = item if isinstance(item, TickerRecord) else TickerRecord(
            ticker=str(item.get("ticker", "")).strip().upper(),
            google_ticker=str(item.get("google_ticker", "")).strip(),
            stock_name=str(item.get("stock_name", "")).strip(),
            sheet_row=int(item["sheet_row"]) if item.get("sheet_row") is not None else None,
        )
        if not record.ticker or record.ticker in seen:
            continue
        seen.add(record.ticker)
        records.append(record)
    if actual_config.test_tickers:
        wanted = set(actual_config.test_tickers)
        records = [record for record in records if record.ticker in wanted]
    if actual_config.max_tickers is not None:
        records = records[: actual_config.max_tickers]

    results: list[TickerAnalysis] = []
    errors: list[str] = []
    for record in records:
        try:
            results.append(
                _analysis_from_record(
                    record,
                    data_source=source,
                    config=actual_config,
                    now_utc=now_utc,
                )
            )
        except Exception as exc:
            logger.exception("VP/AVWAP analysis failed for %s: %s", record.ticker, exc)
            errors.append(f"{record.ticker}: {exc}")
            failed_scan = _analysis_from_record(
                TickerRecord(record.ticker, record.google_ticker, record.stock_name, record.sheet_row),
                data_source=source,
                config=actual_config,
                now_utc=now_utc,
            ) if False else None
            if failed_scan is None:
                routes = score_routes(
                    evaluate_routes(pd.DataFrame(), latest_close=None, avwap=None, poc=None, vah=None, val=None, previous_anchor_vwap_close=None, avwap_slope_pct=None, config=actual_config),
                    profile_state_code=None,
                    avwap_slope_pct=None,
                    latest_close=None,
                )
                preferred = choose_preferred_route(routes)
                results.append(
                    TickerAnalysis(
                        ticker=record.ticker,
                        google_ticker=record.google_ticker,
                        stock_name=record.stock_name,
                        current_price=None,
                        technical_score=0.0,
                        raw_score_tier=4,
                        final_tier=4,
                        profile_state="DATA_UNAVAILABLE",
                        profile_state_code=None,
                        earnings_timestamp=None,
                        earnings_reaction_session=None,
                        earnings_release_timing=None,
                        anchor_confidence=None,
                        previous_earnings_timestamp=None,
                        previous_reaction_session=None,
                        avwap=None,
                        poc=None,
                        vah=None,
                        val=None,
                        previous_anchor_vwap_close=None,
                        avwap_five_session_slope_pct=None,
                        close_vs_avwap_pct=None,
                        close_vs_poc_pct=None,
                        close_vs_vah_pct=None,
                        close_vs_val_pct=None,
                        profile_high=None,
                        profile_low=None,
                        number_of_profile_rows=actual_config.rows,
                        value_area_target_pct=actual_config.value_area_pct,
                        actual_value_area_pct=None,
                        source_bars=0,
                        data_interval_used="daily",
                        data_quality="UNAVAILABLE",
                        hard_override=True,
                        hard_override_reason="Ticker analysis raised an exception.",
                        preferred_route=preferred,
                        routes=routes,
                        technical_reason="Ticker analysis raised an exception.",
                        calculation_version=actual_config.calculation_version,
                        status="DATA_UNAVAILABLE",
                        error=str(exc),
                    )
                )
    ordered = _assign_ranks(results)
    return VpAvwapScanResult(
        observed_at_utc=now_utc.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        tickers_requested=len(records),
        processed_tickers=len(ordered),
        results=ordered,
        errors=errors,
    )
