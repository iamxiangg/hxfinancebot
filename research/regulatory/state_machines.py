from __future__ import annotations

from scanners.no_llm_guard import require_no_llm
from research.regulatory.models import GateDimension, NormalizedRegulatoryEvent, ProgrammeCurrentState, StateTransition
from research.regulatory.identifiers import stable_hash

require_no_llm()


STATE_DIMENSIONS: dict[GateDimension, list[str]] = {
    GateDimension.CLINICAL_EVIDENCE: [
        "NO_HUMAN_DATA",
        "EARLY_SAFETY_PENDING",
        "EARLY_SAFETY_ACCEPTABLE",
        "PROOF_OF_CONCEPT_PENDING",
        "POSITIVE_SIGNAL",
        "NEGATIVE_SIGNAL",
        "PIVOTAL_DATA_PENDING",
        "PIVOTAL_ENDPOINT_PASSED",
        "PIVOTAL_ENDPOINT_FAILED",
        "CONFIRMATORY_EVIDENCE_PENDING",
        "CONFIRMATORY_EVIDENCE_PASSED",
        "CONFIRMATORY_EVIDENCE_FAILED",
    ],
    GateDimension.TRIAL_OPERATIONS: [
        "PROTOCOL_FINAL",
        "REGULATORY_CLEARANCE",
        "FIRST_SITE_ACTIVATED",
        "RECRUITING",
        "FIRST_PATIENT_DOSED",
        "ENROLLMENT_25_PERCENT",
        "ENROLLMENT_50_PERCENT",
        "ENROLLMENT_COMPLETE",
        "DATABASE_LOCK",
        "TOPLINE_REPORTED",
        "COMPLETED",
        "TERMINATED",
        "WITHDRAWN",
    ],
    GateDimension.REGULATORY: [
        "PRE_IND",
        "IND_SUBMITTED",
        "IND_CLEARED",
        "CLINICAL_HOLD",
        "HOLD_REMOVED",
        "PRE_SUBMISSION_MEETING",
        "NDA_BLA_SUBMITTED",
        "APPLICATION_ACCEPTED",
        "PRIORITY_REVIEW",
        "ADCOM_PENDING",
        "FDA_DECISION_PENDING",
        "APPROVED",
        "CONDITIONAL_APPROVAL",
        "COMPLETE_RESPONSE_LETTER",
        "WITHDRAWN",
    ],
    GateDimension.CMC: [
        "CLINICAL_PROCESS",
        "COMMERCIAL_PROCESS_SELECTED",
        "TECH_TRANSFER_STARTED",
        "PROCESS_VALIDATION_STARTED",
        "ANALYTICAL_COMPARABILITY_PENDING",
        "ANALYTICAL_COMPARABILITY_ESTABLISHED",
        "PPQ_LOTS_PENDING",
        "PPQ_LOTS_COMPLETED",
        "STABILITY_DATA_PENDING",
        "FACILITY_INSPECTION_PENDING",
        "FACILITY_READY",
        "CMC_MODULE_READY",
        "CMC_DEFICIENCY",
    ],
    GateDimension.COMMERCIAL: [
        "PRE_LAUNCH",
        "COMMERCIAL_PREPARATION_ACTIVE",
        "LAUNCH_AUTHORISED",
        "FIRST_SHIPMENT",
        "FIRST_REVENUE",
        "INITIAL_ACCOUNT_APPROVAL",
        "INITIAL_ADOPTION",
        "REPEAT_ORDER_VALIDATED",
        "COMMERCIAL_SCALING",
        "MATURE_COMMERCIAL",
        "COMMERCIAL_STALL",
    ],
    GateDimension.REIMBURSEMENT: [
        "NO_COVERAGE",
        "TEMPORARY_REIMBURSEMENT",
        "TRANSITIONAL_PAYMENT_ACTIVE",
        "PERMANENT_CODE_ESTABLISHED",
        "TRANSITION_EXPIRING",
        "POST_TRANSITION_RATE_KNOWN",
        "POST_TRANSITION_UTILISATION_VALIDATED",
    ],
    GateDimension.DEVELOPMENT_STATUS: [
        "ACTIVE",
        "PAUSED",
        "UNFUNDED",
        "PARTNER_SEEKING",
        "DISCONTINUED",
        "RETURNED_TO_LICENSOR",
        "DIVESTED",
        "ACQUIRED",
    ],
    GateDimension.LEGAL_IP: [
        "PATENT_GRANTED",
        "GENERIC_CHALLENGE",
        "LITIGATION_PENDING",
        "DISTRICT_COURT_WON",
        "APPEAL_PENDING",
        "APPEAL_WON",
        "INJUNCTION_ACTIVE",
        "SETTLEMENT_FIXED",
        "SETTLEMENT_CONTINGENT",
        "PATENT_EXPIRY_APPROACHING",
    ],
}


