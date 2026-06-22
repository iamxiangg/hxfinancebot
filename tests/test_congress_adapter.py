# NEW — Funnel Pilot Step 4: Congress adapter unit tests

import unittest

from funnel.congress_adapter import result_to_signal


class TestCongressAdapter(unittest.TestCase):

    def setUp(self) -> None:
        self.observed_at = "2026-06-22"

    def test_actionable_result_converted(self) -> None:
        result = {
            "ticker": "bwxt",
            "category": "actionable",
            "conviction": 75,
            "entry": 68,
            "mid": 180000,
            "low": 100000,
            "high": 250000,
            "buyers": 2,
            "cluster_buyers": 2,
            "flow": "Accumulation",
            "names": ["Smith", "Jones"],
        }

        signal = result_to_signal(
            result=result,
            observed_at=self.observed_at,
        )

        self.assertIsNotNone(signal)

        assert signal is not None

        self.assertEqual(
            signal.ticker,
            "BWXT",
        )

        self.assertEqual(
            signal.classification,
            "actionable",
        )

        self.assertEqual(
            signal.score,
            75.0,
        )

        self.assertEqual(
            signal.details["entry_quality"],
            68.0,
        )

    def test_other_above_threshold_becomes_near_miss(
        self,
    ) -> None:
        result = {
            "ticker": "BIIB",
            "category": "other",
            "conviction": 28,
            "entry": 82,
        }

        signal = result_to_signal(
            result=result,
            observed_at=self.observed_at,
            min_conviction=15,
        )

        self.assertIsNotNone(signal)

        assert signal is not None

        self.assertEqual(
            signal.classification,
            "near_miss",
        )

    def test_other_below_threshold_excluded(
        self,
    ) -> None:
        result = {
            "ticker": "XYZ",
            "category": "other",
            "conviction": 8,
            "entry": 50,
        }

        signal = result_to_signal(
            result=result,
            observed_at=self.observed_at,
            min_conviction=15,
        )

        self.assertIsNone(signal)

    def test_blank_ticker_excluded(self) -> None:
        result = {
            "ticker": "",
            "category": "actionable",
            "conviction": 80,
            "entry": 80,
        }

        signal = result_to_signal(
            result=result,
            observed_at=self.observed_at,
        )

        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()