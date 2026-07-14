from __future__ import annotations

from typing import Any

from research.regulatory.identifiers import (
    build_indication_id,
    build_product_id,
    build_programme_key,
    build_regimen_id,
    build_trial_id,
)
from research.regulatory.models import (
    CompanyEntity,
    Indication,
    Product,
    ProgrammeIdentity,
    RegulatoryProductType,
    Regimen,
    Trial,
)


def _canonical_product_name(raw_name: str, aliases: dict[str, str]) -> str:
    lowered = str(raw_name or "").strip()
    if not lowered:
        return ""
    for alias, canonical in aliases.items():
        if lowered.upper() == str(alias or "").strip().upper():
            return str(canonical or "").strip()
    return lowered


def build_programme_components(
    *,
    company: CompanyEntity,
    product_name: str,
    modality: str = "",
    regulatory_product_type: RegulatoryProductType = RegulatoryProductType.UNKNOWN,
    route: str = "",
    dose: str = "",
    schedule: str = "",
    combination_partners: list[str] | None = None,
    background_standard_of_care: str = "",
    disease: str = "",
    disease_stage: str = "",
    biomarker_population: str = "",
    line_of_therapy: str = "",
    age_group: str = "",
    jurisdiction: str = "US",
    nct_id: str = "",
    sponsor: str = "",
    official_title: str = "",
    phase: str = "",
    aliases: dict[str, str] | None = None,
) -> tuple[Product, Regimen, Indication, Trial, ProgrammeIdentity]:
    alias_map = aliases or {}
    canonical_name = _canonical_product_name(product_name, alias_map)
    product_id = build_product_id(
        canonical_name=canonical_name,
        development_owner_entity_id=company.company_id,
        modality=modality,
    )
    regimen_id = build_regimen_id(
        product_id=product_id,
        route=route,
        dose=dose,
        schedule=schedule,
        combination_partners=combination_partners or [],
        background_standard_of_care=background_standard_of_care,
    )
    indication_id = build_indication_id(
        product_id=product_id,
        disease=disease,
        disease_stage=disease_stage,
        biomarker_population=biomarker_population,
        line_of_therapy=line_of_therapy,
        age_group=age_group,
        jurisdiction=jurisdiction,
    )
    trial_id = build_trial_id(
        nct_id=nct_id,
        sponsor=sponsor,
        official_title=official_title,
        product_name=canonical_name,
        indication_name=disease,
        phase=phase,
    )
    programme_key = build_programme_key(
        company_id=company.company_id,
        product_id=product_id,
        regimen_id=regimen_id,
        indication_id=indication_id,
        jurisdiction=jurisdiction,
    )
    product = Product(
        product_id=product_id,
        canonical_name=canonical_name,
        aliases=sorted({str(product_name or "").strip(), *[str(key).strip() for key, value in alias_map.items() if str(value).strip().upper() == canonical_name.upper()]} - {""}),
        modality=modality,
        regulatory_product_type=regulatory_product_type,
        development_owner=company.company_id,
        regulatory_applicant=company.company_id,
        commercial_rights_holder=company.company_id,
        trial_sponsor=sponsor or company.legal_name,
        jurisdiction=jurisdiction,
    )
    regimen = Regimen(
        regimen_id=regimen_id,
        product_id=product_id,
        route=route,
        dose=dose,
        schedule=schedule,
        combination_partners=combination_partners or [],
        background_standard_of_care=background_standard_of_care,
        disease_stage=disease_stage,
    )
    indication = Indication(
        indication_id=indication_id,
        product_id=product_id,
        disease=disease,
        disease_stage=disease_stage,
        biomarker_population=biomarker_population,
        line_of_therapy=line_of_therapy,
        age_group=age_group,
        jurisdiction=jurisdiction,
    )
    trial = Trial(
        trial_id=trial_id,
        nct_id=nct_id,
        sponsor=sponsor,
        official_title=official_title,
        product_id=product_id,
        indication_id=indication_id,
        regimen_id=regimen_id,
        phase=phase,
    )
    programme = ProgrammeIdentity(
        programme_key=programme_key,
        company_id=company.company_id,
        economic_owner_id=company.company_id,
        product_id=product_id,
        regimen_id=regimen_id,
        indication_id=indication_id,
        trial_id=trial_id,
        jurisdiction=jurisdiction,
        company_name=company.legal_name,
        ticker=company.ticker,
        product_name=canonical_name,
        indication_name=disease,
    )
    return product, regimen, indication, trial, programme

