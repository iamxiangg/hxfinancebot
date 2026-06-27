from __future__ import annotations

from datetime import date, datetime
import unittest

from scanners.earnings.models import EarningsOpportunity, HistoricalMoveSummary
from scanners.earnings.scoring import (
    build_risk_flags,
    calculate_richness_metrics,
    classify_opportunity,
    event_richness_score,
    execution_quality_score,
    historical_reliability_score,
    risk_adjustment_score,
)


class EarningsScoringTests(unittest.TestCase):
    def _opportunity(self, **overrides) -> EarningsOpportunity:
        base = EarningsOpportunity(
            ticker="NVDA",
            classification="REJECTED",
            total_score=82,
            earnings_at=datetime(2026, 8, 19, 16, 5),
            earnings_timing="AMC",
            timing_source="earnings_dates",
            spot_price=180,
            option_expiry=date(2026, 8, 21),
            days_after_event_to_expiry=2,
            event_purity="HIGH",
            implied_move_pct=0.112,
            implied_move_dollars=20.16,
            historical_event_count=12,
            historical_median_move=0.058,
            historical_mean_move=0.061,
            historical_p75_move=0.074,
            historical_p90_move=0.105,
            historical_max_move=0.14,
            historical_breach_rate=0.17,
            move_richness_median=1.93,
            realised_move_percentile=83,
            richness_score=33,
            reliability_score=18,
            execution_score=16,
            risk_adjustment=2,
            short_strike=180,
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
            details={},
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_richness_metrics_and_score_across_thresholds(self) -> None:
        summary = HistoricalMoveSummary(12, 0.05, 0.06, 0.06, 0.07, 0.10, 0.14, 0.02)
        metrics = calculate_richness_metrics(0.08, summary, 85)
        self.assertAlmostEqual(metrics["move_richness_median"], 1.6)
        self.assertEqual(event_richness_score(metrics), 40.0)

    def test_historical_reliability_and_fat_tail_flags(self) -> None:
        summary = HistoricalMoveSummary(12, 0.05, 0.06, 0.06, 0.07, 0.10, 0.16, 0.06)
        score = historical_reliability_score(summary, implied_move_pct=0.08, breach_rate=0.08)
        flags = build_risk_flags(
            summary,
            implied_move_pct=0.08,
            earnings_timing="AMC",
            event_purity="HIGH",
            sector="Technology",
            industry="Software",
            info={},
        )
        self.assertGreaterEqual(score, 15.0)
        self.assertIn("FAT_TAIL_HISTORY", flags)

    def test_execution_quality_and_watch_classification(self) -> None:
        execution = execution_quality_score(liquidity_status="ACCEPTABLE", event_purity="MEDIUM", structure_valid=True)
        self.assertGreaterEqual(execution, 12.0)
        opportunity = self._opportunity(total_score=62, liquidity_status="ACCEPTABLE", event_purity="MEDIUM")
        self.assertEqual(classify_opportunity(opportunity, entry_session_is_today=False, structure_valid=True), "WATCH")

    def test_strong_actionable_and_unknown_timing(self) -> None:
        self.assertEqual(classify_opportunity(self._opportunity(), entry_session_is_today=True, structure_valid=True), "STRONG_ACTIONABLE")
        unknown = self._opportunity(earnings_timing="UNKNOWN")
        self.assertEqual(classify_opportunity(unknown, entry_session_is_today=True, structure_valid=True), "MANUAL_CONFIRMATION_REQUIRED")

    def test_risk_adjustment_penalises_low_purity(self) -> None:
        adjustment = risk_adjustment_score(["CURRENT_IMPLIED_BELOW_P75", "UNSTABLE_EVENT_DISTRIBUTION"], event_purity="LOW", data_confidence="LOW")
        self.assertLess(adjustment, 0)


if __name__ == "__main__":
    unittest.main()
