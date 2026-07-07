from __future__ import annotations

import csv
import json
from datetime import date, datetime
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

TELEGRAM_MAX_SETUPS = 4
CONFIRMED_ACTIONABLE_MAX_DISTANCE_PCT = 2.0
TELEGRAM_BUCKET_BUY_SIGNAL = "BUY_SIGNAL"
TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE = "WAIT_FOR_DAILY_CLOSE"
TELEGRAM_BUCKET_OTHER = "OTHER"
TELEGRAM_DIVIDER = "\u2501" * 18
ICON_HEADER = "\U0001F4CA"
ICON_BUY = "\U0001F7E2"
ICON_WAIT = "\U0001F7E1"
ICON_OTHER = "\u26AA"
ICON_BUY_ACTIVE = "\u2705"
ICON_WAITING = "\u23F3"
ICON_GRADE_CHANGE = "\U0001F504"
BULLET = "\u2022"
MIDDLE_DOT = "\u00B7"

GRADE_LABELS = {
    1: "Grade A",
    2: "Grade B",
    3: "Grade C",
    4: "Grade D",
}


def _money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _finite_number(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price_range(low: float | None, high: float | None) -> str:
    safe_low = _finite_number(low)
    safe_high = _finite_number(high)
    if safe_low is None and safe_high is None:
        return "N/A"
    if safe_low is None:
        return _money(safe_high)
    if safe_high is None:
        return _money(safe_low)
    if abs(safe_low - safe_high) < 1e-9:
        return _money(safe_low)
    return f"{_money(safe_low)}-{_money(safe_high)}"


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


def telegram_grade(tier: int | None) -> str:
    return GRADE_LABELS.get(tier or 0, "Grade ?")


def telegram_execution_distance_pct(result: TickerAnalysis) -> float | None:
    current_price = _finite_number(result.current_price)
    entry_trigger = _finite_number(result.preferred_route.entry_trigger_price)
    if current_price is None or entry_trigger in (None, 0.0):
        return None
    return ((current_price / entry_trigger) - 1.0) * 100.0


def telegram_gap_to_trigger_pct(result: TickerAnalysis) -> float | None:
    current_price = _finite_number(result.current_price)
    entry_trigger = _finite_number(result.preferred_route.entry_trigger_price)
    if current_price is None or entry_trigger in (None, 0.0):
        return None
    return max(0.0, ((entry_trigger - current_price) / entry_trigger) * 100.0)


def telegram_max_execution_price(result: TickerAnalysis) -> float | None:
    entry_trigger = _finite_number(result.preferred_route.entry_trigger_price)
    if entry_trigger in (None, 0.0):
        return None
    return entry_trigger * (1.0 + (CONFIRMED_ACTIONABLE_MAX_DISTANCE_PCT / 100.0))


def telegram_execution_bucket(result: TickerAnalysis) -> str:
    if result.final_tier != 1:
        return TELEGRAM_BUCKET_OTHER
    if result.preferred_route.status == "CONFIRMED":
        execution_distance_pct = telegram_execution_distance_pct(result)
        if execution_distance_pct is not None and 0.0 <= execution_distance_pct <= (CONFIRMED_ACTIONABLE_MAX_DISTANCE_PCT + 1e-9):
            return TELEGRAM_BUCKET_BUY_SIGNAL
        return TELEGRAM_BUCKET_OTHER
    if result.preferred_route.status == "TESTING":
        current_price = _finite_number(result.current_price)
        entry_trigger = _finite_number(result.preferred_route.entry_trigger_price)
        if current_price is not None and entry_trigger not in (None, 0.0):
            return TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE
    return TELEGRAM_BUCKET_OTHER


def telegram_setup_sort_key(result: TickerAnalysis, bucket: str) -> tuple[float, float, str]:
    if bucket == TELEGRAM_BUCKET_BUY_SIGNAL:
        metric = telegram_execution_distance_pct(result)
    elif bucket == TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE:
        metric = telegram_gap_to_trigger_pct(result)
    else:
        metric = None
    return (
        metric if metric is not None else math.inf,
        -result.technical_score,
        result.ticker,
    )


def _route_label(result: TickerAnalysis) -> str:
    return SHORT_ROUTE_LABELS.get(result.preferred_route.route_code, result.preferred_route.route_label)


def telegram_route_trigger_text(result: TickerAnalysis, *, confirmed: bool) -> str:
    trigger = _money(_finite_number(result.preferred_route.entry_trigger_price))
    route_code = result.preferred_route.route_code
    if confirmed:
        mapping = {
            "VAH_DEFENDED_PULLBACK": f"Daily close confirmed above {trigger} after VAH hold.",
            "POC_AVWAP_RECOVERY": f"Daily close confirmed above {trigger} after recovery.",
            "BREAKOUT_RETEST": f"Daily close confirmed back above {trigger} after retest.",
            "VAL_RECLAIM": f"Daily close confirmed above {trigger} after reclaim.",
        }
    else:
        mapping = {
            "VAH_DEFENDED_PULLBACK": f"Above {trigger} after VAH hold",
            "POC_AVWAP_RECOVERY": f"Above {trigger} after recovery",
            "BREAKOUT_RETEST": f"Back above {trigger} after retest",
            "VAL_RECLAIM": f"Above {trigger} after reclaim",
        }
    return mapping.get(route_code, result.preferred_route.entry_trigger_condition or "N/A")


def _support_text(result: TickerAnalysis) -> str:
    name = (result.preferred_route.next_support_name or "").strip()
    price = _finite_number(result.preferred_route.next_support_price)
    if name and price is not None:
        return f"{name} {_money(price)}"
    if name:
        return f"{name} N/A"
    if price is not None:
        return _money(price)
    return "None"


def _scan_date_text(observed_at_utc: str) -> str:
    try:
        stamp = datetime.fromisoformat(str(observed_at_utc).replace("Z", "+00:00"))
    except ValueError:
        return str(observed_at_utc)
    return f"{stamp.day} {stamp.strftime('%b %Y')}"


def _event_date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, date):
        stamp = datetime.combine(value, datetime.min.time())
    elif hasattr(value, "to_pydatetime"):
        stamp = value.to_pydatetime()
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return f"{stamp.day} {stamp.strftime('%b %Y')}"


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


