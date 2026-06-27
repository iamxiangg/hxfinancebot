from __future__ import annotations

import unittest
from unittest.mock import patch

from funnel.vpma_adapter import result_to_signal, run_vpma_adapter
from scanners.vpma.engine import VpmaScanResult, VpmaTickerResult


class VpmaAdapterTests(unittest.TestCase):
    def test_result_to_signal_preserves_scores(self) -> None:
        result = VpmaTickerResult(
            ticker="MSFT",
            classification="actionable",
            core_score=82.0,
            event_score=30.0,
            drift_score=28.0,
            entry_score=24.0,
            confirmation_score=77.0,
            data_confidence="high",
            setup_type="pead_consolidation",
            reason="VPMA core strong",
            valid_for_days=3,
            details={"risk_flags": ["none"]},
        )

        signal = result_to_signal(result, "2026-06-25T12:00:00+00:00")

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.scanner, "vpma")
        self.assertEqual(signal.classification, "actionable")
        self.assertEqual(signal.details["core_score"], 82.0)
        self.assertEqual(signal.details["confirmation_score"], 77.0)
        self.assertIn("+00:00", signal.observed_at)
        self.assertIn("+00:00", signal.valid_until or "")

    def test_excluded_result_is_suppressed(self) -> None:
        result = VpmaTickerResult(
            ticker="MSFT",
            classification="excluded",
            core_score=0.0,
            event_score=0.0,
            drift_score=0.0,
            entry_score=0.0,
            confirmation_score=None,
            data_confidence="low",
            setup_type="pead_deteriorating",
            reason="Excluded",
            valid_for_days=3,
        )

        self.assertIsNone(result_to_signal(result, "2026-06-25T12:00:00+00:00"))

    @patch("funnel.vpma_adapter.run_vpma_scan")
    def test_run_adapter_uses_scan_observed_at_by_default(self, mock_run_vpma_scan) -> None:
        mock_run_vpma_scan.return_value = VpmaScanResult(
            results=[
                VpmaTickerResult(
                    ticker="NVDA",
                    classification="wait",
                    core_score=71.0,
                    event_score=27.0,
                    drift_score=25.0,
                    entry_score=19.0,
                    confirmation_score=None,
                    data_confidence="medium",
                    setup_type="pead_pullback",
                    reason="pullback",
                    valid_for_days=3,
                )
            ],
            observed_at="2026-06-25T14:00:00+00:00",
            analysed_tickers=1,
            counts={},
        )

        signals, analysed = run_vpma_adapter()

        self.assertEqual(analysed, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].observed_at, "2026-06-25T14:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
