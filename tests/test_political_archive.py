from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from funnel.political_archive import (
    build_archive_stats,
    load_political_archive_state,
    persist_raw_archive_updates,
    prepare_raw_archive_upserts,
    seed_review_override_rows,
    update_raw_notification_status,
)
from scanners.congress.engine import run_scan_from_payload
from scanners.congress.flag_ranker import build_digest_plan, classify_release_type, detect_backfill_status
from scanners.congress.ticker_history import build_ticker_histories


def _scan(payload, *, prior_ledger=None, observed_at="2026-06-24T12:00:00+08:00"):
    return run_scan_from_payload(payload, observed_at=observed_at, prior_ledger=prior_ledger, price_fetcher=lambda symbols, earliest: {})


class PoliticalArchiveTests(unittest.TestCase):
    def test_new_trade_inserts_one_raw_row(self) -> None:
        scan = _scan(
            [
                {
                    "id": "new-1",
                    "ticker": "MSFT",
                    "asset_name": "Microsoft Common Stock",
                    "asset_type": "Common Stock",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 100000,
                    "amount_range_high": 150000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        update = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload")
        self.assertEqual(update.new_rows, 1)
        self.assertEqual(len(update.rows_to_upsert), 1)
        self.assertEqual(update.rows_to_upsert[0]["Trade Key"], "id:new-1")

    def test_same_trade_and_fingerprint_is_idempotent(self) -> None:
        scan = _scan(
            [
                {
                    "id": "same-1",
                    "ticker": "MSFT",
                    "asset_name": "Microsoft Common Stock",
                    "asset_type": "Common Stock",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 100000,
                    "amount_range_high": 150000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        first = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload-a")
        existing = {first.rows_to_upsert[0]["Trade Key"]: first.rows_to_upsert[0]}
        second = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows=existing, observed_at="2026-06-24T12:05:00+08:00", payload_hash="payload-b")
        row = second.rows_to_upsert[0]
        self.assertEqual(second.idempotent_rows, 1)
        self.assertEqual(row["Record Version"], 1)
        self.assertEqual(row["Is Materially Amended"], "NO")

    def test_changed_fingerprint_increments_version_and_marks_amendment(self) -> None:
        original_scan = _scan(
            [
                {
                    "id": "amend-1",
                    "ticker": "MSFT",
                    "asset_name": "Microsoft Common Stock",
                    "asset_type": "Common Stock",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 100000,
                    "amount_range_high": 150000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        amended_scan = _scan(
            [
                {
                    "id": "amend-1",
                    "ticker": "MSFT",
                    "asset_name": "Microsoft Common Stock",
                    "asset_type": "Common Stock",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 250000,
                    "amount_range_high": 300000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ],
            prior_ledger=original_scan.ledger,
        )
        first = prepare_raw_archive_upserts(original_scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload-a")
        existing = {first.rows_to_upsert[0]["Trade Key"]: first.rows_to_upsert[0]}
        second = prepare_raw_archive_upserts(amended_scan.transactions[:1], existing_rows=existing, observed_at="2026-06-24T12:05:00+08:00", payload_hash="payload-b")
        row = second.rows_to_upsert[0]
        self.assertEqual(second.amended_rows, 1)
        self.assertEqual(row["Record Version"], 2)
        self.assertEqual(row["Is Materially Amended"], "YES")

    def test_first_seen_timestamp_remains_stable(self) -> None:
        scan = _scan(
            [
                {
                    "id": "stable-1",
                    "ticker": "MSFT",
                    "asset_name": "Microsoft Common Stock",
                    "asset_type": "Common Stock",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 100000,
                    "amount_range_high": 150000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        first = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload-a")
        existing = {first.rows_to_upsert[0]["Trade Key"]: first.rows_to_upsert[0]}
        second = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows=existing, observed_at="2026-06-24T12:05:00+08:00", payload_hash="payload-b")
        self.assertEqual(first.rows_to_upsert[0]["First Seen At"], second.rows_to_upsert[0]["First Seen At"])

    def test_unknown_ticker_is_archived(self) -> None:
        scan = _scan(
            [
                {
                    "id": "unknown-1",
                    "ticker": "",
                    "asset_name": "Private Fund",
                    "asset_type": "Private Fund",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 10000,
                    "amount_range_high": 15000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        update = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload")
        self.assertEqual(update.rows_to_upsert[0]["Ticker"], "")

    def test_excluded_asset_is_archived(self) -> None:
        scan = _scan(
            [
                {
                    "id": "excluded-1",
                    "ticker": "ETF1",
                    "asset_name": "Regional Bank ETF",
                    "asset_type": "ETF",
                    "transaction_type": "Purchase",
                    "transaction_date": "2026-06-20",
                    "filing_date": "2026-06-22",
                    "amount_range_low": 10000,
                    "amount_range_high": 15000,
                    "filer_name": "Alex Doe",
                    "filer_id": "A1",
                    "owner": "Self",
                    "branch": "Legislative",
                    "chamber": "House",
                }
            ]
        )
        update = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload")
        self.assertEqual(update.rows_to_upsert[0]["Asset Intent Class"], "SECTOR_ETF")

    def test_local_fallback_persists_json_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CONGRESS_STATE_DIR": temp_dir,
                "POLITICAL_ARCHIVE_BACKEND": "local",
            },
            clear=False,
        ):
            state = load_political_archive_state()
            scan = _scan(
                [
                    {
                        "id": "persist-1",
                        "ticker": "MSFT",
                        "asset_name": "Microsoft Common Stock",
                        "asset_type": "Common Stock",
                        "transaction_type": "Purchase",
                        "transaction_date": "2026-06-20",
                        "filing_date": "2026-06-22",
                        "amount_range_low": 100000,
                        "amount_range_high": 150000,
                        "filer_name": "Alex Doe",
                        "filer_id": "A1",
                        "owner": "Self",
                        "branch": "Legislative",
                        "chamber": "House",
                    }
                ]
            )
            update = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows=state.raw_rows, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload")
            persist_raw_archive_updates(state, update)
            raw_file = Path(temp_dir) / "political_trades_raw.json"
            self.assertTrue(raw_file.exists())
            payload = json.loads(raw_file.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["Trade Key"], "id:persist-1")

    def test_pending_status_update_does_not_clear_prior_notification_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CONGRESS_STATE_DIR": temp_dir,
                "POLITICAL_ARCHIVE_BACKEND": "local",
            },
            clear=False,
        ):
            state = load_political_archive_state()
            scan = _scan(
                [
                    {
                        "id": "notify-1",
                        "ticker": "MSFT",
                        "asset_name": "Microsoft Common Stock",
                        "asset_type": "Common Stock",
                        "transaction_type": "Purchase",
                        "transaction_date": "2026-06-20",
                        "filing_date": "2026-06-22",
                        "amount_range_low": 100000,
                        "amount_range_high": 150000,
                        "filer_name": "Alex Doe",
                        "filer_id": "A1",
                        "owner": "Self",
                        "branch": "Legislative",
                        "chamber": "House",
                    }
                ]
            )
            update = prepare_raw_archive_upserts(scan.transactions[:1], existing_rows=state.raw_rows, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload")
            persist_raw_archive_updates(state, update)
            update_raw_notification_status(
                state,
                trade_keys=["id:notify-1"],
                notification_status="NOTIFIED",
                notified_at="2026-06-24T12:30:00+08:00",
                digest_delivery_status="DELIVERED",
            )
            update_raw_notification_status(
                state,
                trade_keys=["id:notify-1"],
                notification_status="DIGEST_PENDING",
                notified_at="",
                digest_delivery_status="PENDING",
            )
            row = load_political_archive_state().raw_rows["id:notify-1"]
        self.assertEqual(row["First Successfully Notified At"], "2026-06-24T12:30:00+08:00")
        self.assertEqual(row["Last Successfully Notified At"], "2026-06-24T12:30:00+08:00")

    def test_review_override_seed_preserves_manual_decision_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CONGRESS_STATE_DIR": temp_dir,
                "POLITICAL_ARCHIVE_BACKEND": "local",
            },
            clear=False,
        ):
            state = load_political_archive_state()
            seed_review_override_rows(
                state,
                [
                    {
                        "trade_key": "id:review-1",
                        "asset_name": "Boston Scientific Corp Common Stock",
                        "filer_name": "Evan Smith",
                        "transaction_type": "Purchase",
                        "action": "purchase",
                        "document_url": "https://example.test/doc",
                        "classification": "REQUIRES_REVIEW",
                        "reason": "UNRESOLVED_PUBLIC_SECURITY",
                        "proposed_resolution": "manual_ticker_resolution",
                    }
                ],
                seeded_at="2026-06-24T12:00:00+08:00",
            )
            state.review_override_rows["id:review-1"]["Resolved Ticker"] = "BSX"
            state.review_override_rows["id:review-1"]["Reviewer Note"] = "Exact match"
            seed_review_override_rows(
                state,
                [
                    {
                        "trade_key": "id:review-1",
                        "asset_name": "Boston Scientific Corp Common Stock",
                        "filer_name": "Evan Smith",
                        "transaction_type": "Purchase",
                        "action": "purchase",
                        "document_url": "https://example.test/doc2",
                        "classification": "REQUIRES_REVIEW",
                        "reason": "UNRESOLVED_PUBLIC_SECURITY",
                        "proposed_resolution": "manual_ticker_resolution",
                    }
                ],
                seeded_at="2026-06-24T13:00:00+08:00",
            )
            row = state.review_override_rows["id:review-1"]

        self.assertEqual(row["Resolved Ticker"], "BSX")
        self.assertEqual(row["Reviewer Note"], "Exact match")
        self.assertEqual(row["Document URL"], "https://example.test/doc2")
        self.assertEqual(row["Active"], "YES")

    def test_bootstrap_suppresses_historical_alerts(self) -> None:
        payload = [
            {
                "id": "old-1",
                "ticker": "OLD",
                "asset_name": "Old Corp Common Stock",
                "asset_type": "Common Stock",
                "transaction_type": "Purchase",
                "transaction_date": "2026-01-10",
                "filing_date": "2026-05-01",
                "amount_range_low": 150000,
                "amount_range_high": 150000,
                "filer_name": "Old Buyer",
                "filer_id": "O1",
                "owner": "Self",
                "branch": "Legislative",
                "chamber": "House",
            }
        ]
        scan = _scan(payload, observed_at="2026-06-24T12:00:00+08:00")
        event = {
            "trade_key": scan.transactions[0].trade_key,
            "ticker": "OLD",
            "transaction_age": scan.transactions[0].transaction_age,
            "days_to_file": scan.transactions[0].days_to_file,
            "event_type": "NEW",
        }
        event["release_type"] = classify_release_type(event, bootstrap_run=True)
        histories = build_ticker_histories(
            scan.transactions[:1],
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
            trigger_events={"OLD": [event]},
        )
        backfill = detect_backfill_status(
            bootstrap_run=True,
            new_records=[event],
            material_amendments=[],
            removed_events=[],
            affected_tickers=["OLD"],
        )
        plan = build_digest_plan(
            histories=histories,
            affected_tickers=["OLD"],
            backfill_status=backfill,
            previous_digest_rows=[],
            previous_summary_rows={},
            digest_date="2026-06-24",
            archive_stats=build_archive_stats(
                prepare_raw_archive_upserts(scan.transactions[:1], existing_rows={}, observed_at="2026-06-24T12:00:00+08:00", payload_hash="payload"),
                summary_written=0,
                digest_logged=0,
                bootstrap_completed=True,
            ),
            observed_at=datetime.fromisoformat("2026-06-24T12:00:00+08:00"),
        )
        self.assertFalse(plan.send_digest)


if __name__ == "__main__":
    unittest.main()
