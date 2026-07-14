from __future__ import annotations

SEC_EXACT_PHRASES: dict[str, list[str]] = {
    "PRIMARY_ENDPOINT_MET": ["met the primary endpoint", "achieved the primary endpoint"],
    "PRIMARY_ENDPOINT_MISSED": ["did not meet the primary endpoint", "missed the primary endpoint", "failed to meet the primary endpoint"],
    "NDA_SUBMITTED": ["submitted an nda", "nda submitted", "submitted its nda"],
    "BLA_SUBMITTED": ["submitted a bla", "bla submitted", "submitted its bla"],
    "SNDA_SUBMITTED": ["submitted an snda", "snda submitted"],
    "APPLICATION_ACCEPTED": ["fda accepted the application", "application has been accepted for review", "accepted for filing"],
    "PRIORITY_REVIEW": ["priority review"],
    "PDUFA_DATE": ["pdufa target action date", "target action date"],
    "COMPLETE_RESPONSE_LETTER": ["complete response letter"],
    "CLINICAL_HOLD": ["clinical hold"],
    "HOLD_REMOVED": ["clinical hold removed", "hold removed"],
    "FIRST_PATIENT_DOSED": ["first patient dosed", "first participant dosed"],
    "ENROLLMENT_COMPLETE": ["enrollment completed", "enrolment completed", "fully enrolled"],
    "COMMERCIAL_LAUNCH": ["commercial launch", "launched commercially"],
    "FIRST_SHIPMENT": ["first commercial shipment", "initial shipment"],
    "FIRST_REVENUE": ["first product revenue", "generated first revenue"],
    "FDA_APPROVAL": ["fda approved", "received fda approval", "approved by the fda"],
    "ADVISORY_COMMITTEE": ["advisory committee", "adcom"],
    "CMC_DEFICIENCY": ["manufacturing issue", "inspection issue", "cmc deficiency", "chemistry, manufacturing and controls deficiency"],
}


NUMERIC_PATTERNS: dict[str, str] = {
    "hazard_ratio": r"hazard ratio\s*(?:of|=)?\s*([0-9]*\.?[0-9]+)",
    "p_value": r"p[- ]value\s*(?:of|=)?\s*([0-9]*\.?[0-9]+)",
    "confidence_interval": r"confidence interval\s*(?:of|=)?\s*([0-9]*\.?[0-9]+)\s*(?:to|-)\s*([0-9]*\.?[0-9]+)",
    "enrollment": r"(?:enrollment|enrolment)\s*(?:of|=)?\s*([0-9,]+)",
    "application_number": r"\b(?:nda|bla|snda)\s*(\d{4,})\b",
    "nct_id": r"\b(NCT\d{8})\b",
}


MATERIAL_EVENT_TYPES = {
    "PRIMARY_ENDPOINT_MET",
    "PRIMARY_ENDPOINT_MISSED",
    "NDA_SUBMITTED",
    "BLA_SUBMITTED",
    "APPLICATION_ACCEPTED",
    "PDUFA_DATE",
    "ADVISORY_COMMITTEE",
    "FDA_APPROVAL",
    "COMPLETE_RESPONSE_LETTER",
    "CLINICAL_HOLD",
    "HOLD_REMOVED",
    "CMC_DEFICIENCY",
    "COMMERCIAL_LAUNCH",
    "FIRST_SHIPMENT",
    "FIRST_REVENUE",
    "FINANCING_EVENT",
}


SEC_HISTORICAL_PRECEDENT_MARKERS = {
    "third_party_assets": (
        "brodalumab",
        "belimumab",
        "lumicef",
        "kyowa kirin",
        "gsk",
    ),
    "issuer_context": (
        "investigational agent",
        "not been established or approved by the fda",
        "systemic sclerosis",
        "tibulizumab",
        "zb-106",
    ),
}
