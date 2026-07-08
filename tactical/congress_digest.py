from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from scanners.congress.digest_models import PoliticalDigestFlag, PoliticalDigestPlan
from scanners.congress.trend_classifier import deterministic_interpretation, score_label


TELEGRAM_LIMIT = 3800


@dataclass(frozen=True)
class RenderedDigestPart:
    text: str
    tickers: tuple[str, ...] = ()


def _money(value: float) -> str:
    number = float(value or 0.0)
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}m"
    if number >= 1_000:
        return f"${number / 1_000:.0f}k"
    return f"${number:.0f}"


def _pct(value: float) -> str:
    return f"{max(0.0, min(1.0, value)) * 100:.1f}%"


def _format_event(event: dict[str, Any]) -> list[str]:
    description = str(event.get("transaction_type") or "Unknown transaction")
    option_bits = []
    if event.get("option_side"):
        option_bits.append(str(event.get("option_side")).upper())
    if event.get("strike") not in ("", None):
        option_bits.append(f"strike {event['strike']}")
    if event.get("expiry"):
        option_bits.append(f"expiry {event['expiry']}")
    option_text = f" ({', '.join(option_bits)})" if option_bits else ""
    lines = [
        f"- {event.get('filer_name', 'Unknown filer')} [{event.get('owner_relationship', 'unknown')}]",
        f"  {description}{option_text} | {_money(float(event.get('amount_low') or 0.0))}-{_money(float(event.get('amount_high') or 0.0))}",
        f"  Trade {event.get('transaction_date', '')} | Filing {event.get('filing_date', '')} | Release {event.get('release_type', '')}",
    ]
    if event.get("document_url"):
        lines.append(f"  Source {event['document_url']}")
    return lines


def _format_window(label: str, window) -> list[str]:
    return [
        label,
        f"Purchases {window.purchase_count} | Partial sales {window.partial_sale_count} | Full sales {window.full_sale_count}",
        f"Unique buyers {window.unique_buyer_count} | Unique sellers {window.unique_seller_count}",
        (
            f"Stock {_money(window.stock_purchase_low)}-{_money(window.stock_purchase_high)} | "
            f"Call {_money(window.call_purchase_low)}-{_money(window.call_purchase_high)} | "
            f"Put {_money(window.put_purchase_low)}-{_money(window.put_purchase_high)} | "
            f"Sale {_money(window.sale_low)}-{_money(window.sale_high)}"
        ),
        (
            f"Conservative largest buyer share {_pct(window.largest_buyer_share_lower_bound)} | "
            f"Midpoint estimate {_pct(window.largest_buyer_share_midpoint_estimate)}"
        ),
    ]


def _new_dossier(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    windows = history.windows
    lines = [
        f"{history.ticker} - NEW POLITICAL DISCLOSURE",
        "",
        "WHY FLAGGED",
    ]
    lines.extend(f"- {reason}" for reason in history.flag_reasons[:5])
    lines.extend(["", "NEW EVENT(S)"])
    for event in history.new_events:
        lines.extend(_format_event(event))
    lines.extend(
        [
            "",
            "POLITICAL EVIDENCE",
            f"Primary classification {history.primary_classification}",
            f"Previous classification {history.previous_classification or 'INSUFFICIENT_EVIDENCE'}",
            f"Structure {history.structure_classification}",
            f"Bullish evidence {score_label(history.bullish_evidence_score)} ({history.bullish_evidence_score:.0f})",
            f"Distribution evidence {score_label(history.distribution_evidence_score)} ({history.distribution_evidence_score:.0f})",
            f"Breadth {score_label(history.breadth_score)} ({history.breadth_score:.0f})",
            f"Concentration {score_label(history.concentration_score)} ({history.concentration_score:.0f})",
            f"Inference confidence {history.inference_confidence}",
            f"Data confidence {history.data_confidence}",
            "",
        ]
    )
    lines.extend(_format_window("LAST 45 DAYS", windows[45]))
    lines.extend([""])
    lines.extend(_format_window("LAST 90 DAYS", windows[90]))
    lines.extend([""])
    lines.extend(_format_window("LAST 365 DAYS", windows[365]))
    lines.extend(["", "NOTABLE HISTORY"])
    if history.notable_history:
        lines.extend(f"- {item.get('text', '')}" for item in history.notable_history[:5])
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "INTERPRETATION",
            deterministic_interpretation(
                primary_classification=history.primary_classification,
                structure_classification=history.structure_classification,
                bullish_evidence=history.bullish_evidence_score,
                distribution_evidence=history.distribution_evidence_score,
                breadth_score=history.breadth_score,
                inference_confidence=history.inference_confidence,
            ),
            "",
            "CURRENT STATUS",
            f"Political conviction C{history.political_conviction:.0f}",
            f"Entry quality E{history.entry_quality:.0f}",
            f"Entry category {history.entry_category}",
            f"Risk flags {', '.join(history.risk_flags) if history.risk_flags else 'None'}",
        ]
    )
    return "\n".join(lines)


