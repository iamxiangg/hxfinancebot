from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from funnel.vp_avwap_sheet_writer import build_entry_map_records, build_summary_records
from scanners.vp_avwap.models import TickerAnalysis, VpAvwapScanResult


TIER_LABELS = {
    1: "Actionable or nearly actionable",
    2: "Attractive, but not ready",
    3: "Watch only",
    4: "No valid entry",
}


def _money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _clean_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    if isinstance(value, tuple):
        return [_clean_json(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def format_grouped_report(scan_result: VpAvwapScanResult) -> str:
    lines = ["VP/AVWAP TECHNICAL TIERS", ""]
    for tier in (1, 2, 3, 4):
        lines.append(f"Tier {tier} - {TIER_LABELS[tier]}")
        group = [result for result in scan_result.results if result.final_tier == tier]
        if not group:
            lines.append("None")
            lines.append("")
            continue
        for result in group:
            lines.append(
                f"{result.ticker} - {result.technical_score:.0f} - {result.preferred_route.route_label} - {result.preferred_route.status}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def format_detailed_entry_map(result: TickerAnalysis) -> str:
    route = result.preferred_route
    lines = [
        f"{result.ticker} - TECHNICAL TIER {result.final_tier}",
        "",
        "Preferred route:",
        f"{route.route_label} - {route.route_code}",
        "",
        "Current price:",
        _money(result.current_price),
        "",
        "Buy zone:",
        f"{_money(route.zone_low)}-{_money(route.zone_high)}",
        "",
        "Advance alert:",
        _money(route.advance_alert_price),
        "",
        "Entry condition:",
        route.entry_trigger_condition,
        "",
        "Route invalidation:",
        f"Daily close below {_money(route.route_invalidation)}.",
        "",
        "Next support:",
        f"{route.next_support_name or 'None'} at {_money(route.next_support_price)}.",
        "",
        "Status:",
        route.status,
        "",
        "Score:",
        f"{result.technical_score:.0f}/100",
        "",
        "Reason:",
        result.technical_reason or route.reason,
    ]
    return "\n".join(lines)


def detailed_results(scan_result: VpAvwapScanResult) -> list[TickerAnalysis]:
    tier_one = [result for result in scan_result.results if result.final_tier == 1]
    top_tier_two = [result for result in scan_result.results if result.final_tier == 2][:5]
    improved = [result for result in scan_result.results if result.tier_change == "IMPROVED"]
    newly_confirmed = [result for result in scan_result.results if result.preferred_route.status == "CONFIRMED"]
    ordered: list[TickerAnalysis] = []
    seen: set[str] = set()
    for result in tier_one + top_tier_two + improved + newly_confirmed:
        if result.ticker in seen:
            continue
        seen.add(result.ticker)
        ordered.append(result)
    return ordered


def write_local_artifacts(scan_result: VpAvwapScanResult, *, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_records = build_summary_records(scan_result)
    entry_records = build_entry_map_records(scan_result)
    metadata = {
        "observed_at_utc": scan_result.observed_at_utc,
        "tickers_requested": scan_result.tickers_requested,
        "processed_tickers": scan_result.processed_tickers,
        "errors": scan_result.errors,
        "grouped_report": format_grouped_report(scan_result),
        "detailed_tickers": [result.ticker for result in detailed_results(scan_result)],
        "calibration": [_clean_json(result.calibration) for result in scan_result.results],
    }
    summary_json = output_dir / "latest_summary.json"
    summary_csv = output_dir / "latest_summary.csv"
    entry_json = output_dir / "latest_entry_map.json"
    entry_csv = output_dir / "latest_entry_map.csv"
    metadata_json = output_dir / "latest_run_metadata.json"
    summary_json.write_text(json.dumps(_clean_json(summary_records), indent=2), encoding="utf-8")
    entry_json.write_text(json.dumps(_clean_json(entry_records), indent=2), encoding="utf-8")
    metadata_json.write_text(json.dumps(_clean_json(metadata), indent=2), encoding="utf-8")
    with summary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_records[0].keys()) if summary_records else [])
        if summary_records:
            writer.writeheader()
            writer.writerows(summary_records)
    with entry_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(entry_records[0].keys()) if entry_records else [])
        if entry_records:
            writer.writeheader()
            writer.writerows(entry_records)
    return {
        "latest_summary.json": summary_json,
        "latest_summary.csv": summary_csv,
        "latest_entry_map.json": entry_json,
        "latest_entry_map.csv": entry_csv,
        "latest_run_metadata.json": metadata_json,
    }
