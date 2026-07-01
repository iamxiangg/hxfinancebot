from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Derivation and confidence enums
# ---------------------------------------------------------------------------

class DerivationType(str, Enum):
    API_FIELD = "API_FIELD"
    XBRL_FACT = "XBRL_FACT"
    DETERMINISTIC_PARSE = "DETERMINISTIC_PARSE"
    FORMULA = "FORMULA"
    RULE = "RULE"
    MANUAL = "MANUAL"
    UNAVAILABLE = "UNAVAILABLE"


class ConfidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class MappingStatus(str, Enum):
    EXACT = "EXACT"
    FUZZY_SUGGESTED = "FUZZY_SUGGESTED"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class MappingConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    SUGGESTED_REVIEW = "SUGGESTED_REVIEW"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Core provenance dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEvidence:
    """Provenance record for a value extracted from an external source."""

    source: str
    source_record_id: str = ""
    source_url: str = ""
    source_field: str = ""
    source_document_type: str = ""
    raw_payload_hash: str = ""
    observed_at: str = ""

    @classmethod
    def from_payload(cls, *, source: str, record_id: str, url: str, field: str, payload: str, observed_at: str, doc_type: str = "") -> "SourceEvidence":
        return cls(
            source=source,
            source_record_id=record_id,
            source_url=url,
            source_field=field,
            source_document_type=doc_type,
            raw_payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20],
            observed_at=observed_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DerivedValue:
    """A value computed deterministically with full provenance."""

    value: Any
    unit: str = ""
    as_of: str = ""
    source_evidence: SourceEvidence | None = None
    formula_or_rule: str = ""
    derivation_type: DerivationType = DerivationType.UNAVAILABLE
    confidence_status: ConfidenceStatus = ConfidenceStatus.UNAVAILABLE
    observed_at: str = ""
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if not self.payload_hash:
            payload = f"{self.value}|{self.unit}|{self.as_of}|{self.formula_or_rule}|{self.derivation_type}"
            object.__setattr__(self, "payload_hash", hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManualValue:
    """A manually-entered value with source evidence requirement."""

    field_name: str
    value: Any = None
    unit: str = ""
    reason: str = ""
    source_evidence_url: str = ""
    entered_at: str = ""
    entered_by: str = ""
    status: ConfidenceStatus = ConfidenceStatus.MANUAL_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityMapping:
    """Canonical mapping between identifiers for an entity."""

    entity_id: str
    ticker: str = ""
    exchange: str = ""
    security_type: str = ""
    active: bool = True
    cik: str = ""
    sic: str = ""
    sic_description: str = ""
    current_legal_name: str = ""
    former_legal_names: tuple[str, ...] = ()
    former_tickers: tuple[str, ...] = ()
    parent_entity_id: str = ""
    subsidiary_legal_names: tuple[str, ...] = ()
    government_recipient_names: tuple[str, ...] = ()
    government_ueis: tuple[str, ...] = ()
    clinical_trial_sponsor_names: tuple[str, ...] = ()
    yahoo_ticker: str = ""
    mapping_status: MappingStatus = MappingStatus.UNAVAILABLE
    mapping_confidence: MappingConfidence = MappingConfidence.UNAVAILABLE
    evidence_url: str = ""
    last_verified: str = ""
    manual_override: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntityMappingSuggestion:
    """A potential mapping that requires manual review."""

    ticker: str
    suggested_entity_id: str
    suggested_name: str
    similarity_metric: str = ""
    similarity_score: float = 0.0
    evidence: SourceEvidence | None = None
    status: str = "MANUAL_REQUIRED"


@dataclass(frozen=True)
class EvidenceRecord:
    """Single record in the Evidence Ledger."""

    evidence_id: str
    entity_id: str
    ticker: str = ""
    source: str = ""
    source_record_id: str = ""
    source_document_type: str = ""
    event_type: str = ""
    event_date: str = ""
    observed_at: str = ""
    raw_payload_hash: str = ""
    derived_field: str = ""
    derived_value: Any = None
    unit: str = ""
    derivation_type: DerivationType = DerivationType.UNAVAILABLE
    formula_or_rule: str = ""
    confidence_status: ConfidenceStatus = ConfidenceStatus.UNAVAILABLE
    source_url: str = ""
    superseded_by: str = ""
    active: bool = True

    @classmethod
    def from_derived_value(
        cls,
        *,
        evidence_id: str,
        entity_id: str,
        ticker: str,
        source: str,
        source_record_id: str,
        source_document_type: str,
        event_type: str,
        event_date: str,
        observed_at: str,
        source_url: str,
        derived: DerivedValue,
    ) -> "EvidenceRecord":
        se = derived.source_evidence or SourceEvidence()
        return cls(
            evidence_id=evidence_id,
            entity_id=entity_id,
            ticker=ticker,
            source=source,
            source_record_id=source_record_id,
            source_document_type=source_document_type,
            event_type=event_type,
            event_date=event_date,
            observed_at=observed_at,
            raw_payload_hash=se.raw_payload_hash or derived.payload_hash,
            derived_field=se.source_field,
            derived_value=derived.value,
            unit=derived.unit,
            derivation_type=derived.derivation_type,
            formula_or_rule=derived.formula_or_rule,
            confidence_status=derived.confidence_status,
            source_url=source_url or se.source_url,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MaterialChange:
    """A detected material change event."""

    entity_id: str
    ticker: str
    change_type: str
    field_name: str
    old_value: Any
    new_value: Any
    evidence_id: str = ""
    previous_evidence_id: str = ""
    detected_at: str = ""
    rule_triggered: str = ""
    required_action: str = ""
    telegram_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskAssessment:
    """Deterministic risk classification with triggered rules."""

    entity_id: str
    ticker: str
    category: str
    band: str
    triggered_rules: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionRecommendation:
    """Output of the admission/displacement engine."""

    ticker: str
    decision: str
    proposed_weight_pct: float = 0.0
    funding_source: str = ""
    expected_cagr: float = 0.0
    risk_adjusted_return: float = 0.0
    portfolio_utility_improvement: float = 0.0
    concentration_change: str = ""
    financing_risk: str = ""
    feroldi_gate: str = ""
    thesis_direction: str = ""
    alternatives: list[DecisionRecommendation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["alternatives"] = [alt.to_dict() for alt in (self.alternatives or [])]
        return raw


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def stable_evidence_id(parts: list[str], *, prefix: str = "ev") -> str:
    """Generate a stable, reproducible evidence ID from deterministic parts."""
    payload = "|".join(str(p or "").strip() for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat() + "Z"


__all__ = [
    "DerivationType",
    "ConfidenceStatus",
    "MappingStatus",
    "MappingConfidence",
    "SourceEvidence",
    "DerivedValue",
    "ManualValue",
    "EntityMapping",
    "EntityMappingSuggestion",
    "EvidenceRecord",
    "MaterialChange",
    "RiskAssessment",
    "DecisionRecommendation",
    "stable_evidence_id",
    "now_iso",
]
