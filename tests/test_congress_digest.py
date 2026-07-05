from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from funnel.political_archive import build_archive_stats
from scanners.congress.flag_ranker import build_digest_plan, classify_release_type, detect_backfill_status
from scanners.congress.ticker_history import build_ticker_histories
from tactical.congress_digest import chunk_digest, digest_log_rows, render_digest
from scanners.congress.engine import run_scan_from_payload


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "congress_digest_fixture.json"


def _fixture_payload():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _scan(observed_at="2026-06-24T12:00:00+08:00"):
    return run_scan_from_payload(_fixture_payload(), observed_at=observed_at, price_fetcher=lambda symbols, earliest: {})


def _trigger_events(scan, *, bootstrap_run=False):
    events: dict[str, list[dict[str, object]]] = {}
    for record in scan.transactions:
        if not record.ticker:
            continue
        if not record.is_new_discovery:
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


class CongressDigestTests(unittest.TestCase):
    def test_new_event_history_includes_earlier_purchases_and_sales(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        intc = histories["INTC"]
        self.assertGreater(intc.windows[365].stock_purchase_low, 0.0)
        self.assertGreater(intc.windows[365].sale_low, 0.0)

    def test_several_new_records_for_one_ticker_produce_one_dossier(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        backfill = detect_backfill_status(
            bootstrap_run=False,
            new_records=[event for events in _trigger_events(scan).values() for event in events],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["INTC", "MSFT"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["INTC", "MSFT"],
            backfill_status=backfill,
            previous_digest_rows=[],
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 2, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 2})(),
                summary_written=2,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        self.assertEqual(sum(flag["ticker"] == "MSFT" for flag in [item.to_dict() for item in plan.detailed_flags + plan.compact_flags]), 1)

    def test_stock_call_and_put_amounts_remain_separate(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        intc = histories["INTC"].windows[90]
        self.assertGreater(intc.call_purchase_low, 0.0)
        self.assertGreater(intc.put_purchase_low, 0.0)
        self.assertGreater(intc.stock_purchase_low, 0.0)

    def test_lower_bound_thresholds_determine_flag_reasons(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        self.assertTrue(any("call purchases reached at least" in reason for reason in histories["INTC"].flag_reasons))
        self.assertFalse(any("sales reached at least $500,000" in reason for reason in histories["INTC"].flag_reasons))

    def test_windows_use_transaction_date(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        intc = histories["INTC"]
        self.assertEqual(intc.windows[45].purchase_count, 2)
        self.assertEqual(intc.windows[90].purchase_count, 3)

    def test_filing_date_drives_release_freshness(self) -> None:
        event = {
            "trade_key": "late-1",
            "ticker": "LATE",
            "transaction_age": 40,
            "days_to_file": 50,
            "event_type": "NEW",
        }
        self.assertEqual(classify_release_type(event, bootstrap_run=False), "LATE_DISCLOSURE")

    def test_historical_backfill_is_not_live_disclosure(self) -> None:
        event = {
            "trade_key": "hist-1",
            "ticker": "HIST",
            "transaction_age": 160,
            "days_to_file": 10,
            "event_type": "NEW",
        }
        self.assertEqual(classify_release_type(event, bootstrap_run=False), "HISTORICAL_BACKFILL")

    def test_one_dominant_material_buyer_becomes_single_filer_bullish_bet(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        self.assertEqual(histories["INTC"].primary_classification, "SINGLE_FILER_BULLISH_BET")
        self.assertEqual(histories["INTC"].structure_classification, "OPTIONS_LED")

    def test_two_independent_buyers_become_broad_accumulation(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        self.assertEqual(histories["MSFT"].primary_classification, "BROAD_ACCUMULATION")

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
        events = _trigger_events(scan)
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-07-05T12:00:00+08:00"),
            trigger_events=events,
        )
        backfill = detect_backfill_status(
            bootstrap_run=False,
            new_records=[event for ticker_events in events.values() for event in ticker_events],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["VLTO"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["VLTO"],
            backfill_status=backfill,
            previous_digest_rows=[],
            digest_date="2026-07-05",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 2, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 2})(),
                summary_written=1,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        self.assertEqual(histories["VLTO"].primary_classification, "SINGLE_FILER_BULLISH_BET")
        self.assertEqual(len(plan.detailed_flags), 0)
        self.assertEqual(len(plan.compact_flags), 0)
        self.assertFalse(plan.send_digest)

    def test_sales_are_not_automatically_bearish(self) -> None:
        scan = _scan()
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=_trigger_events(scan),
        )
        self.assertEqual(histories["INTC"].primary_classification, "SINGLE_FILER_BULLISH_BET")
        self.assertGreater(histories["INTC"].distribution_evidence_score, 0.0)

    def test_one_run_generates_one_logical_digest_and_caps_top_flags(self) -> None:
        scan = _scan()
        events = _trigger_events(scan)
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=events,
        )
        backfill = detect_backfill_status(
            bootstrap_run=False,
            new_records=[event for ticker_events in events.values() for event in ticker_events],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["INTC", "MSFT"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["INTC", "MSFT"],
            backfill_status=backfill,
            previous_digest_rows=[],
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 3, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 3})(),
                summary_written=2,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        digest = render_digest(plan, now_sg=datetime.fromisoformat("2026-06-24T12:00:00+08:00"))
        assert digest is not None
        self.assertIn("DAILY POLITICAL-TRADING DIGEST", digest)
        self.assertLessEqual(len(plan.detailed_flags), 3)

    def test_unchanged_summary_hash_is_suppressed(self) -> None:
        scan = _scan()
        events = _trigger_events(scan)
        initial_histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=events,
        )
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            previous_summary_rows={
                "INTC": {"Primary Classification": initial_histories["INTC"].primary_classification},
                "MSFT": {"Primary Classification": initial_histories["MSFT"].primary_classification},
            },
            trigger_events=events,
        )
        previous_rows = [
            {
                "Ticker": "INTC",
                "Summary Hash": histories["INTC"].summary_hash,
            },
            {
                "Ticker": "MSFT",
                "Summary Hash": histories["MSFT"].summary_hash,
            },
        ]
        backfill = detect_backfill_status(
            bootstrap_run=False,
            new_records=[event for ticker_events in events.values() for event in ticker_events],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["INTC", "MSFT"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["INTC", "MSFT"],
            backfill_status=backfill,
            previous_digest_rows=previous_rows,
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 0, "amended_rows": 0, "idempotent_rows": 2, "deactivated_rows": 0, "seen_updates": 2})(),
                summary_written=2,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        self.assertFalse(plan.send_digest)

    def test_backfill_mode_limits_detailed_dossiers(self) -> None:
        scan = _scan()
        events = _trigger_events(scan, bootstrap_run=True)
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=events,
        )
        backfill = detect_backfill_status(
            bootstrap_run=True,
            new_records=[event for ticker_events in events.values() for event in ticker_events] * 101,
            material_amendments=[],
            removed_events=[],
            affected_tickers=["INTC", "MSFT"] * 30,
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["INTC", "MSFT"],
            backfill_status=backfill,
            previous_digest_rows=[],
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 202, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 202})(),
                summary_written=2,
                digest_logged=0,
                bootstrap_completed=True,
            ),
        )
        self.assertLessEqual(len(plan.detailed_flags), 3)

    def test_telegram_chunking_preserves_readable_boundaries(self) -> None:
        text = "\n\n".join(f"SECTION {index}\n" + ("x" * 900) for index in range(6))
        chunks = chunk_digest(text, limit=1500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1515 for chunk in chunks))

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
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 0, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 0})(),
                summary_written=0,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        self.assertIsNone(render_digest(plan, now_sg=datetime.fromisoformat("2026-06-24T12:00:00+08:00")))

    def test_digest_log_rows_capture_summary_hashes(self) -> None:
        scan = _scan()
        events = _trigger_events(scan)
        histories = build_ticker_histories(
            scan.transactions,
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events=events,
        )
        backfill = detect_backfill_status(
            bootstrap_run=False,
            new_records=[event for ticker_events in events.values() for event in ticker_events],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["INTC", "MSFT"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["INTC", "MSFT"],
            backfill_status=backfill,
            previous_digest_rows=[],
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                type("RawUpdate", (), {"new_rows": 3, "amended_rows": 0, "idempotent_rows": 0, "deactivated_rows": 0, "seen_updates": 3})(),
                summary_written=2,
                digest_logged=0,
                bootstrap_completed=False,
            ),
        )
        rows = digest_log_rows(plan, run_id="run-1", payload_hash="hash-1", telegram_included=False)
        self.assertTrue(all(row["Summary Hash"] for row in rows))


if __name__ == "__main__":
    unittest.main()
