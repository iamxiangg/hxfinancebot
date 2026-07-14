from __future__ import annotations

import unittest

from providers.sec.official import SEC_DATA_URL, TICKER_MAP_URL
from research.regulatory.entity_resolution import RegulatoryEntityResolver
from research.regulatory.models import MappingConfidenceLevel


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.ticker_map = {
            "0": {"ticker": "OGN", "cik_str": "1821825", "title": "Organon & Co."},
            "1": {"ticker": "KVUE", "cik_str": "1944048", "title": "Kenvue Inc."},
            "2": {"ticker": "RDAC", "cik_str": "1881741", "title": "Rising Dragon Acquisition Corp."},
            "3": {"ticker": "TEVA", "cik_str": "818686", "title": "TEVA PHARMACEUTICAL INDUSTRIES LTD"},
        }
        self.submissions = {
            "0001821825": {"sic": "2834"},
            "0001944048": {"sic": "2834"},
            "0001881741": {"sic": "6770"},
            "0000818686": {"sic": "2834"},
        }

    def get(self, url: str, **kwargs):
        if url == TICKER_MAP_URL:
            return _FakeResponse(self.ticker_map)
        if url.startswith(f"{SEC_DATA_URL}/submissions/CIK"):
            cik = url.rsplit("CIK", 1)[-1].split(".json", 1)[0]
            return _FakeResponse(self.submissions.get(cik, {}))
        raise AssertionError(f"Unexpected URL: {url}")


class RegulatoryEntityResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _FakeSession()

    def test_canonical_healthcare_name_match_resolves_without_alias(self) -> None:
        resolver = RegulatoryEntityResolver(
            session=self.session,
            sic_allowlist=["2834"],
        )
        result = resolver.resolve(sponsor_name="Kenvue Brands")
        self.assertIsNotNone(result.entity)
        self.assertEqual(result.entity.ticker, "KVUE")
        self.assertEqual(result.confidence, MappingConfidenceLevel.MEDIUM)

    def test_non_healthcare_single_token_candidate_stays_unresolved(self) -> None:
        resolver = RegulatoryEntityResolver(
            session=self.session,
            sic_allowlist=["2834"],
        )
        result = resolver.resolve(sponsor_name="Rising")
        self.assertIsNone(result.entity)
        self.assertTrue(result.manual_required)

    def test_ownership_edge_rolls_subsidiary_up_to_parent(self) -> None:
        resolver = RegulatoryEntityResolver(
            config_payload={
                "ownership_edges": [
                    {
                        "child_name": "Actavis Labs FL Inc",
                        "parent_ticker": "TEVA",
                    }
                ]
            },
            session=self.session,
            sic_allowlist=["2834"],
        )
        result = resolver.resolve(sponsor_name="Actavis Labs FL Inc")
        self.assertIsNotNone(result.entity)
        self.assertEqual(result.entity.ticker, "TEVA")
        self.assertEqual(result.confidence, MappingConfidenceLevel.MEDIUM)


if __name__ == "__main__":
    unittest.main()
