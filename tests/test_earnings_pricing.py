from __future__ import annotations

from datetime import date, datetime
import unittest

import pandas as pd

from scanners.earnings.pricing import (
    LiquidityThresholds,
    build_iron_butterfly,
    calculate_implied_move,
    classify_event_purity,
    conservative_exit_debit,
    find_atm_straddle,
    select_post_event_expiry,
)


class EarningsPricingTests(unittest.TestCase):
    def _chain(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        calls = pd.DataFrame(
            [
                {"strike": 95, "bid": 8, "ask": 8.4, "volume": 20, "openInterest": 200},
                {"strike": 100, "bid": 4.8, "ask": 5.2, "volume": 30, "openInterest": 300},
                {"strike": 110, "bid": 1.4, "ask": 1.6, "volume": 15, "openInterest": 160},
            ]
        )
        puts = pd.DataFrame(
            [
                {"strike": 90, "bid": 1.1, "ask": 1.3, "volume": 12, "openInterest": 150},
                {"strike": 100, "bid": 4.7, "ask": 5.1, "volume": 28, "openInterest": 310},
                {"strike": 105, "bid": 6.7, "ask": 7.2, "volume": 25, "openInterest": 180},
            ]
        )
        return calls, puts

    def test_valid_atm_straddle_and_implied_move(self) -> None:
        calls, puts = self._chain()
        short_call, short_put = find_atm_straddle(calls, puts, 101)  # type: ignore[misc]

        implied_pct, implied_dollars = calculate_implied_move(100, short_call, short_put)
        self.assertAlmostEqual(implied_dollars, 9.9)
        self.assertAlmostEqual(implied_pct, 0.099)

    def test_missing_put_returns_none(self) -> None:
        calls, puts = self._chain()
        puts = puts[puts["strike"] != 100]
        self.assertIsNone(find_atm_straddle(calls, puts, 100))

    def test_crossed_quotes_are_rejected(self) -> None:
        calls, puts = self._chain()
        calls.loc[calls["strike"] == 100, "ask"] = 4.0
        self.assertIsNone(find_atm_straddle(calls, puts, 100))

    def test_event_purity_classification(self) -> None:
        self.assertEqual(classify_event_purity(2), "HIGH")
        self.assertEqual(classify_event_purity(5), "MEDIUM")
        self.assertEqual(classify_event_purity(8), "LOW")

    def test_valid_iron_butterfly_supports_asymmetric_wings(self) -> None:
        calls, puts = self._chain()
        short_call, short_put = find_atm_straddle(calls, puts, 100)  # type: ignore[misc]
        structure = build_iron_butterfly(
            calls=calls,
            puts=puts,
            short_call=short_call,
            short_put=short_put,
            implied_move_dollars=8.0,
            thresholds=LiquidityThresholds(100, 10, 0.15, 0.15),
        )

        self.assertIsNotNone(structure)
        assert structure is not None
        self.assertEqual(structure.short_strike, 100)
        self.assertEqual(structure.long_call_strike, 110)
        self.assertEqual(structure.long_put_strike, 90)
        self.assertGreater(structure.estimated_credit, 0)
        self.assertGreater(structure.estimated_max_loss, 0)

    def test_missing_wing_rejects_structure(self) -> None:
        calls, puts = self._chain()
        puts = puts[puts["strike"] != 90]
        short_call, short_put = find_atm_straddle(calls, puts, 100)  # type: ignore[misc]
        self.assertIsNone(
            build_iron_butterfly(
                calls=calls,
                puts=puts,
                short_call=short_call,
                short_put=short_put,
                implied_move_dollars=8.0,
                thresholds=LiquidityThresholds(100, 10, 0.15, 0.15),
            )
        )

    def test_conservative_exit_debit_uses_bid_ask(self) -> None:
        calls, puts = self._chain()
        debit = conservative_exit_debit(calls, puts, short_strike=100, long_put_strike=90, long_call_strike=110)
        self.assertAlmostEqual(debit, 5.2 + 5.1 - 1.4 - 1.1)

    def test_select_post_event_expiry_differs_for_amc_and_bmo(self) -> None:
        expirations = [date(2026, 8, 20), date(2026, 8, 21)]
        earnings_at = datetime(2026, 8, 20, 16, 5)
        self.assertEqual(select_post_event_expiry(expirations, earnings_at, "AMC"), date(2026, 8, 21))
        self.assertEqual(select_post_event_expiry(expirations, earnings_at.replace(hour=8), "BMO"), date(2026, 8, 20))


if __name__ == "__main__":
    unittest.main()