EVENT_STATE_RULES: dict[str, tuple[GateDimension, str]] = {
    "PRIMARY_ENDPOINT_MET": (GateDimension.CLINICAL_EVIDENCE, "POSITIVE_SIGNAL"),
    "PRIMARY_ENDPOINT_MISSED": (GateDimension.CLINICAL_EVIDENCE, "NEGATIVE_SIGNAL"),
    "FIRST_PATIENT_DOSED": (GateDimension.TRIAL_OPERATIONS, "FIRST_PATIENT_DOSED"),
    "ENROLLMENT_COMPLETE": (GateDimension.TRIAL_OPERATIONS, "ENROLLMENT_COMPLETE"),
    "TRIAL_RECRUITING": (GateDimension.TRIAL_OPERATIONS, "RECRUITING"),
    "RESULTS_POSTED": (GateDimension.TRIAL_OPERATIONS, "TOPLINE_REPORTED"),
    "NDA_SUBMITTED": (GateDimension.REGULATORY, "NDA_BLA_SUBMITTED"),
    "BLA_SUBMITTED": (GateDimension.REGULATORY, "NDA_BLA_SUBMITTED"),
    "SNDA_SUBMITTED": (GateDimension.REGULATORY, "NDA_BLA_SUBMITTED"),
    "APPLICATION_ACCEPTED": (GateDimension.REGULATORY, "APPLICATION_ACCEPTED"),
    "PRIORITY_REVIEW": (GateDimension.REGULATORY, "PRIORITY_REVIEW"),
    "PDUFA_DATE": (GateDimension.REGULATORY, "FDA_DECISION_PENDING"),
    "ADVISORY_COMMITTEE": (GateDimension.REGULATORY, "ADCOM_PENDING"),
    "FDA_APPROVAL": (GateDimension.REGULATORY, "APPROVED"),
    "COMPLETE_RESPONSE_LETTER": (GateDimension.REGULATORY, "COMPLETE_RESPONSE_LETTER"),
    "CLINICAL_HOLD": (GateDimension.REGULATORY, "CLINICAL_HOLD"),
    "HOLD_REMOVED": (GateDimension.REGULATORY, "HOLD_REMOVED"),
    "CMC_DEFICIENCY": (GateDimension.CMC, "CMC_DEFICIENCY"),
    "COMMERCIAL_LAUNCH": (GateDimension.COMMERCIAL, "LAUNCH_AUTHORISED"),
    "FIRST_SHIPMENT": (GateDimension.COMMERCIAL, "FIRST_SHIPMENT"),
    "FIRST_REVENUE": (GateDimension.COMMERCIAL, "FIRST_REVENUE"),
    "REIMBURSEMENT_ESTABLISHED": (GateDimension.REIMBURSEMENT, "PERMANENT_CODE_ESTABLISHED"),
    "PROGRAMME_PAUSED": (GateDimension.DEVELOPMENT_STATUS, "PAUSED"),
    "PROGRAMME_DISCONTINUED": (GateDimension.DEVELOPMENT_STATUS, "DISCONTINUED"),
    "PATENT_CHALLENGE": (GateDimension.LEGAL_IP, "GENERIC_CHALLENGE"),
}


def validate_state_code(dimension: GateDimension, state_code: str) -> bool:
    return str(state_code or "").strip() in STATE_DIMENSIONS.get(dimension, [])


def event_state_target(event: NormalizedRegulatoryEvent) -> tuple[GateDimension, str] | None:
    target = EVENT_STATE_RULES.get(event.normalized_event_type)
    if target is None:
        return None
    if event.normalized_event_type == "PRIMARY_ENDPOINT_MET":
        phase = str(event.metadata.get("trial_phase") or "").lower()
        if "3" in phase or "pivotal" in phase:
            return GateDimension.CLINICAL_EVIDENCE, "PIVOTAL_ENDPOINT_PASSED"
    if event.normalized_event_type == "PRIMARY_ENDPOINT_MISSED":
        phase = str(event.metadata.get("trial_phase") or "").lower()
        if "3" in phase or "pivotal" in phase:
            return GateDimension.CLINICAL_EVIDENCE, "PIVOTAL_ENDPOINT_FAILED"
    return target


def apply_event_to_state(
    current_state: ProgrammeCurrentState,
    event: NormalizedRegulatoryEvent,
) -> tuple[ProgrammeCurrentState, StateTransition | None]:
    target = event_state_target(event)
    if target is None:
        return current_state, None
    dimension, new_state = target
    if not validate_state_code(dimension, new_state):
        return current_state, None
    attr_name = {
        GateDimension.CLINICAL_EVIDENCE: "clinical_evidence",
        GateDimension.TRIAL_OPERATIONS: "trial_operations",
        GateDimension.REGULATORY: "regulatory",
        GateDimension.CMC: "cmc",
        GateDimension.COMMERCIAL: "commercial",
        GateDimension.REIMBURSEMENT: "reimbursement",
        GateDimension.DEVELOPMENT_STATUS: "development_status",
        GateDimension.LEGAL_IP: "legal_ip",
    }[dimension]
    prior_state = str(getattr(current_state, attr_name) or "")
    if prior_state == new_state:
        return current_state, None
    setattr(current_state, attr_name, new_state)
    current_state.last_event_id = event.normalized_event_id
    current_state.last_updated_at = event.event_date
    current_state.current_gate = f"{dimension.value}:{new_state}"
    transition = StateTransition(
        transition_id=stable_hash([current_state.programme_key, dimension.value, event.normalized_event_id], prefix="gate"),
        programme_key=current_state.programme_key,
        dimension=dimension,
        prior_state=prior_state,
        new_state=new_state,
        event_id=event.normalized_event_id,
        effective_at=event.event_date,
        reason=event.factual_summary,
        reconstructed=event.reconstructed,
        source_url=event.source_url,
    )
    return current_state, transition

