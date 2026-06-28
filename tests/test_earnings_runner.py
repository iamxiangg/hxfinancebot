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
from tactical.earnings_telegram import _split_telegram_text


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
            count = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            state = load_state()

        key = notification_key("NVDA", "2026-08-19", "AMC")
        self.assertEqual(count, 1)
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
            count = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            state = load_state()

        self.assertEqual(count, 0)
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
        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        self.assertIn(key, state)

    def test_failed_multi_chunk_delivery_leaves_all_candidates_retryable(self) -> None:
        first_opportunity = make_opportunity("NVDA")
        second_opportunity = make_opportunity("MSFT")
        long_report = "\n".join([f"Line {index} {'x' * 200}" for index in range(60)])
        chunk_count = len(_split_telegram_text(long_report))
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
        ), patch("tactical.earnings_runner.format_screen_report", return_value=long_report), patch(
            "tactical.earnings_telegram.requests.post"
        ) as mock_post:
            first_response = unittest.mock.Mock()
            first_response.raise_for_status.return_value = None
            first_response.json.return_value = {"ok": True}
            second_response = unittest.mock.Mock()
            second_response.raise_for_status.side_effect = RuntimeError("bad chunk")
            mock_post.side_effect = [first_response, second_response]

            first = run_screen(now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=NY))
            self.assertEqual(load_state(), {})

            successful_responses = []
            for _ in range(chunk_count):
                response = unittest.mock.Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {"ok": True}
                successful_responses.append(response)
            mock_post.reset_mock()
            mock_post.side_effect = successful_responses

            second = run_screen(now_ny=datetime(2026, 8, 19, 14, 40, tzinfo=NY))
            state = load_state()

        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        self.assertIn(notification_key("NVDA", "2026-08-19", "AMC"), state)
        self.assertIn(notification_key("MSFT", "2026-08-19", "AMC"), state)

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
            report_opportunities = mock_format.call_args.kwargs["now_ny"], mock_format.call_args.args[0]

        sent_opportunities = report_opportunities[1]
        self.assertEqual([item.ticker for item in sent_opportunities], ["MSFT", "AAPL"])
        mock_send.assert_called_once()

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
            count = run_exit(now_ny=now, data_source=_FakeExitDataSource())
            saved = load_state()

        self.assertEqual(count, 0)
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

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(mock_send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
