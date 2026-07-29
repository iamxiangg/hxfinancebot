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


@dataclass(frozen=True)
class PoliticalWindowSummary:
    window_days: int
    purchase_count: int = 0
    partial_sale_count: int = 0
    full_sale_count: int = 0
    unique_buyer_count: int = 0
    unique_seller_count: int = 0
    repeat_buyer_count: int = 0
    stock_purchase_low: float = 0.0
    stock_purchase_mid_estimate: float = 0.0
    stock_purchase_high: float = 0.0
    call_purchase_low: float = 0.0
    call_purchase_mid_estimate: float = 0.0
    call_purchase_high: float = 0.0
    put_purchase_low: float = 0.0
    put_purchase_mid_estimate: float = 0.0
    put_purchase_high: float = 0.0
    sale_low: float = 0.0
    sale_mid_estimate: float = 0.0
    sale_high: float = 0.0
    largest_bullish_trade_low: float = 0.0
    largest_bullish_trade_high: float = 0.0
    largest_buyer_share_lower_bound: float = 0.0
    largest_buyer_share_midpoint_estimate: float = 0.0
    unique_record_count: int = 0
    unique_filer_count: int = 0
    unique_household_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TickerPoliticalHistory:
    ticker: str
    primary_classification: str = "INSUFFICIENT_EVIDENCE"
    aggregate_direction: str = "INSUFFICIENT_EVIDENCE"
    structure_classification: str = "UNKNOWN_STRUCTURE"
    latest_disclosure_direction: str = "AMBIGUOUS"
    directional_agreement: str = "UNCLEAR"
    bullish_evidence_score: float = 0.0
    distribution_evidence_score: float = 0.0
    breadth_score: float = 0.0
    concentration_score: float = 0.0
    inference_confidence: str = "LOW"
    data_confidence: str = "LOW"
    windows: dict[int, PoliticalWindowSummary] = field(default_factory=dict)
    new_events: list[dict[str, Any]] = field(default_factory=list)
    notable_history: list[dict[str, Any]] = field(default_factory=list)
    flag_reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    previous_classification: str = "INSUFFICIENT_EVIDENCE"
    classification_changed: bool = False
    summary_hash: str = ""
    entry_category: str = "OTHER"
    event_severity: str = "LOW"
    ticker_state_severity: str = "LOW"
    material_effect_category: str = "NO MATERIAL EFFECT"
    material_effect_percent: float = 0.0
    pre_event_purchase_low_90d: float = 0.0
    pre_event_sale_low_90d: float = 0.0
    post_event_purchase_low_90d: float = 0.0
    post_event_sale_low_90d: float = 0.0
    latest_transaction_date: str = ""
    latest_filing_date: str = ""
    latest_trigger_type: str = ""
    latest_trigger_trade_keys: tuple[str, ...] = ()
    release_types: tuple[str, ...] = ()
    political_conviction: float = 0.0
    entry_quality: float = 0.0
    signal_category: str = "other"
    existing_status: str = "other"
    signal_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["windows"] = {
            str(window): summary.to_dict()
            for window, summary in sorted(self.windows.items())
        }
        return payload


@dataclass(frozen=True)
class MaterialStateChange:
    change_type: str
    reason: str
    previous_value: str = ""
    current_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoliticalWatchlistState:
    ticker: str
    structure_classification: str = ""
    bullish_evidence_score: float = 0.0
    distribution_evidence_score: float = 0.0
    breadth_score: float = 0.0
    concentration_score: float = 0.0
    political_conviction: float = 0.0
    entry_quality: float = 0.0
    first_flagged_at: str = ""
    last_flagged_at: str = ""
    watchlist_started_at: str = ""
    watchlist_until: str = ""
    watchlist_status: str = ""
    watchlist_priority: int = 0
    watchlist_retention_type: str = ""
    watchlist_reminder_count: int = 0
    last_detailed_alert_at: str = ""
    last_compact_reminder_at: str = ""
    previous_entry_category: str = "OTHER"
    current_entry_category: str = "OTHER"
    entry_category_changed: bool = False
    previous_political_classification: str = "INSUFFICIENT_EVIDENCE"
    current_political_classification: str = "INSUFFICIENT_EVIDENCE"
    political_classification_changed: bool = False
    last_material_change_at: str = ""
    last_material_change_type: str = ""
    last_material_change_reason: str = ""
    last_detailed_summary_hash: str = ""
    last_compact_summary_hash: str = ""
    last_trigger_trade_keys: tuple[str, ...] = ()
    watchlist_day: int = 0
    watchlist_total_days: int = 0
    current_detailed_summary_hash: str = ""
    current_compact_summary_hash: str = ""
    latest_material_event: str = ""
    primary_risk: str = "None"
    material_change_types: tuple[str, ...] = ()
    material_change_reasons: tuple[str, ...] = ()
    eligible_for_watchlist: bool = False
    reminder_due: bool = False
    has_new_material_event: bool = False
    has_other_new_activity: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoliticalBackfillStatus:
    probable_backfill: bool
    bootstrap_run: bool
    new_trade_count: int
    amended_trade_count: int
    removed_trade_count: int
    new_filing_count: int
    affected_ticker_count: int
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoliticalArchiveStats:
    raw_inserted: int = 0
    raw_amended: int = 0
    raw_idempotent: int = 0
    raw_deactivated: int = 0
    raw_seen_updates: int = 0
    summary_written: int = 0
    digest_logged: int = 0
    bootstrap_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DigestDeliverySnapshot:
    digest_id: str
    digest_date: str
    run_id: str
    digest_status: str
    source_health: str
    payload_hash: str
    payload_refreshed: bool = True
    fetched_records: int = 0
    new_records: int = 0
    amendments: int = 0
    review_required_count: int = 0
    included_trade_keys: tuple[str, ...] = ()
    excluded_trade_keys: tuple[str, ...] = ()
    ticker_summaries_json: str = ""
    threshold_settings_json: str = ""
    rule_version: str = ""
    template_version: str = ""
    code_commit: str = ""
    message_hash: str = ""
    telegram_message_ids: tuple[str, ...] = ()
    chunk_count: int = 0
    successful_chunks: int = 0
    failed_chunks: int = 0
    attempt_count: int = 0
    last_delivery_error: str = ""
    rendered_digest: str = ""
    delivered_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