def _update_dossier(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    state = flag.watchlist_state
    lines = [
        f"{history.ticker} - POLITICAL SIGNAL UPDATE",
        "",
        "WHY UPDATED",
    ]
    lines.extend(f"- {change.reason}" for change in flag.material_changes)
    lines.extend(
        [
            "",
            "CHANGE",
            f"Previous classification / entry {history.previous_classification} / {(state.previous_entry_category if state else 'OTHER')}",
            f"Current classification / entry {history.primary_classification} / {history.entry_category}",
            "",
            "NEW POLITICAL DISCLOSURE",
            "None" if not history.new_events else "See updated event details below.",
        ]
    )
    if history.new_events:
        lines.append("")
        for event in history.new_events:
            lines.extend(_format_event(event))
    lines.extend(
        [
            "",
            "POLITICAL EVIDENCE",
            f"Current classification {history.primary_classification}",
            f"Structure {history.structure_classification}",
            f"Bullish evidence {score_label(history.bullish_evidence_score)} ({history.bullish_evidence_score:.0f})",
            f"Distribution evidence {score_label(history.distribution_evidence_score)} ({history.distribution_evidence_score:.0f})",
            f"Breadth {score_label(history.breadth_score)} ({history.breadth_score:.0f})",
            f"Concentration {score_label(history.concentration_score)} ({history.concentration_score:.0f})",
            f"Inference confidence {history.inference_confidence}",
            "",
            "CURRENT STATUS",
            f"Political conviction C{history.political_conviction:.0f}",
            f"Entry quality E{history.entry_quality:.0f}",
            f"Entry category {history.entry_category}",
            f"Primary risk {(state.primary_risk if state else 'None')}",
            "",
            "INTERPRETATION",
            deterministic_interpretation(
                primary_classification=history.primary_classification,
                structure_classification=history.structure_classification,
                bullish_evidence=history.bullish_evidence_score,
                distribution_evidence=history.distribution_evidence_score,
                breadth_score=history.breadth_score,
                inference_confidence=history.inference_confidence,
            ),
        ]
    )
    return "\n".join(lines)


def _watchlist_compact(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    state = flag.watchlist_state
    assert state is not None
    return "\n".join(
        [
            f"{history.ticker} | Watch Day {max(1, state.watchlist_day)} of {max(1, state.watchlist_total_days)}",
            history.primary_classification.replace("_", " ").title(),
            f"Political C{history.political_conviction:.0f} | Entry E{history.entry_quality:.0f} | Status: {history.entry_category}",
            f"Latest material event: {state.latest_material_event or 'No new political disclosure'}",
            f"Primary risk: {state.primary_risk}",
            "No new political disclosure",
        ]
    )


def _other_new_activity(flag: PoliticalDigestFlag) -> str:
    history = flag.history
    event = history.new_events[-1] if history.new_events else {}
    latest = str(event.get("transaction_type") or "activity").strip()
    amount_low = float(event.get("amount_low") or 0.0)
    amount_high = float(event.get("amount_high") or 0.0)
    amount = ""
    if amount_low > 0 or amount_high > 0:
        amount = f" | {_money(amount_low)}-{_money(amount_high)}"
    return (
        f"{history.ticker} | {history.primary_classification} | {latest}{amount} | "
        f"Political C{history.political_conviction:.0f} | Entry {history.entry_category}"
    )


def _all_flags(plan: PoliticalDigestPlan) -> tuple[PoliticalDigestFlag, ...]:
    return (
        *plan.new_material_flags,
        *plan.material_updates,
        *plan.active_watchlist_items,
        *plan.other_new_activity,
    )


def _render_blocks(plan: PoliticalDigestPlan, *, now_sg: date | datetime) -> list[RenderedDigestPart]:
    today = now_sg.date() if isinstance(now_sg, datetime) else now_sg
    if not _all_flags(plan):
        if not plan.send_digest:
            return []
        lines = [
            "DAILY POLITICAL-TRADING DIGEST",
            today.strftime("%d %B %Y"),
            "",
            "Scan completed successfully.",
            "No political disclosures met the digest criteria in this run.",
            "",
            "DATA STATUS",
            f"New records {plan.data_status.get('new_records', 0)}",
            f"Material amendments {plan.data_status.get('material_amendments', 0)}",
            f"Historical backfills {plan.data_status.get('historical_backfills', 0)}",
            f"Affected tickers {plan.data_status.get('affected_tickers', 0)}",
        ]
        return [RenderedDigestPart("\n".join(lines))]

    blocks = [
        RenderedDigestPart(
            "\n".join(
                [
                    "DAILY POLITICAL-TRADING DIGEST",
                    today.strftime("%d %B %Y"),
                    "",
                    "DATA STATUS",
                    f"New material signals: {plan.data_status.get('new_material_signals', 0)}",
                    f"Material updates: {plan.data_status.get('material_updates', 0)}",
                    f"Active watchlist reminders: {plan.data_status.get('active_watchlist_reminders', 0)}",
                    f"Other new activity: {plan.data_status.get('other_new_activity', 0)}",
                ]
                + list(plan.summary_lines)
            )
        )
    ]
    sections = [
        ("NEW MATERIAL SIGNALS", plan.new_material_flags, _new_dossier),
        ("MATERIAL SIGNAL UPDATES", plan.material_updates, _update_dossier),
        ("ACTIVE POLITICAL WATCHLIST", plan.active_watchlist_items, _watchlist_compact),
        ("OTHER NEW ACTIVITY", plan.other_new_activity, _other_new_activity),
    ]
    for heading, flags, formatter in sections:
        if not flags:
            continue
        blocks.append(RenderedDigestPart(heading))
        for flag in flags:
            blocks.append(RenderedDigestPart(formatter(flag), (flag.ticker,)))
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
    rows: list[dict[str, Any]] = []
    rank = 1
    for flag in _all_flags(plan):
        rows.append(
            {
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
