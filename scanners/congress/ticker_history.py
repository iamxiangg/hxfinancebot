from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Iterable

from scanners.congress.engine import TransactionRecord, pdate
from scanners.congress.models import PoliticalWindowSummary, TickerPoliticalHistory


PRIMARY_WINDOW_DAYS = 90
HISTORY_WINDOW_DAYS = 365


def _float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _json_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _to_date(value: str) -> date | None:
    return pdate(value)


def _days_ago(observed_on: date, record: TransactionRecord) -> int | None:
    transaction_date = _to_date(record.transaction_date)
    if transaction_date is None:
        return None
    return (observed_on - transaction_date).days


def _in_window(record: TransactionRecord, *, observed_on: date, window_days: int) -> bool:
    age = _days_ago(observed_on, record)
    return age is not None and 0 <= age <= window_days


def _household_key(record: TransactionRecord) -> str:
    return str(record.filer_id or "").strip() or str(record.filer_name or "").strip()


def _is_purchase(record: TransactionRecord) -> bool:
    return record.action == "purchase"


def _is_sale(record: TransactionRecord) -> bool:
    return record.action in {"sale_partial", "sale_full", "sale_unknown"}


def _is_bullish_purchase(record: TransactionRecord) -> bool:
    return _is_purchase(record) and record.company_specific and (record.asset_class == "stock" or record.option_side == "call")


def _is_put_purchase(record: TransactionRecord) -> bool:
    return _is_purchase(record) and record.option_side == "put"


def _window_summary(records: list[TransactionRecord], *, observed_on: date, window_days: int) -> PoliticalWindowSummary:
    current = [record for record in records if _in_window(record, observed_on=observed_on, window_days=window_days)]
    bullish_purchases = [record for record in current if _is_bullish_purchase(record)]
    stock_purchases = [record for record in bullish_purchases if record.asset_class == "stock"]
    call_purchases = [record for record in bullish_purchases if record.option_side == "call"]
    put_purchases = [record for record in current if _is_put_purchase(record)]
    sales = [record for record in current if _is_sale(record)]
    buyers = {_household_key(record) for record in bullish_purchases}
    sellers = {_household_key(record) for record in sales}
    buyer_counts = Counter(_household_key(record) for record in bullish_purchases)
    bullish_low_by_buyer: dict[str, float] = defaultdict(float)
    bullish_mid_by_buyer: dict[str, float] = defaultdict(float)
    for record in bullish_purchases:
        bullish_low_by_buyer[_household_key(record)] += float(record.amount_range_low or 0.0)
        bullish_mid_by_buyer[_household_key(record)] += float(record.amount_range_mid or 0.0)

    total_bullish_low = sum(bullish_low_by_buyer.values())
    total_bullish_mid = sum(bullish_mid_by_buyer.values())
    largest_buyer_low = max(bullish_low_by_buyer.values(), default=0.0)
    largest_buyer_mid = max(bullish_mid_by_buyer.values(), default=0.0)

    return PoliticalWindowSummary(
        window_days=window_days,
        purchase_count=sum(_is_purchase(record) for record in current),
        partial_sale_count=sum(record.action == "sale_partial" for record in current),
        full_sale_count=sum(record.action == "sale_full" for record in current),
        unique_buyer_count=len(buyers),
        unique_seller_count=len(sellers),
        repeat_buyer_count=sum(count >= 2 for count in buyer_counts.values()),
        stock_purchase_low=sum(float(record.amount_range_low or 0.0) for record in stock_purchases),
        stock_purchase_mid_estimate=sum(float(record.amount_range_mid or 0.0) for record in stock_purchases),
        stock_purchase_high=sum(float(record.amount_range_high or 0.0) for record in stock_purchases),
        call_purchase_low=sum(float(record.amount_range_low or 0.0) for record in call_purchases),
        call_purchase_mid_estimate=sum(float(record.amount_range_mid or 0.0) for record in call_purchases),
        call_purchase_high=sum(float(record.amount_range_high or 0.0) for record in call_purchases),
        put_purchase_low=sum(float(record.amount_range_low or 0.0) for record in put_purchases),
        put_purchase_mid_estimate=sum(float(record.amount_range_mid or 0.0) for record in put_purchases),
        put_purchase_high=sum(float(record.amount_range_high or 0.0) for record in put_purchases),
        sale_low=sum(float(record.amount_range_low or 0.0) for record in sales),
        sale_mid_estimate=sum(float(record.amount_range_mid or 0.0) for record in sales),
        sale_high=sum(float(record.amount_range_high or 0.0) for record in sales),
        largest_bullish_trade_low=max((float(record.amount_range_low or 0.0) for record in bullish_purchases), default=0.0),
        largest_bullish_trade_high=max((float(record.amount_range_high or 0.0) for record in bullish_purchases), default=0.0),
        largest_buyer_share_lower_bound=(largest_buyer_low / total_bullish_low) if total_bullish_low > 0 else 0.0,
        largest_buyer_share_midpoint_estimate=(largest_buyer_mid / total_bullish_mid) if total_bullish_mid > 0 else 0.0,
        unique_record_count=len(current),
        unique_filer_count=len({_household_key(record) for record in current}),
        unique_household_count=len({_household_key(record) for record in current}),
    )


