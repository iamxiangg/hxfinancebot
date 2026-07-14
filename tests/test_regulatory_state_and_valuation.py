from __future__ import annotations

import unittest

from research.regulatory.history import apply_events
from research.regulatory.models import (
    CompanyOperatingMode,
    FinancialSnapshot,
    NormalizedRegulatoryEvent,
    ProgrammeIdentity,
    ValuationAssumption,
    ValuationStatus,
)
from research.regulatory.valuation import compute_valuation_snapshot


class RegulatoryStateAndValuationTests(unittest.TestCase):
    def test_phase_three_pass_updates_pivotal_state(self) -> None:
        programme = ProgrammeIdentity(
            programme_key="pgm-1",
            company_id="CIK1",
            product_id="prd-1",
            regimen_id="reg-1",
            indication_id="ind-1",
            trial_id="NCT1",
        )
        event = NormalizedRegulatoryEvent(
            normalized_event_id="nev-1",
            raw_event_id="raw-1",
            event_date="2026-07-08",
            normalized_event_type="PRIMARY_ENDPOINT_MET",
            source_name="sec",
            programme_key="pgm-1",
            company_id="CIK1",
            metadata={"trial_phase": "Phase 3"},
            factual_summary="met the primary endpoint",
        )
        update = apply_events(programme=programme, events=[event])
        self.assertEqual(update.current_state.clinical_evidence, "PIVOTAL_ENDPOINT_PASSED")

    def test_pre_funded_warrants_are_included_in_economic_shares(self) -> None:
        snapshot = FinancialSnapshot(
            snapshot_id="fin-1",
            company_id="CIK1",
            as_of="2026-07-08",
            common_shares=100.0,
            issued_shares_post_offering=10.0,
            pre_funded_warrants=5.0,
            exercised_unsettled_shares=2.0,
        )
        self.assertEqual(snapshot.economic_shares, 117.0)

    def test_valuation_is_model_incomplete_without_required_inputs(self) -> None:
        assumption = ValuationAssumption(
            assumption_id="ass-1",
            company_id="CIK1",
            programme_key="pgm-1",
            operating_mode=CompanyOperatingMode.CLINICAL_STAGE,
            active=True,
            success_ev=500.0,
            updated_at="2026-07-08",
        )
        snapshot = compute_valuation_snapshot(company_id="CIK1", programme_key="pgm-1", assumption=assumption, financial_snapshot=None)
        self.assertEqual(snapshot.valuation_status, ValuationStatus.MODEL_INCOMPLETE)

    def test_holding_company_attribution_uses_ownership_percentage(self) -> None:
        assumption = ValuationAssumption(
            assumption_id="ass-2",
            company_id="CIK1",
            programme_key="pgm-1",
            operating_mode=CompanyOperatingMode.HOLDING_COMPANY,
            active=True,
            success_ev=100.0,
            failure_ev=20.0,
            current_ev=60.0,
            updated_at="2026-07-08",
        )
        financial = FinancialSnapshot(
            snapshot_id="fin-2",
            company_id="CIK1",
            as_of="2026-07-08",
            common_shares=10.0,
            attributable_cash=5.0,
            total_debt=0.0,
        )
        snapshot = compute_valuation_snapshot(
            company_id="CIK1",
            programme_key="pgm-1",
            assumption=assumption,
            financial_snapshot=financial,
            economic_attribution_percentage=55.0,
        )
        self.assertAlmostEqual(snapshot.attributable_value or 0.0, 55.0)


if __name__ == "__main__":
    unittest.main()

