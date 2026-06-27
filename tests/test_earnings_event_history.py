from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest

import pandas as pd

from scanners.earnings.event_history import (
    NY_TZ,
    build_historical_event_moves,
    get_upcoming_earnings_event,
    realised_move_percentile,
    summarise_historical_moves,
)


class EarningsEventHistoryTests(unittest.TestCase):
    def _history(self) -> pd.DataFrame:
        index = pd.to_datetime(
            [
                "2026-06-18",
                "2026-06-19",
                "2026-06-22",
                "2026-06-23",
                "2026-06-24",
                "2026-06-25",
            ]
        )
        return pd.DataFrame(
            {
                "Open": [98, 100, 108, 110, 109, 112],
                "High": [101, 109, 112, 111, 114, 115],
                "Low": [97, 99, 107, 108, 108, 111],
                "Close": [100, 108, 110, 109, 112, 114],
                "Volume": [1, 1, 1, 1, 1, 1],
            },
            index=index,
        )

    def test_amc_upcoming_uses_same_day_entry_and_next_day_exit(self) -> None:
        frame = pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp("2026-06-24 16:05", tz=NY_TZ)]))
        event = get_upcoming_earnings_event(
            "NVDA",
            earnings_frame=frame,
            calendar=None,
            sessions=[stamp.date() for stamp in self._history().index],
            now_ny=datetime(2026, 6, 24, 10, 0, tzinfo=NY_TZ),
            lookahead_days=3,
            overrides={},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.earnings_timing, "AMC")
        self.assertEqual(event.entry_session_date.isoformat(), "2026-06-24")
        self.assertEqual(event.exit_session_date.isoformat(), "2026-06-25")

    def test_bmo_upcoming_uses_previous_day_entry(self) -> None:
        frame = pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp("2026-06-24 08:00", tz=NY_TZ)]))
        event = get_upcoming_earnings_event(
            "WMT",
            earnings_frame=frame,
            calendar=None,
            sessions=[stamp.date() for stamp in self._history().index],
            now_ny=datetime(2026, 6, 23, 15, 0, tzinfo=NY_TZ),
            lookahead_days=3,
            overrides={},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.earnings_timing, "BMO")
        self.assertEqual(event.entry_session_date.isoformat(), "2026-06-23")
        self.assertEqual(event.exit_session_date.isoformat(), "2026-06-24")

    def test_unknown_timing_requires_manual_confirmation(self) -> None:
        frame = pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp("2026-06-24")]))
        event = get_upcoming_earnings_event(
            "COST",
            earnings_frame=frame,
            calendar=None,
            sessions=[stamp.date() for stamp in self._history().index],
            now_ny=datetime(2026, 6, 23, 15, 0, tzinfo=NY_TZ),
            lookahead_days=3,
            overrides={},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.earnings_timing, "UNKNOWN")
        self.assertIsNone(event.entry_session_date)

    def test_historical_moves_align_amc_and_bmo(self) -> None:
        frame = pd.DataFrame(
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp("2026-06-19 16:05"),
                    pd.Timestamp("2026-06-23 08:00"),
                ]
            )
        )
        moves = build_historical_event_moves(
            "TEAM",
            earnings_frame=frame,
            history=self._history(),
            now_ny=datetime(2026, 6, 26, 10, 0, tzinfo=NY_TZ),
            overrides={},
            max_events=20,
        )

        self.assertEqual(len(moves), 2)
        values = sorted(round(move.absolute_event_move, 4) for move in moves)
        self.assertEqual(values, [0.0, 0.0])

    def test_weekend_alignment_uses_next_trading_session(self) -> None:
        history = self._history()
        frame = pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp("2026-06-20 16:05")]))
        moves = build_historical_event_moves(
            "TEAM",
            earnings_frame=frame,
            history=history,
            now_ny=datetime(2026, 6, 26, 10, 0, tzinfo=NY_TZ),
            overrides={"TEAM|2026-06-20": "AMC"},
            max_events=20,
        )

        self.assertEqual(len(moves), 0)

    def test_summary_and_percentile_work_with_small_samples(self) -> None:
        moves = build_historical_event_moves(
            "TEAM",
            earnings_frame=pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp("2026-06-19 16:05")])),
            history=self._history(),
            now_ny=datetime(2026, 6, 26, 10, 0, tzinfo=NY_TZ),
            overrides={},
            max_events=20,
        )
        summary = summarise_historical_moves(moves)

        self.assertEqual(summary.usable_event_count, 1)
        self.assertAlmostEqual(realised_move_percentile(0.10, moves), 100.0)


if __name__ == "__main__":
    unittest.main()