def _structure_classification(window: PoliticalWindowSummary) -> str:
    stock_total = window.stock_purchase_low
    option_total = window.call_purchase_low + window.put_purchase_low
    if option_total > 0 and option_total >= stock_total * 1.5:
        return "OPTIONS_LED"
    if stock_total > 0 and stock_total >= option_total * 1.5:
        return "COMMON_STOCK_LED"
    if stock_total > 0 and option_total > 0:
        return "MIXED_INSTRUMENT"
    if option_total > 0:
        return "OPTIONS_LED"
    if stock_total > 0:
        return "COMMON_STOCK_LED"
    return "UNKNOWN_STRUCTURE"


def _bullish_evidence(primary_window: PoliticalWindowSummary) -> float:
    threshold = max(1.0, _float_env("POLITICAL_FLAG_PURCHASE_LOW", 100000.0))
    materiality = min(45.0, (primary_window.stock_purchase_low + primary_window.call_purchase_low) / threshold * 18.0)
    breadth = min(25.0, primary_window.unique_buyer_count * 10.0)
    repeats = min(15.0, primary_window.repeat_buyer_count * 8.0)
    recency = min(15.0, primary_window.purchase_count * 4.0)
    return min(100.0, materiality + breadth + repeats + recency)


def _distribution_evidence(primary_window: PoliticalWindowSummary, full_history: PoliticalWindowSummary) -> float:
    threshold = max(1.0, _float_env("POLITICAL_FLAG_SALE_LOW", 100000.0))
    sale_materiality = min(30.0, primary_window.sale_low / threshold * 10.0)
    seller_breadth = min(12.0, primary_window.unique_seller_count * 6.0)
    exits = min(16.0, primary_window.full_sale_count * 6.0 + full_history.full_sale_count * 1.0)
    puts = min(10.0, primary_window.put_purchase_low / threshold * 8.0)
    return min(100.0, sale_materiality + seller_breadth + exits + puts)


def _breadth_score(primary_window: PoliticalWindowSummary) -> float:
    return min(100.0, primary_window.unique_buyer_count * 30.0 + primary_window.repeat_buyer_count * 10.0)


def _concentration_score(primary_window: PoliticalWindowSummary) -> float:
    return min(100.0, primary_window.largest_buyer_share_midpoint_estimate * 100.0)


def _classify_primary(
    primary_window: PoliticalWindowSummary,
    full_history: PoliticalWindowSummary,
    *,
    bullish_evidence: float,
    distribution_evidence: float,
) -> str:
    broad_min_buyers = int(_float_env("POLITICAL_BROAD_MIN_BUYERS", 2.0))
    if bullish_evidence < 20.0 and distribution_evidence < 20.0:
        return "INSUFFICIENT_EVIDENCE"
    if distribution_evidence >= 45.0 and distribution_evidence >= bullish_evidence * 1.25:
        return "DISTRIBUTION"
    if (
        bullish_evidence >= 40.0
        and distribution_evidence >= 40.0
        and max(bullish_evidence, distribution_evidence) <= max(1.0, min(bullish_evidence, distribution_evidence)) * 1.4
    ):
        return "MIXED_HIGH_ACTIVITY"
    if primary_window.unique_buyer_count >= broad_min_buyers and bullish_evidence >= 35.0 and distribution_evidence < 30.0:
        return "BROAD_ACCUMULATION"
    if bullish_evidence >= 25.0 and full_history.largest_buyer_share_lower_bound >= _float_env("POLITICAL_CONCENTRATION_THRESHOLD", 0.70):
        return "SINGLE_FILER_BULLISH_BET"
    if bullish_evidence >= 30.0 and primary_window.unique_buyer_count <= 1:
        return "SINGLE_FILER_BULLISH_BET"
    if primary_window.repeat_buyer_count >= 1 and bullish_evidence >= 30.0 and distribution_evidence < 35.0:
        return "REPEAT_FILER_ACCUMULATION"
    return "INSUFFICIENT_EVIDENCE"


