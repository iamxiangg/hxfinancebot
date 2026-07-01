from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from models.common import (
    ConfidenceStatus,
    DerivationType,
    DerivedValue,
    EvidenceRecord,
    SourceEvidence,
    now_iso,
    stable_evidence_id,
)

logger = logging.getLogger(__name__)

# Storage key separator -- keeps the states for different concepts separate
INGESTED_KEY = "ingested"
PROCESSED_KEY = "processed"
SCORED_KEY = "scored"
DELIVERED_KEY = "delivered"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EvidenceLedgerConfig:
    enable: bool = True
    enable_hash_check: bool = True

    @classmethod
    def from_env(cls) -> "EvidenceLedgerConfig":
        return cls(
            enable=_env_bool("EVIDENCE_LEDGER_ENABLE", True),
            enable_hash_check=_env_bool("EVIDENCE_LEDGER_HASH_CHECK", True),
        )


# ---------------------------------------------------------------------------
# In-memory store (mirrors Google Sheets for testing / local execution)
# ---------------------------------------------------------------------------


@dataclass
class EvidenceLedgerStore:
    """Local store for evidence records with idempotent upsert semantics.

    Each record is identified by a stable evidence_id. Duplicate evidence_ids
    are detected via hash comparison and superseded records are tracked.
    """

    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    ingested_hashes: set[str] = field(default_factory=set)
    processed_ids: set[str] = field(default_factory=set)
    scored_ids: set[str] = field(default_factory=set)
    delivered_ids: set[str] = field(default_factory=set)

    def ingest(
        self,
        record: EvidenceRecord,
        *,
        source_specific_key: str = "",
    ) -> tuple[bool, str | None]:
        """Attempt to ingest a single evidence record.

        Returns (accepted: bool, superseded_evidence_id: str | None).
        Accepted = True means this is a new or updated record.
        """
        # Check raw payload hash for duplicates
        state_key = source_specific_key or record.source_record_id
        dedupe_key = f"{state_key}:{record.raw_payload_hash}"

        if dedupe_key in self.ingested_hashes:
            logger.debug("Evidence record already ingested: %s (hash match)", record.evidence_id)
            return False, None

        superseded_id: str | None = None

        # Check if this evidence_id already exists
        existing = self.records.get(record.evidence_id)
        if existing is not None:
            if existing.raw_payload_hash == record.raw_payload_hash:
                self.ingested_hashes.add(dedupe_key)
                return False, None

            # Different payload with same evidence_id => amendment/supersession
            logger.info(
                "Evidence record superseded: old=%s new=%s",
                existing.evidence_id,
                record.evidence_id,
            )
            # Store old record with superseded_by pointer under a versioned key
            superseded_id = existing.evidence_id
            version_key = f"{superseded_id}_v{len([k for k in self.records if k.startswith(superseded_id)])}"
            superseded = EvidenceRecord(
                **{
                    **existing.__dict__,
                    "active": False,
                    "superseded_by": record.evidence_id,
                }
            )
            self.records[version_key] = superseded

        self.records[record.evidence_id] = record
        self.ingested_hashes.add(dedupe_key)
        self.processed_ids.add(record.evidence_id)
        return True, superseded_id

    def ingest_batch(
        self,
        records: list[EvidenceRecord],
        *,
        source_specific_key: str = "",
    ) -> dict[str, Any]:
        """Ingest a batch of evidence records. Returns summary stats."""
        accepted = 0
        skipped = 0
        superseded: list[str] = []

        for record in records:
            accepted_flag, old = self.ingest(record, source_specific_key=source_specific_key)
            if accepted_flag:
                accepted += 1
                if old:
                    superseded.append(old)
            else:
                skipped += 1

        return {
            "ingested_total": len(records),
            "ingested_accepted": accepted,
            "ingested_skipped": skipped,
            "ingested_superseded": len(superseded),
            "active_records": len(self.records),
        }

    def mark_scored(self, evidence_id: str) -> None:
        """Mark an evidence record as having been scored."""
        self.scored_ids.add(evidence_id)

    def mark_delivered(self, evidence_id: str) -> None:
        """Mark an evidence record as having been delivered (e.g., Telegram)."""
        self.delivered_ids.add(evidence_id)

    def get_active_records(
        self,
        *,
        entity_id: str | None = None,
        ticker: str | None = None,
        source: str | None = None,
        event_type: str | None = None,
    ) -> list[EvidenceRecord]:
        """Query active evidence records with optional filters."""
        results: list[EvidenceRecord] = []
        for record in self.records.values():
            if not record.active:
                continue
            if entity_id and record.entity_id != entity_id:
                continue
            if ticker and record.ticker.upper() != ticker.upper():
                continue
            if source and record.source != source:
                continue
            if event_type and record.event_type != event_type:
                continue
            results.append(record)
        return sorted(results, key=lambda r: r.observed_at or "", reverse=True)

    def get_record(self, evidence_id: str) -> EvidenceRecord | None:
        """Get a single evidence record by ID."""
        return self.records.get(evidence_id)

    def to_rows(self) -> list[dict[str, Any]]:
        """Convert all active records to row dicts for sheet persistence."""
        rows: list[dict[str, Any]] = []
        for record in self.get_active_records():
            rows.append({
                "Evidence ID": record.evidence_id,
                "Entity ID": record.entity_id,
                "Ticker": record.ticker,
                "Source": record.source,
                "Source Record ID": record.source_record_id,
                "Source Document Type": record.source_document_type,
                "Event Type": record.event_type,
                "Event Date": record.event_date,
                "Observed At": record.observed_at,
                "Raw Payload Hash": record.raw_payload_hash,
                "Derived Field": record.derived_field,
                "Derived Value": json.dumps(record.derived_value) if record.derived_value is not None else "",
                "Unit": record.unit,
                "Derivation Type": record.derivation_type.value if isinstance(record.derivation_type, DerivationType) else str(record.derivation_type),
                "Formula Or Rule": record.formula_or_rule,
                "Confidence Status": record.confidence_status.value if isinstance(record.confidence_status, ConfidenceStatus) else str(record.confidence_status),
                "Source URL": record.source_url,
                "Superseded By": record.superseded_by,
                "Active?": "YES" if record.active else "NO",
            })
        return rows


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class EvidenceLedgerEngine:
    """Deterministic evidence ledger with idempotent upserts."""

    def __init__(
        self,
        *,
        config: EvidenceLedgerConfig | None = None,
    ) -> None:
        self.config = config or EvidenceLedgerConfig.from_env()
        self.store = EvidenceLedgerStore()

    def build_evidence_id(
        self,
        *,
        entity_id: str,
        source: str,
        source_record_id: str,
        event_type: str,
        derived_field: str,
        as_of: str = "",
    ) -> str:
        """Generate a stable, reproducible evidence ID."""
        return stable_evidence_id(
            [
                entity_id,
                source,
                source_record_id,
                event_type,
                derived_field,
                as_of,
            ],
            prefix="ev",
        )

    def create_evidence(
        self,
        *,
        entity_id: str,
        ticker: str,
        source: str,
        source_record_id: str,
        derived: DerivedValue,
        event_type: str = "",
        event_date: str = "",
        source_document_type: str = "",
        source_url: str = "",
    ) -> EvidenceRecord:
        """Create an evidence record from a DerivedValue."""
        observed_at = derived.observed_at or derived.source_evidence.observed_at if derived.source_evidence else now_iso()
        evidence_id = self.build_evidence_id(
            entity_id=entity_id,
            source=source,
            source_record_id=source_record_id,
            event_type=event_type,
            derived_field=derived.source_evidence.source_field if derived.source_evidence else "",
            as_of=derived.as_of or observed_at,
        )

        se = derived.source_evidence or SourceEvidence()
        return EvidenceRecord(
            evidence_id=evidence_id,
            entity_id=entity_id,
            ticker=ticker,
            source=source,
            source_record_id=source_record_id,
            source_document_type=source_document_type or se.source_document_type,
            event_type=event_type,
            event_date=event_date,
            observed_at=observed_at,
            raw_payload_hash=se.raw_payload_hash or derived.payload_hash,
            derived_field=se.source_field,
            derived_value=derived.value,
            unit=derived.unit,
            derivation_type=derived.derivation_type,
            formula_or_rule=derived.formula_or_rule,
            confidence_status=derived.confidence_status,
            source_url=source_url or se.source_url,
            active=True,
        )

    def ingest(
        self,
        record: EvidenceRecord,
        *,
        source_specific_key: str = "",
    ) -> tuple[bool, str | None]:
        """Ingest a single evidence record. Returns (accepted, superseded_id)."""
        if not self.config.enable:
            return False, None
        return self.store.ingest(record, source_specific_key=source_specific_key)

    def ingest_batch(
        self,
        records: list[EvidenceRecord],
        *,
        source_specific_key: str = "",
    ) -> dict[str, Any]:
        """Ingest a batch. Returns summary."""
        if not self.config.enable:
            return {"ingested_total": 0, "ingested_accepted": 0, "ingested_skipped": 0, "ingested_superseded": 0}
        return self.store.ingest_batch(records, source_specific_key=source_specific_key)

    def query(
        self,
        *,
        entity_id: str | None = None,
        ticker: str | None = None,
        source: str | None = None,
        event_type: str | None = None,
    ) -> list[EvidenceRecord]:
        """Query active evidence records."""
        return self.store.get_active_records(
            entity_id=entity_id,
            ticker=ticker,
            source=source,
            event_type=event_type,
        )

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        """Get a single evidence record."""
        return self.store.get_record(evidence_id)

    def to_rows(self) -> list[dict[str, Any]]:
        """Export all active records as sheet row dicts."""
        return self.store.to_rows()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}
