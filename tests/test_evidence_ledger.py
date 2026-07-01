from __future__ import annotations

import unittest
from datetime import datetime, timezone

from models.common import (
    ConfidenceStatus,
    DerivationType,
    DerivedValue,
    EvidenceRecord,
    SourceEvidence,
    now_iso,
    stable_evidence_id,
)
from scanners.evidence_ledger.engine import (
    EvidenceLedgerConfig,
    EvidenceLedgerEngine,
    EvidenceLedgerStore,
)


# ---------------------------------------------------------------------------
# Evidence Ledger Store tests
# ---------------------------------------------------------------------------


class EvidenceLedgerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EvidenceLedgerStore()

    def _make_record(self, evidence_id: str, payload_hash: str = "hash123", **kwargs) -> EvidenceRecord:
        defaults = {
            "evidence_id": evidence_id,
            "entity_id": "CIK0000320193",
            "ticker": "AAPL",
            "source": "congress",
            "source_record_id": "tx-001",
            "source_document_type": "PTR",
            "event_type": "purchase",
            "event_date": "2026-06-28",
            "observed_at": "2026-06-29T10:00:00Z",
            "raw_payload_hash": payload_hash,
            "derived_field": "transaction_value",
            "derived_value": 50000.0,
            "unit": "USD",
            "derivation_type": DerivationType.API_FIELD,
            "formula_or_rule": "congress_trade_extraction",
            "confidence_status": ConfidenceStatus.VERIFIED,
            "source_url": "https://example.com/tx-001",
            "active": True,
        }
        defaults.update(kwargs)
        return EvidenceRecord(**defaults)

    def test_ingest_new_record(self) -> None:
        record = self._make_record("ev-001")
        accepted, superseded = self.store.ingest(record)
        self.assertTrue(accepted)
        self.assertIsNone(superseded)
        self.assertIn("ev-001", self.store.records)
        self.assertIn("ev-001", self.store.processed_ids)

    def test_ingest_duplicate_hash_skipped(self) -> None:
        record = self._make_record("ev-001", payload_hash="abc123")
        self.store.ingest(record)
        duplicate = self._make_record("ev-001", payload_hash="abc123")
        accepted, _ = self.store.ingest(duplicate)
        self.assertFalse(accepted)

    def test_ingest_same_id_different_hash_supersedes(self) -> None:
        old = self._make_record("ev-001", payload_hash="old_hash")
        self.store.ingest(old)
        new = self._make_record("ev-001", payload_hash="new_hash", derived_value=60000.0)
        accepted, superseded_id = self.store.ingest(new)
        self.assertTrue(accepted)
        self.assertEqual(superseded_id, "ev-001")

        # Old record should be inactive
        old_record = self.store.records["ev-001"]
        self.assertTrue(old_record.active)
        self.assertEqual(old_record.derived_value, 60000.0)

    def test_ingest_batch(self) -> None:
        records = [
            self._make_record(f"ev-{i:03d}", payload_hash=f"hash{i}")
            for i in range(10)
        ]
        summary = self.store.ingest_batch(records, source_specific_key="batch-1")
        self.assertEqual(summary["ingested_total"], 10)
        self.assertEqual(summary["ingested_accepted"], 10)
        self.assertEqual(summary["ingested_skipped"], 0)
        self.assertEqual(summary["active_records"], 10)

    def test_ingest_batch_with_duplicates(self) -> None:
        records = [
            self._make_record("ev-001", payload_hash="hash1"),
            self._make_record("ev-001", payload_hash="hash1"),  # duplicate
            self._make_record("ev-002", payload_hash="hash2"),
        ]
        summary = self.store.ingest_batch(records, source_specific_key="batch-1")
        self.assertEqual(summary["ingested_total"], 3)
        self.assertEqual(summary["ingested_accepted"], 2)
        self.assertEqual(summary["ingested_skipped"], 1)
        self.assertEqual(summary["active_records"], 2)

    def test_query_by_entity_id(self) -> None:
        self.store.ingest(self._make_record("ev-001", entity_id="CIK0000320193", source_record_id="r1"), source_specific_key="q-entity-1")
        self.store.ingest(self._make_record("ev-002", entity_id="CIK0000789019", source_record_id="r2"), source_specific_key="q-entity-2")
        results = self.store.get_active_records(entity_id="CIK0000320193")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].evidence_id, "ev-001")

    def test_query_by_ticker(self) -> None:
        self.store.ingest(self._make_record("ev-001", ticker="AAPL", source_record_id="r1"), source_specific_key="q-ticker-1")
        self.store.ingest(self._make_record("ev-002", ticker="MSFT", source_record_id="r2"), source_specific_key="q-ticker-2")
        results = self.store.get_active_records(ticker="MSFT")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ticker, "MSFT")

    def test_query_by_source(self) -> None:
        self.store.ingest(self._make_record("ev-001", source="congress", source_record_id="r1"), source_specific_key="q-source-1")
        self.store.ingest(self._make_record("ev-002", source="insider", source_record_id="r2"), source_specific_key="q-source-2")
        results = self.store.get_active_records(source="insider")
        self.assertEqual(len(results), 1)

    def test_query_by_event_type(self) -> None:
        self.store.ingest(self._make_record("ev-001", event_type="purchase", source_record_id="r1"), source_specific_key="q-event-1")
        self.store.ingest(self._make_record("ev-002", event_type="sale", source_record_id="r2"), source_specific_key="q-event-2")
        results = self.store.get_active_records(event_type="sale")
        self.assertEqual(len(results), 1)

    def test_get_single_record(self) -> None:
        record = self._make_record("ev-001")
        self.store.ingest(record)
        retrieved = self.store.get_record("ev-001")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.ticker, "AAPL")

        not_found = self.store.get_record("ev-nonexistent")
        self.assertIsNone(not_found)

    def test_mark_scored_and_delivered(self) -> None:
        self.store.ingest(self._make_record("ev-001"))
        self.store.mark_scored("ev-001")
        self.store.mark_delivered("ev-001")
        self.assertIn("ev-001", self.store.scored_ids)
        self.assertIn("ev-001", self.store.delivered_ids)

    def test_to_rows_exports_active_only(self) -> None:
        self.store.ingest(self._make_record("ev-001"))
        rows = self.store.to_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Evidence ID"], "ev-001")
        self.assertEqual(rows[0]["Active?"], "YES")

    def test_inactive_records_excluded_from_query(self) -> None:
        record = self._make_record("ev-001", active=True)
        self.store.ingest(record)
        # Override to make inactive
        inactive = EvidenceRecord(**{**record.__dict__, "active": False})
        self.store.records["ev-001"] = inactive
        results = self.store.get_active_records()
        self.assertEqual(len(results), 0)


