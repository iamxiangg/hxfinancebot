from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


ROUTE_PRIORITY = {
    "VAH_DEFENDED_PULLBACK": 0,
    "POC_AVWAP_RECOVERY": 1,
    "BREAKOUT_RETEST": 2,
    "VAL_RECLAIM": 3,
}

STATUS_PRIORITY = {
    "CONFIRMED": 0,
    "TESTING": 1,
    "APPROACHING": 2,
    "WAITING": 3,
    "EXTENDED": 4,
    "FAILED": 5,
    "INVALID": 6,
    "DATA_UNAVAILABLE": 7,
}


@dataclass(frozen=True)
class TickerRecord:
    ticker: str
    google_ticker: str = ""
    stock_name: str = ""
    sheet_row: int | None = None


@dataclass(frozen=True)
class EarningsAnchor:
    earnings_timestamp: datetime
    release_timing: str
    reaction_session: pd.Timestamp
    reaction_session_confidence: str


@dataclass(frozen=True)
class EarningsAnchorSelection:
    current: EarningsAnchor | None
    previous: EarningsAnchor | None
    reason: str | None = None


@dataclass(frozen=True)
class LevelReference:
    name: str
    price: float


@dataclass
class VolumeProfileResult:
    profile_low: float | None
    profile_high: float | None
    row_width: float | None
    row_boundaries: list[tuple[float, float]]
    allocated_row_volumes: list[float]
    poc: float | None
    vah: float | None
    val: float | None
    total_source_volume: float
    total_allocated_volume: float
    included_value_area_volume: float
    actual_value_area_percentage: float | None
    bar_count: int
    interval_used: str
    data_quality: str
    status: str = "OK"
    reason: str | None = None


@dataclass
class AvwapResult:
    current_avwap: float | None
    avwap_series: pd.Series
    end_of_session_snapshots: dict[str, float]
    five_session_slope_pct: float | None
    previous_anchor_vwap_close: float | None
    status: str = "OK"
    reason: str | None = None


@dataclass
class RouteEvaluation:
    route_code: str
    route_label: str
    eligible: bool
    status: str
    zone_low: float | None
    zone_high: float | None
    advance_alert_price: float | None
    entry_trigger_price: float | None
    entry_trigger_condition: str
    route_invalidation: float | None
    next_support_name: str | None
    next_support_price: float | None
    distance_to_zone_pct: float | None
    risk_pct: float | None
    route_score: float = 0.0
    structure_points: float = 0.0
    confluence_points: float = 0.0
    readiness_points: float = 0.0
    price_points: float = 0.0
    risk_points: float = 0.0
    reason: str = ""
    supporting_levels: list[str] = field(default_factory=list)
    error: str = ""
    level_basis: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickerAnalysis:
    ticker: str
    google_ticker: str
    stock_name: str
    current_price: float | None
    technical_score: float
    raw_score_tier: int
    final_tier: int
    profile_state: str
    profile_state_code: int | None
    earnings_timestamp: datetime | None
    earnings_reaction_session: pd.Timestamp | None
    earnings_release_timing: str | None
    anchor_confidence: str | None
    previous_earnings_timestamp: datetime | None
    previous_reaction_session: pd.Timestamp | None
    avwap: float | None
    poc: float | None
    vah: float | None
    val: float | None
    previous_anchor_vwap_close: float | None
    avwap_five_session_slope_pct: float | None
    close_vs_avwap_pct: float | None
    close_vs_poc_pct: float | None
    close_vs_vah_pct: float | None
    close_vs_val_pct: float | None
    profile_high: float | None
    profile_low: float | None
    number_of_profile_rows: int
    value_area_target_pct: float
    actual_value_area_pct: float | None
    source_bars: int
    data_interval_used: str
    data_quality: str
    hard_override: bool
    hard_override_reason: str
    preferred_route: RouteEvaluation
    routes: list[RouteEvaluation]
    technical_reason: str
    calculation_version: str
    status: str = "OK"
    error: str = ""
    rank_within_tier: int | None = None
    overall_technical_rank: int | None = None
    previous_technical_tier: int | None = None
    tier_change: str = "NEW"
    calibration: dict[str, Any] = field(default_factory=dict)


@dataclass
class VpAvwapScanResult:
    observed_at_utc: str
    tickers_requested: int
    processed_tickers: int
    results: list[TickerAnalysis]
    errors: list[str] = field(default_factory=list)
