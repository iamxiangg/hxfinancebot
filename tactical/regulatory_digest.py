from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from research.regulatory.models import RegulatoryDigestPlan


TELEGRAM_LIMIT = 3800


def render_digest(plan: RegulatoryDigestPlan | None) -> str | None:
    if plan is None or not plan.send_digest:
        return None
    lines = [
        "REGULATORY / HEALTHCARE MATERIAL EVENTS",
        "",
        "DATA STATUS",
    ]
    for key, value in sorted(plan.data_status.items()):
        lines.append(f"{key.replace('_', ' ').title()}: {value}")
    if plan.material_events:
        lines.extend(["", "NEW MATERIAL EVENTS"])
        for item in plan.material_events:
            lines.extend(
                [
                    "",
                    f"{item.ticker} - {item.company_name}",
                    f"{item.product_name} - {item.indication_name}".strip(" -"),
                    "",
                    "EVENT",
                    item.event_summary,
                    "",
                    "GATE CHANGE",
                    item.gate_change or "No gate change recorded",
                    "",
                    "OUTCOME",
                    item.outcome.value,
                    "",
                    "PRIORITY",
                    item.priority.value,
                ]
            )
    if plan.state_updates:
        lines.extend(["", "MATERIAL STATE UPDATES"])
        for item in plan.state_updates:
            lines.append(f"{item.ticker} | {item.gate_change or item.event_summary}")
    if plan.other_activity_count:
        lines.extend(["", "OTHER RECORDED ACTIVITY", str(plan.other_activity_count)])
    if plan.unresolved_items:
        lines.extend(["", "UNRESOLVED ITEMS"])
        for item in plan.unresolved_items[:10]:
            lines.append(f"{item.company_name or item.ticker or item.source_name}: {item.reason}")
    return "\n".join(lines).strip()


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
        lines = section.splitlines() or [section]
        line_chunk = ""
        for line in lines:
            next_value = line if not line_chunk else f"{line_chunk}\n{line}"
            if len(next_value) <= limit:
                line_chunk = next_value
                continue
            if line_chunk:
                chunks.append(line_chunk)
            if len(line) <= limit:
                line_chunk = line
                continue
            start = 0
            while start < len(line):
                part = line[start:start + limit]
                if len(part) == limit:
                    chunks.append(part)
                else:
                    line_chunk = part
                start += limit
        current = line_chunk
    if current:
        chunks.append(current)
    if len(chunks) <= 1:
        return chunks
    return [f"{chunk}\n\nPart {idx} of {len(chunks)}" for idx, chunk in enumerate(chunks, start=1)]


def write_digest_preview(path: Path, text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text or "No regulatory digest rendered.") + "\n", encoding="utf-8")
