from __future__ import annotations

from pathlib import Path
import unittest

from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.models import RouteEvaluation
from scanners.vp_avwap.scoring import apply_tier_overrides, choose_preferred_route, raw_score_tier, score_routes


def _config() -> VpAvwapConfig:
    return VpAvwapConfig(
        test_tickers=[],
        max_tickers=None,
        dry_run=True,
        write_sheets=False,
        send_telegram=False,
        telegram_test_mode=False,
        calibration=False,
        rows=60,
        value_area_pct=70.0,
        primary_interval="30m",
        secondary_interval="60m",
        confluence_pct=1.5,
        zone_buffer_pct=0.5,
        approach_pct=2.0,
        invalidation_buffer_pct=0.5,
        extension_pct=8.0,
        avwap_slope_lookback=5,
        avwap_flat_threshold_pct=0.25,
        falling_override_pct=-0.5,
        breakout_buffer_pct=0.5,
        breakout_retest_window=10,
        output_dir=Path("funnel_output/vp_avwap"),
    )


class ScoringTests(unittest.TestCase):
    def test_score_is_clamped_and_tiered(self) -> None:
        route = RouteEvaluation(
            route_code="POC_AVWAP_RECOVERY",
            route_label="Best technical value",
            eligible=True,
            status="CONFIRMED",
            zone_low=100.0,
            zone_high=101.0,
            advance_alert_price=103.0,
            entry_trigger_price=101.0,
            entry_trigger_condition="Close above 101",
            route_invalidation=98.0,
            next_support_name="VAL",
            next_support_price=96.0,
            distance_to_zone_pct=0.0,
            risk_pct=2.97,
            level_basis=["POC", "AVWAP"],
            metadata={"confluence": True},
        )

        scored = score_routes([route], profile_state_code=3, avwap_slope_pct=1.0, latest_close=101.0)[0]
        self.assertEqual(scored.route_score, 100.0)
        self.assertEqual(raw_score_tier(scored.route_score), 1)

    def test_confirmed_setup_above_zone_loses_price_points(self) -> None:
        route = RouteEvaluation(
            route_code="POC_AVWAP_RECOVERY",
            route_label="Best technical value",
            eligible=True,
            status="CONFIRMED",
            zone_low=100.0,
            zone_high=101.0,
            advance_alert_price=103.0,
            entry_trigger_price=101.0,
            entry_trigger_condition="Close above 101",
            route_invalidation=98.0,
            next_support_name="VAL",
            next_support_price=96.0,
            distance_to_zone_pct=4.95,
            risk_pct=2.97,
            level_basis=["POC", "AVWAP"],
            metadata={"confluence": True},
        )

        scored = score_routes([route], profile_state_code=3, avwap_slope_pct=1.0, latest_close=106.0)[0]
        self.assertEqual(scored.price_points, 10.0)
        self.assertEqual(scored.route_score, 95.0)

    def test_preferred_route_tie_breaker_prefers_vah_route(self) -> None:
        first = RouteEvaluation(
            route_code="VAH_DEFENDED_PULLBACK",
            route_label="A",
            eligible=True,
            status="TESTING",
            zone_low=1.0,
            zone_high=2.0,
            advance_alert_price=2.1,
            entry_trigger_price=2.0,
            entry_trigger_condition="x",
            route_invalidation=1.0,
            next_support_name=None,
            next_support_price=None,
            distance_to_zone_pct=0.0,
            risk_pct=2.0,
            route_score=70.0,
        )
        second = RouteEvaluation(
            route_code="POC_AVWAP_RECOVERY",
            route_label="B",
            eligible=True,
            status="CONFIRMED",
            zone_low=1.0,
            zone_high=2.0,
            advance_alert_price=2.1,
            entry_trigger_price=2.0,
            entry_trigger_condition="x",
            route_invalidation=1.0,
            next_support_name=None,
            next_support_price=None,
            distance_to_zone_pct=0.0,
            risk_pct=2.0,
            route_score=70.0,
        )

        preferred = choose_preferred_route([first, second])
        self.assertEqual(preferred.route_code, "VAH_DEFENDED_PULLBACK")

    def test_low_quality_caps_tier_two(self) -> None:
        route = RouteEvaluation(
            route_code="VAH_DEFENDED_PULLBACK",
            route_label="A",
            eligible=True,
            status="APPROACHING",
            zone_low=95.0,
            zone_high=100.0,
            advance_alert_price=102.0,
            entry_trigger_price=100.0,
            entry_trigger_condition="x",
            route_invalidation=94.0,
            next_support_name="VAL",
            next_support_price=90.0,
            distance_to_zone_pct=1.0,
            risk_pct=4.0,
            route_score=80.0,
        )

        final_tier, hard_override, _reason = apply_tier_overrides(
            preferred_route=route,
            routes=[route],
            raw_tier=1,
            data_quality="LOW",
            latest_close=101.0,
            val=90.0,
            avwap=99.0,
            poc=98.0,
            avwap_slope_pct=0.2,
            status="OK",
            missing_anchor=False,
            config=_config(),
        )

        self.assertEqual(final_tier, 2)
        self.assertTrue(hard_override)

    def test_below_val_without_reclaim_forces_tier_four(self) -> None:
        preferred = RouteEvaluation(
            route_code="VAH_DEFENDED_PULLBACK",
            route_label="A",
            eligible=True,
            status="WAITING",
            zone_low=95.0,
            zone_high=100.0,
            advance_alert_price=102.0,
            entry_trigger_price=100.0,
            entry_trigger_condition="x",
            route_invalidation=94.0,
            next_support_name="VAL",
            next_support_price=90.0,
            distance_to_zone_pct=1.0,
            risk_pct=4.0,
            route_score=80.0,
        )
        val_route = RouteEvaluation(
            route_code="VAL_RECLAIM",
            route_label="D",
            eligible=True,
            status="TESTING",
            zone_low=89.0,
            zone_high=91.0,
            advance_alert_price=92.0,
            entry_trigger_price=91.0,
            entry_trigger_condition="x",
            route_invalidation=88.0,
            next_support_name=None,
            next_support_price=None,
            distance_to_zone_pct=0.5,
            risk_pct=3.0,
            route_score=40.0,
        )

        final_tier, _override, reason = apply_tier_overrides(
            preferred_route=preferred,
            routes=[preferred, val_route],
            raw_tier=1,
            data_quality="HIGH",
            latest_close=89.5,
            val=90.0,
            avwap=99.0,
            poc=98.0,
            avwap_slope_pct=0.2,
            status="OK",
            missing_anchor=False,
            config=_config(),
        )

        self.assertEqual(final_tier, 4)
        self.assertIn("below VAL", reason)


if __name__ == "__main__":
    unittest.main()
