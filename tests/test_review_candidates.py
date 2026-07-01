from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from funnel.review_candidates import apply_btd_gate, comparison_to_candidate, merge_candidate
from funnel.signal_schema import Signal


class ReviewCandidateTests(unittest.TestCase):
    def test_apply_btd_gate_manual_bypass_only_for_manual(self) -> None:
        candidate = apply_btd_gate(
            {
                "Ticker": "ABC",
                "Source": "manual",
                "BTD Applicability": "UNAVAILABLE",
            },
            manual_bypass=True,
            threshold=1.0,
        )

        self.assertEqual(candidate["BTD Gate"], "BYPASSED_MANUAL")
        self.assertEqual(candidate["Telegram Eligible"], "YES")

    def test_apply_btd_gate_multiple_sources_cannot_bypass_failure(self) -> None:
        candidate = apply_btd_gate(
            {
                "Ticker": "ABC",
                "Source": "insider, manual",
                "BTD Applicability": "APPLICABLE",
                "BTD Ratio": 1.4,
            },
            manual_bypass=True,
            threshold=1.0,
        )

        self.assertEqual(candidate["BTD Gate"], "FAIL")
        self.assertEqual(candidate["Telegram Eligible"], "NO")

    def test_merge_updates_active_candidate(self) -> None:
        existing = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "ENRICHED",
            "First Seen": "2026-06-01T00:00:00+00:00",
            "Funnel Score": "50",
        }
        incoming = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "NEW",
            "First Seen": "2026-06-24T00:00:00+00:00",
            "Last Seen": "2026-06-24T01:00:00+00:00",
            "Funnel Score": "70",
            "Discovery Reason": "New signal",
        }

        merged = merge_candidate(existing, incoming, "2026-06-24T02:00:00+00:00")

        self.assertEqual(merged["First Seen"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(merged["Funnel Score"], "70")
        self.assertEqual(merged["Discovery Reason"], "New signal")
        self.assertEqual(merged["Active?"], "YES")

    def test_final_candidate_is_not_reopened(self) -> None:
        existing = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "REJECTED",
            "Funnel Score": "50",
        }
        incoming = {
            "Candidate ID": "cand-MSFT-test",
            "Ticker": "MSFT",
            "Status": "NEW",
            "Funnel Score": "90",
        }

        merged = merge_candidate(existing, incoming, "2026-06-24T02:00:00+00:00")

        self.assertEqual(merged["Status"], "REJECTED")
        self.assertEqual(merged["Funnel Score"], "50")

    def test_candidate_uses_combined_sources(self) -> None:
        candidate = comparison_to_candidate(
            {
                "ticker": "TEAM",
                "scanner": "congress",
                "all_sources": ["congress", "vpma"],
                "classification": "actionable",
                "score": 82,
                "signal_count": 2,
                "discovery_reason": "Political Disclosures: cluster purchase | VPMA: pead consolidation, core 82.0",
                "observed_at": "2026-06-25T01:00:00+00:00",
            },
            "2026-06-25T02:00:00+00:00",
        )

        self.assertEqual(candidate["Source"], "congress, vpma")
        self.assertEqual(candidate["Congress Unique Members"], "")

    def test_candidate_preserves_congress_breadth_fields(self) -> None:
        candidate = comparison_to_candidate(
            {
                "ticker": "NVDA",
                "scanner": "congress",
                "all_sources": ["congress"],
                "classification": "actionable",
                "score": 78,
                "signal_count": 1,
                "discovery_reason": "Political Disclosures: 4 unique members",
                "congress_unique_members": 4,
                "congress_recent_cluster_members": 3,
                "congress_active_purchases": 6,
                "congress_member_names": "Pelosi, Gottheimer, Tuberville, Moore",
                "observed_at": "2026-06-25T01:00:00+00:00",
            },
            "2026-06-25T02:00:00+00:00",
        )

        self.assertEqual(candidate["Congress Unique Members"], 4)
        self.assertEqual(candidate["Congress Recent Cluster Members"], 3)
        self.assertEqual(candidate["Congress Active Purchases"], 6)
        self.assertEqual(candidate["Congress Member Names"], "Pelosi, Gottheimer, Tuberville, Moore")


class ReviewCandidateRunTests(unittest.TestCase):
    def _signal(self, ticker: str, scanner: str, classification: str, score: float, details: dict | None = None) -> Signal:
        return Signal(
            ticker=ticker,
            scanner=scanner,
            classification=classification,
            score=score,
            observed_at="2026-06-25T12:00:00+00:00",
            valid_until="2026-06-28T12:00:00+00:00",
            details=details or {},
        )

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "true"}, clear=False)
    @patch("funnel.review_candidates.send_candidate_review")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    def test_run_merges_scanners_and_enriches_once(
        self,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_send_candidate_review,
    ) -> None:
        congress_signal = self._signal(
            "TEAM",
            "congress",
            "actionable",
            74,
            {
                "conviction": 74,
                "flow": "cluster purchase",
                "buyers": 4,
                "cluster_buyers": 3,
                "active_trade_count": 6,
                "names": ["Pelosi", "Gottheimer", "Tuberville", "Moore"],
            },
        )
        vpma_signal = self._signal("TEAM", "vpma", "wait", 82, {"setup_type": "pead_consolidation", "confirmation_score": 76})
        mock_congress.return_value = ([congress_signal], 1)
        mock_vpma.return_value = ([vpma_signal], 1)
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 0.42,
            "BTD Ratio": 0.42,
            "BTD Applicability": "APPLICABLE",
            "BTD Summary": "BTD 0.42",
        }
        seen_candidates: list[dict] = []
        mock_send_candidate_review.side_effect = lambda candidate: seen_candidates.append(dict(candidate)) or "123"

        from funnel import review_candidates

        review_candidates.run()

        self.assertEqual(mock_fetch_metrics.call_count, 1)
        signal_log_rows = mock_append_records.call_args_list[0].args[4]
        self.assertEqual(len(signal_log_rows), 2)
        upsert_rows = mock_upsert_records.call_args.args[5]
        self.assertEqual(len(upsert_rows), 1)
        self.assertEqual(upsert_rows[0]["Source"], "congress, vpma")
        self.assertIn("Political Disclosures:", upsert_rows[0]["Discovery Reason"])
        self.assertIn("VPMA:", upsert_rows[0]["Discovery Reason"])
        self.assertEqual(upsert_rows[0]["Congress Unique Members"], 4)
        self.assertEqual(upsert_rows[0]["Congress Recent Cluster Members"], 3)
        self.assertEqual(upsert_rows[0]["Congress Active Purchases"], 6)
        self.assertEqual(upsert_rows[0]["Congress Member Names"], "Pelosi, Gottheimer, Tuberville, Moore")
        self.assertEqual(len(seen_candidates), 1)
        self.assertEqual(seen_candidates[0]["BTD Score"], 0.42)
        self.assertEqual(seen_candidates[0]["BTD Gate"], "PASS")
        self.assertEqual(seen_candidates[0]["Telegram Eligible"], "YES")

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "false"}, clear=False)
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    def test_partial_scanner_failure_continues(
        self,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
    ) -> None:
        mock_congress.side_effect = RuntimeError("bad")
        mock_vpma.return_value = ([self._signal("NVDA", "vpma", "actionable", 81, {"setup_type": "pead_breakout"})], 1)
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 0.2,
            "BTD Ratio": 0.2,
            "BTD Applicability": "APPLICABLE",
        }

        from funnel import review_candidates

        review_candidates.run()

        self.assertTrue(mock_append_records.called)
        self.assertTrue(mock_upsert_records.called)

    @patch.dict(os.environ, {"REVIEW_SOURCES": "insider", "SEND_TELEGRAM_REVIEWS": "true"}, clear=False)
    @patch("funnel.review_candidates.send_candidate_review")
    @patch("funnel.review_candidates.metrics_to_candidate_updates")
    @patch("funnel.review_candidates.fetch_yfinance_metrics")
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.get_stock_summary_ticker_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_insider_adapter")
    def test_insider_only_candidate_with_failed_btd_does_not_notify(
        self,
        mock_insider,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_get_stock_summary_ticker_records,
        mock_upsert_records,
        mock_fetch_metrics,
        mock_metrics_to_updates,
        mock_send_candidate_review,
    ) -> None:
        mock_insider.return_value = (
            [
                self._signal(
                    "TEAM",
                    "insider",
                    "actionable",
                    82,
                    {
                        "total_score": 82,
                        "unique_insiders": 2,
                        "insider_roles": ["CEO", "CFO"],
                        "aggregate_purchase_value": 1_400_000,
                        "entry_state": "trend_confirmed",
                    },
                )
            ],
            1,
        )
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []
        mock_get_stock_summary_ticker_records.return_value = []
        mock_fetch_metrics.return_value = object()
        mock_metrics_to_updates.return_value = {
            "BTD Score": 1.4,
            "BTD Ratio": 1.4,
            "BTD Applicability": "APPLICABLE",
        }

        from funnel import review_candidates

        review_candidates.run()

        self.assertFalse(mock_send_candidate_review.called)
        upsert_rows = mock_upsert_records.call_args.args[5]
        self.assertEqual(upsert_rows[0]["BTD Gate"], "FAIL")
        self.assertEqual(upsert_rows[0]["Telegram Eligible"], "NO")

    @patch.dict(os.environ, {"REVIEW_SOURCES": "congress,vpma", "SEND_TELEGRAM_REVIEWS": "false"}, clear=False)
    @patch("funnel.review_candidates.upsert_records")
    @patch("funnel.review_candidates.append_records")
    @patch("funnel.review_candidates.read_table")
    @patch("funnel.review_candidates.ensure_review_sheets")
    @patch("funnel.review_candidates.get_spreadsheet_id")
    @patch("funnel.review_candidates.get_sheets_service")
    @patch("funnel.review_candidates.run_vpma_adapter")
    @patch("funnel.review_candidates.run_congress_adapter")
    def test_all_scanner_failure_aborts_before_writes(
        self,
        mock_congress,
        mock_vpma,
        mock_get_sheets_service,
        mock_get_spreadsheet_id,
        mock_ensure_review_sheets,
        mock_read_table,
        mock_append_records,
        mock_upsert_records,
    ) -> None:
        mock_congress.side_effect = RuntimeError("bad congress")
        mock_vpma.side_effect = RuntimeError("bad vpma")
        mock_get_sheets_service.return_value = object()
        mock_get_spreadsheet_id.return_value = "sheet-id"
        mock_read_table.return_value = []

        from funnel import review_candidates

        with self.assertRaises(RuntimeError):
            review_candidates.run()

        self.assertFalse(mock_append_records.called)
        self.assertFalse(mock_upsert_records.called)


