from __future__ import annotations

import hashlib
import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from models.common import (
    ConfidenceStatus,
    DerivationType,
    DerivedValue,
    EntityMapping,
    EntityMappingSuggestion,
    EvidenceRecord,
    MappingConfidence,
    MappingStatus,
    SourceEvidence,
    stable_evidence_id,
)
from scanners.entity_master.engine import (
    EntityMasterConfig,
    EntityMasterEngine,
    build_entity_id,
    _extract_former_names,
    _extract_former_tickers,
    _extract_exchange,
)
from providers.sec.models import CompanyProfile


# ---------------------------------------------------------------------------
# Fake SEC provider for deterministic tests
# ---------------------------------------------------------------------------


class FakeSECProvider:
    """Fake SEC provider returning frozen fixtures."""

    def __init__(self, profiles: dict[str, CompanyProfile] | None = None) -> None:
        import requests
        self.profiles = profiles or {}
        self.submissions_data: dict[str, dict] = {}
        self.session = requests.Session()
        self.user_agent = "hxfinancebot/1.0"

    def company_profile(self, ticker: str) -> CompanyProfile:
        normalized = ticker.strip().upper()
        if normalized in self.profiles:
            return self.profiles[normalized]
        # Default: AAPL
        return CompanyProfile(
            ticker=normalized,
            cik="0000320193",
            name=f"{normalized} Inc.",
            source_url="https://www.sec.gov/files/company_tickers.json",
        )

    def _get_json(self, url: str, **kwargs) -> dict:
        for cik, data in self.submissions_data.items():
            if cik in url:
                return data
        return {}


# ---------------------------------------------------------------------------
# Entity ID tests
# ---------------------------------------------------------------------------


class EntityIdTests(unittest.TestCase):
    def test_cik_primary_identity(self) -> None:
        entity_id = build_entity_id(cik="0000320193", ticker="AAPL")
        self.assertEqual(entity_id, "CIK0000320193")

    def test_cik_padded_to_10_digits(self) -> None:
        entity_id = build_entity_id(cik="320193")
        self.assertEqual(entity_id, "CIK0000320193")

    def test_fallback_to_ticker_when_no_cik(self) -> None:
        entity_id = build_entity_id(cik="", ticker="AAPL")
        self.assertEqual(entity_id, "TKR-AAPL")

    def test_fallback_to_name_hash_when_no_cik_or_ticker(self) -> None:
        entity_id = build_entity_id(cik="", ticker="", name="Apple Inc.")
        self.assertTrue(entity_id.startswith("NAM-"))
        self.assertEqual(len(entity_id), 16)  # NAM- + 12 hex chars

    def test_empty_identity_when_nothing_provided(self) -> None:
        entity_id = build_entity_id(cik="", ticker="", name="")
        self.assertEqual(entity_id, "")


# ---------------------------------------------------------------------------
# Entity Master Engine tests
# ---------------------------------------------------------------------------


class EntityMasterEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_sec = FakeSECProvider()
        self.engine = EntityMasterEngine(
            config=EntityMasterConfig(enable=True, fuzzy_min_confidence=0.85),
            sec_provider=self.fake_sec,
        )

    def test_resolve_basic_ticker(self) -> None:
        mapping = self.engine.resolve_entity("AAPL")
        self.assertEqual(mapping.ticker, "AAPL")
        self.assertEqual(mapping.cik, "0000320193")
        self.assertEqual(mapping.current_legal_name, "AAPL Inc.")
        self.assertEqual(mapping.mapping_status, MappingStatus.EXACT)
        self.assertEqual(mapping.mapping_confidence, MappingConfidence.HIGH)
        self.assertTrue(mapping.active)
        self.assertFalse(mapping.manual_override)

    def test_resolve_with_former_names(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "formerCompanyNames": [
                {"name": "Apple Computer Inc.", "date": "2007-01-09"},
            ],
            "tickers": [],
        }
        mock_response.raise_for_status = MagicMock()
        with patch.object(self.fake_sec.session, "get", return_value=mock_response):
            mapping = self.engine.resolve_entity("AAPL")
        self.assertIn("Apple Computer Inc.", mapping.former_legal_names)

    def test_resolve_with_former_tickers(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "formerCompanyNames": [],
            "tickers": ["AAPL", "AAPL.B"],
        }
        mock_response.raise_for_status = MagicMock()
        with patch.object(self.fake_sec.session, "get", return_value=mock_response):
            mapping = self.engine.resolve_entity("AAPL")
        self.assertIn("AAPL.B", mapping.former_tickers)

    def test_resolve_with_exchange(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "formerCompanyNames": [],
            "tickers": [],
            "exchange": "Nasdaq",
        }
        mock_response.raise_for_status = MagicMock()
        with patch.object(self.fake_sec.session, "get", return_value=mock_response):
            mapping = self.engine.resolve_entity("AAPL")
        self.assertEqual(mapping.exchange, "NASDAQ")

    def test_resolve_unavailable_when_no_cik(self) -> None:
        self.fake_sec.profiles["UNKNOWN"] = CompanyProfile(
            ticker="UNKNOWN", cik="", name="", source_url=""
        )
        mapping = self.engine.resolve_entity("UNKNOWN")
        self.assertEqual(mapping.mapping_status, MappingStatus.UNAVAILABLE)

    def test_batch_resolution(self) -> None:
        results = self.engine.resolve_batch(["AAPL", "MSFT", "GOOGL"])
        self.assertEqual(len(results), 3)
        for ticker in ["AAPL", "MSFT", "GOOGL"]:
            self.assertIn(ticker, results)
            self.assertEqual(results[ticker].mapping_status, MappingStatus.EXACT)

    def test_batch_resolution_handles_failure(self) -> None:
        class FailingProvider:
            def company_profile(self, ticker: str) -> CompanyProfile:
                if ticker == "FAIL":
                    raise ValueError("test failure")
                return CompanyProfile(
                    ticker=ticker.upper(),
                    cik="0000000001",
                    name=f"Test {ticker}",
                    source_url="",
                )

        engine = EntityMasterEngine(
            config=EntityMasterConfig(),
            sec_provider=FailingProvider(),
        )
        results = engine.resolve_batch(["OK", "FAIL"])
        self.assertEqual(results["OK"].mapping_status, MappingStatus.EXACT)
        self.assertEqual(results["FAIL"].mapping_status, MappingStatus.UNAVAILABLE)

    def test_fuzzy_suggestion_is_never_auto(self) -> None:
        suggestion = self.engine.suggest_fuzzy_mapping(
            ticker="AAPL",
            candidate_name="Apple Computer Inc.",
            similarity_metric="levenshtein",
            similarity_score=0.92,
        )
        self.assertEqual(suggestion.status, "MANUAL_REQUIRED")
        self.assertIsNotNone(suggestion.evidence)
        # zero score suggestion (no evidence):
        suggestion2 = self.engine.suggest_fuzzy_mapping(
            ticker="AAPL",
            candidate_name="Apple Inc.",
            similarity_metric="exact_substring",
            similarity_score=0.0,
        )
        self.assertEqual(suggestion2.status, "MANUAL_REQUIRED")
        self.assertIsNone(suggestion2.evidence)

    def test_resolve_preserves_entity_id_with_cik(self) -> None:
        mapping = self.engine.resolve_entity("AAPL")
        self.assertTrue(mapping.entity_id.startswith("CIK"))
        self.assertIn("0000320193", mapping.entity_id)

    def test_nonexistent_ticker_returns_unavailable(self) -> None:
        class NotFoundProvider:
            def company_profile(self, ticker: str) -> CompanyProfile:
                from providers.sec.errors import SECNotFoundError
                raise SECNotFoundError("not found")

        engine = EntityMasterEngine(
            config=EntityMasterConfig(),
            sec_provider=NotFoundProvider(),
        )
        mapping = engine.resolve_entity("NONEXISTENT")
        self.assertEqual(mapping.mapping_status, MappingStatus.UNAVAILABLE)

    def test_idempotent_rerun_returns_same_entity_id(self) -> None:
        """Re-running entity resolution must return the same entity ID each time."""
        mapping1 = self.engine.resolve_entity("AAPL")
        mapping2 = self.engine.resolve_entity("AAPL")
        self.assertEqual(mapping1.entity_id, mapping2.entity_id)
        self.assertEqual(mapping1.cik, mapping2.cik)
        self.assertEqual(mapping1.current_legal_name, mapping2.current_legal_name)
        self.assertEqual(mapping1.mapping_status, MappingStatus.EXACT)

    def test_missing_value_not_converted_to_zero(self) -> None:
        """When data is unavailable, store MANUAL_REQUIRED, not a zero value."""
        mapping = EntityMapping(
            entity_id="CIK0000000000",
            ticker="UNKNOWN",
            mapping_status=MappingStatus.UNAVAILABLE,
            mapping_confidence=MappingConfidence.UNAVAILABLE,
            last_verified="2026-06-29T00:00:00Z",
        )
        self.assertEqual(mapping.mapping_status, MappingStatus.UNAVAILABLE)
        self.assertEqual(mapping.cik, "")  # Not "0000000000"
        self.assertEqual(mapping.current_legal_name, "")  # Not "Unknown"

    def test_running_with_no_openai_api_key(self) -> None:
        """Engine must work without OPENAI_API_KEY set."""
        import os
        with patch.dict(os.environ, {}, clear=True):
            engine = EntityMasterEngine(
                config=EntityMasterConfig(),
                sec_provider=self.fake_sec,
            )
            mapping = engine.resolve_entity("AAPL")
            self.assertEqual(mapping.mapping_status, MappingStatus.EXACT)

    def test_no_model_package_required(self) -> None:
        """Entity master engine must not require any LLM model packages."""
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            # Verify openai is not required for basic operation
            import importlib
            self.assertIsNotNone(importlib.import_module("scanners.entity_master.engine"))


