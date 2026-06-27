from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from scanners.earnings.engine import EarningsScannerConfig, run_earnings_scan
from scanners.earnings.market_data import YahooEarningsDataSource
from scanners.earnings.pricing import conservative_exit_debit
from tactical.earnings_state import (
    cleanup_state,
    load_state,
    mark_exit_notified,
    notification_key,
    record_pre_event_notification,
    save_state,
    should_send_exit,
    should_send_pre_event,
)
from tactical.earnings_telegram import format_exit_reminder, format_screen_report, send_telegram_text


logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")


def _parse_time(raw: str, default: str) -> time:
    text = str(raw or default).strip()
    hour_text, minute_text = text.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


def _screen_window() -> tuple[time, time]:
    start = _parse_time(os.getenv("EARNINGS_SCREEN_EARLIEST_TIME", "14:30"), "14:30")
    end = _parse_time(os.getenv("EARNINGS_SCREEN_LATEST_TIME", "15:00"), "15:00")
    return start, end


def _exit_window() -> tuple[time, time]:
    start = _parse_time(os.getenv("EARNINGS_EXIT_EARLIEST_TIME", "09:35"), "09:35")
    end = _parse_time(os.getenv("EARNINGS_EXIT_LATEST_TIME", "09:50"), "09:50")
    return start, end


def _inside_window(current: datetime, window: tuple[time, time]) -> bool:
    return window[0] <= current.time().replace(second=0, microsecond=0) <= window[1]


def resolve_mode(requested_mode: str, *, now_ny: datetime) -> str | None:
    mode = requested_mode.lower()
    if mode in {"screen", "exit"}:
        return mode
    if mode != "auto":
        raise ValueError(f"Unsupported mode: {requested_mode}")
    if _inside_window(now_ny, _exit_window()):
        return "exit"
    if _inside_window(now_ny, _screen_window()):
        return "screen"
    return None


def run_screen(*, now_ny: datetime, data_source: YahooEarningsDataSource | None = None) -> int:
    result = run_earnings_scan(now_ny=now_ny, data_source=data_source, config=EarningsScannerConfig.from_env())
    state = cleanup_state(load_state(), now_ny=now_ny, retention_days=int(float(os.getenv("EARNINGS_STATE_RETENTION_DAYS", 45))))
    pending_delivery: list[Any] = []
    for opportunity in result.opportunities:
        key = notification_key(opportunity.ticker, opportunity.details.get("event_date_key", ""), opportunity.earnings_timing)
        if opportunity.classification not in {"ACTIONABLE", "STRONG_ACTIONABLE"}:
            continue
        if not should_send_pre_event(state, key):
            continue
        if opportunity.option_expiry is None or opportunity.short_strike is None or opportunity.estimated_credit is None:
            continue
        pending_delivery.append(opportunity)

    report_opportunities = []
    for opportunity in result.opportunities:
        key = notification_key(opportunity.ticker, opportunity.details.get("event_date_key", ""), opportunity.earnings_timing)
        if opportunity.classification in {"ACTIONABLE", "STRONG_ACTIONABLE"}:
            if not any(item is opportunity for item in pending_delivery):
                continue
        elif opportunity.classification == "WATCH":
            pass
        elif opportunity.classification == "MANUAL_CONFIRMATION_REQUIRED":
            pass
        else:
            continue
        report_opportunities.append(opportunity)

    report = format_screen_report(report_opportunities, now_ny=now_ny)
    if report is None:
        save_state(state)
        return 0

    sent = send_telegram_text(report)
    if sent:
        for opportunity in pending_delivery:
            key = notification_key(opportunity.ticker, opportunity.details.get("event_date_key", ""), opportunity.earnings_timing)
            record_pre_event_notification(
                state,
                key=key,
                classification=opportunity.classification,
                notified_at=now_ny,
                earnings_at=opportunity.earnings_at,
                option_expiry=opportunity.option_expiry.isoformat(),
                short_strike=opportunity.short_strike,
                long_put_strike=float(opportunity.long_put_strike or 0.0),
                long_call_strike=float(opportunity.long_call_strike or 0.0),
                entry_estimated_credit=opportunity.estimated_credit,
                entry_spot_price=opportunity.spot_price,
                pre_event_implied_move_pct=opportunity.implied_move_pct,
            )
    else:
        logger.error("Earnings screen report delivery failed; leaving opportunities retryable.")
    save_state(state)
    return 1 if sent else 0


def run_exit(*, now_ny: datetime, data_source: YahooEarningsDataSource | None = None) -> int:
    source = data_source or YahooEarningsDataSource()
    state = cleanup_state(load_state(), now_ny=now_ny, retention_days=int(float(os.getenv("EARNINGS_STATE_RETENTION_DAYS", 45))))
    sent_count = 0
    for key, record in state.items():
        if not should_send_exit(state, key):
            continue
        try:
            earnings_at = datetime.fromisoformat(str(record.get("earnings_at")).replace("Z", "+00:00")).astimezone(NY_TZ)
        except ValueError:
            continue
        if now_ny < earnings_at:
            continue
        ticker = str(key).split("|", 1)[0]
        current_spot = source.spot_price(ticker)
        close_debit = None
        option_expiry = record.get("option_expiry")
        if option_expiry:
            try:
                calls, puts = source.option_chain(ticker, datetime.fromisoformat(f"{option_expiry}T00:00:00").date())
                close_debit = conservative_exit_debit(
                    calls,
                    puts,
                    short_strike=float(record.get("short_strike")),
                    long_put_strike=float(record.get("long_put_strike")),
                    long_call_strike=float(record.get("long_call_strike")),
                )
            except Exception:
                close_debit = None
        message = format_exit_reminder(
            ticker=ticker,
            earnings_at=earnings_at,
            entry_spot_price=float(record.get("entry_spot_price") or 0.0),
            current_spot_price=current_spot,
            entry_estimated_credit=float(record.get("entry_estimated_credit") or 0.0),
            pre_event_implied_move_pct=record.get("pre_event_implied_move_pct"),
            close_debit=close_debit,
        )
        if send_telegram_text(message):
            mark_exit_notified(state, key=key, notified_at=now_ny)
            sent_count += 1
        else:
            logger.error("Earnings exit reminder delivery failed for %s; will retry on next run.", ticker)
    save_state(state)
    return sent_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Earnings short-volatility scanner")
    parser.add_argument("--mode", default="auto", choices=["screen", "exit", "auto"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now_ny = datetime.now(NY_TZ)
    resolved = resolve_mode(args.mode, now_ny=now_ny)
    if resolved is None:
        logger.info("No earnings task window is open. Exiting cleanly.")
        return 0

    source = YahooEarningsDataSource(request_delay_seconds=float(os.getenv("EARNINGS_REQUEST_DELAY_SECONDS", 0.25)))
    if resolved == "screen":
        run_screen(now_ny=now_ny, data_source=source)
    else:
        run_exit(now_ny=now_ny, data_source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
