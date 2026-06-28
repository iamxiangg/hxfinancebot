from __future__ import annotations

from datetime import date, datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scanners.earnings.models import EarningsOpportunity, EarningsScanResult
from tactical.earnings_runner import run_exit, run_screen
from tactical.earnings_state import load_state, notification_key, record_pre_event_notification, save_state


NY = ZoneInfo("America/New_York")


def make_opportunity(ticker: str, *, classification: str = "ACTIONABLE", event_date_key: str = "2026-08-19") -> EarningsOpportunity:
    return EarningsOpportunity(
        ticker=ticker,
        classification=classification,
        total_score=80.0,
        earnings_at=datetime(2026, 8, 19, 16, 5, tzinfo=NY),
        earnings_timing="AMC",
        timing_source="earnings_dates",
        spot_price=180.0,
        option_expiry=date(2026, 8, 21),
        days_after_event_to_expiry=2,
        event_purity="HIGH",
        implied_move_pct=0.09,
        implied_move_dollars=16.2,
        historical_event_count=12,
        historical_median_move=0.05,
        historical_mean_move=0.05,
        historical_p75_move=0.07,
        historical_p90_move=0.10,
        historical_max_move=0.13,
        historical_breach_rate=0.15,
        move_richness_median=1.5,
        realised_move_percentile=82,
        richness_score=35,
        reliability_score=18,
        execution_score=16,
        risk_adjustment=14,
        short_strike=180.0,
        long_put_strike=162.5,
        long_call_strike=197.5,
        estimated_credit=8.2,
        estimated_max_profit=820,
        estimated_max_loss=930,
        lower_breakeven=171.8,
        upper_breakeven=188.2,
        liquidity_status="GOOD",
        data_confidence="HIGH",
        risk_flags=[],
        reason="",
        details={"event_date_key": event_date_key},
    )


class _FakeExitDataSource:
    def spot_price(self, ticker: str) -> float:
        return 185.0

    def option_chain(self, ticker: str, expiry: date):
        raise RuntimeError("no quotes")


