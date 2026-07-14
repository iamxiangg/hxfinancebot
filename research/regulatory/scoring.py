from __future__ import annotations

from scanners.no_llm_guard import require_no_llm
from research.regulatory.models import DimensionAssessment, NormalizedRegulatoryEvent

require_no_llm()


EVENT_DIRECTION_RULES: dict[str, dict[str, int]] = {
    "PRIMARY_ENDPOINT_MET": {"Clinical": 3, "Regulatory": 2, "Timeline": 1},
    "PRIMARY_ENDPOINT_MISSED": {"Clinical": -3, "Regulatory": -2, "Timeline": -2},
    "FIRST_PATIENT_DOSED": {"Operational": 1, "Timeline": 1},
    "ENROLLMENT_COMPLETE": {"Operational": 2, "Timeline": 1},
    "RESULTS_POSTED": {"Operational": 1},
    "NDA_SUBMITTED": {"Regulatory": 2, "Timeline": 1},
    "BLA_SUBMITTED": {"Regulatory": 2, "Timeline": 1},
    "SNDA_SUBMITTED": {"Regulatory": 2, "Timeline": 1},
    "APPLICATION_ACCEPTED": {"Regulatory": 2, "Timeline": 1},
    "PRIORITY_REVIEW": {"Regulatory": 2, "Timeline": 2},
    "PDUFA_DATE": {"Regulatory": 1, "Timeline": 1},
    "FDA_APPROVAL": {"Regulatory": 3, "Commercial": 2, "Timeline": 1},
    "COMPLETE_RESPONSE_LETTER": {"Regulatory": -3, "Timeline": -2, "CMC": -1},
    "CLINICAL_HOLD": {"Regulatory": -3, "Timeline": -3, "Operational": -2},
    "HOLD_REMOVED": {"Regulatory": 2, "Timeline": 2},
    "CMC_DEFICIENCY": {"CMC": -3, "Timeline": -2},
    "COMMERCIAL_LAUNCH": {"Commercial": 2},
    "FIRST_SHIPMENT": {"Commercial": 2},
    "FIRST_REVENUE": {"Commercial": 2},
    "FINANCING_EVENT": {"Funding": 3, "Dilution": -3},
    "REIMBURSEMENT_ESTABLISHED": {"Reimbursement": 3, "Commercial": 2},
}


def score_event_dimensions(event: NormalizedRegulatoryEvent) -> list[DimensionAssessment]:
    rules = EVENT_DIRECTION_RULES.get(event.normalized_event_type, {})
    assessments = [
        DimensionAssessment(dimension=dimension, score=score, rationale=event.normalized_event_type)
        for dimension, score in rules.items()
    ]
    event.dimension_assessments = assessments
    return assessments

