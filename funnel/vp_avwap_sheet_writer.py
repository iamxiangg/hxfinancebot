from __future__ import annotations

import logging
import math
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.sheet_table import column_letter, ensure_sheet
from scanners.vp_avwap.models import RouteEvaluation, TickerAnalysis, VpAvwapScanResult


logger = logging.getLogger(__name__)

SUMMARY_SHEET = "VP_AVWAP_Tiers"
ENTRY_MAP_SHEET = "VP_AVWAP_Entry_Map"
PROTECTED_SHEET = "Stock Summary USD"

SUMMARY_HEADERS = [
    "Run Timestamp UTC",
    "Overall Technical Rank",
    "Rank Within Tier",
    "Technical Tier",
    "Raw Score Tier",
    "Previous Technical Tier",
    "Tier Change",
    "Ticker",
    "Google Ticker",
    "Stock Name",
    "Current Price",
    "Technical Score",
    "Preferred Entry Route",
    "Preferred Route Label",
    "Setup Status",
    "Buy Zone Low",
    "Buy Zone High",
    "Advance Alert Price",
    "Entry Trigger Price",
    "Entry Trigger Condition",
    "Route Invalidation",
    "Next Support Name",
    "Next Support Price",
    "Distance to Buy Zone %",
    "Risk %",
    "Profile State",
    "Earnings Timestamp",
    "Earnings Reaction Session",
    "Earnings Release Timing",
    "Anchor Confidence",
    "AVWAP",
    "POC",
    "VAH",
    "VAL",
    "Previous Anchor VWAP Close",
    "AVWAP Five-Session Slope %",
    "Close vs AVWAP %",
    "Close vs POC %",
    "Close vs VAH %",
    "Close vs VAL %",
    "Profile High",
    "Profile Low",
    "Number of Profile Rows",
    "Value Area Target %",
    "Actual Value Area %",
    "Source Bars",
    "Data Interval Used",
    "Data Quality",
    "Hard Override",
    "Hard Override Reason",
    "Technical Reason",
    "Calculation Version",
    "Error",
]

ENTRY_MAP_HEADERS = [
    "Run Timestamp UTC",
    "Ticker",
    "Stock Name",
    "Final Technical Tier",
    "Preferred Route?",
    "Route Code",
    "Route Label",
    "Eligible",
    "Status",
    "Route Score",
    "Zone Low",
    "Zone High",
    "Advance Alert Price",
    "Entry Trigger Price",
    "Entry Trigger Condition",
    "Route Invalidation",
    "Next Support Name",
    "Next Support Price",
    "Distance to Zone %",
    "Risk %",
    "Structure Points",
    "Confluence Points",
    "Readiness Points",
    "Price Points",
    "Risk Points",
    "Supporting Levels",
    "Reason",
    "Data Quality",
    "Calculation Version",
    "Error",
]


def _sheet_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _timestamp(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _read_existing_rows(service, spreadsheet_id: str, sheet_name: str, headers: list[str]) -> list[list[Any]]:
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A:{column_letter(len(headers))}",
            majorDimension="ROWS",
        )
        .execute()
    )
    return response.get("values", [])


def read_previous_tiers(service=None, spreadsheet_id: str | None = None) -> dict[str, int]:
    service = service or get_sheets_service(readonly=True)
    actual_spreadsheet_id = spreadsheet_id or get_spreadsheet_id()
    try:
        rows = _read_existing_rows(service, actual_spreadsheet_id, SUMMARY_SHEET, SUMMARY_HEADERS)
    except Exception:
        return {}
    if not rows:
        return {}
    header = [str(cell).strip() for cell in rows[0]]
    if header[: len(SUMMARY_HEADERS)] != SUMMARY_HEADERS:
        return {}
    output: dict[str, int] = {}
    for row in rows[1:]:
        padded = list(row) + [""] * (len(SUMMARY_HEADERS) - len(row))
        ticker = str(padded[7]).strip().upper()
        if not ticker:
            continue
        try:
            output[ticker] = int(float(padded[3]))
        except (TypeError, ValueError):
            continue
    return output


def apply_previous_tiers(results: list[TickerAnalysis], previous_tiers: dict[str, int]) -> list[TickerAnalysis]:
    for result in results:
        previous = previous_tiers.get(result.ticker)
        result.previous_technical_tier = previous
        if previous is None:
            result.tier_change = "NEW"
        elif result.final_tier < previous:
            result.tier_change = "IMPROVED"
        elif result.final_tier > previous:
            result.tier_change = "DETERIORATED"
        else:
            result.tier_change = "UNCHANGED"
    return results