def _inference_confidence(
    primary_classification: str,
    primary_window: PoliticalWindowSummary,
    distribution_evidence: float,
) -> str:
    if primary_classification in {"BROAD_ACCUMULATION", "DISTRIBUTION"} and primary_window.unique_buyer_count + primary_window.unique_seller_count >= 2:
        return "HIGH"
    if primary_classification in {"REPEAT_FILER_ACCUMULATION", "SINGLE_FILER_BULLISH_BET", "MIXED_HIGH_ACTIVITY"} or distribution_evidence >= 30.0:
        return "MEDIUM"
    return "LOW"


def _data_confidence(records: Iterable[TransactionRecord]) -> str:
    items = list(records)
    if not items:
        return "LOW"
    complete = sum(bool(record.ticker and record.transaction_date and record.filing_date) for record in items)
    if complete == len(items):
        return "HIGH"
    if complete >= max(1, len(items) // 2):
        return "MEDIUM"
    return "LOW"


def _flag_reasons(
    ticker: str,
    primary_window: PoliticalWindowSummary,
    full_history: PoliticalWindowSummary,
    primary_classification: str,
    structure_classification: str,
) -> list[str]:
    reasons: list[str] = []
    if primary_window.call_purchase_low >= _float_env("POLITICAL_FLAG_CALL_LOW", 100000.0):
        reasons.append(f"{ticker} call purchases reached at least ${primary_window.call_purchase_low:,.0f}")
    if primary_window.stock_purchase_low >= _float_env("POLITICAL_FLAG_PURCHASE_LOW", 100000.0):
        reasons.append(f"{ticker} stock purchases reached at least ${primary_window.stock_purchase_low:,.0f}")
    if primary_window.sale_low >= _float_env("POLITICAL_FLAG_SALE_LOW", 100000.0):
        reasons.append(f"{ticker} sales reached at least ${primary_window.sale_low:,.0f}")
    if primary_window.unique_buyer_count >= int(_float_env("POLITICAL_BROAD_MIN_BUYERS", 2.0)):
        reasons.append(f"{primary_window.unique_buyer_count} independent households bought within {primary_window.window_days} days")
    if primary_window.repeat_buyer_count >= 1:
        reasons.append("repeat household buying is present")
    if full_history.full_sale_count >= 1:
        reasons.append("full-sale history is present in the wider record")
    if primary_classification != "INSUFFICIENT_EVIDENCE":
        reasons.append(f"classification is {primary_classification}")
    if structure_classification == "OPTIONS_LED":
        reasons.append("recent activity is options-led")
    return reasons


def _risk_flags(primary_window: PoliticalWindowSummary, primary_classification: str, data_confidence: str) -> list[str]:
    flags: list[str] = []
    if primary_window.sale_low > 0:
        flags.append("recent_sales_present")
    if primary_window.put_purchase_low > 0:
        flags.append("put_activity_present")
    if primary_classification == "MIXED_HIGH_ACTIVITY":
        flags.append("mixed_directional_history")
    if data_confidence == "LOW":
        flags.append("low_data_confidence")
    return flags


def _notable_history(records: list[TransactionRecord], *, observed_on: date) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    prior_buys = [record for record in records if _is_bullish_purchase(record) and _in_window(record, observed_on=observed_on, window_days=HISTORY_WINDOW_DAYS)]
    if prior_buys:
        largest = max(prior_buys, key=lambda record: (float(record.amount_range_low or 0.0), record.trade_key))
        history.append(
            {
                "kind": "largest_bullish_trade",
                "text": f"Largest bullish trade was reported by {largest.filer_name or largest.filer_id}",
                "trade_key": largest.trade_key,
            }
        )
    household_counts = Counter(_household_key(record) for record in prior_buys)
    for household, count in sorted(household_counts.items()):
        if count >= 2:
            history.append(
                {
                    "kind": "repeat_household_buyer",
                    "text": f"{household} reported {count} bullish purchases in the last 365 days",
                }
            )
    if any(record.action == "sale_full" for record in records):
        history.append({"kind": "possible_exit", "text": "Full-sale disclosures appear in the historical record"})
    return history


def _summary_hash(
    ticker: str,
    primary_classification: str,
    structure_classification: str,
    bullish_evidence: float,
    distribution_evidence: float,
    breadth_score: float,
    concentration_score: float,
    windows: dict[int, PoliticalWindowSummary],
    flag_reasons: list[str],
    political_conviction: float,
    entry_quality: float,
    trigger_trade_keys: tuple[str, ...],
) -> str:
    payload = {
        "ticker": ticker,
        "primary_classification": primary_classification,
        "structure_classification": structure_classification,
        "bullish_evidence": round(bullish_evidence, 4),
        "distribution_evidence": round(distribution_evidence, 4),
        "breadth_score": round(breadth_score, 4),
        "concentration_score": round(concentration_score, 4),
        "windows": {str(key): asdict(value) for key, value in sorted(windows.items())},
        "flag_reasons": list(flag_reasons),
        "political_conviction": round(political_conviction, 4),
        "entry_quality": round(entry_quality, 4),
        "trigger_trade_keys": list(trigger_trade_keys),
    }
    return _json_hash(payload)


def build_ticker_histories(
    records: list[TransactionRecord],
    *,
    observed_at: date | datetime,
    ticker_results: dict[str, Any] | None = None,
    previous_summary_rows: dict[str, dict[str, Any]] | None = None,
    trigger_events: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, TickerPoliticalHistory]:
    observed_on = observed_at.date() if isinstance(observed_at, datetime) else observed_at
    grouped: dict[str, list[TransactionRecord]] = defaultdict(list)
    for record in records:
        if record.ticker:
            grouped[record.ticker].append(record)

    histories: dict[str, TickerPoliticalHistory] = {}
    ticker_results = ticker_results or {}
    previous_summary_rows = previous_summary_rows or {}
    trigger_events = trigger_events or {}

    for ticker, ticker_records in sorted(grouped.items()):
        records_in_order = sorted(
            ticker_records,
            key=lambda record: (
                record.transaction_date,
                record.filing_date,
                record.trade_key,
            ),
        )
        windows = {
            window: _window_summary(records_in_order, observed_on=observed_on, window_days=window)
            for window in (45, 90, 365)
        }
        primary_window = windows[PRIMARY_WINDOW_DAYS]
        full_history = windows[HISTORY_WINDOW_DAYS]
        structure_classification = _structure_classification(primary_window)
        bullish_evidence = _bullish_evidence(primary_window)
        distribution_evidence = _distribution_evidence(primary_window, full_history)
        breadth_score = _breadth_score(primary_window)
        concentration_score = _concentration_score(primary_window)
        primary_classification = _classify_primary(
            primary_window,
            full_history,
            bullish_evidence=bullish_evidence,
            distribution_evidence=distribution_evidence,
        )
        previous_row = previous_summary_rows.get(ticker, {})
        previous_classification = str(previous_row.get("Primary Classification") or "INSUFFICIENT_EVIDENCE")
        current_result = ticker_results.get(ticker)
        trigger_items = sorted(trigger_events.get(ticker, []), key=lambda item: (item.get("transaction_date", ""), item.get("trade_key", "")))
        trigger_trade_keys = tuple(item.get("trade_key", "") for item in trigger_items if item.get("trade_key"))
        flag_reasons = _flag_reasons(
            ticker,
            primary_window,
            full_history,
            primary_classification,
            structure_classification,
        )
        data_confidence = _data_confidence(records_in_order)
        inference_confidence = _inference_confidence(primary_classification, primary_window, distribution_evidence)
        release_types = tuple(dict.fromkeys(item.get("release_type", "") for item in trigger_items if item.get("release_type")))
        political_conviction = float(getattr(current_result, "conviction", 0.0) or 0.0)
        entry_quality = float(getattr(current_result, "entry", 0.0) or 0.0)
        summary_hash = _summary_hash(
            ticker,
            primary_classification,
            structure_classification,
            bullish_evidence,
            distribution_evidence,
            breadth_score,
            concentration_score,
            windows,
            flag_reasons,
            political_conviction,
            entry_quality,
            trigger_trade_keys,
        )
        histories[ticker] = TickerPoliticalHistory(
            ticker=ticker,
            primary_classification=primary_classification,
            structure_classification=structure_classification,
            bullish_evidence_score=bullish_evidence,
            distribution_evidence_score=distribution_evidence,
            breadth_score=breadth_score,
            concentration_score=concentration_score,
            inference_confidence=inference_confidence,
            data_confidence=data_confidence,
            windows=windows,
            new_events=trigger_items,
            notable_history=_notable_history(records_in_order, observed_on=observed_on),
            flag_reasons=flag_reasons,
            risk_flags=_risk_flags(primary_window, primary_classification, data_confidence),
            previous_classification=previous_classification,
            classification_changed=previous_classification != primary_classification,
            summary_hash=summary_hash,
            latest_transaction_date=max((record.transaction_date for record in records_in_order if record.transaction_date), default=""),
            latest_filing_date=max((record.filing_date for record in records_in_order if record.filing_date), default=""),
            latest_trigger_type=trigger_items[0].get("release_type", "") if trigger_items else "",
            latest_trigger_trade_keys=trigger_trade_keys,
            release_types=release_types,
            political_conviction=political_conviction,
            entry_quality=entry_quality,
            signal_category=str(getattr(current_result, "category", "other") or "other"),
            existing_status=str(getattr(current_result, "category", "other") or "other"),
        )
    return histories
