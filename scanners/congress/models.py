from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PoliticalFiler:
    filer_id: str
    filer_name: str
    bioguide_id: str = ""
    branch: str = "unknown"
    chamber: str = ""
    party: str = ""
    state: str = ""
    agency: str = ""
    level: str = ""
    office: str = ""
    owner_relationship: str = "unknown"
    source_id: str = ""
    identity_resolution_status: str = "UNRESOLVED"


@dataclass(frozen=True)
class PoliticalRole:
    role_type: str
    organisation_id: str
    organisation_name: str
    parent_organisation_id: str | None
    parent_organisation_name: str | None
    title: str
    rank: int | None
    seniority_class: str
    source: str
    source_retrieved_at: datetime
    source_payload_hash: str


@dataclass(frozen=True)
class ExecutiveRole:
    agency: str
    agency_key: str
    level: str
    seniority_class: str
    confidence: str


@dataclass(frozen=True)
class PoliticalRoleResolution:
    filer: PoliticalFiler
    status: str
    roles: tuple[PoliticalRole, ...] = ()
    executive_role: ExecutiveRole | None = None
    source_retrieved_at: datetime | None = None
    source_payload_hash: str = ""
    stale_cache: bool = False
    error: str = ""


@dataclass(frozen=True)
class CompanyClassification:
    ticker: str
    sector: str
    industry: str
    thematic_exposures: tuple[str, ...]
    source: str
    confidence: str


@dataclass(frozen=True)
class RoleRelevanceEvidence:
    matched_role: str = ""
    matched_organisation: str = ""
    matched_organisation_id: str = ""
    company_sector: str = ""
    company_industry: str = ""
    matched_thematic_exposure: str = ""
    sector_weight: float = 0.0
    seniority_class: str = ""
    seniority_multiplier: float = 0.0
    final_role_score: float = 0.0
    mapping_version: str = ""
    source_snapshot_hash: str = ""
    limitation: str = (
        "Role relevance reflects overlap between formal responsibilities and a company's policy exposure. "
        "It does not imply possession of confidential information."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoleRelevanceResult:
    score: float
    status: str
    evidences: tuple[RoleRelevanceEvidence, ...] = ()
    committee_ids: tuple[str, ...] = ()
    committee_names: tuple[str, ...] = ()
    subcommittee_ids: tuple[str, ...] = ()
    agency_keys: tuple[str, ...] = ()
    seniority_classes: tuple[str, ...] = ()
    seniority_multipliers: tuple[float, ...] = ()
    high_policy_access_flag: bool = False


@dataclass(frozen=True)
class AssetIntent:
    intent_class: str
    company_specific: bool
    bullish: bool
    bearish: bool
    non_directional: bool
    broad_market: bool
    intentionality_score: float
    note: str = ""


@dataclass
class PoliticalAuditBundle:
    normalised_transactions: list[dict[str, Any]] = field(default_factory=list)
    identity_resolutions: list[dict[str, Any]] = field(default_factory=list)
    committee_role_snapshots: list[dict[str, Any]] = field(default_factory=list)
    executive_role_resolutions: list[dict[str, Any]] = field(default_factory=list)
    company_classifications: list[dict[str, Any]] = field(default_factory=list)
    role_relevance_calculations: list[dict[str, Any]] = field(default_factory=list)
    excluded_record_reasons: list[dict[str, Any]] = field(default_factory=list)

