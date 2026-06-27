from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from scanners.earnings.models import EarningsOpportunity


logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")


def send_telegram_text(text: str) -> bool:
    token = str(os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        return False
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return bool(payload.get("ok"))


def _pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def _money_per_spread(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.0f}"


def format_screen_report(opportunities: list[EarningsOpportunity], *, now_ny: datetime) -> str | None:
    send_empty = str(os.getenv("EARNINGS_SEND_EMPTY_REPORT", "false")).strip().lower() in {"1", "true", "yes", "on"}
    actionable_like = [
        item
        for item in opportunities
        if item.classification in {"STRONG_ACTIONABLE", "ACTIONABLE", "WATCH"}
    ]
    if not actionable_like and not send_empty:
        return None

    header = [
        "EARNINGS SHORT-VOLATILITY OPPORTUNITIES",
        f"Date: {now_ny.strftime('%d %B %Y')}",
        f"Market time: {now_ny.strftime('%H:%M ET')}",
        "",
    ]
    sections: list[str] = []
    for label in ("STRONG_ACTIONABLE", "ACTIONABLE", "WATCH", "MANUAL_CONFIRMATION_REQUIRED"):
        group = [item for item in opportunities if item.classification == label]
        if not group:
            continue
        sections.append(label.replace("_", " "))
        sections.append("")
        for item in group:
            timing_text = "After close today" if item.earnings_timing == "AMC" else "Before open next session"
            sections.extend(
                [
                    item.ticker,
                    f"- Earnings: {timing_text}",
                    f"- Spot: {_money(item.spot_price)}",
                    f"- Implied move: +/- {_pct(item.implied_move_pct)}",
                    f"- Historical median move: {_pct(item.historical_median_move)}",
                    f"- Historical p75: {_pct(item.historical_p75_move)}",
                    f"- Historical p90: {_pct(item.historical_p90_move)}",
                    f"- Median richness: {item.move_richness_median:.2f}x" if item.move_richness_median is not None else "- Median richness: N/A",
                    f"- Historical breaches: {int(round((item.historical_breach_rate or 0.0) * item.historical_event_count))} of {item.historical_event_count}" if item.historical_breach_rate is not None else f"- Historical breaches: N/A of {item.historical_event_count}",
                    f"- Event purity: {item.event_purity.title()}",
                    f"- Liquidity: {item.liquidity_status.title()}",
                ]
            )
            if item.short_strike is not None:
                sections.extend(
                    [
                        "",
                        "Proposed defined-risk structure:",
                        f"- Sell {item.short_strike:.2f} call and {item.short_strike:.2f} put",
                        f"- Buy {item.long_call_strike:.2f} call and {item.long_put_strike:.2f} put",
                        f"- Estimated midpoint credit: {_money(item.estimated_credit)}",
                        f"- Estimated maximum profit: {_money_per_spread(item.estimated_max_profit)}",
                        f"- Estimated maximum loss: {_money_per_spread(item.estimated_max_loss)}",
                        f"- Estimated breakevens: {_money(item.lower_breakeven)}-{_money(item.upper_breakeven)}",
                    ]
                )
            sections.extend(
                [
                    "",
                    f"Score: {item.total_score:.0f}/100",
                    f"Risk flags: {', '.join(item.risk_flags) if item.risk_flags else 'None'}",
                    "",
                    "Current event pricing appears rich relative to historical realised earnings moves.",
                    "Verify earnings timing and live broker quotes before entering.",
                    "Close after the announcement during the first liquid opening quotes.",
                    "",
                ]
            )
    if not sections:
        return "\n".join(header + ["No actionable or watch candidates."])
    return "\n".join(header + sections)


def format_exit_reminder(
    *,
    ticker: str,
    earnings_at: datetime,
    entry_spot_price: float,
    current_spot_price: float | None,
    entry_estimated_credit: float,
    pre_event_implied_move_pct: float | None,
    close_debit: float | None,
) -> str:
    lines = [
        f"EARNINGS SHORT-VOL EXIT REMINDER - {ticker}",
        "",
        f"Earnings occurred {'after close yesterday' if earnings_at.astimezone(NY_TZ).hour >= 16 else 'before the open today'}.",
        "The planned event-volatility exit window is now open.",
        "",
        "Underlying:",
        f"- Entry spot: {_money(entry_spot_price)}",
        f"- Current spot: {_money(current_spot_price)}",
        f"- Actual move: {_pct((current_spot_price / entry_spot_price - 1.0) if current_spot_price not in (None, 0.0) and entry_spot_price > 0 else None)}",
        f"- Pre-event implied move: +/- {_pct(pre_event_implied_move_pct)}",
        "",
        "Position:",
        f"- Entry estimated midpoint credit: {_money(entry_estimated_credit)}",
    ]
    if close_debit is None:
        lines.extend(
            [
                "- Current option quotes could not be validated.",
                "Check the broker and close the position manually.",
            ]
        )
    else:
        estimated_profit = (entry_estimated_credit - close_debit) * 100.0
        lines.extend(
            [
                f"- Current conservative close estimate: {_money(close_debit)}",
                f"- Estimated profit: {_money_per_spread(estimated_profit)} per spread",
            ]
        )
    lines.extend(["", "Action:", "Check live broker quotes and close the position now.", "", "This system does not execute trades."])
    return "\n".join(lines)
