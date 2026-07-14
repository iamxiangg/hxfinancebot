from __future__ import annotations

import unittest

from research.regulatory.entity_resolution import EntityResolutionResult
from research.regulatory.models import (
    CompanyEntity,
    EventOutcome,
    MappingConfidenceLevel,
    RawRegulatoryRecord,
    SourceTier,
)
from research.regulatory.normalizer import normalize_record


class RegulatoryNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = EntityResolutionResult(
            entity=CompanyEntity(company_id="CIK0000000123", legal_name="Example Bio", ticker="EXMP"),
            confidence=MappingConfidenceLevel.HIGH,
        )

    def test_sec_exact_phrase_parsing_is_deterministic(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-1",
            source_name="sec",
            source_record_id="0001",
            source_url="https://sec.example/1",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-08",
            exact_text=(
                "Example Bio announced it met the primary endpoint in NCT01234567 "
                "with hazard ratio of 0.72 and p-value = 0.03."
            ),
            company_name="Example Bio",
            product_name="HX-101",
            indication_name="Glioblastoma",
            structured_data={"phase": "Phase 3"},
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.normalized_event_type, "PRIMARY_ENDPOINT_MET")
        self.assertEqual(event.outcome, EventOutcome.PASSED)
        self.assertEqual(event.metadata["nct_id"], "NCT01234567")
        self.assertEqual(event.metadata["hazard_ratio"], "0.72")
        self.assertEqual(event.metadata["p_value"], "0.03")

    def test_ambiguous_sec_text_routes_to_unresolved(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-2",
            source_name="sec",
            source_record_id="0002",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-08",
            exact_text="Management described progress and encouraging discussions with the FDA.",
            company_name="Example Bio",
            product_name="HX-101",
            indication_name="Glioblastoma",
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        self.assertEqual(len(result.events), 0)
        self.assertEqual(len(result.unresolved), 1)

    def test_clinicaltrials_results_posted_is_not_efficacy_pass(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-3",
            source_name="clinicaltrials",
            source_record_id="NCT01234567",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-08",
            company_name="Example Bio",
            product_name="HX-101",
            indication_name="Glioblastoma",
            structured_data={
                "overall_status": "COMPLETED",
                "previous_status": "ACTIVE_NOT_RECRUITING",
                "results_first_posted": "2026-07-08",
                "phase": "Phase 2",
            },
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        event_types = {item.normalized_event_type: item for item in result.events}
        self.assertIn("RESULTS_POSTED", event_types)
        self.assertEqual(event_types["RESULTS_POSTED"].outcome, EventOutcome.PENDING)
        self.assertNotIn("PRIMARY_ENDPOINT_MET", event_types)

    def test_sec_disclaimer_does_not_trigger_fda_approval(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-4",
            source_name="sec",
            source_record_id="0004",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-08",
            exact_text=(
                "Tibulizumab is an investigational agent. Its efficacy and safety have not been established "
                "or approved by the FDA or any regulatory agency worldwide."
            ),
            company_name="Example Bio",
            product_name="HX-101",
            indication_name="Glioblastoma",
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        self.assertEqual(len(result.events), 0)
        self.assertEqual(len(result.unresolved), 1)

    def test_sec_risk_factor_does_not_trigger_clinical_hold(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-5",
            source_name="sec",
            source_record_id="0005",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-08",
            exact_text=(
                "The FDA may impose a clinical hold on any clinical investigation by the Company or any of its subsidiaries."
            ),
            company_name="Example Bio",
            product_name="HX-101",
            indication_name="Glioblastoma",
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        self.assertEqual(len(result.events), 0)
        self.assertEqual(len(result.unresolved), 1)

    def test_historical_precedent_deck_is_context_not_new_clinical_pass(self) -> None:
        record = RawRegulatoryRecord(
            raw_event_id="raw-6",
            source_name="sec",
            source_record_id="0006",
            source_tier=SourceTier.TIER_1,
            published_at="2026-07-14",
            company_name="Zura Bio Ltd",
            product_name="Tibulizumab (ZB-106)",
            indication_name="Systemic sclerosis / diffuse cutaneous systemic sclerosis",
            exact_text=(
                "Tibulizumab is an investigational agent. Its efficacy and safety have not been established or approved by the FDA. "
                "Brodalumab demonstrated improvement in systemic sclerosis outcomes and belimumab showed directionally favorable results."
            ),
        )
        result = normalize_record(record=record, mapping=self.mapping, programme_key="pgm-1")
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.normalized_event_type, "HISTORICAL_CLINICAL_PRECEDENT")
        self.assertEqual(event.outcome, EventOutcome.NO_STAGE_CHANGE)
        self.assertIn("does not constitute new company-specific clinical data", event.factual_summary.lower())


if __name__ == "__main__":
    unittest.main()