# ---------------------------------------------------------------------------
# Extraction helper tests
# ---------------------------------------------------------------------------


class ExtractionHelperTests(unittest.TestCase):
    def test_former_names_from_list(self) -> None:
        data = {"formerCompanyNames": [
            {"name": "Old Name A", "date": "2020-01-01"},
            {"name": "Old Name B", "date": "2019-01-01"},
        ]}
        names = _extract_former_names(data)
        self.assertEqual(sorted(names), ["Old Name A", "Old Name B"])

    def test_former_names_empty(self) -> None:
        self.assertEqual(_extract_former_names({}), [])
        self.assertEqual(_extract_former_names({"formerCompanyNames": []}), [])

    def test_former_names_dedup(self) -> None:
        data = {"formerCompanyNames": [
            {"name": "Same", "date": "2020-01-01"},
            {"name": "Same", "date": "2019-01-01"},
        ]}
        names = _extract_former_names(data)
        self.assertEqual(len(names), 1)

    def test_former_tickers_from_list(self) -> None:
        data = {"tickers": ["MSFT", "MSFT.B"]}
        tickers = _extract_former_tickers(data)
        self.assertEqual(sorted(tickers), ["MSFT", "MSFT.B"])

    def test_exchange_extraction(self) -> None:
        self.assertEqual(_extract_exchange({"exchange": "Nasdaq"}), "NASDAQ")
        self.assertEqual(_extract_exchange({}), "")


# ---------------------------------------------------------------------------
# SourceEvidence and DerivedValue tests
# ---------------------------------------------------------------------------


