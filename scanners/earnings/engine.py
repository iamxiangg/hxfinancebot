from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from scanners.earnings.event_history import (
    NY_TZ,
    build_historical_event_moves,
    get_upcoming_earnings_event,
    load_timing_overrides,
    realised_move_percentile,
    summarise_historical_moves,
    trading_sessions_from_history,
)
from scanners.earnings.market_data import YahooEarningsDataSource, average_dollar_volume
from scanners.earnings.models import EarningsOpportunity, EarningsScanResult
from scanners.earnings.pricing import (
    LiquidityThresholds,
    build_iron_butterfly,
    calculate_implied_move,
    classify_event_purity,
    find_atm_straddle,
    select_post_event_expiry,
)
from scanners.earnings.scoring import (
    build_risk_flags,
    calculate_richness_metrics,
    classify_opportunity,
    event_richness_score,
    execution_quality_score,
    historical_reliability_score,
    risk_adjustment_score,
)


logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_positive_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    return value if value > 0 else default


@dataclass(frozen=True)
class EarningsScannerConfig:
    lookahead_days: int = 3
    max_tickers: int = 500
    max_candidates: int = 10
    request_delay_seconds: float = 0.5
    min_stock_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    min_leg_open_interest: int = 100
    min_leg_volume: int = 10
    max_leg_spread_pct: float = 0.15
    max_total_spread_pct: float = 0.15
    overrides_path: Path = Path("config/earnings_timing_overrides.json")
    min_history_events: int = 8
    max_history_events: int = 20
    # Per-ticker concurrency. Default 1 (serial) because yfinance is
    # fragile under multi-threaded load: stacking the outer pool on top
    # of yfinance's internal thread pool can hit Yahoo's ~100 req/min
    # rate limit within seconds on a 500-ticker scan. Bump to 2-4 for
    # faster scans only if you've validated your rate-limit headroom.
    max_workers: int = 1
    rate_limit_per_minute: int = YahooEarningsDataSource.DEFAULT_RATE_LIMIT_PER_MINUTE

    @classmethod
    def from_env(cls) -> "EarningsScannerConfig":
        return cls(
            lookahead_days=_env_int("EARNINGS_LOOKAHEAD_DAYS", 3),
            max_tickers=_env_int("EARNINGS_MAX_TICKERS", 500),
            max_candidates=_env_int("EARNINGS_MAX_CANDIDATES", 10),
            request_delay_seconds=_env_float("EARNINGS_REQUEST_DELAY_SECONDS", 0.25),
            min_stock_price=_env_float("EARNINGS_MIN_STOCK_PRICE", 5.0),
            min_avg_dollar_volume=_env_float("EARNINGS_MIN_AVG_DOLLAR_VOLUME", 20_000_000.0),
            min_leg_open_interest=_env_int("EARNINGS_MIN_LEG_OPEN_INTEREST", 100),
            min_leg_volume=_env_int("EARNINGS_MIN_LEG_VOLUME", 10),
            max_leg_spread_pct=_env_float("EARNINGS_MAX_LEG_SPREAD_PCT", 0.15),
            max_total_spread_pct=_env_float("EARNINGS_MAX_TOTAL_SPREAD_PCT", 0.15),
            max_workers=_env_positive_int("EARNINGS_MAX_WORKERS", 1),
            rate_limit_per_minute=_env_positive_int(
                "EARNINGS_RATE_LIMIT_PER_MINUTE",
                YahooEarningsDataSource.DEFAULT_RATE_LIMIT_PER_MINUTE,
            ),
        )


def _configured_tickers() -> list[str]:
    return [
        part.strip()
        for part in str(os.getenv("EARNINGS_TICKERS", "")).split(",")
        if part.strip()
    ]


def _data_confidence(
    *,
    timing_known: bool,
    summary_count: int,
    structure_valid: bool,
    option_expiry_days: int | None,
) -> str:
    if not timing_known:
        return "UNKNOWN"
    if summary_count >= 12 and structure_valid and option_expiry_days is not None and option_expiry_days <= 7:
        return "HIGH"
    if summary_count >= 8 and structure_valid:
        return "MEDIUM"
    return "LOW"