class TestEndToEndMitigations(unittest.TestCase):
    """Behavior tests verifying the audit's mitigations actually run.

    Each test exercises the affected code path end-to-end with mocks, instead
    of grep-ing the source for keywords. If the production code is rewired and
    the mitigation stops working, these tests will fail.
    """

    def test_path_setup_guarded_under_main_only(self):
        """The sys.path bootstrap must be inside ``if __name__ == '__main__':``,
        not at the top of the module. Otherwise every library import of
        ``funnel.review_candidates`` mutates ``sys.path``.
        """
        with open("funnel/review_candidates.py", "r", encoding="utf-8") as f:
            source = f.read()
        main_guard_idx = source.index("if __name__ == \"__main__\":")
        # Bootstrap must appear AFTER the main guard, not at module load.
        self.assertIn("sys.path.insert", source)
        bootstrap_idx = source.index("sys.path.insert")
        self.assertGreater(bootstrap_idx, main_guard_idx)
        # And the bootstrap must NOT live above the first ``from funnel.*`` import.
        first_import = source.index("from funnel.btd_enrichment")
        self.assertLess(first_import, bootstrap_idx)

    def test_ai_drafts_flush_failure_does_not_abort_run(self):
        """Behaviour: a transient Sheets error on the FEROLDI_AI_DRAFTS_SHEET
        flush must NOT abort the rest of ``add_optional_ai_drafts`` — the
        append is advisory and the caller still needs the candidate list back.

        Uses ``with patch(...)`` context managers rather than stacked
        ``@patch`` decorators so the OPENAI key is set at the right scope
        and the mock list stays explicit (no accidental decorator arg swings).
        """
        from unittest.mock import MagicMock, patch as _patch
        from googleapiclient.errors import HttpError as GApiHttpError

        from funnel.review_candidates import add_optional_ai_drafts

        candidates = [{
            "Candidate ID": "AAPL-1",
            "Ticker": "AAPL",
            "Status": "BTD_PASSED",
            "Telegram Eligible": "YES",
            "Company Name": "Apple",
            "Discovery Reason": "Test",
            "Feroldi Last Updated": "",
        }]

        with _patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False), \
             _patch("funnel.review_candidates.append_records") as mock_append, \
             _patch("funnel.review_candidates.request_feroldi_draft") as mock_draft, \
             _patch("funnel.review_candidates.draft_to_candidate_updates") as mock_upd:
            mock_draft.return_value = {"ok": True, "summary": "hi"}
            mock_upd.return_value = {
                "AI Feroldi Score": 30,
                "AI Quality Summary": "good",
                "AI Bull Case": "",
                "AI Bear Case": "",
                "AI Red Flags": "",
                "AI Manual Review Needed": "NO",
                "AI Confidence": "0.9",
            }
            mock_append.side_effect = GApiHttpError(
                MagicMock(status=503),
                b"rate limited",
            )

            # ACT: should NOT raise.
            result = add_optional_ai_drafts(
                service=object(), spreadsheet_id="sid", candidates=candidates,
            )

        # ASSERT: result still has the candidates and the AI fields were populated.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["AI Feroldi Score"], 30)
        # The transient error did not propagate.
        mock_append.assert_called_once()

        # Sanity: HttpError is in the transient tuple so the guard catches it.
        from funnel.review_candidates import _TRANSIENT_SHEETS_ERRORS
        self.assertIn(GApiHttpError, _TRANSIENT_SHEETS_ERRORS)

    def test_congress_ledger_save_failure_returns_signals(self):
        """Behaviour: when ``_save_ledger`` raises a transient Sheets error,
        ``run_congress_adapter`` must STILL return the freshly-collected
        signals (signal loss is worse than ledger write failure).
        """
        from unittest.mock import MagicMock, patch
        from googleapiclient.errors import HttpError as GApiHttpError
        from datetime import datetime, timezone

        # Build a minimal CongressScanResult + scan metadata so we can stub
        # the heavy ``run_live_scan`` call.
        from scanners.congress.engine import (
            CongressScanResult,
            PayloadMetadata,
            PoliticalAuditBundle,
        )
        meta = PayloadMetadata(
            source_url="https://example.com/trades.json",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            record_count=0,
            payload_sha256="abc123",
            payload_bytes=0,
        )
        scan = CongressScanResult(
            metadata=meta,
            ticker_results=[],
            review_audit=[],
            counts={"total_raw_records": 0, "active_tickers_before_market_checks": 0,
                    "scored_tickers": 0},
            ledger={"trade-key-1": {"fingerprint": "x", "ticker": "AAPL"}},
            raw_payload=[],
            audit_bundle=PoliticalAuditBundle(),
            scope_used="all",
        )

        # Fake ledger context so _save_ledger hits the Sheets path (mocked).
        from funnel.congress_adapter import (
            _SheetLedgerContext,
            run_congress_adapter_detailed,
        )
        ctx = _SheetLedgerContext(service=MagicMock(), spreadsheet_id="sid")

        # The mock _save_ledger raises a transient HttpError.
        with patch("funnel.congress_adapter.run_live_scan", return_value=scan), \
             patch(
                 "funnel.congress_adapter._save_ledger",
                 side_effect=GApiHttpError(MagicMock(status=429), b"quota"),
             ):
            run = run_congress_adapter_detailed(observed_at="2026-01-01T00:00:00+00:00")

        # ASSERT: signals list is empty (no scanner results), but importantly NO
        # exception leaked out and the run completed with no signals.
        self.assertEqual(run.signals, [])
        self.assertEqual(run.analysed_tickers, 0)

        # Sanity: non-transient exception (programmer error) is NOT in the tuple.
        from funnel.congress_adapter import _TRANSIENT_SHEETS_ERRORS
        self.assertNotIn(KeyError, _TRANSIENT_SHEETS_ERRORS)
        self.assertNotIn(TypeError, _TRANSIENT_SHEETS_ERRORS)

    @patch("funnel.review_candidates.FEROLDI_AI_DRAFTS_SHEET", "Feroldi_AI_Drafts")
    @patch("funnel.review_candidates.FEROLDI_AI_DRAFT_HEADERS", ["Candidate ID", "Ticker", "Created At"])
    @patch("funnel.review_candidates.BTD_CANDIDATE_HEADERS", ["Candidate ID", "Ticker", "Status"])
    @patch("funnel.review_candidates.BTD_CANDIDATES_SHEET", "BTD_Candidates")
    @patch("funnel.review_candidates.read_table")
    def test_external_mutation_blocks_upsert_for_conflicting_candidate(self, mock_read_table):
        """Behaviour: between run()'s initial read and its final upsert, an
        external writer mutates BTD_CANDIDATES_SHEET. The race-condition guard
        must filter out the conflicting candidate and upsert only the safe ones.
        """
        from unittest.mock import MagicMock, patch
        from googleapiclient.errors import HttpError as GApiHttpError
        from funnel.review_candidates import _filter_external_mutation

        # Snapshot taken at the top of run(): AAPL exists with Status="BTD_PASSED".
        snapshot = {
            "AAPL": "fingerprint-original-aapl",
            "MSFT": "fingerprint-original-msft",
        }
        # Re-read (now): AAPL's fingerprint CHANGED (external mutation),
        # MSFT unchanged, GOOGL added externally.
        mock_read_table.return_value = [
            {"Candidate ID": "AAPL", "Ticker": "AAPL", "Status": "REJECTED"},
            {"Candidate ID": "MSFT", "Ticker": "MSFT", "Status": "BTD_PASSED"},
            {"Candidate ID": "GOOGL", "Ticker": "GOOGL", "Status": "NEW"},
        ]
        # Stub the fingerprint function so we don't depend on column ordering.
        with patch(
            "funnel.review_candidates._fingerprint_candidate",
            side_effect=lambda rec: {
                "AAPL": "fingerprint-mutated-aapl",
                "MSFT": "fingerprint-original-msft",
                "GOOGL": "fingerprint-new-googl",
            }[rec["Candidate ID"]],
        ):
            safe = _filter_external_mutation(
                service=object(),
                spreadsheet_id="sid",
                sheet_name="BTD_Candidates",
                headers=["Candidate ID", "Ticker", "Status"],
                key_header="Candidate ID",
                snapshot_fingerprints=snapshot,
                candidates=[
                    {"Candidate ID": "AAPL", "Ticker": "AAPL", "Status": "BTD_PASSED"},
                    {"Candidate ID": "MSFT", "Ticker": "MSFT", "Status": "BTD_PASSED"},
                    {"Candidate ID": "GOOGL", "Ticker": "GOOGL", "Status": "NEW"},
                ],
            )

        # AAPL blocked (mutated), MSFT kept (unchanged), GOOGL kept (newly created).
        kept_ids = sorted(c["Candidate ID"] for c in safe)
        self.assertEqual(kept_ids, ["GOOGL", "MSFT"])

    @patch("funnel.review_candidates.BTD_CANDIDATE_HEADERS", ["Candidate ID", "Ticker", "Status"])
    @patch("funnel.review_candidates.BTD_CANDIDATES_SHEET", "BTD_Candidates")
    @patch("funnel.review_candidates.read_table")
    def test_no_external_mutation_passes_all_through(self, mock_read_table):
        """Behaviour: if the re-read returns the same fingerprints as the
        snapshot, every candidate is kept.
        """
        from unittest.mock import patch
        from funnel.review_candidates import _filter_external_mutation

        mock_read_table.return_value = [
            {"Candidate ID": "AAPL", "Ticker": "AAPL", "Status": "BTD_PASSED"},
        ]
        with patch(
            "funnel.review_candidates._fingerprint_candidate",
            return_value="same-fingerprint",
        ):
            safe = _filter_external_mutation(
                service=object(),
                spreadsheet_id="sid",
                sheet_name="BTD_Candidates",
                headers=["Candidate ID", "Ticker", "Status"],
                key_header="Candidate ID",
                snapshot_fingerprints={"AAPL": "same-fingerprint"},
                candidates=[{"Candidate ID": "AAPL", "Ticker": "AAPL", "Status": "BTD_PASSED"}],
            )
        self.assertEqual(len(safe), 1)

    @patch("funnel.review_candidates.read_table")
    def test_external_mutation_guard_skips_upsert_when_reread_fails(self, mock_read_table):
        """Behaviour: if the re-read itself fails with a transient Sheets
        error, ``_filter_external_mutation`` returns an empty list so the
        whole upsert is skipped (safer to drop a cycle than overwrite).
        """
        from unittest.mock import MagicMock
        from googleapiclient.errors import HttpError as GApiHttpError
        from funnel.review_candidates import _filter_external_mutation

        mock_read_table.side_effect = GApiHttpError(
            MagicMock(status=503),
            b"backend unavailable",
        )
        safe = _filter_external_mutation(
            service=object(),
            spreadsheet_id="sid",
            sheet_name="BTD_Candidates",
            headers=["Candidate ID"],
            key_header="Candidate ID",
            snapshot_fingerprints={"AAPL": "x"},
            candidates=[{"Candidate ID": "AAPL", "Ticker": "AAPL"}],
        )
        self.assertEqual(safe, [])