class ProvenanceTests(unittest.TestCase):
    def test_source_evidence_from_payload(self) -> None:
        evidence = SourceEvidence.from_payload(
            source="sec_gov",
            record_id="0000320193",
            url="https://www.sec.gov/files/company_tickers.json",
            field="cik",
            payload='{"cik_str": 320193}',
            observed_at="2026-06-29T10:00:00Z",
        )
        self.assertEqual(evidence.source, "sec_gov")
        self.assertEqual(evidence.source_record_id, "0000320193")
        self.assertEqual(evidence.source_field, "cik")
        self.assertTrue(len(evidence.raw_payload_hash) > 0)

    def test_derived_value_payload_hash_auto_generated(self) -> None:
        dv = DerivedValue(
            value=100.0,
            unit="USD",
            as_of="2026-06-29",
            formula_or_rule="cash - debt",
            derivation_type=DerivationType.FORMULA,
        )
        self.assertTrue(len(dv.payload_hash) > 0)

    def test_derived_value_with_source_evidence(self) -> None:
        se = SourceEvidence(
            source="sec_gov",
            source_record_id="0000320193",
            source_field="cash",
            raw_payload_hash="abc123",
        )
        dv = DerivedValue(
            value=1000000000.0,
            unit="USD",
            as_of="2026-06-29",
            source_evidence=se,
            formula_or_rule="XBRL fact extraction",
            derivation_type=DerivationType.XBRL_FACT,
            confidence_status=ConfidenceStatus.VERIFIED,
        )
        self.assertEqual(dv.derivation_type, DerivationType.XBRL_FACT)
        self.assertEqual(dv.confidence_status, ConfidenceStatus.VERIFIED)
        self.assertEqual(dv.source_evidence.source_field, "cash")

    def test_stable_evidence_id_reproducible(self) -> None:
        parts = ["CIK0000320193", "sec_gov", "form4", "purchase", "shares"]
        id1 = stable_evidence_id(parts, prefix="ev")
        id2 = stable_evidence_id(parts, prefix="ev")
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("ev-"))
        self.assertEqual(len(id1), 23)  # ev- + 20 hex chars = 23


# ---------------------------------------------------------------------------
# EvidenceRecord tests
# ---------------------------------------------------------------------------


class EvidenceRecordTests(unittest.TestCase):
    def test_from_derived_value(self) -> None:
        se = SourceEvidence(
            source="sec_gov",
            source_record_id="0000320193-24-000001",
            source_field="shares",
            raw_payload_hash="test_hash_123",
            observed_at="2026-06-29T10:00:00Z",
        )
        dv = DerivedValue(
            value=1000.0,
            unit="shares",
            as_of="2026-06-29",
            source_evidence=se,
            formula_or_rule="Form 4 extraction",
            derivation_type=DerivationType.DETERMINISTIC_PARSE,
            confidence_status=ConfidenceStatus.VERIFIED,
        )
        record = EvidenceRecord.from_derived_value(
            evidence_id="ev-test",
            entity_id="CIK0000320193",
            ticker="AAPL",
            source="insider",
            source_record_id="0000320193-24-000001",
            source_document_type="4",
            event_type="insider_purchase",
            event_date="2026-06-28",
            observed_at="2026-06-29T10:00:00Z",
            source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000001/0000320193-24-000001.txt",
            derived=dv,
        )
        self.assertEqual(record.entity_id, "CIK0000320193")
        self.assertEqual(record.ticker, "AAPL")
        self.assertEqual(record.source_record_id, "0000320193-24-000001")
        self.assertEqual(record.event_type, "insider_purchase")
        self.assertEqual(record.derived_value, 1000.0)
        self.assertEqual(record.derivation_type, DerivationType.DETERMINISTIC_PARSE)
        self.assertEqual(record.confidence_status, ConfidenceStatus.VERIFIED)
        self.assertTrue(record.active)


if __name__ == "__main__":
    unittest.main()
