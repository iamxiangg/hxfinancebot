from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from scanners.congress.digest_models import PoliticalDigestFlag, PoliticalDigestPlan
from scanners.congress.trend_classifier import deterministic_interpretation


TELEGRAM_LIMIT = 3800
DIGEST_TEMPLATE_VERSION = "2026-07-19-consolidated"


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RenderedDigestPart:
    text: str
    tickers: tuple[str, ...] = ()


def _money(value: float) -> str:
    number = float(value or 0.0)
    if number >= 1_000_000:
        return f"US${number / 1_000_000:.1f}m"
    if number >= 1_000:
        return f"US${number / 1_000:.0f}k"
    return f"US${number:.0f}"


def _event_value_range(event: dict[str, Any]) -> str:
    return f"{_money(float(event.get('amount_low') or 0.0))}-{_money(float(event.get('amount_high') or 0.0))}"


def _title_text(value: str) -> str:
    return str(value or "").replace("_", " ").title()


def _detected_date(flag: PoliticalDigestFlag) -> str:
    state = flag.watchlist_state
    if state and state.first_flagged_at:
        return state.first_flagged_at[:10]
    return flag.history.latest_filing_date


def _full_new_disclosure(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    latest = history.new_events[-1] if history.new_events else {}
    filer = str(latest.get("filer_name") or "Unknown filer").strip()
    transaction_label = str(latest.get("transaction_type") or history.latest_disclosure_direction).strip().upper()
    lag = latest.get("days_to_file")
    lag_line = f"Information lag: {lag} days" if lag not in ("", None) else "Information lag: unknown"
    return "\n".join(
        [
            f"{history.ticker} - NEW | Event {history.event_severity} | Ticker {history.ticker_state_severity}",
            "",
            f"{filer} - {transaction_label}",
            f"Amount: {_event_value_range(latest) if latest else 'Unknown'}",
            f"Transaction date: {latest.get('transaction_date', history.latest_transaction_date)}",
            f"Filed: {latest.get('filing_date', history.latest_filing_date)}",
            f"Detected: {_detected_date(flag)}",
            lag_line,
            "",
            "Before disclosure:",
            f"90-day purchases: {_money(history.pre_event_purchase_low_90d)} lower bound",
            f"90-day sales: {_money(history.pre_event_sale_low_90d)} lower bound",
            "",
            "After disclosure:",
            f"90-day purchases: {_money(history.post_event_purchase_low_90d)} lower bound",
            f"90-day sales: {_money(history.post_event_sale_low_90d)} lower bound",
            "",
            f"Latest direction: {history.latest_disclosure_direction}",
            f"Aggregate classification: {history.aggregate_direction}",
            f"Material effect: {history.material_effect_category}",
            "Interpretation: "
            + deterministic_interpretation(
                primary_classification=history.primary_classification,
                structure_classification=history.structure_classification,
                bullish_evidence=history.bullish_evidence_score,
                distribution_evidence=history.distribution_evidence_score,
                breadth_score=history.breadth_score,
                inference_confidence=history.inference_confidence,
            ),
        ]
    )


def _material_update(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    state = flag.watchlist_state
    change_lines = [f"- {change.reason}" for change in flag.material_changes] or ["- Material state changed."]
    return "\n".join(
        [
            f"{history.ticker} - POLITICAL SIGNAL UPDATE | Event {history.event_severity} | Ticker {history.ticker_state_severity}",
            "",
            "WHY UPDATED",
            *change_lines,
            "",
            "CHANGE",
            f"Previous classification / entry: {history.previous_classification} / {(state.previous_entry_category if state else 'OTHER')}",
            f"Current classification / entry: {history.aggregate_direction} / {history.entry_category}",
            "",
            "NEW POLITICAL DISCLOSURE",
            "None" if not history.new_events else "See latest record below.",
            "",
            f"Latest direction: {history.latest_disclosure_direction}",
            f"Aggregate classification: {history.aggregate_direction}",
            f"Material effect: {history.material_effect_category}",
            "Interpretation: "
            + deterministic_interpretation(
                primary_classification=history.primary_classification,
                structure_classification=history.structure_classification,
                bullish_evidence=history.bullish_evidence_score,
                distribution_evidence=history.distribution_evidence_score,
                breadth_score=history.breadth_score,
                inference_confidence=history.inference_confidence,
            ),
        ]
    )


def _watchlist_item(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    state = flag.watchlist_state
    assert state is not None
    event = history.new_events[-1] if history.new_events else {}
    item_text = state.latest_material_event or (
        f"{str(event.get('filer_name') or 'Unknown filer').strip()} {str(event.get('transaction_type') or 'activity').strip().lower()}"
    )
    return "\n".join(
        [
            f"{history.ticker} - {history.aggregate_direction} | Day {max(1, state.watchlist_day)} of {max(1, state.watchlist_total_days)}",
            item_text,
            f"Transaction: {history.latest_transaction_date} | Filed: {history.latest_filing_date} | Detected: {_detected_date(flag)}",
            f"90-day purchases: {_money(history.post_event_purchase_low_90d)} lower bound",
            f"90-day sales: {_money(history.post_event_sale_low_90d)} lower bound",
            f"Breadth: {history.windows[90].unique_buyer_count} buyer versus {history.windows[90].unique_seller_count} sellers",
            f"Latest direction: {history.latest_disclosure_direction}",
            f"Aggregate direction: {history.aggregate_direction}",
            f"Material effect: {history.material_effect_category}",
            "No change since previous digest." if not flag.material_changes else "State changed since previous digest.",
        ]
    )


def _below_threshold(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    event = history.new_events[-1] if history.new_events else {}
    window = history.windows.get(90) or history.windows.get(45) or history.windows.get(365)
    context = history.signal_context or {}
    if context:
        fallback_active_low = (window.stock_purchase_low + window.call_purchase_low) if window else 0.0
        active_low = float(context.get("active_amount_low") or fallback_active_low)
        active_mid = float(context.get("active_amount_mid") or active_low)
        active_high = float(context.get("active_amount_high") or active_low)
        buyers = int(context.get("buyers") or (window.unique_buyer_count if window else 0))
        branches = context.get("branches") if isinstance(context.get("branches"), list) else []
        intents = context.get("asset_intent_classes") if isinstance(context.get("asset_intent_classes"), list) else []
        names = context.get("names") if isinstance(context.get("names"), list) else []
        branch_mix = " + ".join(_title_text(item) for item in branches[:2]) if branches else "Unknown"
        instrument = ", ".join(_title_text(item) for item in intents[:2]) if intents else history.structure_classification.replace("_", " ").title()
        cluster = _title_text(str(context.get("cluster_type") or "Single Filer"))
        flow = str(context.get("flow") or f"{history.aggregate_direction.title()} signal").strip()
        name_text = ", ".join(str(name).strip() for name in names[:4] if str(name).strip()) or "Unknown"
        return "\n".join(
            [
                (
                    f"${history.ticker} | Active {_money(active_mid)} [{_money(active_low)}-{_money(active_high)}] | "
                    f"{buyers} filers | {branch_mix} | {instrument} | {cluster} | "
                    f"Wtd age {float(context.get('weighted_age') or 0.0):.0f}d | "
                    f"Since trade {float(context.get('weighted_return') or 0.0):+.1f}% | {flow} | {name_text}"
                ),
                f"Latest transaction: {history.latest_transaction_date} | Latest filed: {history.latest_filing_date}",
                (
                    f"90d buys {_money(window.stock_purchase_low + window.call_purchase_low)} low / "
                    f"{_money(window.stock_purchase_high + window.call_purchase_high)} high; "
                    f"sales {_money(window.sale_low)} low / {_money(window.sale_high)} high"
                    if window
                    else "90d magnitude unavailable"
                ),
                f"Scores: political {history.political_conviction:.0f}, entry {history.entry_quality:.0f}, breadth {history.breadth_score:.0f}",
            ]
        )
    if window is None:
        filer = str(event.get("filer_name") or "Rolling aggregate").strip()
        transaction_type = str(event.get("transaction_type") or history.aggregate_direction or "activity").strip().lower()
        return "\n".join(
            [
                f"{history.ticker} - {filer} {transaction_type}, {_event_value_range(event) if event else 'Unknown'}",
                f"Latest transaction: {history.latest_transaction_date} | Latest filed: {history.latest_filing_date}",
                f"Classification: {history.aggregate_direction}",
                f"Reason not qualified: material effect {history.material_effect_category.lower()}",
            ]
        )
    reasons = "; ".join(history.flag_reasons[:2]) if history.flag_reasons else "met rolling activity thresholds"
    return "\n".join(
        [
            f"{history.ticker} - {history.aggregate_direction} | {history.primary_classification.replace('_', ' ')}",
            f"Latest transaction: {history.latest_transaction_date} | Latest filed: {history.latest_filing_date}",
            f"90-day stock buys: {_money(window.stock_purchase_low)} low / {_money(window.stock_purchase_high)} high",
            f"90-day call buys: {_money(window.call_purchase_low)} low / {_money(window.call_purchase_high)} high",
            f"90-day sales: {_money(window.sale_low)} low / {_money(window.sale_high)} high",
            f"Congress breadth: {window.unique_buyer_count} buyers, {window.unique_seller_count} sellers, {window.unique_record_count} records",
            f"Scores: political {history.political_conviction:.0f}, entry {history.entry_quality:.0f}, breadth {history.breadth_score:.0f}",
            f"Why shown: {reasons}",
            f"Why compact: material effect {history.material_effect_category.lower()}",
        ]
    )


def _review_required_or_excluded(item: dict[str, Any]) -> str:
    label = str(item.get("ticker") or item.get("asset_name") or item.get("trade_key") or "record").strip()
    reason = str(item.get("reason") or item.get("proposed_resolution") or item.get("classification") or "review").strip()
    return f"{label} - {reason}"


def _manual_review_label(item: dict[str, Any]) -> str:
    ticker = str(item.get("ticker") or "").strip().upper()
    asset_name = str(item.get("asset_name") or item.get("trade_key") or "record").strip()
    reason = str(item.get("proposed_resolution") or item.get("reason") or "ticker resolution").strip()
    label = ticker or asset_name
    return f"{label} - {reason.replace('_', ' ')}"


def _review_summary_blocks(items: tuple[dict[str, Any], ...], *, label: str) -> list[RenderedDigestPart]:
    if not items:
        return []
    max_examples = max(0, _int_env("POLITICAL_DIGEST_MAX_REVIEW_EXAMPLES", 8))
    reasons = Counter(
        str(item.get("reason") or item.get("proposed_resolution") or item.get("classification") or "review").strip()
        for item in items
    )
    reason_lines = [
        f"{reason}: {count}"
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]
    blocks = [
        RenderedDigestPart(
            "\n".join(
                [
                    f"{label}: {len(items)} record(s)",
                    *reason_lines,
                ]
            )
        )
    ]
    if max_examples and items:
        examples = [_review_required_or_excluded(dict(item)) for item in items[:max_examples]]
        remaining = max(0, len(items) - len(examples))
        if remaining:
            examples.append(f"... {remaining} more recorded in the audit archive.")
        blocks.append(RenderedDigestPart("\n".join([f"{label} EXAMPLES", *examples])))
    return blocks


def _automatic_exclusion_blocks(items: tuple[dict[str, Any], ...]) -> list[RenderedDigestPart]:
    if not items:
        return []
    reasons = Counter(str(item.get("reason") or "EXCLUDED").strip() for item in items)
    reason_lines = [
        f"{reason}: {count}"
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]
    return [RenderedDigestPart("\n".join(["AUTOMATICALLY EXCLUDED", f"{len(items):,} records", *reason_lines]))]


def _manual_review_blocks(items: tuple[dict[str, Any], ...]) -> list[RenderedDigestPart]:
    if not items:
        return []
    max_examples = max(0, _int_env("POLITICAL_DIGEST_MAX_REVIEW_EXAMPLES", 8))
    examples = [_manual_review_label(dict(item)) for item in items[:max_examples]]
    remaining = max(0, len(items) - len(examples))
    if remaining:
        examples.append(f"... {remaining} more active review record(s).")
    return [RenderedDigestPart("\n".join(["MANUAL REVIEW REQUIRED", f"{len(items)} active records", *examples]))]


def _expired(flag: PoliticalDigestFlag) -> str:
    return "\n".join(
        [
            f"{flag.ticker} - expired after seven days without a new qualifying disclosure",
            f"Final classification: {flag.history.aggregate_direction}",
        ]
    )


def _all_flags(plan: PoliticalDigestPlan) -> tuple[PoliticalDigestFlag, ...]:
    return (*plan.new_material_flags, *plan.material_updates, *plan.active_watchlist_items, *plan.other_new_activity)


def _render_header(plan: PoliticalDigestPlan, today: date) -> str:
    payload_hash = plan.pending_snapshot.payload_hash if plan.pending_snapshot is not None else ""
    lines = [
        "DAILY POLITICAL-TRADING DIGEST",
        today.strftime("%d %B %Y"),
        "",
        "RUN AND SOURCE STATUS",
        f"Source health: {plan.source_health}",
        f"Payload refreshed: {'Yes' if plan.payload_refreshed else 'No'}",
        f"Payload hash: {payload_hash}",
        f"Fetched records: {plan.data_status.get('fetched_records', 0)}",
        f"New records: {plan.data_status.get('new_records', 0)}",
        f"Amendments: {plan.data_status.get('material_amendments', 0)}",
        f"Automatically excluded records: {plan.data_status.get('excluded_records', 0)}",
        f"Manual review required: {plan.data_status.get('review_required', 0)}",
        "",
        "CHANGES SINCE THE PREVIOUS DIGEST",
        f"New qualifying tickers: {plan.changes_since_previous.get('new_qualifying_tickers', 0)}",
        f"New disclosures on active tickers: {plan.changes_since_previous.get('new_disclosures_on_active_tickers', 0)}",
        f"Classification changes: {plan.changes_since_previous.get('classification_changes', 0)}",
        f"Material amendments: {plan.changes_since_previous.get('material_amendments', 0)}",
        f"Expired tickers: {plan.changes_since_previous.get('expired_tickers', 0)}",
    ]
    if plan.summary_lines:
        lines.extend(["", *plan.summary_lines])
    return "\n".join(lines)


def _render_zero_activity(plan: PoliticalDigestPlan, today: date) -> str:
    if plan.source_health != "HEALTHY":
        status_lines = [
            "Scan completed, but source freshness or completeness needs review.",
            "No zero-activity conclusion is being asserted for this run.",
        ]
    else:
        status_lines = [
            "Scan completed successfully.",
            "No qualifying political disclosures met the digest criteria in this run.",
        ]
    return "\n".join(
        [
            _render_header(plan, today),
            "",
            *status_lines,
            "",
            "DELIVERY RECONCILIATION",
            f"Valid new or amended records: {plan.delivery_reconciliation.get('valid_new_or_amended_records', 0)}",
            f"Included in digest: {plan.delivery_reconciliation.get('included_in_digest', 0)}",
            f"Review required: {plan.delivery_reconciliation.get('review_required', 0)}",
            f"Pending retry: {plan.delivery_reconciliation.get('pending_retry', 0)}",
        ]
    )


def _render_blocks(plan: PoliticalDigestPlan, *, now_sg: date | datetime) -> list[RenderedDigestPart]:
    today = now_sg.date() if isinstance(now_sg, datetime) else now_sg
    if not _all_flags(plan) and not plan.review_required_items and not plan.excluded_items and not plan.expired_watchlist_items:
        if not plan.send_digest:
            return []
        return [RenderedDigestPart(_render_zero_activity(plan, today))]

    blocks = [RenderedDigestPart(_render_header(plan, today))]
    sections = [
        ("NEW DISCLOSURES", plan.new_material_flags, _full_new_disclosure),
        ("MATERIAL SIGNAL UPDATES", plan.material_updates, _material_update),
        ("ROLLING SEVEN-DAY WATCHLIST", plan.active_watchlist_items, _watchlist_item),
        ("ROLLING LATE-FILING ACTIVITY", plan.other_new_activity, _below_threshold),
    ]
    for heading, flags, formatter in sections:
        if not flags:
            continue
        blocks.append(RenderedDigestPart(heading))
        for flag in flags:
            blocks.append(RenderedDigestPart(formatter(flag), (flag.ticker,)))
    if plan.review_required_items or plan.excluded_items:
        blocks.extend(_automatic_exclusion_blocks(plan.excluded_items))
        blocks.extend(_manual_review_blocks(plan.review_required_items))
    if plan.expired_watchlist_items:
        blocks.append(RenderedDigestPart("EXPIRED TODAY"))
        for flag in plan.expired_watchlist_items:
            blocks.append(RenderedDigestPart(_expired(flag), (flag.ticker,)))
    blocks.append(
        RenderedDigestPart(
            "\n".join(
                [
                    "DELIVERY RECONCILIATION",
                    f"Valid new or amended records: {plan.delivery_reconciliation.get('valid_new_or_amended_records', 0)}",
                    f"Included in digest: {plan.delivery_reconciliation.get('included_in_digest', 0)}",
                    f"Review required: {plan.delivery_reconciliation.get('review_required', 0)}",
                    f"Successfully delivered: {plan.delivery_reconciliation.get('successfully_delivered', 0)}",
                    f"Pending retry: {plan.delivery_reconciliation.get('pending_retry', 0)}",
                ]
            )
        )
    )
    return blocks


def render_digest(plan: PoliticalDigestPlan, *, now_sg: date | datetime) -> str | None:
    blocks = _render_blocks(plan, now_sg=now_sg)
    if not blocks:
        return None
    return "\n\n".join(block.text for block in blocks).strip()


def render_digest_parts(plan: PoliticalDigestPlan, *, now_sg: date | datetime, limit: int = TELEGRAM_LIMIT) -> list[RenderedDigestPart]:
    blocks = _render_blocks(plan, now_sg=now_sg)
    if not blocks:
        return []
    parts: list[RenderedDigestPart] = []
    current_text = ""
    current_tickers: list[str] = []
    for block in blocks:
        candidate = block.text if not current_text else f"{current_text}\n\n{block.text}"
        if current_text and len(candidate) > limit:
            parts.append(RenderedDigestPart(current_text, tuple(dict.fromkeys(current_tickers))))
            current_text = block.text
            current_tickers = list(block.tickers)
            continue
        current_text = candidate
        current_tickers.extend(block.tickers)
    if current_text:
        parts.append(RenderedDigestPart(current_text, tuple(dict.fromkeys(current_tickers))))
    if len(parts) <= 1:
        return parts
    return [
        RenderedDigestPart(f"{part.text}\n\nPart {index} of {len(parts)}", part.tickers)
        for index, part in enumerate(parts, start=1)
    ]


def chunk_digest(text: str, *, limit: int = TELEGRAM_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    sections = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for section in sections:
        candidate = section if not current else f"{current}\n\n{section}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(section) <= limit:
            current = section
            continue
        lines = section.splitlines()
        section_chunk = ""
        for line in lines:
            next_value = line if not section_chunk else f"{section_chunk}\n{line}"
            if len(next_value) <= limit:
                section_chunk = next_value
                continue
            if section_chunk:
                chunks.append(section_chunk)
            section_chunk = line
        if section_chunk:
            current = section_chunk
    if current:
        chunks.append(current)
    if len(chunks) <= 1:
        return chunks
    return [f"{chunk}\n\nPart {index} of {len(chunks)}" for index, chunk in enumerate(chunks, start=1)]


def digest_log_rows(
    plan: PoliticalDigestPlan,
    *,
    run_id: str,
    payload_hash: str,
    telegram_included: bool,
    telegram_sent_at: str = "",
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    timestamp = created_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    digest_id = plan.pending_snapshot.digest_id if plan.pending_snapshot is not None else ""
    delivery_status = plan.pending_snapshot.digest_status if plan.pending_snapshot is not None else ""
    rows: list[dict[str, Any]] = []
    rank = 1
    for flag in _all_flags(plan):
        rows.append(
            {
                "Digest ID": digest_id,
                "Digest Date": plan.digest_date,
                "Run ID": run_id,
                "Ticker": flag.ticker,
                "Digest Section": flag.section,
                "Flag Rank": rank,
                "Flag Category": flag.flag_category,
                "Flag Reasons": json.dumps(list(flag.flag_reasons), sort_keys=True),
                "Previous Classification": flag.history.previous_classification,
                "Current Classification": flag.history.primary_classification,
                "Summary Hash": flag.history.summary_hash,
                "Trigger Trade Keys": json.dumps(list(flag.trigger_trade_keys), sort_keys=True),
                "Release Types": json.dumps(list(flag.release_types), sort_keys=True),
                "Delivery Status": delivery_status,
                "Telegram Included": "YES" if telegram_included else "NO",
                "Telegram Sent At": telegram_sent_at,
                "Payload Hash": payload_hash,
                "Created At": timestamp,
            }
        )
        rank += 1
    return rows


def write_digest_preview(path: Path, text: str | None) -> None:
    if text is None:
        path.write_text("No digest rendered.\n", encoding="utf-8")
        return
    path.write_text(text + "\n", encoding="utf-8")