def telegram_grade_change_text(result: TickerAnalysis) -> str | None:
    if result.tier_change not in {"IMPROVED", "DETERIORATED"}:
        return None
    previous_tier = result.previous_technical_tier
    if previous_tier is None or previous_tier == result.final_tier:
        return None
    return f"{result.ticker} {MIDDLE_DOT} {telegram_grade(previous_tier)} -> {telegram_grade(result.final_tier)} {MIDDLE_DOT} {_route_label(result)}"


def format_telegram_entry(result: TickerAnalysis, bucket: str, *, position: int | None = None) -> str:
    route = result.preferred_route
    title_prefix = ICON_BUY if bucket == TELEGRAM_BUCKET_BUY_SIGNAL else f"{ICON_WAIT} {position}."
    lines = [
        f"{title_prefix} {result.ticker} {MIDDLE_DOT} {_route_label(result)} {MIDDLE_DOT} {telegram_grade(result.final_tier)} {MIDDLE_DOT} Score {result.technical_score:.0f}",
        "",
        f"Price: {_money(_finite_number(result.current_price))}",
    ]
    is_breakout_hold = route.route_code == "BREAKOUT_RETEST"
    if bucket == TELEGRAM_BUCKET_BUY_SIGNAL and is_breakout_hold:
        breakout_level = _money(_finite_number(route.metadata.get("breakout_level")))
        breakout_reference_date = _event_date_text(route.metadata.get("breakout_reference_date"))
        breakout_confirmation_date = _event_date_text(route.metadata.get("breakout_confirmation_date"))
        breakout_confirmation_close = _money(_finite_number(route.metadata.get("breakout_confirmation_close")))
        retest_confirmation_date = _event_date_text(route.metadata.get("retest_confirmation_date"))
        lines.extend(
            [
                f"Retest zone: {_price_range(route.zone_low, route.zone_high)}",
                f"Stored breakout level: {breakout_level}",
                f"Max execution: {_money(telegram_max_execution_price(result))}",
                "",
                f"{ICON_BUY_ACTIVE} BUY SIGNAL ACTIVE",
                "",
                "Breakout reference:",
                f"Prior post-earnings high from {breakout_reference_date or 'N/A'} at {breakout_level}",
                "",
                "Breakout confirmed:",
                f"{breakout_confirmation_date or 'N/A'} daily close at {breakout_confirmation_close} cleared the breakout level",
                "",
                "Retest confirmed:",
                f"{retest_confirmation_date or 'N/A'} daily close held the breakout zone and stayed above {breakout_level}",
            ]
        )
    elif bucket == TELEGRAM_BUCKET_BUY_SIGNAL:
        lines.extend(
            [
                f"Entry trigger: {_money(_finite_number(route.entry_trigger_price))}",
                f"Max execution: {_money(telegram_max_execution_price(result))}",
                f"Buy zone: {_price_range(route.zone_low, route.zone_high)}",
                "",
                f"{ICON_BUY_ACTIVE} BUY SIGNAL ACTIVE",
                "",
                "Confirmed:",
                telegram_route_trigger_text(result, confirmed=True),
            ]
        )
    else:
        gap_to_trigger_pct = telegram_gap_to_trigger_pct(result)
        lines.extend(
            [
                f"Buy zone: {_price_range(route.zone_low, route.zone_high)}",
                f"Required close: {telegram_route_trigger_text(result, confirmed=False)}",
                f"Gap to trigger: {gap_to_trigger_pct:.2f}%" if gap_to_trigger_pct is not None else "Gap to trigger: N/A",
                "",
                f"{ICON_WAITING} NO BUY SIGNAL YET",
            ]
        )
    lines.extend(
        [
            "",
            "Setup fails on daily close below:",
            _money(_finite_number(route.route_invalidation)),
            "",
            "Next support:",
            _support_text(result),
            "",
            "Chart:",
            _tradingview_url(result),
        ]
    )
    return "\n".join(lines)


