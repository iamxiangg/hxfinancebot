from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scanners.no_llm_guard import require_no_llm
from research.regulatory.entity_resolution import EntityResolutionResult
from research.regulatory.event_types import NUMERIC_PATTERNS, SEC_EXACT_PHRASES, SEC_HISTORICAL_PRECEDENT_MARKERS
from research.regulatory.identifiers import build_normalized_event_id, build_unresolved_issue_id
from research.regulatory.models import (
    DataCompleteness,
    EconomicAttributionConfidence,
    EventOutcome,
    EvidenceGrade,
    MappingConfidenceLevel,
    NormalizedRegulatoryEvent,
    RawRegulatoryRecord,
    SourceConfidence,
    SourceTier,
    UnresolvedEvent,
)
from research.regulatory.scoring import score_event_dimensions

require_no_llm()


@dataclass
class NormalizationResult:
    events: list[NormalizedRegulatoryEvent] = field(default_factory=list)
    unresolved: list[UnresolvedEvent] = field(default_factory=list)


def _regex_value(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return ""
    return " ".join(part for part in match.groups() if part is not None)


def _sentence_candidates(text: str) -> list[str]:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return []
    pieces = re.split(r"(?<=[.!?])\s+|(?<=;)\s+", compact)
    return [piece.strip() for piece in pieces if piece.strip()]


def _skip_sec_match(*, event_type: str, sentence: str, phrase: str) -> bool:
    lowered = sentence.lower()
    if event_type == "FDA_APPROVAL":
        if re.search(r"\b(?:not|never|unapproved)\b.{0,30}\bapproved by the fda\b", lowered):
            return True
        if re.search(r"\b(?:not|never)\b.{0,30}\bfda approved\b", lowered):
            return True
    if event_type == "CLINICAL_HOLD":
        boilerplate_patterns = (
            "imposes a clinical hold on any",
            "may impose a clinical hold",
            "could impose a clinical hold",
            "if the fda imposes a clinical hold",
        )
        if any(pattern in lowered for pattern in boilerplate_patterns):
            return True
    return False


def _find_sec_phrase(text: str, event_type: str, phrase: str) -> str:
    for sentence in _sentence_candidates(text):
        lowered = sentence.lower()
        if phrase not in lowered:
            continue
        if _skip_sec_match(event_type=event_type, sentence=sentence, phrase=phrase):
            continue
        return sentence[:1000]
    return ""


def _historical_precedent_summary(record: RawRegulatoryRecord) -> str:
    product_name = str(record.product_name or "company programme").strip()
    indication_name = str(record.indication_name or "the referenced indication").strip()
    return (
        f"Historical clinical-precedent material supporting {product_name} was identified in an issuer presentation. "
        f"Third-party evidence in {indication_name} supports pathway plausibility but does not constitute new company-specific clinical data."
    )


def _looks_like_historical_precedent(record: RawRegulatoryRecord) -> bool:
    text = str(record.exact_text or "").lower()
    if not text:
        return False
    has_third_party_asset = any(marker in text for marker in SEC_HISTORICAL_PRECEDENT_MARKERS["third_party_assets"])
    has_issuer_context = all(marker in text for marker in ("investigational agent", "not been established or approved by the fda"))
    mentions_internal_programme = any(marker in text for marker in SEC_HISTORICAL_PRECEDENT_MARKERS["issuer_context"])
    return bool(has_third_party_asset and has_issuer_context and mentions_internal_programme)


def _source_priority(tier: SourceTier) -> int:
    return {SourceTier.TIER_1: 1, SourceTier.TIER_2: 2, SourceTier.TIER_3: 3}[tier]


def _confidence_from_tier(tier: SourceTier) -> SourceConfidence:
    return {
        SourceTier.TIER_1: SourceConfidence.HIGH,
        SourceTier.TIER_2: SourceConfidence.MEDIUM,
        SourceTier.TIER_3: SourceConfidence.LOW,
    }[tier]


def _event(
    *,
    record: RawRegulatoryRecord,
    mapping: EntityResolutionResult,
    programme_key: str,
    normalized_event_type: str,
    event_date: str,
    factual_summary: str,
    exact_phrase: str = "",
    outcome: EventOutcome = EventOutcome.NO_STAGE_CHANGE,
    metadata: dict[str, Any] | None = None,
) -> NormalizedRegulatoryEvent:
    company_id = mapping.entity.company_id if mapping.entity else ""
    event = NormalizedRegulatoryEvent(
        normalized_event_id=build_normalized_event_id(
            company_id=company_id,
            programme_key=programme_key,
            normalized_event_type=normalized_event_type,
            event_date=event_date,
            source_record_id=record.source_record_id,
        ),
        raw_event_id=record.raw_event_id,
        event_date=event_date,
        normalized_event_type=normalized_event_type,
        source_name=record.source_name,
        source_url=record.source_url,
        company_id=company_id,
        ticker=(mapping.entity.ticker if mapping.entity else record.ticker),
        company_name=(mapping.entity.legal_name if mapping.entity else record.company_name),
        programme_key=programme_key,
        factual_summary=factual_summary,
        exact_phrase=exact_phrase,
        outcome=outcome,
        source_confidence=_confidence_from_tier(record.source_tier),
        mapping_confidence=mapping.confidence,
        evidence_grade=EvidenceGrade.MEDIUM,
        economic_attribution_confidence=EconomicAttributionConfidence.MANUAL_REQUIRED if mapping.manual_required else EconomicAttributionConfidence.HIGH,
        data_completeness=DataCompleteness.PARTIAL,
        source_priority=_source_priority(record.source_tier),
        metadata=metadata or {},
    )
    score_event_dimensions(event)
    return event


def _unresolved(record: RawRegulatoryRecord, reason: str) -> UnresolvedEvent:
    return UnresolvedEvent(
        unresolved_id=build_unresolved_issue_id(
            source_name=record.source_name,
            source_record_id=record.source_record_id,
            company_name=record.company_name,
            ticker=record.ticker,
            trial_nct_id=record.trial_nct_id,
            product_name=record.product_name,
            reason=reason,
        ),
        raw_event_id=record.raw_event_id,
        reason=reason,
        source_name=record.source_name,
        source_record_id=record.source_record_id,
        source_url=record.source_url,
        company_name=record.company_name,
        ticker=record.ticker,
        trial_nct_id=record.trial_nct_id,
        product_name=record.product_name,
        created_at=record.observed_at or record.published_at,
    )


def _normalize_sec(record: RawRegulatoryRecord, mapping: EntityResolutionResult, programme_key: str) -> NormalizationResult:
    text = str(record.exact_text or "").strip()
    lowered = text.lower()
    if not lowered:
        return NormalizationResult(unresolved=[_unresolved(record, "Missing SEC filing text.")])
    if _looks_like_historical_precedent(record):
        return NormalizationResult(
            events=[
                _event(
                    record=record,
                    mapping=mapping,
                    programme_key=programme_key,
                    normalized_event_type="HISTORICAL_CLINICAL_PRECEDENT",
                    event_date=(record.published_at or record.observed_at)[:10],
                    factual_summary=_historical_precedent_summary(record),
                    outcome=EventOutcome.NO_STAGE_CHANGE,
                    metadata={
                        "historical_precedent": True,
                        "product_name": record.product_name,
                        "indication_name": record.indication_name,
                    },
                )
            ]
        )
    events: list[NormalizedRegulatoryEvent] = []
    for event_type, phrases in SEC_EXACT_PHRASES.items():
        for phrase in phrases:
            matched_sentence = _find_sec_phrase(text, event_type, phrase)
            if matched_sentence:
                metadata = {
                    "nct_id": _regex_value(NUMERIC_PATTERNS["nct_id"], text),
                    "application_number": _regex_value(NUMERIC_PATTERNS["application_number"], text),
                    "hazard_ratio": _regex_value(NUMERIC_PATTERNS["hazard_ratio"], text),
                    "p_value": _regex_value(NUMERIC_PATTERNS["p_value"], text),
                    "confidence_interval": _regex_value(NUMERIC_PATTERNS["confidence_interval"], text),
                    "enrollment": _regex_value(NUMERIC_PATTERNS["enrollment"], text),
                    "trial_phase": str(record.structured_data.get("phase") or ""),
                }
                outcome = {
                    "PRIMARY_ENDPOINT_MET": EventOutcome.PASSED,
                    "PRIMARY_ENDPOINT_MISSED": EventOutcome.FAILED,
                    "COMPLETE_RESPONSE_LETTER": EventOutcome.FAILED,
                }.get(event_type, EventOutcome.PENDING if event_type in {"NDA_SUBMITTED", "BLA_SUBMITTED", "APPLICATION_ACCEPTED", "PDUFA_DATE"} else EventOutcome.NO_STAGE_CHANGE)
                events.append(
                    _event(
                        record=record,
                        mapping=mapping,
                        programme_key=programme_key,
                        normalized_event_type=event_type,
                        event_date=(record.published_at or record.observed_at)[:10],
                        factual_summary=matched_sentence,
                        exact_phrase=phrase,
                        outcome=outcome,
                        metadata=metadata,
                    )
                )
                break
    if not events:
        return NormalizationResult(unresolved=[_unresolved(record, "No unambiguous deterministic phrase match found.")])
    return NormalizationResult(events=events)


def _normalize_clinicaltrials(record: RawRegulatoryRecord, mapping: EntityResolutionResult, programme_key: str) -> NormalizationResult:
    data = record.structured_data
    events: list[NormalizedRegulatoryEvent] = []
    status = str(data.get("overall_status") or data.get("status") or "").upper()
    previous_status = str(data.get("previous_status") or "").upper()
    if status == "RECRUITING" and previous_status != status:
        events.append(_event(record=record, mapping=mapping, programme_key=programme_key, normalized_event_type="TRIAL_RECRUITING", event_date=(record.published_at or record.observed_at)[:10], factual_summary="ClinicalTrials.gov status updated to recruiting.", metadata={"trial_phase": str(data.get("phase") or "")}))
    if status in {"COMPLETED", "ACTIVE_NOT_RECRUITING"} and previous_status != status:
        events.append(_event(record=record, mapping=mapping, programme_key=programme_key, normalized_event_type="ENROLLMENT_COMPLETE", event_date=(record.published_at or record.observed_at)[:10], factual_summary="ClinicalTrials.gov status reached completed or active-not-recruiting.", metadata={"trial_phase": str(data.get("phase") or "")}))
    if data.get("results_first_posted"):
        events.append(_event(record=record, mapping=mapping, programme_key=programme_key, normalized_event_type="RESULTS_POSTED", event_date=str(data.get("results_first_posted"))[:10], factual_summary="ClinicalTrials.gov results were first posted.", outcome=EventOutcome.PENDING, metadata={"trial_phase": str(data.get("phase") or "")}))
    if not events:
        events.append(_event(record=record, mapping=mapping, programme_key=programme_key, normalized_event_type="CT_STUDY_UPDATE", event_date=(record.published_at or record.observed_at)[:10], factual_summary="ClinicalTrials.gov study metadata changed.", outcome=EventOutcome.NO_STAGE_CHANGE, metadata={"trial_phase": str(data.get("phase") or "")}))
    return NormalizationResult(events=events)


def _normalize_fda(record: RawRegulatoryRecord, mapping: EntityResolutionResult, programme_key: str) -> NormalizationResult:
    data = record.structured_data
    status = str(data.get("submission_status") or data.get("status") or "").lower()
    event_type = "FDA_APPLICATION_UPDATE"
    outcome = EventOutcome.NO_STAGE_CHANGE
    if "approved" in status:
        event_type = "FDA_APPROVAL"
        outcome = EventOutcome.PASSED
    elif "accepted" in status or "filing review" in status:
        event_type = "APPLICATION_ACCEPTED"
        outcome = EventOutcome.PENDING
    elif "priority" in status:
        event_type = "PRIORITY_REVIEW"
        outcome = EventOutcome.PENDING
    return NormalizationResult(
        events=[
            _event(
                record=record,
                mapping=mapping,
                programme_key=programme_key,
                normalized_event_type=event_type,
                event_date=(str(data.get("status_date") or record.published_at or record.observed_at))[:10],
                factual_summary=str(data.get("status_text") or data.get("submission_status") or "FDA application update."),
                outcome=outcome,
                metadata={"application_number": str(data.get("application_number") or "")},
            )
        ]
    )


def normalize_record(
    *,
    record: RawRegulatoryRecord,
    mapping: EntityResolutionResult,
    programme_key: str,
) -> NormalizationResult:
    if mapping.entity is None:
        return NormalizationResult(unresolved=[_unresolved(record, mapping.reason or "Company mapping unresolved.")])
    if record.source_name == "clinicaltrials":
        return _normalize_clinicaltrials(record, mapping, programme_key)
    if record.source_name == "sec":
        return _normalize_sec(record, mapping, programme_key)
    if record.source_name in {"drugs_at_fda", "openfda"}:
        return _normalize_fda(record, mapping, programme_key)
    return NormalizationResult(
        events=[
            _event(
                record=record,
                mapping=mapping,
                programme_key=programme_key,
                normalized_event_type=record.event_type or "SOURCE_UPDATE",
                event_date=(record.published_at or record.observed_at)[:10],
                factual_summary=record.exact_text or f"{record.source_name} source update.",
            )
        ]
    )
