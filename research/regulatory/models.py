from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


class SerializableDataclass:
    def to_dict(self) -> dict[str, Any]:
        return {key: _serialize(value) for key, value in asdict(self).items()}


class SourceTier(str, Enum):
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    TIER_3 = "TIER_3"


class CompanyOperatingMode(str, Enum):
    PRECLINICAL = "PRECLINICAL"
    CLINICAL_STAGE = "CLINICAL_STAGE"
    PRE_COMMERCIAL = "PRE_COMMERCIAL"
    EARLY_COMMERCIAL = "EARLY_COMMERCIAL"
    SCALED_COMMERCIAL = "SCALED_COMMERCIAL"
    PROFITABLE_COMMERCIAL = "PROFITABLE_COMMERCIAL"
    MULTI_PRODUCT_SPECIALTY_PHARMA = "MULTI_PRODUCT_SPECIALTY_PHARMA"
    PROCEDURE_PLATFORM = "PROCEDURE_PLATFORM"
    HOLDING_COMPANY = "HOLDING_COMPANY"
    UNKNOWN = "UNKNOWN"


class RegulatoryProductType(str, Enum):
    DRUG = "DRUG"
    BIOLOGIC = "BIOLOGIC"
    GENE_THERAPY = "GENE_THERAPY"
    CELL_THERAPY = "CELL_THERAPY"
    VACCINE = "VACCINE"
    MEDICAL_DEVICE = "MEDICAL_DEVICE"
    DRUG_DEVICE_COMBINATION = "DRUG_DEVICE_COMBINATION"
    DIAGNOSTIC = "DIAGNOSTIC"
    PROCEDURE_PLATFORM = "PROCEDURE_PLATFORM"
    UNKNOWN = "UNKNOWN"


class EventOutcome(str, Enum):
    PASSED = "PASSED"
    CONDITIONAL_PASS = "CONDITIONAL_PASS"
    FAILED = "FAILED"
    DELAYED = "DELAYED"
    PENDING = "PENDING"
    NO_STAGE_CHANGE = "NO_STAGE_CHANGE"
    DATA_CORRECTION = "DATA_CORRECTION"
    UNRESOLVED = "UNRESOLVED"


class SourceConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNAVAILABLE = "UNAVAILABLE"


class MappingConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceGrade(str, Enum):
    HIGH = "HIGH"
    MEDIUM_HIGH = "MEDIUM_HIGH"
    MEDIUM = "MEDIUM"
    LOW_MEDIUM = "LOW_MEDIUM"
    LOW = "LOW"
    PENDING = "PENDING"
    UNAVAILABLE = "UNAVAILABLE"


class EconomicAttributionConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class DataCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    SPARSE = "SPARSE"
    CONFLICTING = "CONFLICTING"
    UNAVAILABLE = "UNAVAILABLE"


class GateDimension(str, Enum):
    CLINICAL_EVIDENCE = "CLINICAL_EVIDENCE"
    TRIAL_OPERATIONS = "TRIAL_OPERATIONS"
    REGULATORY = "REGULATORY"
    CMC = "CMC"
    COMMERCIAL = "COMMERCIAL"
    REIMBURSEMENT = "REIMBURSEMENT"
    DEVELOPMENT_STATUS = "DEVELOPMENT_STATUS"
    LEGAL_IP = "LEGAL_IP"


class EndpointRole(str, Enum):
    PRIMARY = "PRIMARY"
    KEY_SECONDARY = "KEY_SECONDARY"
    SECONDARY = "SECONDARY"
    EXPLORATORY = "EXPLORATORY"
    POST_HOC = "POST_HOC"
    SUBGROUP = "SUBGROUP"


