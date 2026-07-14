from __future__ import annotations

import unittest

from research.regulatory.identifiers import build_programme_key, build_trial_id, build_unresolved_issue_id
from research.regulatory.programme_registry import build_programme_components
from research.regulatory.models import CompanyEntity


class RegulatoryIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = CompanyEntity(company_id="CIK0000000001", legal_name="Example Bio", ticker="EXMP")

    def test_different_indications_remain_separate(self) -> None:
        _, _, _, _, first = build_programme_components(
            company=self.company,
            product_name="HX-101",
            disease="Glioblastoma",
            line_of_therapy="2L",
        )
        _, _, _, _, second = build_programme_components(
            company=self.company,
            product_name="HX-101",
            disease="Ovarian cancer",
            line_of_therapy="2L",
        )
        self.assertNotEqual(first.programme_key, second.programme_key)

    def test_different_regimens_remain_separate(self) -> None:
        _, regimen_a, _, _, first = build_programme_components(
            company=self.company,
            product_name="HX-101",
            disease="Glioblastoma",
            route="IV",
            schedule="Q2W",
        )
        _, regimen_b, _, _, second = build_programme_components(
            company=self.company,
            product_name="HX-101",
            disease="Glioblastoma",
            route="IV",
            schedule="Q1W",
        )
        self.assertNotEqual(regimen_a.regimen_id, regimen_b.regimen_id)
        self.assertNotEqual(first.programme_key, second.programme_key)

    def test_trial_id_prefers_nct_id(self) -> None:
        trial_id = build_trial_id(
            nct_id="NCT01234567",
            sponsor="Example Bio",
            official_title="Ignored because NCT exists",
            product_name="HX-101",
            indication_name="Glioblastoma",
            phase="Phase 2",
        )
        self.assertEqual(trial_id, "NCT01234567")

    def test_fda_unresolved_issue_ids_collapse_repeated_sponsor_product_noise(self) -> None:
        first = build_unresolved_issue_id(
            source_name="drugs_at_fda",
            source_record_id="NDA-1",
            company_name="KENVUE BRANDS",
            ticker="",
            trial_nct_id="",
            product_name="ZYRTEC",
            reason="Exact company mapping unavailable.",
        )
        second = build_unresolved_issue_id(
            source_name="drugs_at_fda",
            source_record_id="NDA-2",
            company_name="KENVUE BRANDS",
            ticker="",
            trial_nct_id="",
            product_name="ZYRTEC",
            reason="Exact company mapping unavailable.",
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
