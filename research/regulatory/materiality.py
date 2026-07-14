from __future__ import annotations

import hashlib
import json

from scanners.no_llm_guard import require_no_llm
from research.regulatory.event_types import MATERIAL_EVENT_TYPES
from research.regulatory.models import (
    NormalizedRegulatoryEvent,
    RegulatoryDigestFlag,
    ResearchPriority,
)

require_no_llm()


def event_summary_hash(event: NormalizedRegulatoryEvent) -> str:
    payload = {
        "ticker": event.ticker,
        "programme_key": event.programme_key,
        "event_type": event.normalized_event_type,
        "event_date": event.event_date,
        "outcome": event.outcome.value,
        "summary": event.factual_summary,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def event_state_hash(current_gate: str, next_catalyst: str, catalyst_date: str) -> str:
    payload = f"{current_gate}|{next_catalyst}|{catalyst_date}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def determine_priority(event: NormalizedRegulatoryEvent) -> ResearchPriority:
    if event.outcome.value == "UNRESOLVED":
        return ResearchPriority.UNRESOLVED
    if event.normalized_event_type in {
        "FDA_APPROVAL",
        "COMPLETE_RESPONSE_LETTER",
        "CLINICAL_HOLD",
        "PRIMARY_ENDPOINT_MET",
        "PRIMARY_ENDPOINT_MISSED",
        "CMC_DEFICIENCY",
        "FINANCING_EVENT",
    }:
        return ResearchPriority.URGENT
    if event.normalized_event_type == "HISTORICAL_CLINICAL_PRECEDENT":
        return ResearchPriority.CONTEXT
    if event.normalized_event_type in {"NDA_SUBMITTED", "BLA_SUBMITTED", "APPLICATION_ACCEPTED", "COMMERCIAL_LAUNCH"}:
        return ResearchPriority.HIGH
    if event.normalized_event_type in MATERIAL_EVENT_TYPES:
        return ResearchPriority.MONITOR
    return ResearchPriority.CONTEXT


def build_digest_flag(event: NormalizedRegulatoryEvent, *, company_name: str, product_name: str, indication_name: str, gate_change: str) -> RegulatoryDigestFlag:
    priority = determine_priority(event)
    return RegulatoryDigestFlag(
        event_id=event.normalized_event_id,
        ticker=event.ticker,
        company_name=company_name,
        product_name=product_name,
        indication_name=indication_name,
        event_summary=event.factual_summary,
        gate_change=gate_change,
        outcome=event.outcome,
        priority=priority,
        detailed=priority in {ResearchPriority.URGENT, ResearchPriority.HIGH},
        summary_hash=event_summary_hash(event),
    )


def should_repeat_alert(*, previous_hashes: set[str], new_hash: str) -> bool:
    return bool(new_hash and new_hash not in previous_hashes)