def _summary_record(scan_result: VpAvwapScanResult, result: TickerAnalysis) -> dict[str, Any]:
    route = result.preferred_route
    return {
        "Run Timestamp UTC": scan_result.observed_at_utc,
        "Overall Technical Rank": _sheet_value(result.overall_technical_rank),
        "Rank Within Tier": _sheet_value(result.rank_within_tier),
        "Technical Tier": result.final_tier,
        "Raw Score Tier": result.raw_score_tier,
        "Previous Technical Tier": _sheet_value(result.previous_technical_tier),
        "Tier Change": result.tier_change,
        "Ticker": result.ticker,
        "Google Ticker": result.google_ticker,
        "Stock Name": result.stock_name,
        "Current Price": _sheet_value(result.current_price),
        "Technical Score": round(result.technical_score, 2),
        "Preferred Entry Route": route.route_code,
        "Preferred Route Label": route.route_label,
        "Setup Status": route.status,
        "Buy Zone Low": _sheet_value(route.zone_low),
        "Buy Zone High": _sheet_value(route.zone_high),
        "Advance Alert Price": _sheet_value(route.advance_alert_price),
        "Entry Trigger Price": _sheet_value(route.entry_trigger_price),
        "Entry Trigger Condition": route.entry_trigger_condition,
        "Route Invalidation": _sheet_value(route.route_invalidation),
        "Next Support Name": _sheet_value(route.next_support_name),
        "Next Support Price": _sheet_value(route.next_support_price),
        "Distance to Buy Zone %": _sheet_value(route.distance_to_zone_pct),
        "Risk %": _sheet_value(route.risk_pct),
        "Profile State": result.profile_state,
        "Earnings Timestamp": _timestamp(result.earnings_timestamp),
        "Earnings Reaction Session": _timestamp(result.earnings_reaction_session),
        "Earnings Release Timing": _sheet_value(result.earnings_release_timing),
        "Anchor Confidence": _sheet_value(result.anchor_confidence),
        "AVWAP": _sheet_value(result.avwap),
        "POC": _sheet_value(result.poc),
        "VAH": _sheet_value(result.vah),
        "VAL": _sheet_value(result.val),
        "Previous Anchor VWAP Close": _sheet_value(result.previous_anchor_vwap_close),
        "AVWAP Five-Session Slope %": _sheet_value(result.avwap_five_session_slope_pct),
        "Close vs AVWAP %": _sheet_value(result.close_vs_avwap_pct),
        "Close vs POC %": _sheet_value(result.close_vs_poc_pct),
        "Close vs VAH %": _sheet_value(result.close_vs_vah_pct),
        "Close vs VAL %": _sheet_value(result.close_vs_val_pct),
        "Profile High": _sheet_value(result.profile_high),
        "Profile Low": _sheet_value(result.profile_low),
        "Number of Profile Rows": result.number_of_profile_rows,
        "Value Area Target %": result.value_area_target_pct,
        "Actual Value Area %": _sheet_value(result.actual_value_area_pct),
        "Source Bars": result.source_bars,
        "Data Interval Used": result.data_interval_used,
        "Data Quality": result.data_quality,
        "Hard Override": "YES" if result.hard_override else "NO",
        "Hard Override Reason": result.hard_override_reason,
        "Technical Reason": result.technical_reason,
        "Calculation Version": result.calculation_version,
        "Error": result.error,
    }


def _entry_map_record(scan_result: VpAvwapScanResult, result: TickerAnalysis, route: RouteEvaluation) -> dict[str, Any]:
    return {
        "Run Timestamp UTC": scan_result.observed_at_utc,
        "Ticker": result.ticker,
        "Stock Name": result.stock_name,
        "Final Technical Tier": result.final_tier,
        "Preferred Route?": "YES" if route.route_code == result.preferred_route.route_code else "NO",
        "Route Code": route.route_code,
        "Route Label": route.route_label,
        "Eligible": "YES" if route.eligible else "NO",
        "Status": route.status,
        "Route Score": round(route.route_score, 2),
        "Zone Low": _sheet_value(route.zone_low),
        "Zone High": _sheet_value(route.zone_high),
        "Advance Alert Price": _sheet_value(route.advance_alert_price),
        "Entry Trigger Price": _sheet_value(route.entry_trigger_price),
        "Entry Trigger Condition": route.entry_trigger_condition,
        "Route Invalidation": _sheet_value(route.route_invalidation),
        "Next Support Name": _sheet_value(route.next_support_name),
        "Next Support Price": _sheet_value(route.next_support_price),
        "Distance to Zone %": _sheet_value(route.distance_to_zone_pct),
        "Risk %": _sheet_value(route.risk_pct),
        "Structure Points": route.structure_points,
        "Confluence Points": route.confluence_points,
        "Readiness Points": route.readiness_points,
        "Price Points": route.price_points,
        "Risk Points": route.risk_points,
        "Supporting Levels": ", ".join(route.supporting_levels),
        "Reason": route.reason,
        "Data Quality": result.data_quality,
        "Calculation Version": result.calculation_version,
        "Error": route.error,
    }


