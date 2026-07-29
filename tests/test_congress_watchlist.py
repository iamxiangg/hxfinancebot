from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

from funnel.political_archive import load_political_archive_state, persist_summary_rows, summary_row_from_history
from scanners.congress.flag_ranker import build_digest_plan, detect_backfill_status
from scanners.congress.models import PoliticalWatchlistState, PoliticalWindowSummary, TickerPoliticalHistory
from scanners.congress.watchlist import (
    PoliticalWatchlistConfig,
    count_trading_sessions,
    load_watchlist_state_from_row,
    update_watchlist_state,
)


OBSERVED_AT = datetime.fromisoformat("2026-06-24T12:00:00+08:00")


def _window(*, purchase_low: float = 200000.0, sale_low: float = 0.0, unique_buyers: int = 1) -> PoliticalWindowSummary:
    return PoliticalWindowSummary(
        window_days=90,
        purchase_count=1,
        full_sale_count=1 if sale_low else 0,
        unique_buyer_count=unique_buyers,
        unique_seller_count=1 if sale_low else 0,
        stock_purchase_low=purchase_low,
        stock_purchase_high=purchase_low * 1.2,
        call_purchase_low=0.0,
        call_purchase_high=0.0,
        sale_low=sale_low,
        sale_high=sale_low * 1.1 if sale_low else 0.0,
        largest_buyer_share_lower_bound=0.8 if unique_buyers == 1 else 0.4,
        largest_buyer_share_midpoint_estimate=0.8 if unique_buyers == 1 else 0.4,
    )


def _history(
    ticker: str,
    *,
    primary_classification: str = "SINGLE_FILER_BULLISH_BET",
    entry_category: str = "WAIT",
    entry_quality: float = 55.0,
    political_conviction: float = 78.0,
    purchase_low: float = 200000.0,
    sale_low: float = 0.0,
    unique_buyers: int = 1,
    release_types=(),
    new_events=None,
    summary_hash: str | None = None,
) -> TickerPoliticalHistory:
    windows = {
        45: _window(purchase_low=purchase_low, sale_low=sale_low, unique_buyers=unique_buyers),
        90: _window(purchase_low=purchase_low, sale_low=sale_low, unique_buyers=unique_buyers),
        365: PoliticalWindowSummary(
            window_days=365,
            purchase_count=1,
            full_sale_count=1 if sale_low else 0,
            unique_buyer_count=unique_buyers,
            unique_seller_count=1 if sale_low else 0,
            stock_purchase_low=purchase_low,
            stock_purchase_high=purchase_low * 1.2,
            sale_low=sale_low,
            sale_high=sale_low * 1.1 if sale_low else 0.0,
            largest_bullish_trade_low=purchase_low,
            largest_bullish_trade_high=purchase_low * 1.2,
            largest_buyer_share_lower_bound=0.8 if unique_buyers == 1 else 0.4,
            largest_buyer_share_midpoint_estimate=0.8 if unique_buyers == 1 else 0.4,
        ),
    }
    return TickerPoliticalHistory(
        ticker=ticker,
        primary_classification=primary_classification,
        structure_classification="OPTIONS_LED",
        bullish_evidence_score=75.0,
        distribution_evidence_score=65.0 if primary_classification == "DISTRIBUTION" else 10.0,
        breadth_score=65.0 if unique_buyers >= 2 else 35.0,
        concentration_score=40.0 if unique_buyers >= 2 else 80.0,
        inference_confidence="HIGH",
        data_confidence="HIGH",
        windows=windows,
        new_events=list(new_events or []),
        flag_reasons=[f"{ticker} flag"],
        risk_flags=["recent_sales_present"] if sale_low else [],
        previous_classification=primary_classification,
        classification_changed=False,
        summary_hash=summary_hash or f"{ticker.lower()}-{entry_category.lower()}",
        entry_category=entry_category,
        latest_transaction_date="2026-06-23",
        latest_filing_date="2026-06-24",
        latest_trigger_type=release_types[0] if release_types else "",
        latest_trigger_trade_keys=tuple(event.get("trade_key", "") for event in (new_events or []) if event.get("trade_key")),
        release_types=tuple(release_types),
        political_conviction=political_conviction,
        entry_quality=entry_quality,
        signal_category=entry_category.lower(),
        existing_status=entry_category.lower(),
    )