def _build_reason(classification: str, move_richness: float | None, breach_rate: float | None, purity: str) -> str:
    richness_text = f"{move_richness:.2f}x median" if move_richness is not None else "missing richness"
    breach_text = f"{breach_rate:.0%} breach rate" if breach_rate is not None else "unknown breach rate"
    return f"{classification}: {richness_text}, {breach_text}, purity {purity.lower()}"


def _empty_counts() -> dict[str, int]:
    return {
        "universe_size": 0,
        "earnings_candidates": 0,
        "timing_confirmed": 0,
        "option_chains_retrieved": 0,
        "historical_datasets_usable": 0,
        "actionable": 0,
        "watch": 0,
        "rejected": 0,
        "errors": 0,
    }


def _process_ticker(
    ticker: str,
    history: pd.DataFrame,
    *,
    config: EarningsScannerConfig,
    data_source: YahooEarningsDataSource,
    now_ny: datetime,
    overrides: dict,
    thresholds: LiquidityThresholds,
    universe_source: str,
) -> tuple[EarningsOpportunity | None, dict[str, int], list[str]]:
    """Process a single ticker; safe to call from multiple worker threads.

    Returns (opportunity_or_none, count_deltas, error_messages). All
    returned containers are owned by the caller after this returns, so
    the main thread can mutate them without locking.
    """
    local_counts: dict[str, int] = {
        "earnings_candidates": 0,
        "timing_confirmed": 0,
        "option_chains_retrieved": 0,
        "historical_datasets_usable": 0,
        "actionable": 0,
        "watch": 0,
        "rejected": 0,
        "errors": 0,
    }
    local_errors: list[str] = []

    try:
        if history.empty or not {"Open", "High", "Low", "Close", "Volume"}.issubset(history.columns):
            local_counts["errors"] += 1
            return None, local_counts, local_errors
        sessions = trading_sessions_from_history(history)
        earnings_frame = data_source.earnings_dates(ticker, limit=40)
        calendar = data_source.calendar(ticker)
        upcoming = get_upcoming_earnings_event(
            ticker,
            earnings_frame=earnings_frame,
            calendar=calendar,
            sessions=sessions,
            now_ny=now_ny,
            lookahead_days=config.lookahead_days,
            overrides=overrides,
        )
        if upcoming is None:
            return None, local_counts, local_errors
        local_counts["earnings_candidates"] += 1
        if upcoming.earnings_timing != "UNKNOWN":
            local_counts["timing_confirmed"] += 1

        spot_price = float(history["Close"].iloc[-1])
        if spot_price < config.min_stock_price:
            local_counts["rejected"] += 1
            return None, local_counts, local_errors
        avg_dollar_volume = average_dollar_volume(history)
        if avg_dollar_volume is None or avg_dollar_volume < config.min_avg_dollar_volume:
            local_counts["rejected"] += 1
            return None, local_counts, local_errors

        moves = build_historical_event_moves(
            ticker,
            earnings_frame=earnings_frame,
            history=history,
            now_ny=now_ny,
            overrides=overrides,
            max_events=config.max_history_events,
        )
        summary = summarise_historical_moves(moves)
        if summary.usable_event_count >= config.min_history_events:
            local_counts["historical_datasets_usable"] += 1

        expiry = None
        event_purity = "LOW"
        implied_move_pct = None
        implied_move_dollars = None
        structure = None
        days_after_event_to_expiry = None
        liquidity_status = "POOR"
        info = data_source.info(ticker)

        if upcoming.earnings_timing != "UNKNOWN":
            expiry = select_post_event_expiry(
                data_source.option_expirations(ticker),
                upcoming.earnings_at,
                upcoming.earnings_timing,
            )
            if expiry is not None:
                local_counts["option_chains_retrieved"] += 1
                days_after_event_to_expiry = (expiry - upcoming.event_session_date).days
                event_purity = classify_event_purity(days_after_event_to_expiry)
                calls, puts = data_source.option_chain(ticker, expiry)
                atm = find_atm_straddle(calls, puts, spot_price)
                if atm is not None:
                    short_call, short_put = atm
                    implied_move_pct, implied_move_dollars = calculate_implied_move(spot_price, short_call, short_put)
                    structure = build_iron_butterfly(
                        calls=calls,
                        puts=puts,
                        short_call=short_call,
                        short_put=short_put,
                        implied_move_dollars=implied_move_dollars,
                        thresholds=thresholds,
                    )
                    if structure is not None:
                        liquidity_status = structure.liquidity_status

        realised_percentile = realised_move_percentile(implied_move_pct or 0.0, moves) if implied_move_pct is not None else None
        richness_metrics = calculate_richness_metrics(implied_move_pct, summary, realised_percentile)
        breach_count = sum(move.absolute_event_move > (implied_move_pct or 0.0) for move in moves) if implied_move_pct is not None else 0
        breach_rate = (breach_count / len(moves)) if moves and implied_move_pct is not None else None
        flags = build_risk_flags(
            summary,
            implied_move_pct=implied_move_pct,
            earnings_timing=upcoming.earnings_timing,
            event_purity=event_purity,
            sector=str(info.get("sector") or ""),
            industry=str(info.get("industry") or ""),
            info=info,
        )
        data_confidence = _data_confidence(
            timing_known=upcoming.earnings_timing != "UNKNOWN",
            summary_count=summary.usable_event_count,
            structure_valid=structure is not None,
            option_expiry_days=days_after_event_to_expiry,
        )
        richness_score = event_richness_score(richness_metrics)
        reliability_score = historical_reliability_score(summary, implied_move_pct=implied_move_pct, breach_rate=breach_rate)
        execution_score = execution_quality_score(
            liquidity_status=liquidity_status,
            event_purity=event_purity,
            structure_valid=structure is not None,
        )
        risk_adjustment = risk_adjustment_score(flags, event_purity=event_purity, data_confidence=data_confidence)
        total_score = max(0.0, min(100.0, richness_score + reliability_score + execution_score + risk_adjustment))

        opportunity = EarningsOpportunity(
            ticker=ticker,
            classification="REJECTED",
            total_score=round(total_score, 2),
            earnings_at=upcoming.earnings_at,
            earnings_timing=upcoming.earnings_timing,
            timing_source=upcoming.timing_source,
            spot_price=spot_price,
            option_expiry=expiry,
            days_after_event_to_expiry=days_after_event_to_expiry,
            event_purity=event_purity,
            implied_move_pct=implied_move_pct,
            implied_move_dollars=implied_move_dollars,
            historical_event_count=summary.usable_event_count,
            historical_median_move=summary.median_absolute_move,
            historical_mean_move=summary.mean_absolute_move,
            historical_p75_move=summary.p75_move,
            historical_p90_move=summary.p90_move,
            historical_max_move=summary.maximum_move,
            historical_breach_rate=breach_rate,
            move_richness_median=richness_metrics.get("move_richness_median"),
            realised_move_percentile=richness_metrics.get("realised_move_percentile"),
            richness_score=round(richness_score, 2),
            reliability_score=round(reliability_score, 2),
            execution_score=round(execution_score, 2),
            risk_adjustment=round(risk_adjustment, 2),
            short_strike=structure.short_strike if structure else None,
            long_put_strike=structure.long_put_strike if structure else None,
            long_call_strike=structure.long_call_strike if structure else None,
            estimated_credit=structure.estimated_credit if structure else None,
            estimated_max_profit=structure.estimated_max_profit if structure else None,
            estimated_max_loss=structure.estimated_max_loss if structure else None,
            lower_breakeven=structure.lower_breakeven if structure else None,
            upper_breakeven=structure.upper_breakeven if structure else None,
            liquidity_status=liquidity_status,
            data_confidence=data_confidence,
            risk_flags=flags,
            reason="",
            details={
                "timing_reason": upcoming.timing_reason,
                "entry_session_date": upcoming.entry_session_date.isoformat() if upcoming.entry_session_date else "",
                "exit_session_date": upcoming.exit_session_date.isoformat() if upcoming.exit_session_date else "",
                "universe_source": universe_source,
                "average_dollar_volume": avg_dollar_volume,
                "historical_breach_count": breach_count,
                "historical_event_moves": [move.absolute_event_move for move in moves],
                "sector": str(info.get("sector") or ""),
                "industry": str(info.get("industry") or ""),
                "timing_source": upcoming.timing_source,
                "event_date_key": upcoming.event_date_key,
            },
        )
        opportunity.classification = classify_opportunity(
            opportunity,
            entry_session_is_today=upcoming.entry_session_date == now_ny.date(),
            structure_valid=structure is not None,
        )
        opportunity.reason = _build_reason(
            opportunity.classification,
            opportunity.move_richness_median,
            opportunity.historical_breach_rate,
            event_purity,
        )

        if opportunity.classification in {"ACTIONABLE", "STRONG_ACTIONABLE"}:
            local_counts["actionable"] += 1
        elif opportunity.classification in {"WATCH", "MANUAL_CONFIRMATION_REQUIRED"}:
            local_counts["watch"] += 1
        else:
            local_counts["rejected"] += 1

        return opportunity, local_counts, local_errors
    except Exception as exc:
        local_counts["errors"] += 1
        local_errors.append(f"{ticker}:{exc.__class__.__name__}")
        logger.exception("Earnings scanner ticker failed for %s", ticker)
        return None, local_counts, local_errors


