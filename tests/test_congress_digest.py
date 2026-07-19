from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from funnel.political_archive import build_archive_stats, summary_row_from_history
from scanners.congress.flag_ranker import build_digest_plan, classify_release_type, detect_backfill_status
from scanners.congress.models import PoliticalWatchlistState, PoliticalWindowSummary, TickerPoliticalHistory
from scanners.congress.ticker_history import build_ticker_histories
from tactical.congress_digest import chunk_digest, digest_log_rows, render_digest
from scanners.congress.engine import run_scan_from_payload


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "congress_digest_fixture.json"
OBSERVED_AT = datetime.fromisoformat("2026-06-24T12:00:00+08:00")


def _fixture_payload():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _scan(observed_at="2026-06-24T12:00:00+08:00"):
    return run_scan_from_payload(_fixture_payload(), observed_at=observed_at, price_fetcher=lambda symbols, earliest: {})


def _archive_stats(new_rows: int = 0):
    return build_archive_stats(
        type("RawUpdate", (), {"new_rows": new_rows, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": new_rows})(),
        summary_written=0,
        digest_logged=0,
        bootstrap_completed=False,
    )


def _trigger_events(scan, *, bootstrap_run=False):
    events: dict[str, list[dict[str, object]]] = {}
    for record in scan.transactions:
        if not record.ticker or not record.is_new_discovery:
            continue
        event = {
            "trade_key": record.trade_key,
            "ticker": record.ticker,
            "filer_name": record.filer_name,
            "owner_relationship": record.owner_relationship,
            "transaction_type": record.transaction_type,
            "transaction_date": record.transaction_date,
            "filing_date": record.filing_date,
            "transaction_age": record.transaction_age,
            "days_to_file": record.days_to_file,
            "amount_low": record.amount_range_low,
            "amount_high": record.amount_range_high,
            "event_type": "NEW",
        }
        event["release_type"] = classify_release_type(event, bootstrap_run=bootstrap_run)
        events.setdefault(record.ticker, []).append(event)
    return events


def _build_plan(histories, events, *, previous_summary_rows=None, previous_digest_rows=None, affected_tickers=None, observed_at=OBSERVED_AT, bootstrap_run=False):
    affected = affected_tickers if affected_tickers is not None else sorted(events)
    backfill = detect_backfill_status(
        bootstrap_run=bootstrap_run,
        new_records=[event for ticker_events in events.values() for event in ticker_events],
        material_amendments=[],
        removed_events=[],
        affected_tickers=affected,
    )
    return build_digest_plan(
        histories=histories,
        affected_tickers=affected,
        backfill_status=backfill,
        previous_digest_rows=previous_digest_rows or [],
        previous_summary_rows=previous_summary_rows or {},
        digest_date=observed_at.date().isoformat(),
        archive_stats=_archive_stats(new_rows=len([event for ticker_events in events.values() for event in ticker_events])),
        observed_at=observed_at,
    )


def _window() -> PoliticalWindowSummary:
    return PoliticalWindowSummary(
        window_days=90,
        purchase_count=1,
        unique_buyer_count=1,
        stock_purchase_low=200000.0,
        stock_purchase_high=300000.0,
        largest_buyer_share_lower_bound=0.8,
        largest_buyer_share_midpoint_estimate=0.8,
    )


def _history(
    ticker: str,
    *,
    primary_classification: str = "SINGLE_FILER_BULLISH_BET",
    entry_category: str = "WAIT",
    entry_quality: float = 55.0,
    political_conviction: float = 78.0,
    new_events=None,
    release_types=(),
    summary_hash: str | None = None,
) -> TickerPoliticalHistory:
    windows = {
        45: _window(),
        90: _window(),
        365: PoliticalWindowSummary(
            window_days=365,
            purchase_count=1,
            unique_buyer_count=1,
            stock_purchase_low=200000.0,
            stock_purchase_high=300000.0,
            largest_bullish_trade_low=200000.0,
            largest_bullish_trade_high=300000.0,
            largest_buyer_share_lower_bound=0.8,
            largest_buyer_share_midpoint_estimate=0.8,
        ),
    }
    return TickerPoliticalHistory(
        ticker=ticker,
        primary_classification=primary_classification,
        structure_classification="OPTIONS_LED",
        bullish_evidence_score=70.0,
        distribution_evidence_score=10.0 if primary_classification != "DISTRIBUTION" else 65.0,
        breadth_score=40.0,
        concentration_score=80.0,
        inference_confidence="HIGH",
        data_confidence="HIGH",
        windows=windows,
        new_events=list(new_events or []),
        flag_reasons=[f"{ticker} flag"],
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


class CongressDigestTests(unittest.TestCase):
    def test_new_event_history_includes_earlier_purchases_and_sales(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(scan.transactions, observed_at=OBSERVED_AT, trigger_events=_trigger_events(scan))
        intc = histories["INTC"]
        self.assertGreater(intc.windows[365].stock_purchase_low, 0.0)
        self.assertGreater(intc.windows[365].sale_low, 0.0)

    def test_several_new_records_for_one_ticker_produce_one_digest_appearance(self) -> None:
        scan = _scan()
        events = _trigger_events(scan)
        histories = build_ticker_histories(scan.transactions, observed_at=OBSERVED_AT, trigger_events=events)
        plan = _build_plan(histories, events, affected_tickers=["INTC", "MSFT"])
        appearances = [
            flag.ticker
            for flag in (*plan.new_material_flags, *plan.material_updates, *plan.active_watchlist_items, *plan.other_new_activity)
        ]
        self.assertEqual(appearances.count("MSFT"), 1)

    def test_small_repeat_single_filer_buys_do_not_qualify_for_digest(self) -> None:
        payload = [
            {
                "id": "small-1",
                "ticker": "VLTO",
                "asset_name": "Veralto Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-24",
                "filing_date": "2026-07-02",
                "amount_range_low": 1000,
                "amount_range_high": 15000,
                "filer_name": "Gilbert Cisneros",
                "filer_id": "house_gilbert_cisneros",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
            {
                "id": "small-2",
                "ticker": "VLTO",
                "asset_name": "Veralto Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-06-10",
                "filing_date": "2026-06-12",
                "amount_range_low": 1000,
                "amount_range_high": 15000,
                "filer_name": "Gilbert Cisneros",
                "filer_id": "house_gilbert_cisneros",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            },
        ]
        scan = run_scan_from_payload(payload, observed_at="2026-07-05T12:00:00+08:00", price_fetcher=lambda symbols, earliest: {})
        observed_at = datetime.fromisoformat("2026-07-05T12:00:00+08:00")
        events = _trigger_events(scan)
        histories = build_ticker_histories(scan.transactions, observed_at=observed_at, trigger_events=events)
        plan = _build_plan(histories, events, affected_tickers=["VLTO"], observed_at=observed_at)
        self.assertEqual(histories["VLTO"].primary_classification, "SINGLE_FILER_BULLISH_BET")
        self.assertFalse(plan.new_material_flags)
        self.assertFalse(plan.other_new_activity)
        self.assertFalse(plan.send_digest)

    def test_unchanged_summary_hash_is_suppressed(self) -> None:
        scan = _scan()
        events = _trigger_events(scan)
        histories = build_ticker_histories(scan.transactions, observed_at=OBSERVED_AT, trigger_events=events)
        previous_rows = {
            ticker: {"Summary Hash": history.summary_hash, "Last Detailed Summary Hash": history.summary_hash}
            for ticker, history in histories.items()
        }
        plan = _build_plan(histories, events, previous_summary_rows=previous_rows, affected_tickers=["INTC", "MSFT"])
        self.assertFalse(plan.new_material_flags)
        self.assertFalse(plan.other_new_activity)
        self.assertFalse(plan.send_digest)

    def test_no_new_event_run_sends_nothing_by_default(self) -> None:
        plan = build_digest_plan(
            histories={},
            affected_tickers=[],
            backfill_status=detect_backfill_status(
                bootstrap_run=False,
                new_records=[],
                material_amendments=[],
                removed_events=[],
                affected_tickers=[],
            ),
            previous_digest_rows=[],
            previous_summary_rows={},
            digest_date="2026-06-24",
            archive_stats=_archive_stats(),
            observed_at=OBSERVED_AT,
        )
        self.assertIsNone(render_digest(plan, now_sg=OBSERVED_AT))

    def test_no_new_event_run_can_send_success_notification_when_enabled(self) -> None:
        with patch.dict(os.environ, {"POLITICAL_DIGEST_SEND_EMPTY": "true"}, clear=False):
            plan = build_digest_plan(
                histories={},
                affected_tickers=[],
                backfill_status=detect_backfill_status(
                    bootstrap_run=False,
                    new_records=[],
                    material_amendments=[],
                    removed_events=[],
                    affected_tickers=[],
                ),
                previous_digest_rows=[],
                previous_summary_rows={},
                digest_date="2026-06-24",
                archive_stats=_archive_stats(),
                observed_at=OBSERVED_AT,
            )
            digest = render_digest(plan, now_sg=OBSERVED_AT)
        assert digest is not None
        self.assertIn("Scan completed successfully.", digest)
        self.assertIn("No qualifying political disclosures met the digest criteria in this run.", digest)

    def test_rendered_digest_uses_new_section_headings(self) -> None:
        new_history = _history(
            "INTC",
            new_events=[{"trade_key": "t1", "transaction_type": "Purchase", "amount_low": 1_000_000, "amount_high": 5_000_000, "transaction_date": "2026-06-23", "filing_date": "2026-06-24", "release_type": "LIVE_DISCLOSURE", "owner_relationship": "spouse"}],
            release_types=("LIVE_DISCLOSURE",),
        )
        active_history = _history("MSFT")
        active_state = PoliticalWatchlistState(
            ticker="MSFT",
            watchlist_started_at="2026-06-23T12:00:00+08:00",
            watchlist_until="2026-06-30",
            watchlist_status="ACTIVE",
            watchlist_retention_type="STANDARD",
            watchlist_day=1,
            watchlist_total_days=7,
            current_entry_category="WAIT",
            previous_entry_category="WAIT",
            current_political_classification="SINGLE_FILER_BULLISH_BET",
            previous_political_classification="SINGLE_FILER_BULLISH_BET",
            reminder_due=True,
            latest_material_event="second company-specific purchase",
            primary_risk="single-household concentration",
            current_compact_summary_hash="compact-msft",
        )
        previous_rows = {
            "MSFT": summary_row_from_history(active_history, updated_at="2026-06-23T12:00:00+08:00", watchlist_state=active_state)
        }
        histories = {"INTC": new_history, "MSFT": active_history}
        events = {"INTC": list(new_history.new_events)}
        plan = _build_plan(histories, events, previous_summary_rows=previous_rows, affected_tickers=["INTC"])
        digest = render_digest(plan, now_sg=OBSERVED_AT)
        assert digest is not None
        self.assertIn("NEW DISCLOSURES", digest)
        self.assertIn("ROLLING SEVEN-DAY WATCHLIST", digest)

    def test_digest_log_rows_capture_summary_hashes_and_sections(self) -> None:
        history = _history(
            "INTC",
            new_events=[{"trade_key": "t1", "transaction_type": "Purchase", "amount_low": 1_000_000, "amount_high": 5_000_000, "transaction_date": "2026-06-23", "filing_date": "2026-06-24", "release_type": "LIVE_DISCLOSURE"}],
            release_types=("LIVE_DISCLOSURE",),
        )
        plan = _build_plan({"INTC": history}, {"INTC": list(history.new_events)}, affected_tickers=["INTC"])
        rows = digest_log_rows(plan, run_id="run-1", payload_hash="hash-1", telegram_included=False)
        self.assertTrue(all(row["Summary Hash"] for row in rows))
        self.assertEqual(rows[0]["Digest Section"], "NEW_MATERIAL_SIGNALS")

    def test_telegram_chunking_preserves_readable_boundaries(self) -> None:
        text = "\n\n".join(f"SECTION {index}\n" + ("x" * 900) for index in range(6))
        chunks = chunk_digest(text, limit=1500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1515 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
