from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from tactical.earnings_state import (
    cleanup_state,
    load_state,
    mark_exit_notified,
    notification_key,
    record_pre_event_notification,
    save_state,
    should_send_exit,
    should_send_pre_event,
)


class EarningsStateTests(unittest.TestCase):
    def test_pre_event_and_exit_deduplication(self) -> None:
        now = datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York"))
        state: dict[str, dict[str, object]] = {}
        key = notification_key("NVDA", "2026-08-19", "AMC")
        self.assertTrue(should_send_pre_event(state, key))
        record_pre_event_notification(
            state,
            key=key,
            classification="STRONG_ACTIONABLE",
            notified_at=now,
            earnings_at=now,
            option_expiry="2026-08-21",
            short_strike=180,
            long_put_strike=162.5,
            long_call_strike=197.5,
            entry_estimated_credit=8.2,
            entry_spot_price=180,
            pre_event_implied_move_pct=0.092,
        )
        self.assertFalse(should_send_pre_event(state, key))
        self.assertTrue(should_send_exit(state, key))
        mark_exit_notified(state, key=key, notified_at=now)
        self.assertFalse(should_send_exit(state, key))

    def test_state_round_trip_and_cleanup(self) -> None:
        now = datetime(2026, 8, 19, 14, 35, tzinfo=ZoneInfo("America/New_York"))
        stale = now - timedelta(days=60)
        state = {
            "NVDA|2026-08-19|AMC": {"earnings_at": now.isoformat(), "pre_event_notified_at": now.isoformat()},
            "OLD|2026-05-01|AMC": {"earnings_at": stale.isoformat(), "pre_event_notified_at": stale.isoformat()},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            save_state(state, path)
            loaded = load_state(path)
            cleaned = cleanup_state(loaded, now_ny=now, retention_days=45)
        self.assertIn("NVDA|2026-08-19|AMC", cleaned)
        self.assertNotIn("OLD|2026-05-01|AMC", cleaned)


if __name__ == "__main__":
    unittest.main()
