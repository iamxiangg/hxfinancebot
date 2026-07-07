from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

from funnel.vp_avwap_report import (
    TELEGRAM_BUCKET_BUY_SIGNAL,
    TELEGRAM_BUCKET_OTHER,
    TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE,
    format_telegram_report,
    telegram_execution_bucket,
    telegram_execution_distance_pct,
    telegram_gap_to_trigger_pct,
    telegram_grade,
    telegram_grade_change_text,
    telegram_max_execution_price,
    telegram_route_trigger_text,
)
from scanners.vp_avwap.models import TickerAnalysis, VpAvwapScanResult
from tests.test_vp_avwap_sheet_writer import _scan_result


def _make_result(
    *,
    ticker: str = "AAA",
    google_ticker: str | None = None,
    final_tier: int = 1,
    score: float = 88.0,
    status: str = "CONFIRMED",
    current_price: float | None = 100.0,
    zone_low: float | None = 99.0,
    zone_high: float | None = 100.0,
    entry_trigger: float | None = 100.0,
    route_invalidation: float | None = 98.0,
    next_support_name: str | None = "VAL",
    next_support_price: float | None = 95.0,
    route_code: str = "VAH_DEFENDED_PULLBACK",
    route_metadata: dict | None = None,
    previous_tier: int | None = None,
    tier_change: str = "UNCHANGED",
) -> TickerAnalysis:
    result = copy.deepcopy(_scan_result().results[0])
    result.ticker = ticker
    result.google_ticker = google_ticker if google_ticker is not None else f"NASDAQ:{ticker}"
    result.final_tier = final_tier
    result.raw_score_tier = final_tier
    result.technical_score = score
    result.current_price = current_price
    result.previous_technical_tier = previous_tier
    result.tier_change = tier_change
    result.preferred_route.route_code = route_code
    result.preferred_route.status = status
    result.preferred_route.zone_low = zone_low
    result.preferred_route.zone_high = zone_high
    result.preferred_route.entry_trigger_price = entry_trigger
    result.preferred_route.route_invalidation = route_invalidation
    result.preferred_route.next_support_name = next_support_name
    result.preferred_route.next_support_price = next_support_price
    result.preferred_route.metadata = route_metadata or {}
    return result


def _make_scan(*results: TickerAnalysis, observed_at_utc: str = "2026-07-05T00:00:00Z") -> VpAvwapScanResult:
    return VpAvwapScanResult(
        observed_at_utc=observed_at_utc,
        tickers_requested=len(results),
        processed_tickers=len(results),
        results=list(results),
    )


