from __future__ import annotations

import hashlib


def _norm(value: str) -> str:
    return str(value or "").strip().upper()


def stable_hash(parts: list[str], *, prefix: str) -> str:
    payload = "|".join(_norm(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def stable_payload_hash(payload: str) -> str:
    return hashlib.sha256(str(payload or "").encode("utf-8")).hexdigest()[:20]


def build_product_id(*, canonical_name: str, development_owner_entity_id: str, modality: str) -> str:
    return stable_hash([canonical_name, development_owner_entity_id, modality], prefix="prd")


def build_regimen_id(
    *,
    product_id: str,
    route: str,
    dose: str,
    schedule: str,
    combination_partners: list[str],
    background_standard_of_care: str,
) -> str:
    return stable_hash(
        [
            product_id,
            route,
            dose,
            schedule,
            ",".join(sorted(_norm(item) for item in combination_partners)),
            background_standard_of_care,
        ],
        prefix="reg",
    )


def build_indication_id(
    *,
    product_id: str,
    disease: str,
    disease_stage: str,
    biomarker_population: str,
    line_of_therapy: str,
    age_group: str,
    jurisdiction: str,
) -> str:
    return stable_hash(
        [
            product_id,
            disease,
            disease_stage,
            biomarker_population,
            line_of_therapy,
            age_group,
            jurisdiction,
        ],
        prefix="ind",
    )


def build_trial_id(
    *,
    nct_id: str,
    sponsor: str,
    official_title: str,
    product_name: str,
    indication_name: str,
    phase: str,
) -> str:
    if _norm(nct_id):
        return _norm(nct_id)
    return stable_hash([sponsor, official_title, product_name, indication_name, phase], prefix="trl")


def build_programme_key(
    *,
    company_id: str,
    product_id: str,
    regimen_id: str,
    indication_id: str,
    jurisdiction: str,
) -> str:
    return stable_hash([company_id, product_id, regimen_id, indication_id, jurisdiction], prefix="pgm")


def build_raw_event_id(
    *,
    source: str,
    source_record_id: str,
    source_event_type: str,
    source_publication_date: str,
) -> str:
    return stable_hash([source, source_record_id, source_event_type, source_publication_date], prefix="raw")


def build_normalized_event_id(
    *,
    company_id: str,
    programme_key: str,
    normalized_event_type: str,
    event_date: str,
    source_record_id: str,
) -> str:
    return stable_hash(
        [company_id, programme_key, normalized_event_type, event_date, source_record_id],
        prefix="nev",
    )


def build_unresolved_issue_id(
    *,
    source_name: str,
    source_record_id: str,
    company_name: str,
    ticker: str,
    trial_nct_id: str,
    product_name: str,
    reason: str,
) -> str:
    if _norm(source_name) in {"DRUGS_AT_FDA", "OPENFDA"}:
        parts = [source_name, company_name, ticker, product_name, reason]
    else:
        parts = [source_name, source_record_id, company_name, ticker, trial_nct_id, product_name, reason]
    return stable_hash(parts, prefix="unr")
