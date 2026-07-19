from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

import pandas as pd

from funnel.congress_adapter import result_to_signal
from scanners.congress.engine import run_scan_from_payload
from scanners.congress.models import CompanyClassification, ExecutiveRole, PoliticalFiler, PoliticalRoleResolution


def market_data(
    close_values: list[float],
    volume_values: list[float],
    *,
    start: str,
) -> dict[str, pd.Series]:
    index = pd.date_range(start, periods=len(close_values), freq="D")
    return {
        "close": pd.Series(close_values, index=index),
        "volume": pd.Series(volume_values, index=index),
    }


def price_fetcher_factory(payload: dict[str, dict[str, pd.Series]]):
    def _fetch(symbols: list[str], earliest):
        return {symbol: payload[symbol] for symbol in symbols if symbol in payload}

    return _fetch


def fake_classification(*args, **kwargs) -> CompanyClassification:
    ticker = args[-1] if args else kwargs["ticker"]
    mapping = {
        "LMT": CompanyClassification("LMT", "industrials", "aerospace_defense", ("defense",), "override", "HIGH"),
        "NVDA": CompanyClassification("NVDA", "technology", "semiconductors", ("semiconductors",), "override", "HIGH"),
        "VOO": CompanyClassification("VOO", "broad_market", "broad_market_etf", ("broad_market",), "override", "HIGH"),
        "FRESH": CompanyClassification("FRESH", "technology", "software_infrastructure", ("software",), "test", "MEDIUM"),
        "LATE": CompanyClassification("LATE", "technology", "software_infrastructure", ("software",), "test", "MEDIUM"),
        "BREADTH": CompanyClassification("BREADTH", "technology", "software_infrastructure", ("software",), "test", "MEDIUM"),
        "TDG": CompanyClassification("TDG", "industrials", "aerospace_defense", ("defense",), "test", "MEDIUM"),
    }
    return mapping.get(ticker, CompanyClassification(ticker, "technology", "software", ("software",), "test", "MEDIUM"))


def fake_current_roles(*args, **kwargs) -> PoliticalRoleResolution:
    filer = args[-1] if args else kwargs["filer"]
    if filer.branch == "executive":
        return PoliticalRoleResolution(
            filer=PoliticalFiler(**{**filer.__dict__, "identity_resolution_status": "NOT_APPLICABLE_EXECUTIVE"}),
            status="NOT_APPLICABLE_EXECUTIVE",
            executive_role=ExecutiveRole(
                agency=filer.agency,
                agency_key="white_house_office",
                level=str(filer.level or ""),
                seniority_class="PRESIDENT" if "trump" in filer.filer_id else "OTHER_EXECUTIVE",
                confidence="HIGH",
            ),
        )
    return PoliticalRoleResolution(
        filer=PoliticalFiler(**{**filer.__dict__, "bioguide_id": "P000197", "identity_resolution_status": "EXPLICIT_OVERRIDE"}),
        status="RESOLVED",
        source_payload_hash="role-hash",
        source_retrieved_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
    )


