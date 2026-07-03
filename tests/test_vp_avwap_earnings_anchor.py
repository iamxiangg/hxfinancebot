from __future__ import annotations

from datetime import datetime
import unittest

import pandas as pd

from scanners.vp_avwap.earnings_anchor import classify_release_timing, map_reaction_session, select_latest_confirmed_earnings_anchor
from scanners.vpma.engine import map_reaction_session as vpma_map_reaction_session


class EarningsAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = pd.bdate_range("2026-05-01", periods=30)

    def test_release_timing_and_session_mapping_cases(self) -> None:
        before = datetime(2026, 6, 3, 8, 0)
        after = datetime(2026, 6, 3, 16, 30)
        during = datetime(2026, 6, 3, 13, 0)
        unknown = datetime(2026, 6, 3, 0, 0)

        self.assertEqual(classify_release_timing(before), ("before_market", "high"))
        self.assertEqual(map_reaction_session(before, self.sessions)[0], pd.Timestamp("2026-06-03"))
        self.assertEqual(map_reaction_session(after, self.sessions)[0], pd.Timestamp("2026-06-04"))
        self.assertEqual(
            map_reaction_session(during, self.sessions, during_market_policy="next_session")[0],
            pd.Timestamp("2026-06-04"),
        )
        self.assertEqual(map_reaction_session(unknown, self.sessions)[1:], ("uncertain", "low"))

    def test_weekend_unknown_maps_to_next_trading_day(self) -> None:
        weekend = datetime(2026, 6, 6, 0, 0)
        reaction, release_timing, confidence = map_reaction_session(weekend, self.sessions)

        self.assertEqual(reaction, pd.Timestamp("2026-06-08"))
        self.assertEqual(release_timing, "uncertain")
        self.assertEqual(confidence, "low")

    def test_selection_returns_current_and_previous_anchor(self) -> None:
        frame = pd.DataFrame(
            {"surprise": [1.0, 2.0]},
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp("2026-05-01 16:05"),
                    pd.Timestamp("2026-06-10 08:00"),
                ]
            )
        )

        result = select_latest_confirmed_earnings_anchor(
            frame,
            self.sessions,
            latest_completed_session=pd.Timestamp("2026-06-12"),
        )

        self.assertIsNotNone(result.current)
        self.assertIsNotNone(result.previous)
        assert result.current is not None
        assert result.previous is not None
        self.assertEqual(result.current.reaction_session, pd.Timestamp("2026-06-10"))
        self.assertEqual(result.previous.reaction_session, pd.Timestamp("2026-05-04"))

    def test_missing_earnings_returns_reason(self) -> None:
        result = select_latest_confirmed_earnings_anchor(
            pd.DataFrame(),
            self.sessions,
            latest_completed_session=pd.Timestamp("2026-06-12"),
        )

        self.assertIsNone(result.current)
        self.assertIn("Missing", result.reason or "")

    def test_same_session_policy_matches_current_vpma_behavior(self) -> None:
        event = datetime(2026, 6, 3, 13, 0)
        current = map_reaction_session(event, self.sessions, during_market_policy="same_session")
        legacy = vpma_map_reaction_session(event, self.sessions)

        self.assertEqual(current, legacy)


if __name__ == "__main__":
    unittest.main()
