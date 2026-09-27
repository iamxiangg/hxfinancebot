import os
import unittest
from unittest.mock import Mock, patch

import requests

from providers.hx_market_bridge import HxMarketBridgeError, post_bridge


class HxMarketBridgeRetryTests(unittest.TestCase):
    def _env(self) -> dict[str, str]:
        return {
            "HX_MARKET_INGEST_URL": "https://example.test/functions/v1/hx-market-ingest",
            "HX_MARKET_INGEST_SECRET": "secret",
            "HX_MARKET_BRIDGE_MAX_ATTEMPTS": "3",
            "HX_MARKET_BRIDGE_BACKOFF_SECONDS": "0",
        }

    def _response(self, status: int, payload: object) -> Mock:
        response = Mock()
        response.status_code = status
        response.ok = 200 <= status < 300
        response.json.return_value = payload
        return response

    def test_transport_failure_retries_then_succeeds(self) -> None:
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "providers.hx_market_bridge.requests.post",
            side_effect=[
                requests.ConnectionError("connection reset by peer"),
                self._response(200, {"status": "OK", "items": []}),
            ],
        ) as mock_post, patch("providers.hx_market_bridge.time.sleep") as mock_sleep:
            result = post_bridge({"action": "GET_CAPABILITY_WORK", "limit": 50})

        self.assertEqual(result["status"], "OK")
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(0.0)

    def test_retryable_http_503_retries_then_succeeds(self) -> None:
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "providers.hx_market_bridge.requests.post",
            side_effect=[
                self._response(503, {"error": "temporary"}),
                self._response(200, {"status": "OK"}),
            ],
        ) as mock_post, patch("providers.hx_market_bridge.time.sleep") as mock_sleep:
            result = post_bridge({"action": "PING"})

        self.assertEqual(result, {"status": "OK"})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(0.0)

    def test_non_retryable_http_400_fails_immediately(self) -> None:
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "providers.hx_market_bridge.requests.post",
            return_value=self._response(400, {"error": "bad_request"}),
        ) as mock_post, patch("providers.hx_market_bridge.time.sleep") as mock_sleep:
            with self.assertRaisesRegex(HxMarketBridgeError, "HTTP 400"):
                post_bridge({"action": "GET_CAPABILITY_WORK"})

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    def test_transport_failure_reports_attempt_count(self) -> None:
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "providers.hx_market_bridge.requests.post",
            side_effect=requests.ConnectionError("connection reset by peer"),
        ) as mock_post, patch("providers.hx_market_bridge.time.sleep"):
            with self.assertRaisesRegex(
                HxMarketBridgeError,
                r"transport failure action=GET_CAPABILITY_WORK after 3 attempt\(s\)",
            ):
                post_bridge({"action": "GET_CAPABILITY_WORK"})

        self.assertEqual(mock_post.call_count, 3)

    def test_unknown_action_does_not_retry(self) -> None:
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "providers.hx_market_bridge.requests.post",
            side_effect=requests.ConnectionError("connection reset by peer"),
        ) as mock_post, patch("providers.hx_market_bridge.time.sleep") as mock_sleep:
            with self.assertRaises(HxMarketBridgeError):
                post_bridge({"action": "FUTURE_NON_IDEMPOTENT_ACTION"})

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
