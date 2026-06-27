from __future__ import annotations

import unittest

from funnel.signal_schema import Signal


class SignalSchemaTests(unittest.TestCase):
    def test_signal_normalises_and_generates_id(self):
        signal = Signal(
            ticker=" msft ",
            scanner="Congress",
            classification="Actionable",
            score=72,
            observed_at="2026-06-22T20:00:00+08:00",
            details={"conviction": 72},
        )
        self.assertEqual(signal.ticker, "MSFT")
        self.assertEqual(signal.scanner, "congress")
        self.assertEqual(signal.classification, "actionable")
        self.assertTrue(signal.signal_id.startswith("congress-MSFT-"))

    def test_timezone_is_required(self):
        with self.assertRaises(ValueError):
            Signal(
                ticker="MSFT",
                scanner="congress",
                classification="actionable",
                score=72,
                observed_at="2026-06-22T20:00:00",
            )

    def test_insider_scanner_is_supported(self):
        signal = Signal(
            ticker="TEAM",
            scanner="insider",
            classification="actionable",
            score=81,
            observed_at="2026-06-22T20:00:00+08:00",
        )
        self.assertEqual(signal.scanner, "insider")


if __name__ == "__main__":
    unittest.main()
