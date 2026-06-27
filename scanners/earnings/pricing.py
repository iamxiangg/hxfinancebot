from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from scanners.earnings.models import IronButterflyStructure, OptionQuote


def _to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


@dataclass(frozen=True)
class LiquidityThresholds:
    min_leg_open_interest: int
    min_leg_volume: int
    max_leg_spread_pct: float
    max_total_spread_pct: float


def select_post_event_expiry(expirations: list[date], earnings_at: datetime, earnings_timing: str) -> date | None:
    if earnings_timing == "AMC":
        valid = [expiry for expiry in expirations if expiry > earnings_at.date()]
    else:
        valid = [expiry for expiry in expirations if expiry >= earnings_at.date()]
    return valid[0] if valid else None


def classify_event_purity(days_after_event_to_expiry: int | None) -> str:
    if days_after_event_to_expiry is None:
        return "LOW"
    if days_after_event_to_expiry <= 3:
        return "HIGH"
    if days_after_event_to_expiry <= 7:
        return "MEDIUM"
    return "LOW"


def _quote_from_row(row: pd.Series) -> OptionQuote | None:
    bid = _to_float(row.get("bid"))
    ask = _to_float(row.get("ask"))
    strike = _to_float(row.get("strike"))
    if bid is None or ask is None or strike is None or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
    return OptionQuote(
        strike=strike,
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        volume=int(_to_float(row.get("volume")) or 0),
        open_interest=int(_to_float(row.get("openInterest")) or 0),
        spread_pct=spread_pct,
    )


def find_atm_straddle(calls: pd.DataFrame, puts: pd.DataFrame, spot_price: float) -> tuple[OptionQuote, OptionQuote] | None:
    if calls.empty or puts.empty:
        return None
    call_quotes: dict[float, OptionQuote] = {}
    for _, row in calls.iterrows():
        quote = _quote_from_row(row)
        if quote is not None:
            call_quotes[quote.strike] = quote
    put_quotes: dict[float, OptionQuote] = {}
    for _, row in puts.iterrows():
        quote = _quote_from_row(row)
        if quote is not None:
            put_quotes[quote.strike] = quote
    common = sorted(set(call_quotes).intersection(put_quotes))
    if not common:
        return None
    strike = min(common, key=lambda value: abs(value - spot_price))
    return call_quotes[strike], put_quotes[strike]


def calculate_implied_move(spot_price: float, short_call: OptionQuote, short_put: OptionQuote) -> tuple[float, float]:
    straddle_mid = short_call.midpoint + short_put.midpoint
    return straddle_mid / spot_price, straddle_mid


def _nearest_strike(strikes: list[float], target: float, *, greater_than: float | None = None, less_than: float | None = None) -> float | None:
    filtered = strikes
    if greater_than is not None:
        filtered = [strike for strike in filtered if strike > greater_than]
    if less_than is not None:
        filtered = [strike for strike in filtered if strike < less_than]
    if not filtered:
        return None
    return min(filtered, key=lambda value: abs(value - target))


def assess_liquidity(quotes: list[OptionQuote], thresholds: LiquidityThresholds) -> str:
    if not quotes:
        return "POOR"
    total_midpoint = sum(quote.midpoint for quote in quotes)
    total_spread = sum(quote.ask - quote.bid for quote in quotes)
    if total_midpoint <= 0:
        return "POOR"
    total_spread_pct = total_spread / total_midpoint
    leg_good = all(
        quote.open_interest >= thresholds.min_leg_open_interest
        and quote.volume >= thresholds.min_leg_volume
        and quote.spread_pct <= thresholds.max_leg_spread_pct
        for quote in quotes
    )
    if leg_good and total_spread_pct <= thresholds.max_total_spread_pct:
        return "GOOD"
    leg_acceptable = all(
        quote.open_interest >= max(1, thresholds.min_leg_open_interest // 2)
        and quote.volume >= max(1, thresholds.min_leg_volume // 2)
        and quote.spread_pct <= thresholds.max_leg_spread_pct * 1.35
        for quote in quotes
    )
    if leg_acceptable and total_spread_pct <= thresholds.max_total_spread_pct * 1.35:
        return "ACCEPTABLE"
    return "POOR"


def build_iron_butterfly(
    *,
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    short_call: OptionQuote,
    short_put: OptionQuote,
    implied_move_dollars: float,
    thresholds: LiquidityThresholds,
) -> IronButterflyStructure | None:
    short_strike = short_call.strike
    call_quotes: dict[float, OptionQuote] = {}
    put_quotes: dict[float, OptionQuote] = {}
    for _, row in calls.iterrows():
        quote = _quote_from_row(row)
        if quote is not None:
            call_quotes[quote.strike] = quote
    for _, row in puts.iterrows():
        quote = _quote_from_row(row)
        if quote is not None:
            put_quotes[quote.strike] = quote

    long_call_target = short_strike + implied_move_dollars
    long_put_target = short_strike - implied_move_dollars
    long_call_strike = _nearest_strike(sorted(call_quotes), long_call_target, greater_than=short_strike)
    long_put_strike = _nearest_strike(sorted(put_quotes), long_put_target, less_than=short_strike)
    if long_call_strike is None or long_put_strike is None:
        return None

    long_call = call_quotes[long_call_strike]
    long_put = put_quotes[long_put_strike]
    quotes = [short_call, short_put, long_call, long_put]
    liquidity_status = assess_liquidity(quotes, thresholds)

    net_credit = short_call.midpoint + short_put.midpoint - long_call.midpoint - long_put.midpoint
    call_width = long_call.strike - short_strike
    put_width = short_strike - long_put.strike
    if net_credit <= 0 or call_width <= 0 or put_width <= 0:
        return None
    max_call_loss = call_width - net_credit
    max_put_loss = put_width - net_credit
    max_loss = max(max_call_loss, max_put_loss)
    if max_loss <= 0:
        return None

    return IronButterflyStructure(
        short_strike=short_strike,
        long_put_strike=long_put.strike,
        long_call_strike=long_call.strike,
        short_call=short_call,
        short_put=short_put,
        long_call=long_call,
        long_put=long_put,
        estimated_credit=net_credit,
        estimated_max_profit=net_credit * 100.0,
        estimated_max_loss=max_loss * 100.0,
        lower_breakeven=short_strike - net_credit,
        upper_breakeven=short_strike + net_credit,
        call_width=call_width,
        put_width=put_width,
        liquidity_status=liquidity_status,
    )


def conservative_exit_debit(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    *,
    short_strike: float,
    long_put_strike: float,
    long_call_strike: float,
) -> float | None:
    def quote_lookup(frame: pd.DataFrame, strike: float) -> OptionQuote | None:
        rows = frame[frame["strike"] == strike]
        if rows.empty:
            return None
        return _quote_from_row(rows.iloc[0])

    short_call = quote_lookup(calls, short_strike)
    short_put = quote_lookup(puts, short_strike)
    long_call = quote_lookup(calls, long_call_strike)
    long_put = quote_lookup(puts, long_put_strike)
    if None in {short_call, short_put, long_call, long_put}:
        return None
    assert short_call is not None and short_put is not None and long_call is not None and long_put is not None
    debit = short_call.ask + short_put.ask - long_call.bid - long_put.bid
    return debit if debit >= 0 else None
