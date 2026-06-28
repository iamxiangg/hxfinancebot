from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
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
from tactical.earnings_telegram import format_candidate_message, format_exit_reminder, format_screen_report, send_telegram_text


logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")


# --- Workstream F1: typed run outcome ---

@dataclass(frozen=True)
class RunOutcome:
    health_status: str  # HEALTHY, DEGRADED, FAILED
    completed: bool
    delivery_required: bool
    delivery_attempted: int
    delivery_succeeded: int
    delivery_failed: int
    critical_error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.critical_error:
            return 1
        if self.health_status == "FAILED":
            return 1
        if self.delivery_required and self.delivery_failed > 0 and self.delivery_succeeded == 0:
            return 1
        return 0


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


def run_screen(*, now_ny: datetime, data_source: YahooEarningsDataSource | None = None) -> RunOutcome:
    result = run_earnings_scan(now_ny=now_ny, data_source=data_source, config=EarningsScannerConfig.from_env())
    state = cleanup_state(load_state(), now_ny=now_ny, retention_days=int(float(os.getenv("EARNINGS_STATE_RETENTION_DAYS", 45))))

    # --- Collect actionable candidates needing delivery ---
    pending_delivery: list[Any] = []
    watch_candidates: list[Any] = []
    for opportunity in result.opportunities:
        key = notification_key(opportunity.ticker, opportunity.details.get("event_date_key", ""), opportunity.earnings_timing)
        if opportunity.classification in {"ACTIONABLE", "STRONG_ACTIONABLE"}:
            if should_send_pre_event(state, key):
                if opportunity.option_expiry is not None and opportunity.short_strike is not None and opportunity.estimated_credit is not None:
                    pending_delivery.append(opportunity)
        elif opportunity.classification in {"WATCH", "MANUAL_CONFIRMATION_REQUIRED"}:
            watch_candidates.append(opportunity)

    delivery_required = len(pending_delivery) > 0
    delivery_attempted = 0
    delivery_succeeded = 0
    delivery_failed = 0

    # --- Per-candidate delivery (Workstream F2) ---
    for opportunity in pending_delivery:
        ticker = opportunity.ticker
        key = notification_key(ticker, opportunity.details.get("event_date_key", ""), opportunity.earnings_timing)
        try:
            message = format_candidate_message(opportunity, now_ny=now_ny)
        except Exception as exc:
            logger.error("Failed to format message for %s: %r", ticker, exc)
            delivery_failed += 1
            continue

        delivery_attempted += 1
        if send_telegram_text(message):
            record_pre_event_notification(
                state,
                key=key,
                classification=opportunity.classification,
                notified_at=now_ny,
                earnings_at=opportunity.earnings_at,
                option_expiry=opportunity.option_expiry.isoformat() if opportunity.option_expiry else "",
                short_strike=opportunity.short_strike or 0.0,
                long_put_strike=float(opportunity.long_put_strike or 0.0),
                long_call_strike=float(opportunity.long_call_strike or 0.0),
                entry_estimated_credit=opportunity.estimated_credit or 0.0,
                entry_spot_price=opportunity.spot_price,
                pre_event_implied_move_pct=opportunity.implied_move_pct,
            )
            delivery_succeeded += 1
            logger.info("Delivered earnings alert for %s", ticker)
        else:
            delivery_failed += 1
            logger.error("Earnings alert delivery failed for %s; will retry on next run.", ticker)

    # --- Optional summary message for watch candidates ---
    summary_report = format_screen_report(watch_candidates, now_ny=now_ny)
    if summary_report:
        try:
            send_telegram_text(summary_report)
        except Exception as exc:
            logger.warning("Watch summary delivery failed (non-fatal): %r", exc)

    save_state(state)
    return RunOutcome(
        health_status="HEALTHY",
        completed=True,
        delivery_required=delivery_required,
        delivery_attempted=delivery_attempted,
        delivery_succeeded=delivery_succeeded,
        delivery_failed=delivery_failed,
    )


def run_exit(*, now_ny: datetime, data_source: YahooEarningsDataSource | None = None) -> RunOutcome:
    source = data_source or YahooEarningsDataSource()
    state = cleanup_state(load_state(), now_ny=now_ny, retention_days=int(float(os.getenv("EARNINGS_STATE_RETENTION_DAYS", 45))))
    attempted = 0
    succeeded = 0
    failed = 0
    required = False
    for key, record in state.items():
        if not should_send_exit(state, key):
            continue
        required = True
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
        attempted += 1
        if send_telegram_text(message):
            mark_exit_notified(state, key=key, notified_at=now_ny)
            succeeded += 1
        else:
            failed += 1
            logger.error("Earnings exit reminder delivery failed for %s; will retry on next run.", ticker)
    save_state(state)
    return RunOutcome(
        health_status="HEALTHY",
        completed=True,
        delivery_required=required,
        delivery_attempted=attempted,
        delivery_succeeded=succeeded,
        delivery_failed=failed,
    )


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
        outcome = run_screen(now_ny=now_ny, data_source=source)
    else:
        outcome = run_exit(now_ny=now_ny, data_source=source)

    logger.info(
        "Run outcome: health=%s delivery=%d/%d failed=%d critical=%s exit=%d",
        outcome.health_status,
        outcome.delivery_succeeded,
        outcome.delivery_attempted,
        outcome.delivery_failed,
        outcome.critical_error or "none",
        outcome.exit_code,
    )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