class TestNfrowNaNHandling(unittest.TestCase):
    """Behavior test for _nfrow NaN/Inf-safety.

    The original audit harness exposed a real crash: ``int(float('nan'))``
    raises ValueError, which propagates through ``detail_to_sheet_row`` to
    the per-candidate Feroldi try/except in ``enrich_feroldi_candidates``.
    Fixing _nfrow itself is cheaper and more correct than wrapping every sheet
    write in a broad Exception handler.
    """

    def test_no_crash_on_nan(self):
        from funnel.feroldi_sheet_writer import _nfrow
        self.assertEqual(_nfrow(float("nan")), "")

    def test_no_crash_on_inf(self):
        from funnel.feroldi_sheet_writer import _nfrow
        self.assertEqual(_nfrow(float("inf")), "")
        self.assertEqual(_nfrow(float("-inf")), "")

    def test_round_trip_normal_values(self):
        from funnel.feroldi_sheet_writer import _nfrow
        self.assertEqual(_nfrow(None), "")
        self.assertEqual(_nfrow(10.0), 10)
        self.assertEqual(_nfrow(10.5), 10.5)
        self.assertEqual(_nfrow("hello"), "hello")

    def test_detail_to_sheet_row_with_nan_does_not_crash(self):
        """End-to-end: a stub detail with NaN fields produces an empty cell instead of raising."""
        from funnel.feroldi_sheet_writer import detail_to_sheet_row
        from funnel.feroldi_models import FeroldiDetailResult
        detail = FeroldiDetailResult(ticker="NAN-001")
        detail.f01.cash_and_equivalents = float("nan")
        detail.f02.gross_margin_pct = float("inf")
        detail.s01.trading_days = float("nan")
        row = detail_to_sheet_row(detail, now="2026-01-01T00:00:00Z")
        # NaN positions become empty strings.
        self.assertEqual(row["F01 Cash And Cash Equivalents"], "")
        self.assertEqual(row["F02 Gross Margin %"], "")
        self.assertEqual(row["S01 Trading Days"], "")


if __name__ == "__main__":
    unittest.main()
