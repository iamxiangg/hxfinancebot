from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import MagicMock

import pandas as pd

from funnel.vp_avwap_sheet_writer import (
    ENTRY_MAP_SHEET,
    SUMMARY_SHEET,
    apply_previous_tiers,
    build_summary_records,
    read_previous_tiers,
    write_vp_avwap_sheets,
)
from scanners.vp_avwap.models import RouteEvaluation, TickerAnalysis, VpAvwapScanResult


def _scan_result() -> VpAvwapScanResult:
    route = RouteEvaluation(
        route_code="VAH_DEFENDED_PULLBACK",
        route_label="Best balance of price and trend",
        eligible=True,
        status="APPROACHING",
        zone_low=99.0,
        zone_high=100.0,
        advance_alert_price=102.0,
        entry_trigger_price=100.0,
        entry_trigger_condition="Close above 100",
        route_invalidation=98.0,
        next_support_name="VAL",
        next_support_price=95.0,
        distance_to_zone_pct=1.0,
        risk_pct=2.0,
        route_score=88.0,
    )
    result = TickerAnalysis(
        ticker="AAA",
        google_ticker="NASDAQ:AAA",
        stock_name="Alpha",
        current_price=101.0,
        technical_score=88.0,
        raw_score_tier=1,
        final_tier=1,
        profile_state="ABOVE_VAH",
        profile_state_code=3,
        earnings_timestamp=datetime(2026, 6, 5, 8, 0),
        earnings_reaction_session=pd.Timestamp("2026-06-05"),
        earnings_release_timing="before_market",
        anchor_confidence="high",
        previous_earnings_timestamp=datetime(2026, 5, 1, 16, 5),
        previous_reaction_session=pd.Timestamp("2026-05-02"),
        avwap=100.0,
        poc=99.5,
        vah=100.0,
        val=95.0,
        previous_anchor_vwap_close=96.0,
        avwap_five_session_slope_pct=0.6,
        close_vs_avwap_pct=1.0,
        close_vs_poc_pct=1.5,
        close_vs_vah_pct=1.0,
        close_vs_val_pct=6.3,
        profile_high=102.0,
        profile_low=94.0,
        number_of_profile_rows=60,
        value_area_target_pct=70.0,
        actual_value_area_pct=71.0,
        source_bars=10,
        data_interval_used="30m",
        data_quality="HIGH",
        hard_override=False,
        hard_override_reason="",
        preferred_route=route,
        routes=[route],
        technical_reason="Constructive post-earnings structure.",
        calculation_version="2026-07-vp-avwap-v1",
        overall_technical_rank=1,
        rank_within_tier=1,
    )
    return VpAvwapScanResult(
        observed_at_utc="2026-07-03T23:30:00Z",
        tickers_requested=1,
        processed_tickers=1,
        results=[result],
    )


class SheetWriterTests(unittest.TestCase):
    def test_previous_tier_lookup_and_tier_change(self) -> None:
        scan = _scan_result()
        apply_previous_tiers(scan.results, {"AAA": 2})

        self.assertEqual(scan.results[0].previous_technical_tier, 2)
        self.assertEqual(scan.results[0].tier_change, "IMPROVED")

    def test_read_previous_tiers_parses_existing_summary(self) -> None:
        service = MagicMock()
        service.spreadsheets().values().get().execute.return_value = {
            "values": [
                build_summary_records(_scan_result()) and list(build_summary_records(_scan_result())[0].keys()),
                list(build_summary_records(_scan_result())[0].values()),
            ]
        }

        tiers = read_previous_tiers(service=service, spreadsheet_id="sid")

        self.assertEqual(tiers["AAA"], 1)

    def test_dry_run_performs_no_writes(self) -> None:
        service = MagicMock()

        result = write_vp_avwap_sheets(_scan_result(), service=service, spreadsheet_id="sid", dry_run=True)

        self.assertTrue(result["dry_run"])
        service.spreadsheets().values().clear.assert_not_called()

    def test_only_target_sheets_are_written(self) -> None:
        service = MagicMock()
        service.spreadsheets().get().execute.side_effect = [
            {"sheets": [{"properties": {"title": SUMMARY_SHEET}}, {"properties": {"title": ENTRY_MAP_SHEET}}]},
            {"sheets": [{"properties": {"title": SUMMARY_SHEET}}, {"properties": {"title": ENTRY_MAP_SHEET}}]},
            {"sheets": [{"properties": {"title": SUMMARY_SHEET, "sheetId": 1}}, {"properties": {"title": ENTRY_MAP_SHEET, "sheetId": 2}}]},
        ]
        service.spreadsheets().values().get().execute.return_value = {"values": [[]]}
        service.spreadsheets().batchUpdate.return_value.execute.return_value = {}
        service.spreadsheets().values().batchUpdate.return_value.execute.return_value = {}
        service.spreadsheets().values().clear.return_value.execute.return_value = {}

        write_vp_avwap_sheets(_scan_result(), service=service, spreadsheet_id="sid", dry_run=False)

        clear_calls = [call.kwargs["range"] for call in service.spreadsheets().values().clear.call_args_list]
        self.assertTrue(all(SUMMARY_SHEET in call or ENTRY_MAP_SHEET in call for call in clear_calls))


if __name__ == "__main__":
    unittest.main()