class EndpointStatisticalState(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    FAVOURABLE_TREND = "FAVOURABLE_TREND"
    NOT_POWERED = "NOT_POWERED"
    IMMATURE = "IMMATURE"
    NOT_REPORTED = "NOT_REPORTED"


class OwnershipRelationship(str, Enum):
    WHOLLY_OWNED = "WHOLLY_OWNED"
    MAJORITY_OWNED = "MAJORITY_OWNED"
    MINORITY_OWNED = "MINORITY_OWNED"
    JOINT_VENTURE = "JOINT_VENTURE"
    LICENSEE = "LICENSEE"
    LICENSOR = "LICENSOR"
    COMMERCIAL_PARTNER = "COMMERCIAL_PARTNER"
    DIVESTED = "DIVESTED"
    ACQUIRED = "ACQUIRED"
    PUBLIC_SUBSIDIARY = "PUBLIC_SUBSIDIARY"


class ValuationStatus(str, Enum):
    MODEL_INCOMPLETE = "MODEL_INCOMPLETE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DE_RISKING_NOT_PRICED = "DE_RISKING_NOT_PRICED"
    PARTIALLY_PRICED = "PARTIALLY_PRICED"
    FAIRLY_PRICED = "FAIRLY_PRICED"
    PRICING_SUBSTANTIAL_SUCCESS = "PRICING_SUBSTANTIAL_SUCCESS"
    VALUATION_AHEAD_OF_EVIDENCE = "VALUATION_AHEAD_OF_EVIDENCE"
    FUNDING_RISK_DOMINATES = "FUNDING_RISK_DOMINATES"


class ResearchPriority(str, Enum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    MONITOR = "MONITOR"
    CONTEXT = "CONTEXT"
    UNRESOLVED = "UNRESOLVED"


class AnnouncementTiming(str, Enum):
    PREMARKET = "PREMARKET"
    INTRADAY = "INTRADAY"
    AFTERHOURS = "AFTERHOURS"
    UNKNOWN = "UNKNOWN"


class TimingConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class NonClinicalAssetClass(str, Enum):
    LEGAL_SETTLEMENT = "LEGAL_SETTLEMENT"
    PATENT_LITIGATION = "PATENT_LITIGATION"
    ROYALTY_STREAM = "ROYALTY_STREAM"
    PUBLIC_EQUITY_STAKE = "PUBLIC_EQUITY_STAKE"
    PRIVATE_EQUITY_STAKE = "PRIVATE_EQUITY_STAKE"
    CONTINGENT_MILESTONE = "CONTINGENT_MILESTONE"
    DIVESTITURE_RECEIVABLE = "DIVESTITURE_RECEIVABLE"
    ACQUISITION_ASSET = "ACQUISITION_ASSET"
    SHARE_REPURCHASE = "SHARE_REPURCHASE"


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


@dataclass
class RawRegulatoryRecord(SerializableDataclass):
    raw_event_id: str = ""
    source_name: str = ""
    source_record_id: str = ""
    source_url: str = ""
    source_document_type: str = ""
    source_tier: SourceTier = SourceTier.TIER_3
    published_at: str = ""
    observed_at: str = ""
    event_type: str = ""
    company_name: str = ""
    ticker: str = ""
    cik: str = ""
    product_name: str = ""
    indication_name: str = ""
    regimen_name: str = ""
    trial_nct_id: str = ""
    jurisdiction: str = "US"
    exact_text: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    structured_data: dict[str, Any] = field(default_factory=dict)
    payload_hash: str = ""
    payload_path: str = ""
    amendment_of: str = ""
    version: int = 1
    active: bool = True

    def __post_init__(self) -> None:
        if not self.payload_hash:
            self.payload_hash = _hash_payload(
                {
                    "raw_payload": self.raw_payload,
                    "exact_text": self.exact_text,
                    "structured_data": self.structured_data,
                }
            )


@dataclass
class CompanyEntity(SerializableDataclass):
    company_id: str
    legal_name: str = ""
    ticker: str = ""
    exchange: str = ""
    cik: str = ""
    country: str = "US"
    company_type: str = "PUBLIC_COMPANY"
    operating_mode: CompanyOperatingMode = CompanyOperatingMode.UNKNOWN
    source_url: str = ""


@dataclass
class OwnershipEdge(SerializableDataclass):
    ownership_edge_id: str
    parent_entity_id: str
    child_entity_id: str
    parent_ticker: str = ""
    child_ticker: str = ""
    legal_relationship: OwnershipRelationship = OwnershipRelationship.MINORITY_OWNED
    ownership_percentage: float | None = None
    voting_percentage: float | None = None
    economic_percentage: float | None = None
    consolidation_status: str = ""
    territory: str = ""
    effective_date: str = ""
    end_date: str = ""
    source_url: str = ""
    confidence: MappingConfidenceLevel = MappingConfidenceLevel.UNAVAILABLE
    active: bool = True


@dataclass
class Franchise(SerializableDataclass):
    franchise_id: str
    canonical_name: str = ""
    platform_type: str = ""
    notes: str = ""


@dataclass
class Product(SerializableDataclass):
    product_id: str
    canonical_name: str = ""
    aliases: list[str] = field(default_factory=list)
    former_names: list[str] = field(default_factory=list)
    modality: str = ""
    regulatory_product_type: RegulatoryProductType = RegulatoryProductType.UNKNOWN
    development_owner: str = ""
    regulatory_applicant: str = ""
    commercial_rights_holder: str = ""
    trial_sponsor: str = ""
    ind_holder: str = ""
    jurisdiction: str = "US"
    territory_rights: str = ""
    royalty_rate: str = ""
    milestones: str = ""
    profit_share: str = ""
    supply_agreement: str = ""
    patent_expiry: str = ""
    exclusivity_expiry: str = ""


@dataclass
class Regimen(SerializableDataclass):
    regimen_id: str
    product_id: str
    route: str = ""
    dose: str = ""
    schedule: str = ""
    combination_partners: list[str] = field(default_factory=list)
    background_standard_of_care: str = ""
    disease_stage: str = ""


@dataclass
class Indication(SerializableDataclass):
    indication_id: str
    product_id: str
    disease: str = ""
    disease_stage: str = ""
    biomarker_population: str = ""
    line_of_therapy: str = ""
    age_group: str = ""
    jurisdiction: str = "US"


@dataclass
class Trial(SerializableDataclass):
    trial_id: str
    nct_id: str = ""
    sponsor: str = ""
    official_title: str = ""
    product_id: str = ""
    indication_id: str = ""
    regimen_id: str = ""
    phase: str = ""
    company_sponsored: bool = False
    investigator_sponsored: bool = False
    design: str = ""
    comparator_quality: str = ""
    enrollment: int | None = None
    primary_endpoint: str = ""
    current_operational_state: str = ""


@dataclass
class EndpointResult(SerializableDataclass):
    endpoint_id: str
    trial_id: str = ""
    endpoint_name: str = ""
    endpoint_role: EndpointRole = EndpointRole.SECONDARY
    statistical_state: EndpointStatisticalState = EndpointStatisticalState.NOT_REPORTED
    effect_size: str = ""
    hazard_ratio: float | None = None
    confidence_interval_low: float | None = None
    confidence_interval_high: float | None = None
    p_value: float | None = None
    is_hierarchically_tested: bool = False
    data_cutoff_date: str = ""
    evidence_text: str = ""


@dataclass
class RegulatoryApplication(SerializableDataclass):
    application_id: str
    company_id: str = ""
    product_id: str = ""
    indication_id: str = ""
    application_type: str = ""
    application_number: str = ""
    jurisdiction: str = "US"
    regulator: str = "FDA"
    submission_date: str = ""
    acceptance_date: str = ""
    target_action_date: str = ""
    priority_review: bool = False
    status: str = ""


@dataclass
class EconomicRight(SerializableDataclass):
    economic_right_id: str
    programme_key: str = ""
    company_id: str = ""
    partner_company_id: str = ""
    legal_owner: str = ""
    development_owner: str = ""
    regulatory_applicant: str = ""
    commercial_rights_holder: str = ""
    territory: str = ""
    royalty_rate: str = ""
    milestones: str = ""
    license_obligations: str = ""
    profit_share: str = ""
    ownership_percentage: float | None = None
    economic_attribution_percentage: float | None = None
    effective_date: str = ""
    end_date: str = ""


@dataclass
class NonClinicalAsset(SerializableDataclass):
    asset_id: str
    company_id: str
    asset_class: NonClinicalAssetClass
    asset_name: str = ""
    description: str = ""
    attributable_percentage: float | None = None
    value: float | None = None
    currency: str = "USD"
    effective_date: str = ""


@dataclass
class ProgrammeIdentity(SerializableDataclass):
    programme_key: str
    company_id: str = ""
    economic_owner_id: str = ""
    product_id: str = ""
    regimen_id: str = ""
    indication_id: str = ""
    trial_id: str = ""
    jurisdiction: str = "US"
    company_name: str = ""
    ticker: str = ""
    product_name: str = ""
    indication_name: str = ""


@dataclass
class ProgrammeCurrentState(SerializableDataclass):
    programme_key: str
    company_id: str = ""
    product_id: str = ""
    indication_id: str = ""
    clinical_evidence: str = "NO_HUMAN_DATA"
    trial_operations: str = ""
    regulatory: str = ""
    cmc: str = ""
    commercial: str = ""
    reimbursement: str = ""
    development_status: str = "ACTIVE"
    legal_ip: str = ""
    last_event_id: str = ""
    last_updated_at: str = ""
    current_gate: str = ""
    next_catalyst: str = ""
    catalyst_date: str = ""
    date_precision: str = ""


@dataclass
class StateTransition(SerializableDataclass):
    transition_id: str
    programme_key: str
    dimension: GateDimension
    prior_state: str = ""
    new_state: str = ""
    event_id: str = ""
    effective_at: str = ""
    reason: str = ""
    reconstructed: bool = False
    source_url: str = ""


@dataclass
class DimensionAssessment(SerializableDataclass):
    dimension: str
    score: int = 0
    rationale: str = ""


@dataclass
class MarketSnapshot(SerializableDataclass):
    snapshot_id: str
    ticker: str
    event_date: str
    previous_close: float | None = None
    event_close: float | None = None
    next_close: float | None = None
    five_session_close: float | None = None
    twenty_session_close: float | None = None
    current_close: float | None = None
    spy_relative_return: float | None = None
    xbi_relative_return: float | None = None
    observed_price_direction: str = ""
    announcement_timing: AnnouncementTiming = AnnouncementTiming.UNKNOWN
    timing_confidence: TimingConfidence = TimingConfidence.LOW
    trading_volume: float | None = None


@dataclass
class FinancialSnapshot(SerializableDataclass):
    snapshot_id: str
    company_id: str
    as_of: str
    common_shares: float | None = None
    pre_funded_warrants: float | None = None
    traditional_warrants: float | None = None
    options: float | None = None
    convertible_notes: float | None = None
    atm_capacity: float | None = None
    contingent_shares: float | None = None
    issued_shares_post_offering: float | None = None
    exercised_unsettled_shares: float | None = None
    parent_cash: float | None = None
    subsidiary_cash: float | None = None
    restricted_cash: float | None = None
    consolidated_cash: float | None = None
    non_controlling_interest_cash: float | None = None
    attributable_cash: float | None = None
    total_debt: float | None = None
    source_url: str = ""

    @property
    def economic_shares(self) -> float:
        total = float(self.common_shares or 0.0)
        total += float(self.issued_shares_post_offering or 0.0)
        total += float(self.pre_funded_warrants or 0.0)
        total += float(self.exercised_unsettled_shares or 0.0)
        return total


@dataclass
class ValuationAssumption(SerializableDataclass):
    assumption_id: str
    programme_key: str = ""
    company_id: str = ""
    operating_mode: CompanyOperatingMode = CompanyOperatingMode.UNKNOWN
    active: bool = True
    success_ev: float | None = None
    failure_ev: float | None = None
    current_ev: float | None = None
    launch_year: int | None = None
    approval_probability: float | None = None
    eligible_population: float | None = None
    net_price: float | None = None
    peak_penetration: float | None = None
    peak_sales: float | None = None
    gross_margin: float | None = None
    patent_life_years: float | None = None
    future_dilution: float | None = None
    launch_costs: float | None = None
    manufacturing_scale_up: float | None = None
    sourced_fields: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""


@dataclass
class ValuationSnapshot(SerializableDataclass):
    valuation_id: str
    programme_key: str = ""
    company_id: str = ""
    valuation_status: ValuationStatus = ValuationStatus.MODEL_INCOMPLETE
    attributable_value: float | None = None
    success_ev: float | None = None
    failure_ev: float | None = None
    current_ev: float | None = None
    market_implied_probability: float | None = None
    equity_value: float | None = None
    per_share_value: float | None = None
    notes: list[str] = field(default_factory=list)
    updated_at: str = ""


@dataclass
class UnresolvedEvent(SerializableDataclass):
    unresolved_id: str
    raw_event_id: str
    reason: str
    source_name: str = ""
    source_record_id: str = ""
    source_url: str = ""
    company_name: str = ""
    ticker: str = ""
    trial_nct_id: str = ""
    product_name: str = ""
    required_action: str = "MANUAL_REVIEW_REQUIRED"
    conflicting_source: str = ""
    created_at: str = ""


@dataclass
class RegulatoryDigestFlag(SerializableDataclass):
    event_id: str
    ticker: str
    company_name: str
    product_name: str = ""
    indication_name: str = ""
    event_summary: str = ""
    gate_change: str = ""
    outcome: EventOutcome = EventOutcome.NO_STAGE_CHANGE
    priority: ResearchPriority = ResearchPriority.MONITOR
    detailed: bool = False
    summary_hash: str = ""
    state_hash: str = ""


@dataclass
class RegulatoryDigestPlan(SerializableDataclass):
    digest_date: str
    data_status: dict[str, Any] = field(default_factory=dict)
    material_events: list[RegulatoryDigestFlag] = field(default_factory=list)
    state_updates: list[RegulatoryDigestFlag] = field(default_factory=list)
    active_watchlist: list[dict[str, Any]] = field(default_factory=list)
    other_activity_count: int = 0
    unresolved_items: list[UnresolvedEvent] = field(default_factory=list)
    send_digest: bool = False
    preview_text: str = ""


@dataclass
class SourceCheckpoint(SerializableDataclass):
    source_name: str
    cursor: str = ""
    last_success_at: str = ""
    last_event_at: str = ""
    bootstrap_complete: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedRegulatoryEvent(SerializableDataclass):
    normalized_event_id: str
    raw_event_id: str
    event_date: str
    normalized_event_type: str
    source_name: str
    source_url: str = ""
    company_id: str = ""
    ticker: str = ""
    company_name: str = ""
    programme_key: str = ""
    product_id: str = ""
    regimen_id: str = ""
    indication_id: str = ""
    trial_id: str = ""
    application_id: str = ""
    factual_summary: str = ""
    exact_phrase: str = ""
    outcome: EventOutcome = EventOutcome.NO_STAGE_CHANGE
    dimension_assessments: list[DimensionAssessment] = field(default_factory=list)
    source_confidence: SourceConfidence = SourceConfidence.UNAVAILABLE
    mapping_confidence: MappingConfidenceLevel = MappingConfidenceLevel.UNAVAILABLE
    evidence_grade: EvidenceGrade = EvidenceGrade.UNAVAILABLE
    economic_attribution_confidence: EconomicAttributionConfidence = EconomicAttributionConfidence.UNAVAILABLE
    data_completeness: DataCompleteness = DataCompleteness.UNAVAILABLE
    source_priority: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)
    reconstructed: bool = False
    correction: bool = False