def run_earnings_scan(
    *,
    now_ny: datetime | None = None,
    config: EarningsScannerConfig | None = None,
    data_source: YahooEarningsDataSource | None = None,
) -> EarningsScanResult:
    config = config or EarningsScannerConfig.from_env()
    data_source = data_source or YahooEarningsDataSource(
        request_delay_seconds=config.request_delay_seconds,
        rate_limit_per_minute=config.rate_limit_per_minute,
    )
    now_ny = now_ny or datetime.now(NY_TZ)
    overrides = load_timing_overrides(config.overrides_path)
    universe = data_source.load_universe(
        configured_tickers=_configured_tickers(),
        max_tickers=config.max_tickers,
    )

    logger.info(
        "Starting earnings scan over %d tickers (source=%s, request_delay=%.2fs, max_workers=%d, max_candidates=%d).",
        len(universe.tickers),
        universe.source,
        config.request_delay_seconds,
        config.max_workers,
        config.max_candidates,
    )

    if not universe.tickers:
        logger.warning("Earnings scan universe is empty after filtering — nothing to do.")
        return EarningsScanResult(
            opportunities=[],
            counts=_empty_counts(),
            errors=[],
        )

    thresholds = LiquidityThresholds(
        min_leg_open_interest=config.min_leg_open_interest,
        min_leg_volume=config.min_leg_volume,
        max_leg_spread_pct=config.max_leg_spread_pct,
        max_total_spread_pct=config.max_total_spread_pct,
    )
    counts = _empty_counts()
    counts["universe_size"] = len(universe.tickers)
    errors: list[str] = []
    opportunities: list[EarningsOpportunity] = []

    # Batch the history call: 1 yf.download replaces 500 sequential
    # Ticker.history() calls. Tickers with no rows are omitted; the
    # engine treats a missing entry as empty (and counts it as an error).
    history_by_ticker = data_source.batch_history(universe.tickers, period="2y")
    logger.info(
        "Batch history returned data for %d / %d tickers.",
        len(history_by_ticker),
        len(universe.tickers),
    )

    def _submit_all() -> dict[Any, str]:
        return {
            executor.submit(
                _process_ticker,
                ticker,
                history_by_ticker.get(ticker, pd.DataFrame()),
                config=config,
                data_source=data_source,
                now_ny=now_ny,
                overrides=overrides,
                thresholds=thresholds,
                universe_source=universe.source,
            ): ticker
            for ticker in universe.tickers
        }

    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="earn-scan") as executor:
        future_to_ticker = _submit_all()
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                opportunity, local_counts, local_errors = future.result()
            except Exception as exc:
                counts["errors"] += 1
                errors.append(f"{ticker}:{exc.__class__.__name__}")
                logger.exception("Earnings worker crashed for %s", ticker)
                continue
            for key, value in local_counts.items():
                counts[key] += value
            errors.extend(local_errors)
            if opportunity is not None:
                opportunities.append(opportunity)

    ranking = {
        "STRONG_ACTIONABLE": 4,
        "ACTIONABLE": 3,
        "WATCH": 2,
        "MANUAL_CONFIRMATION_REQUIRED": 1,
        "REJECTED": 0,
    }
    opportunities.sort(key=lambda item: (ranking.get(item.classification, 0), item.total_score), reverse=True)
    logger.info(
        "Earnings scan complete: universe=%d, actionable=%d, watch=%d, rejected=%d, errors=%d, opportunities_kept=%d.",
        counts["universe_size"],
        counts["actionable"],
        counts["watch"],
        counts["rejected"],
        counts["errors"],
        min(len(opportunities), config.max_candidates),
    )
    return EarningsScanResult(
        opportunities=opportunities[: config.max_candidates],
        counts=counts,
        errors=errors,
    )