def build_summary_records(scan_result: VpAvwapScanResult) -> list[dict[str, Any]]:
    return [_summary_record(scan_result, result) for result in scan_result.results]


def build_entry_map_records(scan_result: VpAvwapScanResult) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for result in scan_result.results:
        for route in result.routes:
            records.append(_entry_map_record(scan_result, result, route))
    return records


def _rows_from_records(records: list[dict[str, Any]], headers: list[str]) -> list[list[Any]]:
    return [[record.get(header, "") for header in headers] for record in records]


def _ensure_target_sheets(service, spreadsheet_id: str) -> None:
    if SUMMARY_SHEET == PROTECTED_SHEET or ENTRY_MAP_SHEET == PROTECTED_SHEET:
        raise RuntimeError("VP/AVWAP writer is forbidden from writing to Stock Summary USD")
    ensure_sheet(service, spreadsheet_id, SUMMARY_SHEET, SUMMARY_HEADERS, rows=1000)
    ensure_sheet(service, spreadsheet_id, ENTRY_MAP_SHEET, ENTRY_MAP_HEADERS, rows=2000)


def _sheet_ids(service, spreadsheet_id: str) -> dict[str, int]:
    metadata = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)")
        .execute()
    )
    return {
        sheet["properties"]["title"]: int(sheet["properties"]["sheetId"])
        for sheet in metadata.get("sheets", [])
    }


def _format_requests(sheet_id: int, *, column_count: int, tier_column_index: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(textFormat,wrapStrategy)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    }
                }
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": column_count,
                }
            }
        },
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": tier_column_index,
                            "endColumnIndex": tier_column_index + 1,
                        }
                    ],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_EQ", "values": [{"userEnteredValue": "1"}]},
                        "format": {"backgroundColor": {"red": 0.88, "green": 0.95, "blue": 0.87}},
                    },
                },
                "index": 0,
            }
        },
    ]
    return requests


def write_vp_avwap_sheets(
    scan_result: VpAvwapScanResult,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    service = service or get_sheets_service(readonly=False)
    actual_spreadsheet_id = spreadsheet_id or get_spreadsheet_id()
    if dry_run:
        return {
            "summary_records": len(scan_result.results),
            "entry_records": sum(len(result.routes) for result in scan_result.results),
            "dry_run": True,
        }
    _ensure_target_sheets(service, actual_spreadsheet_id)
    summary_records = build_summary_records(scan_result)
    entry_records = build_entry_map_records(scan_result)
    summary_rows = [SUMMARY_HEADERS] + _rows_from_records(summary_records, SUMMARY_HEADERS)
    entry_rows = [ENTRY_MAP_HEADERS] + _rows_from_records(entry_records, ENTRY_MAP_HEADERS)
    service.spreadsheets().values().clear(
        spreadsheetId=actual_spreadsheet_id,
        range=f"'{SUMMARY_SHEET}'!A:{column_letter(len(SUMMARY_HEADERS))}",
        body={},
    ).execute()
    service.spreadsheets().values().clear(
        spreadsheetId=actual_spreadsheet_id,
        range=f"'{ENTRY_MAP_SHEET}'!A:{column_letter(len(ENTRY_MAP_HEADERS))}",
        body={},
    ).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=actual_spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"'{SUMMARY_SHEET}'!A1", "values": summary_rows},
                {"range": f"'{ENTRY_MAP_SHEET}'!A1", "values": entry_rows},
            ],
        },
    ).execute()
    sheet_ids = _sheet_ids(service, actual_spreadsheet_id)
    requests = []
    requests.extend(_format_requests(sheet_ids[SUMMARY_SHEET], column_count=len(SUMMARY_HEADERS), tier_column_index=3))
    requests.extend(_format_requests(sheet_ids[ENTRY_MAP_SHEET], column_count=len(ENTRY_MAP_HEADERS), tier_column_index=3))
    service.spreadsheets().batchUpdate(
        spreadsheetId=actual_spreadsheet_id,
        body={"requests": requests},
    ).execute()
    logger.info("VP/AVWAP worksheets updated; Stock Summary USD was not modified")
    return {
        "summary_records": len(summary_records),
        "entry_records": len(entry_records),
        "dry_run": False,
    }
