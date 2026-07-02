from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import requests

from scanners.vpma.alpha_vantage import (
    AlphaVantageClient,
    parse_earnings_estimates,
    parse_earnings_history,
)


class _MockResponse:
    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


class _MockSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AlphaVantageTests(unittest.TestCase):
    def test_no_key_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = AlphaVantageClient(max_calls=2)
            result = client.fetch_earnings_estimates("MSFT")

        self.assertEqual(result.status, "SKIPPED_NO_KEY")
        self.assertIsNone(result.confirmation_score)

    def test_successful_parse(self) -> None:
        payload = {
            "quarterlyEstimates": [
                {
                    "fiscalDateEnding": "2026-09-30",
                    "epsEstimate": "1.50",
                    "epsEstimatePrevious": "1.30",
                    "revenueEstimate": "1500000000",
                    "revenueEstimatePrevious": "1400000000",
                    "numberOfAnalysts": "24",
                    "epsEstimateRevisionUp": "10",
                    "epsEstimateRevisionDown": "2",
                    "revenueEstimateRevisionUp": "7",
                    "revenueEstimateRevisionDown": "1",
                }
            ]
        }

        result = parse_earnings_estimates("NVDA", payload)

        self.assertEqual(result.status, "ENRICHED")
        self.assertTrue(result.confirmation_score and result.confirmation_score > 65.0)
        self.assertTrue(result.fundamentally_confirmed)
        self.assertEqual(result.details["analyst_count"], 24)

    def test_partial_fields_do_not_crash(self) -> None:
        result = parse_earnings_estimates("AAPL", {"quarterlyEstimates": [{"fiscalDateEnding": "2026-09-30"}]})
        self.assertEqual(result.status, "ENRICHED")
        self.assertIsNone(result.confirmation_score)

    def test_parse_earnings_history_maps_latest_quarters(self) -> None:
        payload = {
            "quarterlyEarnings": [
                {
                    "reportedDate": "2026-06-30",
                    "reportedEPS": "1.25",
                    "estimatedEPS": "1.10",
                },
                {
                    "reportedDate": "2026-03-31",
                    "reportedEPS": "1.05",
                    "estimatedEPS": "0.95",
                },
            ]
        }

        result = parse_earnings_history(payload)

        self.assertEqual(result["q1_reported"], 1.25)
        self.assertEqual(result["q1_estimated"], 1.10)
        self.assertEqual(result["q1_report_date"], "2026-06-30")
        self.assertEqual(result["q2_reported"], 1.05)
        self.assertEqual(result["q2_estimated"], 0.95)
        self.assertEqual(result["q2_report_date"], "2026-03-31")

    def test_quota_and_api_error_responses(self) -> None:
        quota_client = AlphaVantageClient(
            api_key="demo",
            session=_MockSession([_MockResponse({"Note": "quota"})]),
        )
        api_error_client = AlphaVantageClient(
            api_key="demo",
            session=_MockSession([_MockResponse({"Error Message": "bad symbol"})]),
        )

        self.assertEqual(quota_client.fetch_earnings_estimates("SHOP").status, "RATE_LIMITED")
        self.assertEqual(api_error_client.fetch_earnings_estimates("SHOP").status, "API_ERROR")

    def test_timeout_retries_once(self) -> None:
        session = _MockSession(
            [
                requests.Timeout("first"),
                _MockResponse({"quarterlyEstimates": [{"fiscalDateEnding": "2026-09-30"}]}),
            ]
        )
        client = AlphaVantageClient(api_key="demo", session=session, retry_limit=1)

        result = client.fetch_earnings_estimates("AMD")

        self.assertEqual(session.calls, 2)
        self.assertEqual(result.status, "ENRICHED")

    def test_budget_limit(self) -> None:
        session = _MockSession(
            [
                _MockResponse({"quarterlyEstimates": [{"fiscalDateEnding": "2026-09-30"}]}),
            ]
        )
        client = AlphaVantageClient(api_key="demo", session=session, max_calls=1)

        first = client.fetch_earnings_estimates("MSFT")
        second = client.fetch_earnings_estimates("AAPL")

        self.assertEqual(first.status, "ENRICHED")
        self.assertEqual(second.status, "SKIPPED_BUDGET")

    def test_api_key_not_logged_on_request_failure(self) -> None:
        secret = "very-secret-key"
        session = _MockSession([requests.RequestException(f"failed with {secret}")])
        client = AlphaVantageClient(api_key=secret, session=session, retry_limit=0)

        with self.assertLogs("scanners.vpma.alpha_vantage", level="WARNING") as captured:
            result = client.fetch_earnings_estimates("META")

        self.assertEqual(result.status, "REQUEST_ERROR")
        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
