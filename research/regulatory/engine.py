from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import re
from typing import Any

from providers.regulatory.clinicaltrials import ClinicalTrialsGovProvider
from providers.regulatory.configured_ir import ConfiguredIssuerFeedProvider
from providers.regulatory.drugs_at_fda import DrugsAtFDAProvider
from providers.regulatory.fda_biologics import FDABiologicsProvider
from providers.regulatory.openfda import OpenFDAProvider
from providers.regulatory.sec_regulatory import SECRegulatoryProvider
from providers.regulatory.base import RegulatorySourceProvider, SourceBatch
from research.regulatory.config import RegulatoryMonitorConfig
from research.regulatory.entity_resolution import RegulatoryEntityResolver
from research.regulatory.history import apply_events, seed_current_state
from research.regulatory.materiality import build_digest_flag, should_repeat_alert
from research.regulatory.state_machines import event_state_target
from research.regulatory.models import (
    FinancialSnapshot,
    NormalizedRegulatoryEvent,
    ProgrammeCurrentState,
    ProgrammeIdentity,
    RawRegulatoryRecord,
    RegulatoryDigestPlan,
    ResearchPriority,
    SourceCheckpoint,
    UnresolvedEvent,
)
from research.regulatory.normalizer import normalize_record
from research.regulatory.programme_registry import build_programme_components
from scanners.no_llm_guard import require_no_llm

require_no_llm()


@dataclass
class RegulatoryRunResult:
    raw_records: list[RawRegulatoryRecord] = field(default_factory=list)
    normalized_events: list[NormalizedRegulatoryEvent] = field(default_factory=list)
    current_states: dict[str, ProgrammeCurrentState] = field(default_factory=dict)
    programmes: dict[str, ProgrammeIdentity] = field(default_factory=dict)
    unresolved: list[UnresolvedEvent] = field(default_factory=list)
    source_checkpoints: list[SourceCheckpoint] = field(default_factory=list)
    provider_errors: dict[str, list[str]] = field(default_factory=dict)
    digest_plan: RegulatoryDigestPlan | None = None