def format_telegram_report(scan_result: VpAvwapScanResult) -> str:
    total_results = len(scan_result.results)
    buy_signals = sorted(
        [result for result in scan_result.results if telegram_execution_bucket(result) == TELEGRAM_BUCKET_BUY_SIGNAL],
        key=lambda result: telegram_setup_sort_key(result, TELEGRAM_BUCKET_BUY_SIGNAL),
    )
    wait_for_daily_close = sorted(
        [result for result in scan_result.results if telegram_execution_bucket(result) == TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE],
        key=lambda result: telegram_setup_sort_key(result, TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE),
    )
    other_count = max(0, total_results - len(buy_signals) - len(wait_for_daily_close))

    displayed_buy_signals = buy_signals[:TELEGRAM_MAX_SETUPS]
    remaining_slots = max(0, TELEGRAM_MAX_SETUPS - len(displayed_buy_signals))
    displayed_wait_for_daily_close = wait_for_daily_close[:remaining_slots]
    hidden_setup_count = (len(buy_signals) + len(wait_for_daily_close)) - (
        len(displayed_buy_signals) + len(displayed_wait_for_daily_close)
    )

    grade_change_results = sorted(
        [
            result
            for result in scan_result.results
            if telegram_grade_change_text(result) is not None
        ],
        key=lambda result: (
            0 if result.tier_change == "IMPROVED" else 1,
            -abs((result.previous_technical_tier or result.final_tier) - result.final_tier),
            -result.technical_score,
            result.ticker,
        ),
    )

    lines = [
        f"{ICON_HEADER} VP/AVWAP ENTRY ALERT",
        _scan_date_text(scan_result.observed_at_utc),
        "",
        f"{ICON_BUY} BUY SIGNALS: {len(buy_signals)}",
        f"{ICON_WAIT} WAIT FOR DAILY CLOSE: {len(wait_for_daily_close)}",
        f"{ICON_OTHER} OTHER WATCHLIST TICKERS: {other_count}",
        "",
        TELEGRAM_DIVIDER,
        f"{ICON_BUY} BUY SIGNALS",
        TELEGRAM_DIVIDER,
        "",
        "Confirmed daily signal has occurred.",
        "Only enter while price remains within 2% of the trigger.",
        "",
    ]

    if displayed_buy_signals:
        for index, result in enumerate(displayed_buy_signals):
            if index:
                lines.extend(["", TELEGRAM_DIVIDER, ""])
            lines.append(format_telegram_entry(result, TELEGRAM_BUCKET_BUY_SIGNAL))
    else:
        lines.append("None currently within the execution range.")

    lines.extend(
        [
            "",
            TELEGRAM_DIVIDER,
            f"{ICON_WAIT} WAIT FOR DAILY CLOSE",
            TELEGRAM_DIVIDER,
            "",
            "Price is at the intended entry zone.",
            "No buy signal exists until the daily trigger is met.",
            "",
        ]
    )

    if displayed_wait_for_daily_close:
        for index, result in enumerate(displayed_wait_for_daily_close, start=1):
            if index > 1:
                lines.extend(["", TELEGRAM_DIVIDER, ""])
            lines.append(
                format_telegram_entry(
                    result,
                    TELEGRAM_BUCKET_WAIT_FOR_DAILY_CLOSE,
                    position=index,
                )
            )
    elif wait_for_daily_close:
        lines.append("Additional waiting setups were omitted by the Telegram detail limit.")
    else:
        lines.append("None currently testing a Grade A buy zone.")

    if grade_change_results:
        lines.extend(["", TELEGRAM_DIVIDER, f"{ICON_GRADE_CHANGE} GRADE CHANGES", TELEGRAM_DIVIDER, ""])
        for result in grade_change_results:
            lines.append(telegram_grade_change_text(result) or "")

    if hidden_setup_count > 0:
        lines.extend(
            [
                "",
                f"{hidden_setup_count} additional high-priority setups are available in:",
                f"{BULLET} VP_AVWAP_Tiers",
                f"{BULLET} VP_AVWAP_Entry_Map",
            ]
        )
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