class EarningsRunnerTests(unittest.TestCase):
    def test_successful_delivery_records_pre_event_notification(self) -> None:
        opportunity = make_opportunity("NVDA")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([opportunity], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=True
        ), patch("tactical.earnings_runner.format_screen_report", return_value="report"):
            outcome = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            state = load_state()

        key = notification_key("NVDA", "2026-08-19", "AMC")
        self.assertEqual(outcome.delivery_succeeded, 1)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn(key, state)
        self.assertTrue(state[key]["pre_event_notified_at"])

    def test_failed_delivery_leaves_pre_event_notification_empty(self) -> None:
        opportunity = make_opportunity("NVDA")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([opportunity], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=False
        ), patch("tactical.earnings_runner.format_screen_report", return_value="report"):
            outcome = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            state = load_state()

        self.assertEqual(outcome.delivery_failed, 1)
        self.assertEqual(outcome.delivery_succeeded, 0)
        self.assertEqual(state, {})

    def test_failed_candidate_is_retried_on_next_run(self) -> None:
        opportunity = make_opportunity("NVDA")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([opportunity], counts={})), patch(
            "tactical.earnings_runner.format_screen_report", return_value="report"
        ):
            with patch("tactical.earnings_runner.send_telegram_text", return_value=False):
                first = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            with patch("tactical.earnings_runner.send_telegram_text", return_value=True):
                second = run_screen(now_ny=datetime(2026, 8, 19, 14, 40, tzinfo=NY))
            state = load_state()

        key = notification_key("NVDA", "2026-08-19", "AMC")
        self.assertEqual(first.delivery_failed, 1)
        self.assertEqual(first.delivery_succeeded, 0)
        self.assertEqual(second.delivery_succeeded, 1)
        self.assertIn(key, state)

    def test_per_candidate_delivery_success_and_failure(self) -> None:
        """Per-candidate delivery: first candidate succeeds and is persisted, second candidate fails and is retryable."""
        first_opportunity = make_opportunity("NVDA")
        second_opportunity = make_opportunity("MSFT")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json"),
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
            },
            clear=False,
        ), patch(
            "tactical.earnings_runner.run_earnings_scan",
            return_value=EarningsScanResult([first_opportunity, second_opportunity], counts={}),
        ), patch(
            "tactical.earnings_runner.send_telegram_text"
        ) as mock_send:
            # First candidate succeeds, second fails
            mock_send.side_effect = [True, False]

            first = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            first_state = load_state()

        # NVDA delivered successfully, MSFT failed → retryable
        self.assertEqual(first.delivery_succeeded, 1)
        self.assertEqual(first.delivery_failed, 1)
        nvda_key = notification_key("NVDA", "2026-08-19", "AMC")
        msft_key = notification_key("MSFT", "2026-08-19", "AMC")
        self.assertIn(nvda_key, first_state)
        self.assertNotIn(msft_key, first_state)

        # On next run, MSFT is retried and now succeeds
        with tempfile.TemporaryDirectory() as temp_dir2, patch.dict(
            "os.environ",
            {
                "EARNINGS_STATE_PATH": str(Path(temp_dir2) / "state.json"),
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
            },
            clear=False,
        ), patch(
            "tactical.earnings_runner.run_earnings_scan",
            return_value=EarningsScanResult([second_opportunity], counts={}),
        ), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=True
        ):
            second = run_screen(now_ny=datetime(2026, 8, 19, 14, 40, tzinfo=NY))

        self.assertEqual(second.delivery_succeeded, 1)
        self.assertEqual(second.delivery_failed, 0)

    def test_already_delivered_actionable_candidates_excluded_from_normal_report(self) -> None:
        delivered = make_opportunity("NVDA")
        pending = make_opportunity("MSFT", classification="STRONG_ACTIONABLE")
        watch = make_opportunity("AAPL", classification="WATCH")
        now = datetime(2026, 8, 19, 14, 35, tzinfo=NY)
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([delivered, pending, watch], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=True
        ) as mock_send, patch("tactical.earnings_runner.format_screen_report", return_value="report") as mock_format:
            state = {}
            record_pre_event_notification(
                state,
                key=notification_key("NVDA", "2026-08-19", "AMC"),
                classification="ACTIONABLE",
                notified_at=now,
                earnings_at=now,
                option_expiry="2026-08-21",
                short_strike=180.0,
                long_put_strike=162.5,
                long_call_strike=197.5,
                entry_estimated_credit=8.2,
                entry_spot_price=180.0,
                pre_event_implied_move_pct=0.09,
            )
            save_state(state)
            run_screen(now_ny=now)
            # format_screen_report now only receives watch/non-actionable candidates (AAPL)
            report_opportunities = mock_format.call_args.args[0]

        self.assertEqual([item.ticker for item in report_opportunities], ["AAPL"])

    def test_only_newly_delivered_actionable_candidates_are_added_to_state(self) -> None:
        delivered = make_opportunity("NVDA")
        pending = make_opportunity("MSFT", classification="STRONG_ACTIONABLE")
        watch = make_opportunity("AAPL", classification="WATCH")
        now = datetime(2026, 8, 19, 14, 35, tzinfo=NY)
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([delivered, pending, watch], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=True
        ), patch("tactical.earnings_runner.format_screen_report", return_value="report"):
            state = {}
            record_pre_event_notification(
                state,
                key=notification_key("NVDA", "2026-08-19", "AMC"),
                classification="ACTIONABLE",
                notified_at=now,
                earnings_at=now,
                option_expiry="2026-08-21",
                short_strike=180.0,
                long_put_strike=162.5,
                long_call_strike=197.5,
                entry_estimated_credit=8.2,
                entry_spot_price=180.0,
                pre_event_implied_move_pct=0.09,
            )
            save_state(state)
            run_screen(now_ny=now)
            saved = load_state()

        self.assertIn(notification_key("NVDA", "2026-08-19", "AMC"), saved)
        self.assertIn(notification_key("MSFT", "2026-08-19", "AMC"), saved)
        self.assertNotIn(notification_key("AAPL", "2026-08-19", "AMC"), saved)

    def test_watch_candidates_do_not_create_exit_state(self) -> None:
        watch = make_opportunity("AAPL", classification="WATCH")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([watch], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=True
        ), patch("tactical.earnings_runner.format_screen_report", return_value="report"):
            run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            state = load_state()

        self.assertEqual(state, {})

    def test_state_cleanup_still_runs_during_failed_delivery(self) -> None:
        stale_now = datetime(2026, 8, 19, 14, 35, tzinfo=NY)
        stale_earnings = stale_now.replace(year=2026, month=6, day=1)
        opportunity = make_opportunity("NVDA")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.run_earnings_scan", return_value=EarningsScanResult([opportunity], counts={})), patch(
            "tactical.earnings_runner.send_telegram_text", return_value=False
        ), patch("tactical.earnings_runner.format_screen_report", return_value="report"):
            save_state({"OLD|2026-06-01|AMC": {"earnings_at": stale_earnings.isoformat(), "pre_event_notified_at": stale_earnings.isoformat()}})
            run_screen(now_ny=stale_now)
            state = load_state()

        self.assertEqual(state, {})

    def test_exit_reminder_marks_state_only_after_success(self) -> None:
        now = datetime(2026, 8, 20, 9, 40, tzinfo=NY)
        key = notification_key("NVDA", "2026-08-19", "AMC")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.send_telegram_text", return_value=False), patch(
            "tactical.earnings_runner.format_exit_reminder", return_value="exit"
        ):
            state = {}
            record_pre_event_notification(
                state,
                key=key,
                classification="ACTIONABLE",
                notified_at=datetime(2026, 8, 19, 14, 35, tzinfo=NY),
                earnings_at=datetime(2026, 8, 19, 16, 5, tzinfo=NY),
                option_expiry="2026-08-21",
                short_strike=180.0,
                long_put_strike=162.5,
                long_call_strike=197.5,
                entry_estimated_credit=8.2,
                entry_spot_price=180.0,
                pre_event_implied_move_pct=0.09,
            )
            save_state(state)
            outcome = run_exit(now_ny=now, data_source=_FakeExitDataSource())
            saved = load_state()

        self.assertEqual(outcome.delivery_succeeded, 0)
        self.assertEqual(outcome.delivery_failed, 1)
        self.assertFalse(saved[key].get("exit_notified_at"))

    def test_no_duplicate_exit_reminder_is_sent(self) -> None:
        now = datetime(2026, 8, 20, 9, 40, tzinfo=NY)
        key = notification_key("NVDA", "2026-08-19", "AMC")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"EARNINGS_STATE_PATH": str(Path(temp_dir) / "state.json")},
            clear=False,
        ), patch("tactical.earnings_runner.send_telegram_text", return_value=True) as mock_send, patch(
            "tactical.earnings_runner.format_exit_reminder", return_value="exit"
        ):
            state = {}
            record_pre_event_notification(
                state,
                key=key,
                classification="ACTIONABLE",
                notified_at=datetime(2026, 8, 19, 14, 35, tzinfo=NY),
                earnings_at=datetime(2026, 8, 19, 16, 5, tzinfo=NY),
                option_expiry="2026-08-21",
                short_strike=180.0,
                long_put_strike=162.5,
                long_call_strike=197.5,
                entry_estimated_credit=8.2,
                entry_spot_price=180.0,
                pre_event_implied_move_pct=0.09,
            )
            save_state(state)
            first = run_exit(now_ny=now, data_source=_FakeExitDataSource())
            second = run_exit(now_ny=now, data_source=_FakeExitDataSource())

        self.assertEqual(first.delivery_succeeded, 1)
        self.assertEqual(second.delivery_succeeded, 0)
        self.assertEqual(mock_send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
