from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from funnel.vp_avwap_sheet_writer import build_entry_map_records, build_summary_records
from scanners.vp_avwap.models import TickerAnalysis, VpAvwapScanResult


TIER_LABELS = {
    1: "Actionable or nearly actionable",
    2: "Attractive, but not ready",
    3: "Watch only",
    4: "No valid entry",
}

SHORT_ROUTE_LABELS = {
    "VAH_DEFENDED_PULLBACK": "Hold Above VAH",
    "POC_AVWAP_RECOVERY": "Recover POC/AVWAP",
    "BREAKOUT_RETEST": "Breakout Hold",
    "VAL_RECLAIM": "Reclaim VAL",
}

HIGH_SIGNAL_STATUSES = {"CONFIRMED", "TESTING", "APPROACHING"}
TELEGRAM_MAX_SETUPS = 4


def _money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _price_range(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return "N/A"
    if low is None:
        return _money(high)
    if high is None:
        return _money(low)
    if abs(low - high) < 1e-9:
        return _money(low)
    return f"{_money(low)}-{_money(high)}"


def _short_reason(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return "N/A"
    for marker in (". ", "."):
        if marker in cleaned:
            sentence = cleaned.split(marker, 1)[0].strip()
            return sentence if sentence.endswith(".") else f"{sentence}."
    return cleaned


def _tradingview_url(result: TickerAnalysis) -> str:
    symbol = (result.google_ticker or result.ticker).strip()
    if not symbol:
        return "N/A"
    chart_id = str(os.getenv("VP_AVWAP_TRADINGVIEW_CHART_ID", "")).strip().strip("/")
    encoded_symbol = quote(symbol, safe="")
    if chart_id:
        return f"https://www.tradingview.com/chart/{chart_id}/?symbol={encoded_symbol}"
    return f"https://www.tradingview.com/chart/?symbol={encoded_symbol}"


def _telegram_trigger(result: TickerAnalysis) -> str:
    route = result.preferred_route
    trigger = _money(route.entry_trigger_price)
    if route.entry_trigger_price is None:
        return route.entry_trigger_condition
    if route.route_code == "BREAKOUT_RETEST":
        return f"Close back above {trigger} after retest"
    if route.route_code == "VAL_RECLAIM":
        return f"Close above {trigger} after reclaim"
    if route.route_code == "POC_AVWAP_RECOVERY":
        return f"Close above {trigger} after recovery"
    if route.route_code == "VAH_DEFENDED_PULLBACK":
        return f"Close above {trigger} after VAH hold"
    return route.entry_trigger_condition


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


def format_telegram_entry(result: TickerAnalysis) -> str:
    route = result.preferred_route
    lines = [
        f"{result.ticker} | {route.status} | {SHORT_ROUTE_LABELS.get(route.route_code, route.route_label)} | {result.technical_score:.0f}",
        f"Price {_money(result.current_price)} | Zone {_price_range(route.zone_low, route.zone_high)} | Stop {_money(route.route_invalidation)}",
        f"Trigger {_telegram_trigger(result)} | Support {(route.next_support_name or 'None')} {_money(route.next_support_price)}",
        f"Why {_short_reason(result.technical_reason or route.reason)}",
        f"Chart {_tradingview_url(result)}",
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


def format_telegram_report(scan_result: VpAvwapScanResult) -> str:
    lines = ["VP/AVWAP TECHNICAL TIERS", ""]
    for tier in (1, 2, 3, 4):
        group = [result for result in scan_result.results if result.final_tier == tier]
        if tier <= 2:
            sample = ", ".join(result.ticker for result in group[:3]) if group else "None"
            more = f" +{len(group) - 3} more" if len(group) > 3 else ""
            lines.append(f"Tier {tier}: {len(group)} ({sample}{more})")
        else:
            lines.append(f"Tier {tier}: {len(group)}")

    changed = [
        result
        for result in scan_result.results
        if result.tier_change in {"IMPROVED", "DETERIORATED"} or result.preferred_route.status == "CONFIRMED"
    ]
    if changed:
        lines.extend(["", "Changes"])
        for result in changed:
            change_label = result.tier_change if result.tier_change in {"IMPROVED", "DETERIORATED"} else result.preferred_route.status
            lines.append(
                f"{result.ticker} | {change_label} | Tier {result.final_tier} | {SHORT_ROUTE_LABELS.get(result.preferred_route.route_code, result.preferred_route.route_label)}"
            )

    details = [
        result
        for result in scan_result.results
        if result.final_tier == 1 and result.preferred_route.status in HIGH_SIGNAL_STATUSES
    ]
    details.sort(
        key=lambda result: (
            {"CONFIRMED": 0, "TESTING": 1, "APPROACHING": 2}.get(result.preferred_route.status, 9),
            -result.technical_score,
            result.ticker,
        )
    )
    if not details:
        details = [
            result
            for result in detailed_results(scan_result)
            if result.final_tier in {1, 2}
        ]
    total_detail_count = len(details)
    details = details[:TELEGRAM_MAX_SETUPS]
    if details:
        lines.extend(["", "High Signals"])
        for result in details:
            lines.extend(["", format_telegram_entry(result)])
        hidden = total_detail_count - len(details)
        if hidden > 0:
            lines.extend(["", f"More candidates: {hidden} additional high-priority setups in sheets/artifacts."])
    return "\n".join(lines).strip()


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