# ---------------------------------------------------------------------------
# Evidence Ledger Engine tests
# ---------------------------------------------------------------------------


class EvidenceLedgerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = EvidenceLedgerEngine()

    def test_build_evidence_id_reproducible(self) -> None:
        id1 = self.engine.build_evidence_id(
            entity_id="CIK0000320193",
            source="insider",
            source_record_id="0000320193-24-000001",
            event_type="purchase",
            derived_field="shares",
            as_of="2026-06-29",
        )
        id2 = self.engine.build_evidence_id(
            entity_id="CIK0000320193",
            source="insider",
            source_record_id="0000320193-24-000001",
            event_type="purchase",
            derived_field="shares",
            as_of="2026-06-29",
        )
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("ev-"))

    def test_different_inputs_produce_different_ids(self) -> None:
        id1 = self.engine.build_evidence_id(
            entity_id="CIK0000320193", source="insider",
            source_record_id="rec-1", event_type="purchase",
            derived_field="shares", as_of="2026-06-29",
        )
        id2 = self.engine.build_evidence_id(
            entity_id="CIK0000320193", source="insider",
            source_record_id="rec-2", event_type="purchase",
            derived_field="shares", as_of="2026-06-29",
        )
        self.assertNotEqual(id1, id2)

    def test_create_evidence_from_derived_value(self) -> None:
        se = SourceEvidence(
            source="sec_gov",
            source_record_id="0000320193-24-000001",
            source_field="shares",
            raw_payload_hash="test_hash",
            observed_at="2026-06-29T10:00:00Z",
        )
        dv = DerivedValue(
            value=1000.0,
            unit="shares",
            as_of="2026-06-29",
            source_evidence=se,
            formula_or_rule="Form 4 XML extraction",
            derivation_type=DerivationType.DETERMINISTIC_PARSE,
            confidence_status=ConfidenceStatus.VERIFIED,
        )
        record = self.engine.create_evidence(
            entity_id="CIK0000320193",
            ticker="AAPL",
            source="insider",
            source_record_id="0000320193-24-000001",
            derived=dv,
            event_type="insider_purchase",
            event_date="2026-06-28",
            source_document_type="4",
            source_url="https://www.sec.gov/test",
        )
        self.assertTrue(record.active)
        self.assertEqual(record.ticker, "AAPL")
        self.assertEqual(record.derived_value, 1000.0)
        self.assertEqual(record.derivation_type, DerivationType.DETERMINISTIC_PARSE)
        self.assertEqual(record.confidence_status, ConfidenceStatus.VERIFIED)

    def test_ingest_and_query(self) -> None:
        se = SourceEvidence(source="test", source_record_id="r1",
                           source_field="test_field", raw_payload_hash="h1")
        dv = DerivedValue(value=42.0, source_evidence=se,
                         derivation_type=DerivationType.FORMULA,
                         confidence_status=ConfidenceStatus.VERIFIED)
        record = self.engine.create_evidence(
            entity_id="CIK0000320193", ticker="AAPL", source="test",
            source_record_id="r1", derived=dv, event_type="test_event",
        )
        accepted, _ = self.engine.ingest(record)
        self.assertTrue(accepted)

        results = self.engine.query(ticker="AAPL")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ticker, "AAPL")

    def test_ingest_duplicate_skipped(self) -> None:
        se = SourceEvidence(source="test", source_record_id="r1",
                           source_field="f", raw_payload_hash="dup")
        dv = DerivedValue(value=42.0, source_evidence=se,
                         derivation_type=DerivationType.FORMULA)
        record = self.engine.create_evidence(
            entity_id="CIK", ticker="TST", source="test",
            source_record_id="r1", derived=dv, event_type="test",
        )
        self.assertTrue(self.engine.ingest(record)[0])
        self.assertFalse(self.engine.ingest(record)[0])

    def test_disabled_config_skips_ingestion(self) -> None:
        engine = EvidenceLedgerEngine(config=EvidenceLedgerConfig(enable=False))
        se = SourceEvidence(source="test", source_record_id="r1",
                           source_field="f", raw_payload_hash="h1")
        dv = DerivedValue(value=42.0, source_evidence=se,
                         derivation_type=DerivationType.FORMULA)
        record = engine.create_evidence(
            entity_id="CIK", ticker="TST", source="test",
            source_record_id="r1", derived=dv, event_type="test",
        )
        accepted, _ = engine.ingest(record)
        self.assertFalse(accepted)

    def test_get_nonexistent_returns_none(self) -> None:
        self.assertIsNone(self.engine.get("ev-nonexistent"))

    def test_to_rows(self) -> None:
        se = SourceEvidence(source="test", source_record_id="r1",
                           source_field="f", raw_payload_hash="h1")
        dv = DerivedValue(value=42.0, source_evidence=se,
                         derivation_type=DerivationType.FORMULA)
        record = self.engine.create_evidence(
            entity_id="CIK", ticker="TST", source="test",
            source_record_id="r1", derived=dv, event_type="test",
        )
        self.engine.ingest(record)
        rows = self.engine.to_rows()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
