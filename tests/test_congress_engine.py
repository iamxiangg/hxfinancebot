from __future__ import annotations

import unittest

import pandas as pd

from funnel.congress_adapter import result_to_signal
from scanners.congress.engine import run_scan_from_payload


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


class CongressEngineTests(unittest.TestCase):
    def test_late_disclosed_trade_is_weighted_lower_than_fresh(self) -> None:
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

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=fetcher,
        )

        results = {result.ticker: result for result in scan.ticker_results}
        self.assertIn("FRESH", results)
        self.assertIn("LATE", results)
        self.assertGreater(results["FRESH"].conviction, results["LATE"].conviction)
        self.assertEqual(results["LATE"].signal_trigger, "late_disclosure")
        self.assertEqual(scan.counts["active_fresh_transactions"], 1)
        self.assertEqual(scan.counts["active_late_disclosed_transactions"], 1)

    def test_known_late_disclosure_is_suppressed_on_repeat_run(self) -> None:
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
        fetcher = price_fetcher_factory(
            {"TDG": market_data([100, 98, 96, 95], [1_000_000] * 4, start="2026-04-30")}
        )

        first = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=fetcher,
        )
        second = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:05:00+08:00",
            prior_ledger=first.ledger,
            price_fetcher=fetcher,
        )

        self.assertEqual(len(first.ticker_results), 1)
        self.assertEqual(len(second.ticker_results), 1)
        self.assertTrue(first.ticker_results[0].alertable)
        self.assertFalse(second.ticker_results[0].alertable)
        self.assertIsNone(
            result_to_signal(
                second.ticker_results[0],
                observed_at="2026-06-24T12:05:00+08:00",
            )
        )

    def test_unresolved_public_security_routes_to_review(self) -> None:
        payload = [
            {
                "id": "review-1",
                "ticker": "",
                "asset_name": "Acme Inc Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-21",
                "filing_date": "2026-06-22",
                "amount_range_low": 100000,
                "amount_range_high": 150000,
                "filer_name": "Dana Smith",
                "filer_id": "D1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory({}),
        )

        self.assertEqual(scan.review_audit[0]["reason"], "UNRESOLVED_PUBLIC_SECURITY")
        self.assertEqual(scan.transactions[0].broad_outcome, "REQUIRES_REVIEW")

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

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory(
                {"MSFT": market_data([100, 99, 98, 97], [1_000_000] * 4, start="2026-06-20")}
            ),
        )

        self.assertEqual(scan.counts["duplicate_records"], 1)
        self.assertEqual(scan.transactions[1].reason, "DUPLICATE")

    def test_fresh_transaction_preserves_legacy_score_shape(self) -> None:
        payload = [
            {
                "id": "legacy-1",
                "ticker": "LEG",
                "asset_name": "Legacy Co Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-14",
                "filing_date": "2026-06-20",
                "amount_range_low": 500000,
                "amount_range_high": 500000,
                "filer_name": "Legacy Buyer",
                "filer_id": "L1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]

        scan = run_scan_from_payload(
            payload,
            observed_at="2026-06-24T12:00:00+08:00",
            price_fetcher=price_fetcher_factory(
                {"LEG": market_data([100, 98, 96, 95], [1_000_000] * 4, start="2026-06-14")}
            ),
        )

        result = scan.ticker_results[0]
        self.assertAlmostEqual(result.conviction, 60.78, places=2)
        self.assertAlmostEqual(result.entry, 68.56, places=2)
        self.assertEqual(result.signal_trigger, "fresh_transaction")
        self.assertAlmostEqual(result.weighted_average_activity_weight, 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