class ReportTests(unittest.TestCase):
    def test_confirmed_at_trigger_is_buy_signal(self) -> None:
        result = _make_result(current_price=100.0, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_BUY_SIGNAL)
        self.assertEqual(telegram_execution_distance_pct(result), 0.0)

    def test_confirmed_inside_execution_range_is_buy_signal(self) -> None:
        result = _make_result(current_price=101.99, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_BUY_SIGNAL)
        self.assertAlmostEqual(telegram_execution_distance_pct(result), 1.99, places=2)

    def test_confirmed_at_max_execution_range_is_buy_signal(self) -> None:
        result = _make_result(current_price=102.0, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_BUY_SIGNAL)
        self.assertEqual(telegram_max_execution_price(result), 102.0)

    def test_confirmed_above_execution_range_is_other(self) -> None:
        result = _make_result(current_price=102.01, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_OTHER)

    def test_confirmed_below_trigger_is_other(self) -> None:
        result = _make_result(current_price=99.99, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_OTHER)

    def test_testing_is_wait_for_daily_close(self) -> None:
        result = _make_result(status="TESTING", current_price=99.0, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE)
        self.assertAlmostEqual(telegram_gap_to_trigger_pct(result), 1.0)

    def test_approaching_is_other(self) -> None:
        result = _make_result(status="APPROACHING", current_price=99.0, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_OTHER)

    def test_non_grade_a_is_other(self) -> None:
        result = _make_result(final_tier=2, status="CONFIRMED", current_price=100.0, entry_trigger=100.0)
        self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_OTHER)

    def test_non_qualifying_statuses_are_other(self) -> None:
        for status in ("WAITING", "EXTENDED", "FAILED", "INVALID", "DATA_UNAVAILABLE"):
            with self.subTest(status=status):
                result = _make_result(status=status, current_price=100.0, entry_trigger=100.0)
                self.assertEqual(telegram_execution_bucket(result), TELEGRAM_BUCKET_OTHER)

    def test_missing_price_or_trigger_cannot_create_false_buy_signal(self) -> None:
        self.assertEqual(telegram_execution_bucket(_make_result(current_price=None)), TELEGRAM_BUCKET_OTHER)
        self.assertEqual(telegram_execution_bucket(_make_result(entry_trigger=None)), TELEGRAM_BUCKET_OTHER)

    def test_grade_mapping_is_stable(self) -> None:
        self.assertEqual(telegram_grade(1), "Grade A")
        self.assertEqual(telegram_grade(2), "Grade B")
        self.assertEqual(telegram_grade(3), "Grade C")
        self.assertEqual(telegram_grade(4), "Grade D")

    def test_buy_signals_sort_by_execution_distance_then_score_then_ticker(self) -> None:
        near = _make_result(ticker="BBB", current_price=100.20, entry_trigger=100.0, score=70.0)
        tied_high_score = _make_result(ticker="CCC", current_price=101.00, entry_trigger=100.0, score=95.0)
        tied_low_score = _make_result(ticker="AAA", current_price=101.00, entry_trigger=100.0, score=90.0)
        message = format_telegram_report(_make_scan(tied_low_score, near, tied_high_score))
        self.assertLess(message.index("BBB"), message.index("CCC"))
        self.assertLess(message.index("CCC"), message.index("AAA"))

    def test_waiting_setups_sort_by_gap_then_score_then_ticker(self) -> None:
        first = _make_result(ticker="ONON", status="TESTING", score=86.0, current_price=36.83, zone_low=36.58, zone_high=37.31, entry_trigger=37.31, route_invalidation=36.39, next_support_name="Previous Anchor VWAP Close", next_support_price=36.41, route_code="POC_AVWAP_RECOVERY")
        second = _make_result(ticker="HUBB", status="TESTING", score=91.0, current_price=487.10, zone_low=481.56, zone_high=493.67, entry_trigger=493.67, route_invalidation=479.15, next_support_name="VAL", next_support_price=458.17, route_code="POC_AVWAP_RECOVERY")
        tied_gap_high_score = _make_result(ticker="SHOP", status="TESTING", score=82.0, current_price=119.46, zone_low=119.24, zone_high=120.44, entry_trigger=120.44, route_invalidation=118.64, next_support_name="VAH", next_support_price=116.46, route_code="BREAKOUT_RETEST")
        tied_gap_low_score = _make_result(ticker="PINS", status="TESTING", score=80.0, current_price=119.46, zone_low=119.24, zone_high=120.44, entry_trigger=120.44, route_invalidation=118.64, next_support_name="VAH", next_support_price=116.46, route_code="BREAKOUT_RETEST")
        message = format_telegram_report(_make_scan(second, tied_gap_low_score, first, tied_gap_high_score))
        self.assertLess(message.index("SHOP"), message.index("PINS"))
        self.assertLess(message.index("PINS"), message.index("ONON"))
        self.assertLess(message.index("ONON"), message.index("HUBB"))

    def test_total_setup_cap_prioritises_buy_signals_before_waiting(self) -> None:
        buy_1 = _make_result(ticker="A1", current_price=100.0, entry_trigger=100.0)
        buy_2 = _make_result(ticker="A2", current_price=100.5, entry_trigger=100.0)
        buy_3 = _make_result(ticker="A3", current_price=101.0, entry_trigger=100.0)
        wait_1 = _make_result(ticker="W1", status="TESTING", current_price=99.5, entry_trigger=100.0)
        wait_2 = _make_result(ticker="W2", status="TESTING", current_price=99.0, entry_trigger=100.0)
        message = format_telegram_report(_make_scan(buy_1, buy_2, buy_3, wait_1, wait_2))
        self.assertIn("🟢 BUY SIGNALS: 3", message)
        self.assertIn("🟡 WAIT FOR DAILY CLOSE: 2", message)
        self.assertIn("🟡 1. W1", message)
        self.assertNotIn("🟡 2. W2", message)
        self.assertIn("1 additional high-priority setups are available in:", message)

    def test_header_counts_are_calculated_before_truncation(self) -> None:
        results = [
            _make_result(ticker="BUY1", current_price=100.0, entry_trigger=100.0),
            _make_result(ticker="BUY2", current_price=101.0, entry_trigger=100.0),
            _make_result(ticker="WAIT1", status="TESTING", current_price=99.0, entry_trigger=100.0),
            _make_result(ticker="WAIT2", status="TESTING", current_price=98.5, entry_trigger=100.0),
            _make_result(ticker="OTHER1", status="APPROACHING", current_price=99.0, entry_trigger=100.0),
        ]
        message = format_telegram_report(_make_scan(*results))
        self.assertIn("🟢 BUY SIGNALS: 2", message)
        self.assertIn("🟡 WAIT FOR DAILY CLOSE: 2", message)
        self.assertIn("⚪ OTHER WATCHLIST TICKERS: 1", message)

    def test_grade_changes_show_only_improved_and_deteriorated(self) -> None:
        improved = _make_result(ticker="TSLA", route_code="POC_AVWAP_RECOVERY", previous_tier=2, final_tier=1, tier_change="IMPROVED", current_price=100.0, entry_trigger=100.0)
        deteriorated = _make_result(ticker="YOU", route_code="VAL_RECLAIM", previous_tier=1, final_tier=2, tier_change="DETERIORATED", status="TESTING", current_price=99.0, entry_trigger=100.0)
        unchanged_confirmed = _make_result(ticker="DUOL", previous_tier=1, final_tier=1, tier_change="UNCHANGED", current_price=100.0, entry_trigger=100.0)
        new_name = _make_result(ticker="NEW1", previous_tier=None, final_tier=1, tier_change="NEW", current_price=100.0, entry_trigger=100.0)
        message = format_telegram_report(_make_scan(improved, deteriorated, unchanged_confirmed, new_name))
        self.assertIn("GRADE CHANGES", message)
        self.assertIn("TSLA · Grade B -> Grade A · Recover POC/AVWAP", message)
        self.assertIn("YOU · Grade A -> Grade B · Reclaim VAL", message)
        self.assertNotIn("DUOL · Grade A ->", message)
        self.assertNotIn("NEW1 · Grade", message)

    def test_no_grade_changes_omits_section(self) -> None:
        message = format_telegram_report(_make_scan(_make_result(status="TESTING", current_price=99.0, entry_trigger=100.0)))
        self.assertNotIn("GRADE CHANGES", message)

    def test_onon_regression_fixture_appears_only_under_wait_for_daily_close(self) -> None:
        onon = _make_result(
            ticker="ONON",
            google_ticker="ONON",
            final_tier=1,
            score=86.0,
            status="TESTING",
            current_price=36.83,
            zone_low=36.58,
            zone_high=37.31,
            entry_trigger=37.31,
            route_invalidation=36.39,
            next_support_name="Previous Anchor VWAP Close",
            next_support_price=36.41,
            route_code="POC_AVWAP_RECOVERY",
        )
        message = format_telegram_report(_make_scan(onon))
        self.assertIn("🟢 BUY SIGNALS: 0", message)
        self.assertIn("🟡 WAIT FOR DAILY CLOSE: 1", message)
        self.assertIn("🟡 1. ONON · Recover POC/AVWAP · Grade A · Score 86", message)
        self.assertIn("⏳ NO BUY SIGNAL YET", message)
        self.assertIn("Required close: Above $37.31 after recovery", message)
        self.assertIn("Gap to trigger: 1.29%", message)
        self.assertIn("Setup fails on daily close below:", message)
        self.assertNotIn("✅ BUY SIGNAL ACTIVE", message)
        self.assertNotIn("Stop $36.39", message)

    def test_breakout_hold_buy_signal_uses_exact_telegram_wording(self) -> None:
        result = _make_result(
            ticker="FOUR",
            google_ticker="FOUR",
            score=85.0,
            current_price=51.38,
            zone_low=50.37,
            zone_high=50.87,
            entry_trigger=50.87,
            route_invalidation=50.12,
            next_support_name="Previous Anchor VWAP Close",
            next_support_price=46.01,
            route_code="BREAKOUT_RETEST",
            route_metadata={
                "breakout_level": 50.62,
                "breakout_reference_date": "2026-05-07T00:00:00",
                "breakout_confirmation_date": "2026-07-02T00:00:00",
                "breakout_confirmation_close": 51.35,
                "retest_confirmation_date": "2026-07-06T00:00:00",
            },
        )
        message = format_telegram_report(_make_scan(result))
        self.assertIn("Price: $51.38", message)
        self.assertIn("Retest zone: $50.37-$50.87", message)
        self.assertIn("Stored breakout level: $50.62", message)
        self.assertIn("Max execution: $51.89", message)
        self.assertIn("Breakout reference:", message)
        self.assertIn("Prior post-earnings high from 7 May 2026 at $50.62", message)
        self.assertIn("Breakout confirmed:", message)
        self.assertIn("2 Jul 2026 daily close at $51.35 cleared the breakout level", message)
        self.assertIn("Retest confirmed:", message)
        self.assertIn("6 Jul 2026 daily close held the breakout zone and stayed above $50.62", message)
        self.assertNotIn("Entry trigger:", message)
        self.assertNotIn("Buy zone:", message)

    def test_missing_support_and_zone_render_cleanly(self) -> None:
        result = _make_result(
            ticker="NOSUP",
            status="TESTING",
            current_price=99.0,
            zone_low=None,
            zone_high=None,
            entry_trigger=100.0,
            next_support_name=None,
            next_support_price=None,
        )
        message = format_telegram_report(_make_scan(result))
        self.assertIn("Buy zone: N/A", message)
        self.assertIn("Next support:\nNone", message)

    def test_tradingview_layout_override_remains_unchanged(self) -> None:
        result = _make_result(ticker="ZETA", google_ticker="NYSE:ZETA", current_price=100.0, entry_trigger=100.0)
        with patch.dict(os.environ, {"VP_AVWAP_TRADINGVIEW_CHART_ID": "9OmQpc2c"}, clear=False):
            message = format_telegram_report(_make_scan(result))
        self.assertIn("https://www.tradingview.com/chart/9OmQpc2c/?symbol=NYSE%3AZETA", message)

    def test_format_telegram_report_is_deterministic_and_non_mutating(self) -> None:
        result = _make_result(
            ticker="TSLA",
            current_price=100.0,
            entry_trigger=100.0,
            previous_tier=2,
            tier_change="IMPROVED",
        )
        original = copy.deepcopy(result)
        scan = _make_scan(result)
        first = format_telegram_report(scan)
        second = format_telegram_report(scan)
        self.assertEqual(first, second)
        self.assertEqual(result.current_price, original.current_price)
        self.assertEqual(result.final_tier, original.final_tier)
        self.assertEqual(result.preferred_route.status, original.preferred_route.status)
        self.assertEqual(result.technical_score, original.technical_score)

    def test_route_trigger_text_is_route_aware(self) -> None:
        self.assertEqual(
            telegram_route_trigger_text(_make_result(route_code="BREAKOUT_RETEST", entry_trigger=120.44), confirmed=False),
            "Back above $120.44 after retest",
        )
        self.assertEqual(
            telegram_route_trigger_text(_make_result(route_code="VAL_RECLAIM", entry_trigger=53.74), confirmed=True),
            "Daily close confirmed above $53.74 after reclaim.",
        )

    def test_helper_for_grade_change_text_ignores_new_and_unchanged(self) -> None:
        self.assertIsNone(telegram_grade_change_text(_make_result(tier_change="NEW", previous_tier=None)))
        self.assertIsNone(telegram_grade_change_text(_make_result(tier_change="UNCHANGED", previous_tier=1)))


if __name__ == "__main__":
    unittest.main()
