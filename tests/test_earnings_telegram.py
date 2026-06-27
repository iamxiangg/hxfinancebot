from __future__ import annotations

from datetime import date, datetime
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scanners.earnings.models import EarningsOpportunity
from tactical.earnings_telegram import format_exit_reminder, format_screen_report


class EarningsTelegramTests(unittest.TestCase):
    def _opportunity(self, classification: str = "STRONG_ACTIONABLE") -> EarningsOpportunity:
        return EarningsOpportunity(
            ticker="NVDA",
            classification=classification,
            total_score=84,
            earnings_at=datetime(2026, 8, 19, 16, 5, tzinfo=ZoneInfo("America/New_York")),
            earnings_timing="AMC",
            timing_source="earnings_dates",
            spot_price=180.0,
            option_expiry=date(2026, 8, 21),
            days_after_event_to_expiry=2,
            event_purity="HIGH",
            implied_move_pct=0.092,
            implied_move_dollars=16.56,
            historical_event_count=12,
            historical_median_move=0.058,
            historical_mean_move=0.061,
            historical_p75_move=0.074,
            historical_p90_move=0.105,
            historical_max_move=0.14,
            historical_breach_rate=0.17,
            move_richness_median=1.59,
            realised_move_percentile=83,
            richness_score=35,
            reliability_score=18,
            execution_score=16,
            risk_adjustment=15,
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
            risk_flags=["FAT_TAIL_HISTORY"],
            reason="",
            details={},
        )

    @patch.dict(os.environ, {"EARNINGS_SEND_EMPTY_REPORT": "false"}, clear=False)
    def test_empty_report_is_suppressed(self) -> None:
        self.assertIsNone(
            format_screen_report(
                [self._opportunity("REJECTED")],
                now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
            )
        )

    def test_screen_report_and_exit_reminder_formatting(self) -> None:
        report = format_screen_report(
            [self._opportunity()],
            now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York")),
        )
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIn("EARNINGS SHORT-VOLATILITY OPPORTUNITIES", report)
        self.assertIn("Current event pricing appears rich relative to historical realised earnings moves.", report)
        self.assertIn("Estimated midpoint credit", report)

        reminder = format_exit_reminder(
            ticker="NVDA",
            earnings_at=datetime(2026, 8, 19, 16, 5, tzinfo=ZoneInfo("America/New_York")),
            entry_spot_price=180.0,
            current_spot_price=185.2,
            entry_estimated_credit=8.2,
            pre_event_implied_move_pct=0.092,
            close_debit=None,
        )
        self.assertIn("Current option quotes could not be validated.", reminder)
        self.assertIn("This system does not execute trades.", reminder)


if __name__ == "__main__":
    unittest.main()
