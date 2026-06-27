from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class HistoricalEventMove:
    event_date: date
    timing: str
    pre_event_close: float
    post_event_open: float
    absolute_event_move: float
    close_to_close_move: float
    maximum_first_session_excursion: float


@dataclass(frozen=True)
class HistoricalMoveSummary:
    usable_event_count: int
    median_absolute_move: float | None
    mean_absolute_move: float | None
    recent_eight_event_mean: float | None
    p75_move: float | None
    p90_move: float | None
    maximum_move: float | None
    standard_deviation: float | None


@dataclass(frozen=True)
class EarningsEventInfo:
    ticker: str
    earnings_at: datetime
    earnings_timing: str
    timing_source: str
    timing_reason: str
    entry_session_date: date | None
    event_session_date: date
    exit_session_date: date | None
    event_date_key: str


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    bid: float
    ask: float
    midpoint: float
    volume: int
    open_interest: int
    spread_pct: float


@dataclass(frozen=True)
class IronButterflyStructure:
    short_strike: float
    long_put_strike: float
    long_call_strike: float
    short_call: OptionQuote
    short_put: OptionQuote
    long_call: OptionQuote
    long_put: OptionQuote
    estimated_credit: float
    estimated_max_profit: float
    estimated_max_loss: float
    lower_breakeven: float
    upper_breakeven: float
    call_width: float
    put_width: float
    liquidity_status: str


@dataclass
class EarningsOpportunity:
    ticker: str
    classification: str
    total_score: float

    earnings_at: datetime
    earnings_timing: str
    timing_source: str

    spot_price: float
    option_expiry: date | None
    days_after_event_to_expiry: int | None
    event_purity: str

    implied_move_pct: float | None
    implied_move_dollars: float | None

    historical_event_count: int
    historical_median_move: float | None
    historical_mean_move: float | None
    historical_p75_move: float | None
    historical_p90_move: float | None
    historical_max_move: float | None
    historical_breach_rate: float | None

    move_richness_median: float | None
    realised_move_percentile: float | None

    richness_score: float
    reliability_score: float
    execution_score: float
    risk_adjustment: float

    short_strike: float | None
    long_put_strike: float | None
    long_call_strike: float | None

    estimated_credit: float | None
    estimated_max_profit: float | None
    estimated_max_loss: float | None
    lower_breakeven: float | None
    upper_breakeven: float | None

    liquidity_status: str
    data_confidence: str
    risk_flags: list[str]
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EarningsScanResult:
    opportunities: list[EarningsOpportunity]
    counts: dict[str, int]
    errors: list[str] = field(default_factory=list)

