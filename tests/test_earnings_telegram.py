from __future__ import annotations

from datetime import date, datetime
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scanners.earnings.models import EarningsOpportunity
from tactical.earnings_telegram import _split_telegram_text, format_candidate_message, format_exit_reminder, format_screen_report, send_telegram_text


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

    def test_multi_chunk_report_marks_delivery_only_when_all_chunks_succeed(self) -> None:
        long_text = "\n".join([f"Line {index} {'x' * 200}" for index in range(60)])
        chunks = _split_telegram_text(long_text)
        self.assertGreater(len(chunks), 1)

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=False), patch(
            "tactical.earnings_telegram.requests.post"
        ) as mock_post:
            responses = []
            for _ in chunks:
                response = unittest.mock.Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {"ok": True}
                responses.append(response)
            mock_post.side_effect = responses
            self.assertTrue(send_telegram_text(long_text))
            self.assertEqual(mock_post.call_count, len(chunks))

    def test_failed_chunk_causes_overall_delivery_failure(self) -> None:
        long_text = "\n".join([f"Line {index} {'x' * 200}" for index in range(60)])
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=False), patch(
            "tactical.earnings_telegram.requests.post"
        ) as mock_post:
            first = unittest.mock.Mock()
            first.raise_for_status.return_value = None
            first.json.return_value = {"ok": True}
            second = unittest.mock.Mock()
            second.raise_for_status.side_effect = RuntimeError("bad chunk")
            mock_post.side_effect = [first, second]
            self.assertFalse(send_telegram_text(long_text))
            self.assertEqual(mock_post.call_count, 2)


class CandidateMessageTests(unittest.TestCase):
    """Tests for the per-candidate alert formatter (Follow-up F2)."""

    NY = ZoneInfo("America/New_York")

    def _opportunity(self, **overrides: object) -> EarningsOpportunity:
        kwargs: dict[str, object] = {
            "ticker": "NVDA",
            "classification": "ACTIONABLE",
            "total_score": 80.0,
            "earnings_at": datetime(2026, 8, 19, 16, 5, tzinfo=self.NY),
            "earnings_timing": "AMC",
            "timing_source": "earnings_dates",
            "spot_price": 180.0,
            "option_expiry": date(2026, 8, 21),
            "days_after_event_to_expiry": 2,
            "event_purity": "HIGH",
            "implied_move_pct": 0.09,
            "implied_move_dollars": 16.2,
            "historical_event_count": 12,
            "historical_median_move": 0.05,
            "historical_mean_move": 0.05,
            "historical_p75_move": 0.07,
            "historical_p90_move": 0.10,
            "historical_max_move": 0.13,
            "historical_breach_rate": 0.15,
            "move_richness_median": 1.5,
            "realised_move_percentile": 82,
            "richness_score": 35,
            "reliability_score": 18,
            "execution_score": 16,
            "risk_adjustment": 14,
            "short_strike": 180.0,
            "long_put_strike": 162.5,
            "long_call_strike": 197.5,
            "estimated_credit": 8.2,
            "estimated_max_profit": 820,
            "estimated_max_loss": 930,
            "lower_breakeven": 171.8,
            "upper_breakeven": 188.2,
            "liquidity_status": "GOOD",
            "data_confidence": "HIGH",
            "risk_flags": [],
            "reason": "",
            "details": {},
        }
        kwargs.update(overrides)
        return EarningsOpportunity(**kwargs)  # type: ignore[arg-type]

    def test_contains_ticker_and_classification(self) -> None:
        msg = format_candidate_message(self._opportunity(), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("NVDA", msg)
        self.assertIn("ACTIONABLE", msg)  # classification case preserved, underscores replaced with space

    def test_contains_earnings_timing_amc(self) -> None:
        msg = format_candidate_message(self._opportunity(earnings_timing="AMC"), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("After close today", msg)

    def test_contains_earnings_timing_bmo(self) -> None:
        msg = format_candidate_message(self._opportunity(earnings_timing="BMO"), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("Before open next session", msg)

    def test_contains_spot_and_move_metrics(self) -> None:
        msg = format_candidate_message(self._opportunity(), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("$180.00", msg)
        self.assertIn("9.0%", msg)  # implied move
        self.assertIn("5.0%", msg)  # historical median
        self.assertIn("7.0%", msg)  # historical p75

    def test_contains_richness_metric(self) -> None:
        msg = format_candidate_message(self._opportunity(move_richness_median=2.15), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("2.15x", msg)

    def test_omits_richness_when_none(self) -> None:
        msg = format_candidate_message(self._opportunity(move_richness_median=None), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertNotIn("richness", msg.lower())

    def test_contains_option_structure(self) -> None:
        msg = format_candidate_message(self._opportunity(), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("Proposed defined-risk structure", msg)
        self.assertIn("$8.20", msg)  # estimated credit
        self.assertIn("$820", msg)   # max profit
        self.assertIn("$930", msg)   # max loss

    def test_contains_risk_flags(self) -> None:
        msg = format_candidate_message(self._opportunity(risk_flags=["FAT_TAIL_HISTORY", "LOW_LIQUIDITY"]), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("FAT_TAIL_HISTORY", msg)
        self.assertIn("LOW_LIQUIDITY", msg)

    def test_risk_flags_none(self) -> None:
        msg = format_candidate_message(self._opportunity(risk_flags=[]), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("None", msg)

    def test_contains_disclaimer(self) -> None:
        msg = format_candidate_message(self._opportunity(), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("This system does not execute trades.", msg)

    def test_no_option_structure_when_no_strikes(self) -> None:
        msg = format_candidate_message(
            self._opportunity(short_strike=None, long_put_strike=None, long_call_strike=None, estimated_credit=None),
            now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY),
        )
        self.assertNotIn("Proposed defined-risk structure", msg)

    def test_breach_rate_displayed(self) -> None:
        msg = format_candidate_message(self._opportunity(historical_breach_rate=0.25, historical_event_count=12), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("3 of 12", msg)

    def test_breach_rate_omitted_when_none(self) -> None:
        msg = format_candidate_message(self._opportunity(historical_breach_rate=None), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertNotIn("breaches", msg.lower())

    def test_score_displayed(self) -> None:
        msg = format_candidate_message(self._opportunity(total_score=78.0), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertIn("78/100", msg)

    def test_msg_fits_in_single_telegram_chunk(self) -> None:
        msg = format_candidate_message(self._opportunity(), now_ny=datetime(2026, 8, 19, 14, 35, tzinfo=self.NY))
        self.assertLess(len(msg), 3800)


if __name__ == "__main__":
    unittest.main()