class CongressEngineTests(unittest.TestCase):
    @patch("scanners.congress.engine.CompanyClassificationProvider.classify", side_effect=fake_classification)
    @patch("scanners.congress.engine.CongressionalRoleProvider.current_roles", side_effect=fake_current_roles)
    def test_late_disclosed_trade_is_weighted_lower_than_fresh(self, _mock_roles, _mock_classify) -> None:
        payload = [
            {
                "id": "fresh-1",
                "ticker": "FRESH",
                "asset_name": "Fresh Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-14",
                "filing_date": "2026-06-20",
                "amount_range_low": 500000,
                "amount_range_high": 500000,
                "filer_name": "Alice Smith",
                "filer_id": "A1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
            {
                "id": "late-1",
                "ticker": "LATE",
                "asset_name": "Late Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-04-25",
                "filing_date": "2026-06-20",
                "amount_range_low": 500000,
                "amount_range_high": 500000,
                "filer_name": "Bob Jones",
                "filer_id": "B1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "Senate",
            },
        ]

        fetcher = price_fetcher_factory(
            {
                "FRESH": market_data([100, 98, 96, 95], [1_000_000] * 4, start="2026-06-14"),
                "LATE": market_data([100, 98, 96, 95], [1_000_000] * 4, start="2026-04-25"),
            }
        )

        scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", price_fetcher=fetcher)

        results = {result.ticker: result for result in scan.ticker_results}
        self.assertGreater(results["FRESH"].conviction, results["LATE"].conviction)
        self.assertEqual(results["LATE"].signal_trigger, "late_disclosure")
        self.assertEqual(scan.counts["active_fresh_transactions"], 1)
        self.assertEqual(scan.counts["active_late_disclosed_transactions"], 1)

    def test_invalid_scope_fails_clearly(self) -> None:
        with self.assertRaises(ValueError):
            run_scan_from_payload([], branch_scope="bad-scope")

    def test_source_id_fallback_is_retained(self) -> None:
        payload = [
            {
                "id": "src-1",
                "ticker": "MSFT",
                "asset_name": "Microsoft Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
                "source": "house_disclosure",
            }
        ]
        scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", price_fetcher=price_fetcher_factory({}))
        self.assertEqual(scan.transactions[0].source_id, "house_disclosure")

    def test_unresolved_public_security_enters_manual_review_queue(self) -> None:
        payload = [
            {
                "id": "review-1",
                "ticker": "",
                "asset_name": "Boston Scientific Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
            {
                "id": "invalid-1",
                "ticker": "",
                "asset_name": "Unknown Asset",
                "asset_type": "Other",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
        ]

        scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", price_fetcher=price_fetcher_factory({}))

        self.assertEqual(len(scan.review_audit), 1)
        self.assertEqual(scan.review_audit[0]["trade_key"], "id:review-1")
        self.assertEqual(scan.review_audit[0]["classification"], "REQUIRES_REVIEW")
        self.assertTrue(scan.review_audit[0]["manual_review_required"])

    def test_review_override_resolves_unresolved_public_security(self) -> None:
        payload = [
            {
                "id": "review-1",
                "ticker": "",
                "asset_name": "Boston Scientific Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory({}),
            review_overrides={
                "id:review-1": {
                    "Review Decision": "RESOLVE",
                    "Resolved Ticker": "BSX",
                    "Resolved Yahoo Ticker": "BSX",
                    "Resolved Asset Class": "stock",
                    "Resolved Action": "purchase",
                    "Reviewer Note": "Exact public-company name match",
                    "Active": "YES",
                }
            },
        )

        self.assertFalse(scan.review_audit)
        self.assertEqual(scan.transactions[0].ticker, "BSX")
        self.assertEqual(scan.transactions[0].yf_ticker, "BSX")
        self.assertEqual(scan.transactions[0].reason, "ACTIVE_FRESH")
        self.assertEqual(scan.transactions[0].broad_outcome, "RETAINED_ACTIVE")

    def test_review_override_reuses_exact_asset_name_resolution(self) -> None:
        payload = [
            {
                "id": "review-2",
                "ticker": "",
                "asset_name": "Boston Scientific Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory({}),
            review_overrides={
                "id:review-1": {
                    "Asset Name": "BOSTON SCIENTIFIC CORP COMMON STOCK",
                    "Resolved Ticker": "BSX",
                    "Active": "YES",
                }
            },
        )

        self.assertFalse(scan.review_audit)
        self.assertEqual(scan.transactions[0].ticker, "BSX")
        self.assertEqual(scan.transactions[0].yf_ticker, "BSX")
        self.assertEqual(scan.transactions[0].reason, "ACTIVE_FRESH")

    @patch("scanners.congress.engine.CompanyClassificationProvider.classify", side_effect=fake_classification)
    @patch("scanners.congress.engine.CongressionalRoleProvider.current_roles", side_effect=fake_current_roles)
    def test_spouse_ownership_is_included_and_not_counted_twice(self, _mock_roles, _mock_classify) -> None:
        payload = [
            {
                "id": "breadth-1",
                "ticker": "BREADTH",
                "asset_name": "Breadth Co Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-14",
                "filing_date": "2026-06-20",
                "amount_range_low": 250000,
                "amount_range_high": 250000,
                "filer_name": "Nancy Pelosi",
                "filer_id": "house_nancy_pelosi",
                "owner": "SP",
                "branch": "Legislative",
                "chamber": "House",
                "source_id": "house_nancy_pelosi",
            },
            {
                "id": "breadth-2",
                "ticker": "BREADTH",
                "asset_name": "Breadth Co Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-15",
                "filing_date": "2026-06-20",
                "amount_range_low": 250000,
                "amount_range_high": 250000,
                "filer_name": "Nancy Pelosi",
                "filer_id": "house_nancy_pelosi",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
                "source_id": "house_nancy_pelosi",
            },
        ]

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory({"BREADTH": market_data([100, 99, 98, 97], [1_000_000] * 4, start="2026-06-14")}),
        )
        result = scan.ticker_results[0]

        self.assertEqual(result.buyers, 1)
        self.assertEqual(result.active_trade_count, 2)
        self.assertEqual(scan.transactions[0].owner_relationship, "spouse")
        self.assertIn(result.category, {"wait", "other", "actionable"})

    def test_duplicate_trade_is_audited(self) -> None:
        payload = [
            {
                "id": "dup-1",
                "ticker": "MSFT",
                "asset_name": "Microsoft Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
            {
                "id": "dup-1",
                "ticker": "MSFT",
                "asset_name": "Microsoft Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 100000,
                "filer_name": "Evan Smith",
                "filer_id": "E1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
        ]
        scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", price_fetcher=price_fetcher_factory({}))
        self.assertEqual(scan.counts["duplicate_records"], 1)
        self.assertEqual(scan.transactions[1].reason, "DUPLICATE")

    @patch("scanners.congress.engine.CompanyClassificationProvider.classify", side_effect=fake_classification)
    def test_trump_executive_voo_is_context_only(self, _mock_classify) -> None:
        payload = [
            {
                "id": "trump-1",
                "ticker": "VOO",
                "asset_name": "Vanguard S&P 500 ETF",
                "asset_type": "ETF",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 1000000,
                "amount_range_high": 5000000,
                "filer_name": "Donald Trump",
                "filer_id": "oge_donald_trump",
                "owner": "Self",
                "branch": "Executive",
                "agency": "White House Office",
                "office": "President",
                "filing_type": "278-T",
            }
        ]
        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            branch_scope="all",
            price_fetcher=price_fetcher_factory({"VOO": market_data([100, 101, 102, 103], [1_000_000] * 4, start="2026-06-20")}),
        )
        result = scan.ticker_results[0]

        self.assertEqual(result.category, "context")
        self.assertIn("executive", result.branches)
        self.assertEqual(result.asset_intent_classes, ["BROAD_MARKET_ETF"])
        self.assertIsNone(result_to_signal(result, observed_at="2026-06-24T12:00:00+08:00"))

    @patch("scanners.congress.engine.CompanyClassificationProvider.classify", side_effect=fake_classification)
    @patch("scanners.congress.engine.CongressionalRoleProvider.current_roles", side_effect=fake_current_roles)
    def test_scope_filters_congress_and_executive_records(self, _mock_roles, _mock_classify) -> None:
        payload = [
            {
                "id": "pelosi-1",
                "ticker": "LMT",
                "asset_name": "Lockheed Martin Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 250000,
                "filer_name": "Nancy Pelosi",
                "filer_id": "house_nancy_pelosi",
                "owner": "SP",
                "branch": "Legislative",
                "chamber": "House",
                "source_id": "house_nancy_pelosi",
            },
            {
                "id": "trump-1",
                "ticker": "VOO",
                "asset_name": "Vanguard S&P 500 ETF",
                "asset_type": "ETF",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-20",
                "filing_date": "2026-06-22",
                "amount_range_low": 1000000,
                "amount_range_high": 5000000,
                "filer_name": "Donald Trump",
                "filer_id": "oge_donald_trump",
                "owner": "Self",
                "branch": "Executive",
                "agency": "White House Office",
                "office": "President",
                "filing_type": "278-T",
            },
        ]
        fetcher = price_fetcher_factory(
            {
                "LMT": market_data([100, 101, 102, 103], [1_000_000] * 4, start="2026-06-20"),
                "VOO": market_data([100, 101, 102, 103], [1_000_000] * 4, start="2026-06-20"),
            }
        )
        all_scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", branch_scope="all", price_fetcher=fetcher)
        congress_scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", branch_scope="congress_only", price_fetcher=fetcher)
        executive_scan = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", branch_scope="executive_only", price_fetcher=fetcher)

        self.assertEqual({result.ticker for result in all_scan.ticker_results}, {"LMT", "VOO"})
        self.assertEqual({result.ticker for result in congress_scan.ticker_results}, {"LMT"})
        self.assertEqual({result.ticker for result in executive_scan.ticker_results}, {"VOO"})

    def test_known_disclosure_is_suppressed_on_repeat_run(self) -> None:
        payload = [
            {
                "id": "late-2",
                "ticker": "TDG",
                "asset_name": "TDG Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-04-30",
                "filing_date": "2026-06-20",
                "amount_range_low": 600000,
                "amount_range_high": 600000,
                "filer_name": "Chris Doe",
                "filer_id": "C1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]
        fetcher = price_fetcher_factory({"TDG": market_data([100, 98, 96, 95], [1_000_000] * 4, start="2026-04-30")})
        with patch("scanners.congress.engine.CompanyClassificationProvider.classify", side_effect=fake_classification), patch(
            "scanners.congress.engine.CongressionalRoleProvider.current_roles", side_effect=fake_current_roles
        ):
            first = run_scan_from_payload(payload, observed_at="2026-06-24T12:00:00+08:00", price_fetcher=fetcher)
            second = run_scan_from_payload(
                payload,
                observed_at="2026-06-24T12:05:00+08:00",
                prior_ledger=first.ledger,
                price_fetcher=fetcher,
            )

        self.assertTrue(first.ticker_results[0].alertable)
        self.assertFalse(second.ticker_results[0].alertable)
        self.assertIsNone(result_to_signal(second.ticker_results[0], observed_at="2026-06-24T12:05:00+08:00"))


if __name__ == "__main__":
    unittest.main()