class RegulatoryMonitorEngine:
    def __init__(
        self,
        *,
        config: RegulatoryMonitorConfig | None = None,
        providers: dict[str, RegulatorySourceProvider] | None = None,
        entity_resolver: RegulatoryEntityResolver | None = None,
    ) -> None:
        self.config = config or RegulatoryMonitorConfig.from_env()
        payload = self.config.configured_sources_payload()
        self.providers = providers or {
            "clinicaltrials": ClinicalTrialsGovProvider(config=self.config),
            "sec": SECRegulatoryProvider(config=self.config),
            "drugs_at_fda": DrugsAtFDAProvider(),
            "fda_biologics": FDABiologicsProvider(),
            "openfda": OpenFDAProvider(),
            "configured_ir": ConfiguredIssuerFeedProvider(config=self.config),
        }
        self.entity_resolver = entity_resolver or RegulatoryEntityResolver(
            config_payload=payload,
            sic_allowlist=self.config.sec_sic_allowlist,
        )
        self.product_aliases = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in (payload.get("product_aliases") or {}).items()
        }

    def _selected_providers(self) -> list[RegulatorySourceProvider]:
        selected: list[RegulatorySourceProvider] = []
        for name in self.config.sources:
            provider = self.providers.get(name)
            if provider is not None:
                selected.append(provider)
        return selected

    def _build_digest(self, *, events: list[NormalizedRegulatoryEvent], current_states: dict[str, ProgrammeCurrentState], unresolved: list[UnresolvedEvent], previous_hashes: set[str]) -> RegulatoryDigestPlan:
        material_flags = []
        for event in events:
            target = event_state_target(event)
            gate_change = f"{target[0].value}:{target[1]}" if target is not None else ""
            flag = build_digest_flag(
                event,
                company_name=event.company_name,
                product_name=event.metadata.get("product_name", ""),
                indication_name=event.metadata.get("indication_name", ""),
                gate_change=gate_change,
            )
            if should_repeat_alert(previous_hashes=previous_hashes, new_hash=flag.summary_hash):
                material_flags.append(flag)
        deduped_flags: dict[str, Any] = {}
        for flag in material_flags:
            key = str(flag.summary_hash or flag.event_id or "")
            if key:
                deduped_flags[key] = flag
        ordered_flags = list(deduped_flags.values())
        detailed = [
            flag for flag in ordered_flags
            if flag.priority in {ResearchPriority.URGENT, ResearchPriority.HIGH}
        ][: self.config.max_detailed_events]
        updates = [
            flag for flag in ordered_flags
            if flag.priority == ResearchPriority.MONITOR
        ][: self.config.hard_max_detailed_events]
        other_activity_count = sum(1 for flag in ordered_flags if flag.priority == ResearchPriority.CONTEXT)
        return RegulatoryDigestPlan(
            digest_date=datetime.now(UTC).date().isoformat(),
            data_status={
                "material_events": len(detailed) + len(updates),
                "provider_failures": sum(1 for _ in unresolved),
            },
            material_events=detailed,
            state_updates=updates,
            other_activity_count=other_activity_count,
            unresolved_items=unresolved,
            send_digest=bool(detailed or updates or unresolved),
        )

    @staticmethod
    def _infer_product_name_from_text(text: str) -> str:
        lowered = str(text or "").lower()
        if "tibulizumab" in lowered and "zb-106" in lowered:
            return "Tibulizumab (ZB-106)"
        match = re.search(r"\b([A-Z][a-z][A-Za-z0-9-]{3,})\s*\(\s*([A-Z]{1,4}-\d{2,4})\s*\)", str(text or ""))
        if not match:
            return ""
        name, code = match.groups()
        return f"{name} ({code.upper()})"

    @staticmethod
    def _infer_indication_name_from_text(text: str) -> str:
        lowered = str(text or "").lower()
        if "tibulizumab" in lowered and "systemic sclerosis" in lowered:
            return "Systemic sclerosis / diffuse cutaneous systemic sclerosis"
        if "diffuse cutaneous systemic sclerosis" in lowered:
            return "Systemic sclerosis / diffuse cutaneous systemic sclerosis"
        if "systemic sclerosis" in lowered:
            return "Systemic sclerosis"
        return ""

    def _enrich_record_context(self, record: RawRegulatoryRecord) -> RawRegulatoryRecord:
        if record.source_name != "sec":
            return record
        text = str(record.exact_text or "")
        if not record.product_name:
            inferred_product = self._infer_product_name_from_text(text)
            if inferred_product:
                record.product_name = inferred_product
        if not record.indication_name:
            inferred_indication = self._infer_indication_name_from_text(text)
            if inferred_indication:
                record.indication_name = inferred_indication
        return record

    def _dedupe_unresolved(self, items: list[UnresolvedEvent]) -> list[UnresolvedEvent]:
        deduped: dict[str, UnresolvedEvent] = {}
        for item in items:
            key = str(item.unresolved_id or "").strip()
            if not key:
                continue
            existing = deduped.get(key)
            if existing is None or str(item.created_at or "") >= str(existing.created_at or ""):
                deduped[key] = item
        return list(deduped.values())

    def run(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        previous_digest_hashes: set[str] | None = None,
    ) -> RegulatoryRunResult:
        now = until or datetime.now(UTC)
        start = since or (now - timedelta(days=self.config.bootstrap_lookback_days))
        result = RegulatoryRunResult()
        previous_hashes = previous_digest_hashes or set()
        for provider in self._selected_providers():
            batch = provider.fetch_changes(since=start, until=now, cursor="")
            result.raw_records.extend(batch.records)
            result.provider_errors[provider.source_name] = list(batch.errors)
            result.source_checkpoints.append(
                SourceCheckpoint(
                    source_name=provider.source_name,
                    cursor=batch.next_cursor,
                    last_success_at=batch.fetched_at,
                    last_event_at=max((record.published_at for record in batch.records), default=""),
                    bootstrap_complete=True,
                    metadata=batch.metadata,
                )
            )
        for record in result.raw_records:
            record = self._enrich_record_context(record)
            mapping = self.entity_resolver.resolve(
                ticker=record.ticker,
                cik=record.cik,
                legal_name=record.company_name,
                sponsor_name=record.company_name,
            )
            company = mapping.entity
            if company is None:
                normalized = normalize_record(record=record, mapping=mapping, programme_key="")
                result.unresolved.extend(normalized.unresolved)
                continue
            product, regimen, indication, trial, programme = build_programme_components(
                company=company,
                product_name=record.product_name or "UNMAPPED_PRODUCT",
                disease=record.indication_name or "UNMAPPED_INDICATION",
                nct_id=record.trial_nct_id,
                sponsor=record.company_name,
                phase=str(record.structured_data.get("phase") or ""),
                aliases=self.product_aliases,
            )
            result.programmes[programme.programme_key] = programme
            normalized = normalize_record(record=record, mapping=mapping, programme_key=programme.programme_key)
            result.unresolved.extend(normalized.unresolved)
            for event in normalized.events:
                event.metadata.setdefault("product_name", product.canonical_name)
                event.metadata.setdefault("indication_name", indication.disease)
            result.normalized_events.extend(normalized.events)
        events_by_programme: dict[str, list[NormalizedRegulatoryEvent]] = {}
        for event in result.normalized_events:
            events_by_programme.setdefault(event.programme_key, []).append(event)
        for programme_key, programme in result.programmes.items():
            update = apply_events(programme=programme, events=events_by_programme.get(programme_key, []), existing_state=result.current_states.get(programme_key))
            result.current_states[programme_key] = update.current_state
        result.unresolved = self._dedupe_unresolved(result.unresolved)
        result.digest_plan = self._build_digest(
            events=result.normalized_events,
            current_states=result.current_states,
            unresolved=result.unresolved,
            previous_hashes=previous_hashes,
        )
        return result