def _base_config() -> PoliticalWatchlistConfig:
    return PoliticalWatchlistConfig(
        enabled=True,
        standard_retention_trading_days=7,
        exceptional_retention_trading_days=7,
        risk_retention_trading_days=7,
        max_watchlist_items=8,
        compact_reminder_interval_days=1,
        repeat_full_on_entry_change=True,
        repeat_full_on_classification_change=True,
        repeat_full_on_new_trade=True,
        repeat_full_on_material_amendment=True,
        repeat_full_on_major_evidence_change=True,
        send_expired_notice=False,
        bullish_evidence_threshold=15.0,
        distribution_evidence_threshold=15.0,
        breadth_threshold=20.0,
        concentration_threshold=0.20,
        conviction_threshold=15.0,
    )


class CongressWatchlistTests(unittest.TestCase):
    def test_monday_to_following_monday_counts_five_sessions(self) -> None:
        self.assertEqual(count_trading_sessions(date(2026, 7, 6), date(2026, 7, 13)), 5)

    def test_weekend_does_not_consume_sessions(self) -> None:
        self.assertEqual(count_trading_sessions(date(2026, 7, 10), date(2026, 7, 12)), 0)

    def test_friday_to_monday_advances_one_session(self) -> None:
        self.assertEqual(count_trading_sessions(date(2026, 7, 10), date(2026, 7, 13)), 1)

    def test_same_date_produces_zero_elapsed_sessions(self) -> None:
        self.assertEqual(count_trading_sessions(date(2026, 7, 8), date(2026, 7, 8)), 0)

    def test_reversed_range_raises_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "earlier than start"):
            count_trading_sessions(date(2026, 7, 9), date(2026, 7, 8))

    def test_detailed_standard_flag_starts_seven_day_watchlist(self) -> None:
        history = _history(
            "INTC",
            political_conviction=60.0,
            new_events=[{"trade_key": "t1", "transaction_type": "Purchase", "amount_low": 200000.0, "amount_high": 300000.0, "transaction_date": "2026-06-23", "filing_date": "2026-06-24", "release_type": "LIVE_DISCLOSURE"}],
            release_types=("LIVE_DISCLOSURE",),
        )
        state = update_watchlist_state(None, history, observed_at=OBSERVED_AT, config=_base_config(), detailed_material_flag=True, bootstrap_run=False)
        self.assertEqual(state.watchlist_status, "ACTIVE")
        self.assertEqual(state.watchlist_retention_type, "STANDARD")
        self.assertEqual(state.watchlist_total_days, 7)

    def test_broad_accumulation_uses_same_seven_day_watchlist(self) -> None:
        history = _history(
            "MSFT",
            purchase_low=1_500_000.0,
            unique_buyers=2,
            primary_classification="BROAD_ACCUMULATION",
            new_events=[{"trade_key": "t1", "transaction_type": "Purchase", "amount_low": 1_500_000.0, "amount_high": 2_000_000.0, "transaction_date": "2026-06-23", "filing_date": "2026-06-24", "release_type": "LIVE_DISCLOSURE"}],
            release_types=("LIVE_DISCLOSURE",),
        )
        state = update_watchlist_state(None, history, observed_at=OBSERVED_AT, config=_base_config(), detailed_material_flag=True, bootstrap_run=False)
        self.assertEqual(state.watchlist_retention_type, "STANDARD")
        self.assertEqual(state.watchlist_total_days, 7)

    def test_distribution_flag_uses_risk_retention(self) -> None:
        history = _history(
            "JPM",
            primary_classification="DISTRIBUTION",
            sale_low=500000.0,
            new_events=[{"trade_key": "t1", "transaction_type": "Sale", "amount_low": 500000.0, "amount_high": 750000.0, "transaction_date": "2026-06-23", "filing_date": "2026-06-24", "release_type": "LIVE_DISCLOSURE"}],
            release_types=("LIVE_DISCLOSURE",),
        )
        state = update_watchlist_state(None, history, observed_at=OBSERVED_AT, config=_base_config(), detailed_material_flag=True, bootstrap_run=False)
        self.assertEqual(state.watchlist_retention_type, "RISK")
        self.assertEqual(state.watchlist_total_days, 7)

    def test_old_ticker_summary_row_loads_with_safe_inactive_defaults(self) -> None:
        row = {"Ticker": "INTC", "Primary Classification": "SINGLE_FILER_BULLISH_BET", "Summary Hash": "hash-1"}
        state = load_watchlist_state_from_row(row)
        assert state is not None
        self.assertEqual(state.watchlist_status, "")
        self.assertEqual(state.current_entry_category, "OTHER")

    def test_next_eligible_digest_produces_compact_watchlist_reminder(self) -> None:
        history = _history("INTC")
        previous_state = PoliticalWatchlistState(
            ticker="INTC",
            watchlist_started_at="2026-06-23T12:00:00+08:00",
            watchlist_until="2026-06-30",
            watchlist_status="ACTIVE",
            watchlist_retention_type="STANDARD",
            current_entry_category="WAIT",
            previous_entry_category="WAIT",
            current_political_classification="SINGLE_FILER_BULLISH_BET",
            previous_political_classification="SINGLE_FILER_BULLISH_BET",
            last_detailed_summary_hash="older-hash",
        )
        previous_row = summary_row_from_history(history, updated_at="2026-06-23T12:00:00+08:00", watchlist_state=previous_state)
        plan = build_digest_plan(
            histories={"INTC": history},
            affected_tickers=[],
            backfill_status=detect_backfill_status(
                bootstrap_run=False,
                new_records=[],
                material_amendments=[],
                removed_events=[],
                affected_tickers=[],
            ),
            previous_digest_rows=[],
            previous_summary_rows={"INTC": previous_row},
            digest_date="2026-06-24",
            archive_stats=type("ArchiveStats", (), {"raw_inserted": 0, "raw_amended": 0, "raw_idempotent": 0, "raw_deactivated": 0, "raw_seen_updates": 0, "summary_written": 1, "digest_logged": 0, "bootstrap_completed": False})(),
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(len(plan.active_watchlist_items), 1)
        self.assertEqual(plan.active_watchlist_items[0].ticker, "INTC")

    def test_wait_to_actionable_creates_material_update(self) -> None:
        previous_history = _history("AMD", entry_category="WAIT", summary_hash="amd-wait")
        previous_state = PoliticalWatchlistState(
            ticker="AMD",
            first_flagged_at="2026-06-20T12:00:00+08:00",
            last_flagged_at="2026-06-20T12:00:00+08:00",
            watchlist_started_at="2026-06-20T12:00:00+08:00",
            watchlist_until="2026-06-27",
            watchlist_status="ACTIVE",
            watchlist_retention_type="STANDARD",
            current_entry_category="WAIT",
            previous_entry_category="WAIT",
            current_political_classification="SINGLE_FILER_BULLISH_BET",
            previous_political_classification="SINGLE_FILER_BULLISH_BET",
            last_detailed_summary_hash="amd-original",
        )
        previous_row = summary_row_from_history(previous_history, updated_at="2026-06-20T12:00:00+08:00", watchlist_state=previous_state)
        current_history = _history("AMD", entry_category="ACTIONABLE", entry_quality=64.0, summary_hash="amd-actionable")
        plan = build_digest_plan(
            histories={"AMD": current_history},
            affected_tickers=[],
            backfill_status=detect_backfill_status(
                bootstrap_run=False,
                new_records=[],
                material_amendments=[],
                removed_events=[],
                affected_tickers=[],
            ),
            previous_digest_rows=[],
            previous_summary_rows={"AMD": previous_row},
            digest_date="2026-06-24",
            archive_stats=type("ArchiveStats", (), {"raw_inserted": 0, "raw_amended": 0, "raw_idempotent": 0, "raw_deactivated": 0, "raw_seen_updates": 0, "summary_written": 1, "digest_logged": 0, "bootstrap_completed": False})(),
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(len(plan.material_updates), 1)
        self.assertEqual(plan.material_updates[0].ticker, "AMD")

    def test_entry_score_change_within_wait_does_not_create_material_update(self) -> None:
        previous_history = _history("AMD", entry_category="WAIT", entry_quality=57.0, summary_hash="amd-wait-57")
        previous_state = PoliticalWatchlistState(
            ticker="AMD",
            first_flagged_at="2026-06-20T12:00:00+08:00",
            last_flagged_at="2026-06-20T12:00:00+08:00",
            watchlist_started_at="2026-06-20T12:00:00+08:00",
            watchlist_until="2026-06-27",
            watchlist_status="ACTIVE",
            watchlist_retention_type="STANDARD",
            current_entry_category="WAIT",
            previous_entry_category="WAIT",
            current_political_classification="SINGLE_FILER_BULLISH_BET",
            previous_political_classification="SINGLE_FILER_BULLISH_BET",
            last_detailed_summary_hash="amd-wait-57",
        )
        previous_row = summary_row_from_history(previous_history, updated_at="2026-06-20T12:00:00+08:00", watchlist_state=previous_state)
        current_history = _history("AMD", entry_category="WAIT", entry_quality=59.0, summary_hash="amd-wait-59")
        plan = build_digest_plan(
            histories={"AMD": current_history},
            affected_tickers=[],
            backfill_status=detect_backfill_status(
                bootstrap_run=False,
                new_records=[],
                material_amendments=[],
                removed_events=[],
                affected_tickers=[],
            ),
            previous_digest_rows=[],
            previous_summary_rows={"AMD": previous_row},
            digest_date="2026-06-24",
            archive_stats=type("ArchiveStats", (), {"raw_inserted": 0, "raw_amended": 0, "raw_idempotent": 0, "raw_deactivated": 0, "raw_seen_updates": 0, "summary_written": 1, "digest_logged": 0, "bootstrap_completed": False})(),
            observed_at=OBSERVED_AT,
        )
        self.assertFalse(plan.material_updates)

    def test_watchlist_cap_is_enforced_and_hidden_count_reported(self) -> None:
        histories = {}
        previous_rows = {}
        for index in range(9):
            ticker = f"T{index}"
            history = _history(ticker, summary_hash=f"{ticker.lower()}-hash")
            histories[ticker] = history
            state = PoliticalWatchlistState(
                ticker=ticker,
                first_flagged_at="2026-06-20T12:00:00+08:00",
                last_flagged_at="2026-06-20T12:00:00+08:00",
                watchlist_started_at="2026-06-20T12:00:00+08:00",
                watchlist_until="2026-06-27",
                watchlist_status="ACTIVE",
                watchlist_retention_type="STANDARD",
                current_entry_category="WAIT",
                previous_entry_category="WAIT",
                current_political_classification="SINGLE_FILER_BULLISH_BET",
                previous_political_classification="SINGLE_FILER_BULLISH_BET",
                last_detailed_summary_hash="older-hash",
            )
            previous_rows[ticker] = summary_row_from_history(history, updated_at="2026-06-20T12:00:00+08:00", watchlist_state=state)
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=[],
            backfill_status=detect_backfill_status(
                bootstrap_run=False,
                new_records=[],
                material_amendments=[],
                removed_events=[],
                affected_tickers=[],
            ),
            previous_digest_rows=[],
            previous_summary_rows=previous_rows,
            digest_date="2026-06-24",
            archive_stats=type("ArchiveStats", (), {"raw_inserted": 0, "raw_amended": 0, "raw_idempotent": 0, "raw_deactivated": 0, "raw_seen_updates": 0, "summary_written": 9, "digest_logged": 0, "bootstrap_completed": False})(),
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(len(plan.active_watchlist_items), 8)
        self.assertEqual(plan.hidden_watchlist_count, 1)

    def test_local_fallback_preserves_watchlist_state(self) -> None:
        history = _history("INTC")
        state = PoliticalWatchlistState(
            ticker="INTC",
            first_flagged_at="2026-06-20T12:00:00+08:00",
            last_flagged_at="2026-06-24T12:00:00+08:00",
            watchlist_started_at="2026-06-20T12:00:00+08:00",
            watchlist_until="2026-06-27",
            watchlist_status="ACTIVE",
            watchlist_retention_type="STANDARD",
            current_entry_category="WAIT",
            previous_entry_category="WAIT",
            current_political_classification="SINGLE_FILER_BULLISH_BET",
            previous_political_classification="SINGLE_FILER_BULLISH_BET",
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CONGRESS_STATE_DIR": temp_dir,
                "POLITICAL_ARCHIVE_BACKEND": "local",
            },
            clear=False,
        ):
            archive_state = load_political_archive_state()
            persist_summary_rows(archive_state, [summary_row_from_history(history, updated_at="2026-06-24T12:00:00+08:00", watchlist_state=state)])
            loaded_state = load_political_archive_state()
        row = loaded_state.summary_rows["INTC"]
        self.assertEqual(row["Watchlist Status"], "ACTIVE")
        self.assertEqual(row["Watchlist Started At"], "2026-06-20T12:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
