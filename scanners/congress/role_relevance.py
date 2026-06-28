from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scanners.congress.models import CompanyClassification, PoliticalRoleResolution, RoleRelevanceEvidence, RoleRelevanceResult
from scanners.congress.political_roles import EXECUTIVE_SENIORITY


MAPPINGS_DIR = Path(__file__).with_name("mappings")
MAPPING_VERSION = "2026-06-28-political-role-v1"

CONGRESSIONAL_MULTIPLIERS = {
    "CHAIR": 1.00,
    "RANKING_MEMBER": 0.90,
    "SUBCOMMITTEE_CHAIR": 0.90,
    "SUBCOMMITTEE_RANKING_MEMBER": 0.82,
    "VICE_CHAIR": 0.80,
    "COMMITTEE_MEMBER": 0.65,
    "SUBCOMMITTEE_MEMBER": 0.60,
    "LEADERSHIP": 0.75,
    "UNKNOWN": 0.35,
}

EXECUTIVE_MULTIPLIERS = {value[0]: value[1] for value in EXECUTIVE_SENIORITY.values()}


def _load_mapping(name: str) -> dict[str, Any]:
    return json.loads((MAPPINGS_DIR / name).read_text(encoding="utf-8"))


def evaluate_role_relevance(
    role_resolution: PoliticalRoleResolution,
    classification: CompanyClassification,
) -> RoleRelevanceResult:
    if role_resolution.filer.branch == "unknown":
        return RoleRelevanceResult(score=0.0, status="NOT_APPLICABLE")
    if classification.confidence == "UNAVAILABLE":
        return RoleRelevanceResult(score=0.0, status="COMPANY_CLASSIFICATION_UNAVAILABLE")
    if role_resolution.status in {"UNRESOLVED", "AMBIGUOUS"}:
        return RoleRelevanceResult(score=0.0, status="IDENTITY_UNRESOLVED")
    if role_resolution.status == "ROLE_SOURCE_UNAVAILABLE":
        return RoleRelevanceResult(score=0.0, status="ROLE_SOURCE_UNAVAILABLE")
    if role_resolution.status == "HISTORICAL_ROLE_UNAVAILABLE":
        return RoleRelevanceResult(score=0.0, status="HISTORICAL_ROLE_UNAVAILABLE")

    if role_resolution.filer.branch == "executive" and role_resolution.executive_role is not None:
        return _evaluate_executive(role_resolution, classification)
    return _evaluate_congressional(role_resolution, classification)


def _evaluate_congressional(
    role_resolution: PoliticalRoleResolution,
    classification: CompanyClassification,
) -> RoleRelevanceResult:
    mapping = _load_mapping("committee_sector_map.yaml").get("committee_sector_map", {})
    evidences: list[RoleRelevanceEvidence] = []
    contributions: list[float] = []
    matched_ids: list[str] = []
    matched_names: list[str] = []
    subcommittee_ids: list[str] = []
    seniority_classes: list[str] = []
    seniority_multipliers: list[float] = []
    for role in role_resolution.roles:
        matched = mapping.get(role.organisation_id) or mapping.get(role.parent_organisation_id or "")
        if not isinstance(matched, dict):
            continue
        weight, exposure = _best_weight(matched.get("sector_weights", {}), classification)
        if weight <= 0:
            continue
        multiplier = CONGRESSIONAL_MULTIPLIERS.get(role.seniority_class, CONGRESSIONAL_MULTIPLIERS["UNKNOWN"])
        contribution = weight * multiplier
        evidences.append(
            RoleRelevanceEvidence(
                matched_role=role.title or role.seniority_class,
                matched_organisation=role.organisation_name,
                matched_organisation_id=role.organisation_id,
                company_sector=classification.sector,
                company_industry=classification.industry,
                matched_thematic_exposure=exposure,
                sector_weight=weight,
                seniority_class=role.seniority_class,
                seniority_multiplier=multiplier,
                final_role_score=contribution,
                mapping_version=MAPPING_VERSION,
                source_snapshot_hash=role.source_payload_hash,
            )
        )
        contributions.append(contribution)
        matched_ids.append(role.organisation_id)
        matched_names.append(role.organisation_name)
        if role.role_type == "SUBCOMMITTEE":
            subcommittee_ids.append(role.organisation_id)
        seniority_classes.append(role.seniority_class)
        seniority_multipliers.append(multiplier)
    if not contributions:
        return RoleRelevanceResult(score=0.0, status="UNMAPPED_ROLE")
    best = max(contributions)
    corroboration_bonus = 2 if len(contributions) >= 3 else 1 if len(contributions) >= 2 else 0
    score = min(20, round(20 * best) + corroboration_bonus)
    status = "RESOLVED_HIGH_CONFIDENCE" if classification.confidence == "HIGH" else "RESOLVED_MEDIUM_CONFIDENCE"
    return RoleRelevanceResult(
        score=float(score),
        status=status,
        evidences=tuple(evidences),
        committee_ids=tuple(dict.fromkeys(matched_ids)),
        committee_names=tuple(dict.fromkeys(matched_names)),
        subcommittee_ids=tuple(dict.fromkeys(subcommittee_ids)),
        seniority_classes=tuple(dict.fromkeys(seniority_classes)),
        seniority_multipliers=tuple(seniority_multipliers),
        high_policy_access_flag=score >= 14,
    )


def _evaluate_executive(
    role_resolution: PoliticalRoleResolution,
    classification: CompanyClassification,
) -> RoleRelevanceResult:
    executive_role = role_resolution.executive_role
    assert executive_role is not None
    mapping = _load_mapping("agency_sector_map.yaml").get("agency_sector_map", {})
    matched = mapping.get(executive_role.agency_key)
    if not isinstance(matched, dict):
        return RoleRelevanceResult(score=0.0, status="UNMAPPED_ROLE", agency_keys=(executive_role.agency_key,))
    weight, exposure = _best_weight(matched.get("sector_weights", {}), classification)
    if weight <= 0:
        return RoleRelevanceResult(score=0.0, status="UNMAPPED_ROLE", agency_keys=(executive_role.agency_key,))
    multiplier = EXECUTIVE_MULTIPLIERS.get(executive_role.seniority_class, EXECUTIVE_MULTIPLIERS["UNKNOWN"])
    contribution = weight * multiplier
    score = min(20, round(20 * contribution))
    evidence = RoleRelevanceEvidence(
        matched_role=executive_role.seniority_class,
        matched_organisation=executive_role.agency,
        matched_organisation_id=executive_role.agency_key,
        company_sector=classification.sector,
        company_industry=classification.industry,
        matched_thematic_exposure=exposure,
        sector_weight=weight,
        seniority_class=executive_role.seniority_class,
        seniority_multiplier=multiplier,
        final_role_score=contribution,
        mapping_version=MAPPING_VERSION,
        source_snapshot_hash=role_resolution.source_payload_hash,
    )
    return RoleRelevanceResult(
        score=float(score),
        status="RESOLVED_MEDIUM_CONFIDENCE",
        evidences=(evidence,),
        agency_keys=(executive_role.agency_key,),
        seniority_classes=(executive_role.seniority_class,),
        seniority_multipliers=(multiplier,),
        high_policy_access_flag=score >= 14,
    )


def _best_weight(mapping: dict[str, Any], classification: CompanyClassification) -> tuple[float, str]:
    candidates = [classification.industry, classification.sector, *classification.thematic_exposures]
    best_weight = 0.0
    best_key = ""
    for key in candidates:
        value = float(mapping.get(key, 0.0) or 0.0)
        if value > best_weight:
            best_weight = value
            best_key = key
    return best_weight, best_key

