from __future__ import annotations

from datetime import datetime
import unittest

from scanners.congress.models import CompanyClassification, PoliticalFiler
from scanners.congress.political_roles import CongressionalRoleProvider
from scanners.congress.role_relevance import evaluate_role_relevance


class PoliticalRoleTests(unittest.TestCase):
    def test_pelosi_override_resolves_without_fuzzy_matching(self) -> None:
        provider = CongressionalRoleProvider(enabled=False)
        payload_provider = CongressionalRoleProvider(
            enabled=True,
            payload_overrides={
                "legislators_current": [
                    {
                        "id": {"bioguide": "P000197"},
                        "name": {"official_full": "Nancy Pelosi", "first": "Nancy", "last": "Pelosi"},
                        "terms": [{"type": "rep", "state": "CA", "party": "Democrat"}],
                    }
                ],
                "legislators_historical": [],
                "committees_current": [],
                "committee_membership_current": {},
            },
        )
        filer = PoliticalFiler(
            filer_id="house_nancy_pelosi",
            filer_name="Nancy Pelosi",
            branch="congress",
            chamber="house",
            state="CA",
            source_id="house_nancy_pelosi",
        )

        disabled = provider.current_roles(filer, as_of=datetime.fromisoformat("2026-06-24T12:00:00+08:00"))
        resolved = payload_provider.current_roles(filer, as_of=datetime.fromisoformat("2026-06-24T12:00:00+08:00"))

        self.assertEqual(disabled.status, "ROLE_SOURCE_UNAVAILABLE")
        self.assertEqual(resolved.filer.bioguide_id, "P000197")
        self.assertEqual(resolved.filer.identity_resolution_status, "EXPLICIT_OVERRIDE")

    def test_executive_role_relevance_and_broad_market_etf_stays_zero(self) -> None:
        provider = CongressionalRoleProvider(enabled=True, payload_overrides={})
        filer = PoliticalFiler(
            filer_id="oge_donald_trump",
            filer_name="Donald Trump",
            branch="executive",
            agency="White House Office",
            office="President",
            level="I",
            source_id="oge_donald_trump",
        )
        role_resolution = provider.current_roles(filer, as_of=datetime.fromisoformat("2026-06-24T12:00:00+08:00"))
        classification = CompanyClassification(
            ticker="VOO",
            sector="broad_market",
            industry="broad_market_etf",
            thematic_exposures=("broad_market",),
            source="override",
            confidence="HIGH",
        )
        relevance = evaluate_role_relevance(role_resolution, classification)

        self.assertEqual(role_resolution.executive_role.seniority_class, "PRESIDENT")
        self.assertEqual(relevance.score, 0.0)
        self.assertIn(relevance.status, {"UNMAPPED_ROLE", "NOT_APPLICABLE"})


if __name__ == "__main__":
    unittest.main()
