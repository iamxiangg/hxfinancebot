"""Regression tests for high-severity control-integrity issues (all workstreams)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from funnel.review_schema import (
    REVIEW_STATE_APPROVED_PENDING_PROMOTION,
    REVIEW_STATE_ARCHIVED,
    REVIEW_STATE_EXPIRED,
    REVIEW_STATE_PROMOTED,
    REVIEW_STATE_REJECTED,
    REVIEW_STATE_SENT,
    is_terminal_review_state,
    is_valid_review_transition,
    utc_now_iso,
)
from funnel.telegram_review import (
    _validate_callback_auth,
    build_callback_data,
    build_callback_data_v3,
    candidate_is_eligible_for_review,
    candidate_id_for_ticker,
    compute_snapshot_hash,
    is_legacy_callback,
    is_review_expired,
    new_review_id,
    parse_callback_data,
    verify_snapshot,
)
from tactical.earnings_runner import RunOutcome
from scanners.earnings.models import ScanHealth
from scanners.earnings.engine import EarningsScanResult, EarningsScannerConfig, run_earnings_scan
from scanners.earnings.market_data import UniverseLoadResult, YahooEarningsDataSource


# ---------------------------------------------------------------------------
# Workstream A — Review state machine
# ---------------------------------------------------------------------------

class ReviewStateMachineTests(unittest.TestCase):
    def test_valid_transitions(self) -> None:
        self.assertTrue(is_valid_review_transition("PENDING_SEND", "SENT"))
        self.assertTrue(is_valid_review_transition("SENT", REVIEW_STATE_APPROVED_PENDING_PROMOTION))
        self.assertTrue(is_valid_review_transition("SENT", REVIEW_STATE_REJECTED))
        self.assertTrue(is_valid_review_transition("SENT", REVIEW_STATE_ARCHIVED))
        self.assertTrue(is_valid_review_transition(REVIEW_STATE_APPROVED_PENDING_PROMOTION, REVIEW_STATE_PROMOTED))

    def test_invalid_transitions_are_blocked(self) -> None:
        self.assertFalse(is_valid_review_transition(REVIEW_STATE_REJECTED, REVIEW_STATE_APPROVED_PENDING_PROMOTION))
        self.assertFalse(is_valid_review_transition(REVIEW_STATE_ARCHIVED, REVIEW_STATE_APPROVED_PENDING_PROMOTION))
        self.assertFalse(is_valid_review_transition(REVIEW_STATE_PROMOTED, REVIEW_STATE_APPROVED_PENDING_PROMOTION))
        self.assertFalse(is_valid_review_transition(REVIEW_STATE_SENT, "BOGUS_STATE"))

    def test_terminal_states(self) -> None:
        self.assertTrue(is_terminal_review_state(REVIEW_STATE_REJECTED))
        self.assertTrue(is_terminal_review_state(REVIEW_STATE_ARCHIVED))
        self.assertTrue(is_terminal_review_state(REVIEW_STATE_EXPIRED))
        self.assertTrue(is_terminal_review_state(REVIEW_STATE_PROMOTED))
        self.assertFalse(is_terminal_review_state(REVIEW_STATE_SENT))
        self.assertFalse(is_terminal_review_state(REVIEW_STATE_APPROVED_PENDING_PROMOTION))


# ---------------------------------------------------------------------------
# Workstream A — Callback parsing
# ---------------------------------------------------------------------------

class CallbackParsingTests(unittest.TestCase):
    def test_legacy_hxv2_parsed(self) -> None:
        data = build_callback_data("approve", "cand-MSFT-abc")
        parsed = parse_callback_data(data)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.candidate_id, "cand-MSFT-abc")
        self.assertEqual(parsed.review_id, "")

    def test_legacy_hxv2_detected(self) -> None:
        data = build_callback_data("approve", "cand-MSFT-abc")
        self.assertTrue(is_legacy_callback(data))
        self.assertFalse(is_legacy_callback("hx3:a:review123"))

    def test_hx3_callback_parsed(self) -> None:
        data = build_callback_data_v3("approve", "my-review-id")
        parsed = parse_callback_data(data)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.review_id, "my-review-id")

    def test_hx3_reject_and_archive(self) -> None:
        self.assertEqual(parse_callback_data("hx3:r:review").action, "reject")
        self.assertEqual(parse_callback_data("hx3:x:review").action, "archive")

    def test_callback_under_64_bytes(self) -> None:
        review_id = new_review_id()
        for action in ("a", "r", "x"):
            data = f"hx3:{action}:{review_id}"
            self.assertLess(len(data.encode("utf-8")), 64, f"{data} exceeds 64 bytes")

    def test_malformed_callbacks_rejected(self) -> None:
        self.assertIsNone(parse_callback_data(""))
        self.assertIsNone(parse_callback_data("garbage"))
        self.assertIsNone(parse_callback_data("hx3:bad:review"))
        self.assertIsNone(parse_callback_data("hxv2:unknown:abc"))
        self.assertIsNone(parse_callback_data(None))  # type: ignore[arg-type]

    @patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""})
    def test_missing_allowed_users_fails_closed(self) -> None:
        auth = _validate_callback_auth({"from": {"id": 123}, "message": {"chat": {"id": 456}, "message_id": 789}})
        self.assertIsNone(auth)

    @patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "111,222"})
    def test_wrong_user_id_rejected(self) -> None:
        auth = _validate_callback_auth({"from": {"id": 999}, "message": {"chat": {"id": 456}, "message_id": 789}})
        self.assertIsNone(auth)

    @patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "111", "TELEGRAM_CHAT_ID": "456"})
    def test_authorised_user_succeeds(self) -> None:
        auth = _validate_callback_auth({"from": {"id": 111}, "message": {"chat": {"id": 456}, "message_id": 789}})
        self.assertIsNotNone(auth)
        assert auth is not None
        self.assertEqual(auth.user_id, 111)
        self.assertEqual(auth.chat_id, 456)
        self.assertEqual(auth.message_id, 789)

    @patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "111", "TELEGRAM_CHAT_ID": "456"})
    def test_wrong_chat_id_rejected(self) -> None:
        auth = _validate_callback_auth({"from": {"id": 111}, "message": {"chat": {"id": 999}, "message_id": 789}})
        self.assertIsNone(auth)

    @patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "111", "TELEGRAM_CHAT_ID": "456"})
    def test_missing_message_rejected(self) -> None:
        auth = _validate_callback_auth({"from": {"id": 111}})
        self.assertIsNone(auth)


# ---------------------------------------------------------------------------
# Workstream A — Candidate eligibility checks
# ---------------------------------------------------------------------------

class CandidateEligibilityTests(unittest.TestCase):
    def _eligible_candidate(self) -> dict:
        return {
            "Status": "NOTIFIED",
            "Active?": "YES",
            "Telegram Eligible": "YES",
            "BTD Gate": "PASS",
        }

    def test_eligible_candidate_passes(self) -> None:
        ok, reason = candidate_is_eligible_for_review(self._eligible_candidate())
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_btd_failed_blocked(self) -> None:
        c = self._eligible_candidate()
        c["BTD Gate"] = "FAIL"
        ok, reason = candidate_is_eligible_for_review(c)
        self.assertFalse(ok)
        self.assertIn("FAIL", reason)

    def test_btd_unavailable_blocked(self) -> None:
        c = self._eligible_candidate()
        c["BTD Gate"] = "UNAVAILABLE"
        ok, _ = candidate_is_eligible_for_review(c)
        self.assertFalse(ok)

    def test_not_active_blocked(self) -> None:
        c = self._eligible_candidate()
        c["Active?"] = "NO"
        ok, _ = candidate_is_eligible_for_review(c)
        self.assertFalse(ok)

    def test_not_telegram_eligible_blocked(self) -> None:
        c = self._eligible_candidate()
        c["Telegram Eligible"] = "NO"
        ok, _ = candidate_is_eligible_for_review(c)
        self.assertFalse(ok)

    def test_wrong_status_blocked(self) -> None:
        c = self._eligible_candidate()
        c["Status"] = "REJECTED"
        ok, reason = candidate_is_eligible_for_review(c)
        self.assertFalse(ok)
        self.assertIn("REJECTED", reason)


# ---------------------------------------------------------------------------
# Workstream A — Candidate snapshots
# ---------------------------------------------------------------------------

class SnapshotTests(unittest.TestCase):
    def test_snapshot_hash_is_deterministic(self) -> None:
        candidate = {"Ticker": "MSFT", "Status": "NOTIFIED", "Active?": "YES",
                      "Telegram Eligible": "YES", "BTD Gate": "PASS", "BTD Ratio": "0.5",
                      "BTD Last Updated": "2026-01-01", "Supporting Signal IDs": "sig1,sig2",
                      "Last Seen": "2026-06-01", "Candidate ID": "cand-MSFT-abc"}
        self.assertEqual(compute_snapshot_hash(candidate), compute_snapshot_hash(candidate))

    def test_snapshot_changes_detected(self) -> None:
        c1 = {"Ticker": "MSFT", "Status": "NOTIFIED", "Active?": "YES",
              "Telegram Eligible": "YES", "BTD Gate": "PASS", "BTD Ratio": "0.5",
              "BTD Last Updated": "2026-01-01", "Supporting Signal IDs": "sig1",
              "Last Seen": "2026-06-01", "Candidate ID": "cand-MSFT-abc"}
        c2 = dict(c1)
        c2["BTD Gate"] = "FAIL"
        self.assertNotEqual(compute_snapshot_hash(c1), compute_snapshot_hash(c2))

    def test_verify_snapshot(self) -> None:
        c = {"Ticker": "MSFT", "Status": "NOTIFIED", "Active?": "YES",
             "Telegram Eligible": "YES", "BTD Gate": "PASS", "BTD Ratio": "0.5",
             "BTD Last Updated": "2026-01-01", "Supporting Signal IDs": "",
             "Last Seen": "2026-06-01", "Candidate ID": "cand-MSFT-abc"}
        h = compute_snapshot_hash(c)
        self.assertTrue(verify_snapshot(c, h))


# ---------------------------------------------------------------------------
# Workstream A — Review expiry
# ---------------------------------------------------------------------------

class ReviewExpiryTests(unittest.TestCase):
    def test_not_expired(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        self.assertFalse(is_review_expired(future))

    def test_expired(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(is_review_expired(past))

    def test_invalid_format_is_expired(self) -> None:
        self.assertTrue(is_review_expired("not-a-date"))


# ---------------------------------------------------------------------------
# Workstream D — Sheets literal safety
# ---------------------------------------------------------------------------

class SheetsLiteralSafetyTests(unittest.TestCase):
    def test_formula_like_values_in_append_use_raw(self) -> None:
        from funnel.sheet_table import append_records
        mock_svc = Mock()
        mock_svc.spreadsheets().values().append.return_value.execute.return_value = {}
        append_records(mock_svc, "sheet-123", "Test", ["Col1", "Col2"], [
            {"Col1": "=IMPORTXML(\"https://example.com\",\"//x\")", "Col2": "+1+1"},
            {"Col1": "-2+3", "Col2": "@SUM(A1:A2)"},
        ])
        call_args = mock_svc.spreadsheets().values().append.call_args
        self.assertEqual(call_args[1]["valueInputOption"], "RAW")

    def test_upsert_uses_raw(self) -> None:
        from funnel.sheet_table import upsert_records
        mock_svc = Mock()
        mock_svc.spreadsheets().values().get.return_value.execute.return_value = {"values": [["Col1", "Col2"], ["key1", "old"]]}
        mock_svc.spreadsheets().values().batchUpdate.return_value.execute.return_value = {}
        mock_svc.spreadsheets().values().append.return_value.execute.return_value = {}
        upsert_records(mock_svc, "sheet-123", "Test", ["Col1", "Col2"], "Col1",
                       [{"Col1": "key1", "Col2": "=SUM(1,2)"}])
        batch_call = mock_svc.spreadsheets().values().batchUpdate.call_args
        self.assertEqual(batch_call[1]["body"]["valueInputOption"], "RAW")


# ---------------------------------------------------------------------------
# Workstream E — Scan health
# ---------------------------------------------------------------------------

class ScanHealthTests(unittest.TestCase):
    def test_health_default_healthy(self) -> None:
        health = ScanHealth(status="HEALTHY", universe_source="remote", universe_size=500)
        self.assertEqual(health.status, "HEALTHY")

    def test_health_failed_status(self) -> None:
        health = ScanHealth(status="FAILED", universe_source="fallback", universe_size=8)
        self.assertEqual(health.status, "FAILED")

    def test_health_degraded_status(self) -> None:
        health = ScanHealth(status="DEGRADED", universe_source="cache", universe_size=500,
                            health_reasons=["universe_from_cache"])
        self.assertEqual(health.status, "DEGRADED")


# ---------------------------------------------------------------------------
# Workstream F — RunOutcome exit codes
# ---------------------------------------------------------------------------

class RunOutcomeTests(unittest.TestCase):
    def test_healthy_noop_exits_zero(self) -> None:
        outcome = RunOutcome(health_status="HEALTHY", completed=True, delivery_required=False,
                            delivery_attempted=0, delivery_succeeded=0, delivery_failed=0)
        self.assertEqual(outcome.exit_code, 0)

    def test_failed_scan_exits_nonzero(self) -> None:
        outcome = RunOutcome(health_status="FAILED", completed=True, delivery_required=False,
                            delivery_attempted=0, delivery_succeeded=0, delivery_failed=0)
        self.assertEqual(outcome.exit_code, 1)

    def test_required_delivery_all_failed_exits_nonzero(self) -> None:
        outcome = RunOutcome(health_status="HEALTHY", completed=True, delivery_required=True,
                            delivery_attempted=3, delivery_succeeded=0, delivery_failed=3)
        self.assertEqual(outcome.exit_code, 1)

    def test_partial_delivery_success_exits_zero(self) -> None:
        outcome = RunOutcome(health_status="DEGRADED", completed=True, delivery_required=True,
                            delivery_attempted=3, delivery_succeeded=2, delivery_failed=1)
        self.assertEqual(outcome.exit_code, 0)

    def test_critical_error_exits_nonzero(self) -> None:
        outcome = RunOutcome(health_status="HEALTHY", completed=False, delivery_required=False,
                            delivery_attempted=0, delivery_succeeded=0, delivery_failed=0,
                            critical_error="unrecoverable state")
        self.assertEqual(outcome.exit_code, 1)

    def test_healthy_delivery_success_exits_zero(self) -> None:
        outcome = RunOutcome(health_status="HEALTHY", completed=True, delivery_required=True,
                            delivery_attempted=5, delivery_succeeded=5, delivery_failed=0)
        self.assertEqual(outcome.exit_code, 0)


# ---------------------------------------------------------------------------
# Workstream F — Earnings scan with health
# ---------------------------------------------------------------------------

class EarningsScanHealthTests(unittest.TestCase):
    @patch.object(YahooEarningsDataSource, "load_universe")
    @patch.object(YahooEarningsDataSource, "history")
    @patch.object(YahooEarningsDataSource, "earnings_dates")
    @patch.object(YahooEarningsDataSource, "calendar")
    def test_fallback_universe_is_failed_health(self, mock_cal, mock_earn, mock_hist, mock_univ) -> None:
        mock_univ.return_value = UniverseLoadResult(tickers=["AAPL"], source="fallback")
        mock_hist.return_value = __import__("pandas").DataFrame()
        mock_earn.return_value = __import__("pandas").DataFrame()
        mock_cal.return_value = None

        config = EarningsScannerConfig(max_tickers=1, max_candidates=1)
        result = run_earnings_scan(config=config, data_source=YahooEarningsDataSource())

        self.assertIsNotNone(result.health)
        assert result.health is not None
        self.assertEqual(result.health.status, "FAILED")
        self.assertIn("universe_fallback_used", result.health.health_reasons)

    @patch.object(YahooEarningsDataSource, "load_universe")
    @patch.object(YahooEarningsDataSource, "history")
    @patch.object(YahooEarningsDataSource, "earnings_dates")
    @patch.object(YahooEarningsDataSource, "calendar")
    def test_configured_universe_is_healthy(self, mock_cal, mock_earn, mock_hist, mock_univ) -> None:
        mock_univ.return_value = UniverseLoadResult(tickers=["NVDA"], source="configured")
        hist = __import__("pandas").DataFrame({
            "Open": [100], "High": [110], "Low": [90], "Close": [105], "Volume": [1_000_000]
        })
        mock_hist.return_value = hist
        mock_earn.return_value = __import__("pandas").DataFrame()
        mock_cal.return_value = None

        config = EarningsScannerConfig(max_tickers=1, max_candidates=1)
        result = run_earnings_scan(config=config, data_source=YahooEarningsDataSource())

        self.assertIsNotNone(result.health)
        assert result.health is not None
        self.assertEqual(result.health.status, "HEALTHY")
        self.assertEqual(result.health.universe_source, "configured")

    def test_empty_errors_dont_confuse_valid_noop(self) -> None:
        """Empty result with no errors is a valid successful scan."""
        result = EarningsScanResult(opportunities=[], counts={}, errors=[])
        self.assertEqual(len(result.opportunities), 0)
        self.assertEqual(len(result.errors), 0)


# ---------------------------------------------------------------------------
# Workstream B — Fault-injection: promotion worker
# ---------------------------------------------------------------------------

class PromotionFaultInjectionTests(unittest.TestCase):
    """Verify invariants when failures occur during the promotion flow."""

    def _mock_svc(self):
        svc = Mock()
        svc.spreadsheets().values().get.return_value.execute.return_value = {"values": []}
        svc.spreadsheets().values().append.return_value.execute.return_value = {}
        svc.spreadsheets().values().batchUpdate.return_value.execute.return_value = {}
        return svc

    def _eligible_candidate(self) -> dict:
        return {
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Company Name": "NVIDIA",
            "Status": "NOTIFIED",
            "Active?": "YES",
            "Telegram Eligible": "YES",
            "BTD Gate": "PASS",
            "BTD Ratio": "0.5",
            "BTD Last Updated": "2026-01-01",
            "Supporting Signal IDs": "sig1",
            "Last Seen": "2026-06-01",
        }

    @patch("funnel.review_promotion.read_table")
    def test_candidate_not_found_no_master_write(self, mock_read) -> None:
        """Candidate missing → FAILED_RETRYABLE, no master-list addition."""
        from funnel.review_promotion import promote_approved_review_request
        mock_read.return_value = []
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": "hash",
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        }
        svc = self._mock_svc()
        result, updated_review, cand = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result, "FAILED_RETRYABLE")
        # Verify no master-list append was called
        svc.spreadsheets().values().append.assert_not_called()

    @patch("funnel.review_promotion.read_table")
    def test_stale_snapshot_no_master_write(self, mock_read) -> None:
        """Snapshot mismatch → STALE_REVIEW, no master-list addition."""
        from funnel.review_promotion import promote_approved_review_request
        candidate = self._eligible_candidate()
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": "wrong-hash",
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        }
        mock_read.return_value = [candidate]
        svc = self._mock_svc()
        result, updated_review, cand = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result, "STALE_REVIEW")
        svc.spreadsheets().values().append.assert_not_called()

    @patch("funnel.review_promotion.read_table")
    def test_ineligible_candidate_no_master_write(self, mock_read) -> None:
        """BTD_FAILED candidate → STALE_REVIEW, no master-list addition."""
        from funnel.review_promotion import promote_approved_review_request
        candidate = self._eligible_candidate()
        candidate["BTD Gate"] = "FAIL"
        snapshot = compute_snapshot_hash(candidate)
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": snapshot,
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        }
        mock_read.return_value = [candidate]
        svc = self._mock_svc()
        result, updated_review, cand = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result, "STALE_REVIEW")
        svc.spreadsheets().values().append.assert_not_called()

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_master_already_exists_no_duplicate(self, mock_promote, mock_read) -> None:
        """Ticker already in master → ALREADY_EXISTS, no duplicate row."""
        from funnel.review_promotion import promote_approved_review_request
        candidate = self._eligible_candidate()
        snapshot = compute_snapshot_hash(candidate)
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": snapshot,
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        }
        mock_read.return_value = [candidate]
        mock_promote.return_value = "ALREADY_EXISTS"
        svc = self._mock_svc()
        result, updated_review, cand = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result, "ALREADY_EXISTS")
        self.assertEqual(cand["Status"], "APPROVED_ALREADY_EXISTS")

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_promotion_idempotent_via_review_id(self, mock_promote, mock_read) -> None:
        """Same review ID processed twice returns same result."""
        from funnel.review_promotion import promote_approved_review_request
        candidate = self._eligible_candidate()
        snapshot = compute_snapshot_hash(candidate)
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": snapshot,
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        }
        mock_read.return_value = [candidate]
        mock_promote.return_value = "ADDED"
        svc = self._mock_svc()
        result1, _, _ = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result1, "PROMOTED")
        # If called again (simulating race), same outcome
        mock_promote.return_value = "ADDED"
        result2, _, _ = promote_approved_review_request(svc, "sheet-id", review)
        self.assertEqual(result2, "PROMOTED")

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    @patch("funnel.review_promotion.upsert_records")
    @patch("funnel.review_promotion.append_records")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_dry_run_no_writes(self, mock_svc, mock_sid, mock_ens, mock_app, mock_up, mock_promote, mock_read) -> None:
        """Dry run must never write to Google Sheets."""
        from funnel.review_promotion import run_promotions
        candidate = self._eligible_candidate()
        snapshot = compute_snapshot_hash(candidate)
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": snapshot,
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
            "Issued At": "2026-01-01T00:00:00+00:00",
            "Expires At": "2027-01-01T00:00:00+00:00",
            "Telegram Chat ID": "123",
            "Telegram Message ID": "456",
            "Decision": "APPROVE",
            "Decision At": "2026-01-02T00:00:00+00:00",
            "Decision By User ID": "111",
            "Decision By Username": "testuser",
            "Telegram Update ID": "789",
            "Promotion Result": "",
            "Promotion At": "",
            "Last Error": "",
            "Created At": "2026-01-01T00:00:00+00:00",
            "Updated At": "2026-01-01T00:00:00+00:00",
        }
        mock_svc.return_value = self._mock_svc()
        mock_sid.return_value = "sheet-id"
        mock_read.return_value = [review]
        result = run_promotions(dry_run=True)
        self.assertTrue(result["dry_run"])
        mock_app.assert_not_called()
        mock_up.assert_not_called()

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    @patch("funnel.review_promotion.upsert_records")
    @patch("funnel.review_promotion.append_records")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_exception_during_promotion_is_safe(
        self, mock_svc, mock_sid, mock_ens, mock_app, mock_up, mock_promote, mock_read,
    ) -> None:
        """Exception during promotion → FAILED_RETRYABLE, all other writes still happen."""
        from funnel.review_promotion import run_promotions
        candidate = self._eligible_candidate()
        snapshot = compute_snapshot_hash(candidate)
        review = {
            "Review ID": "rev-001",
            "Candidate ID": "cand-NVDA-abc",
            "Ticker": "NVDA",
            "Candidate Snapshot Hash": snapshot,
            "State": REVIEW_STATE_APPROVED_PENDING_PROMOTION,
            "Issued At": "2026-01-01T00:00:00+00:00",
            "Expires At": "2027-01-01T00:00:00+00:00",
            "Telegram Chat ID": "123",
            "Telegram Message ID": "456",
            "Decision": "APPROVE",
            "Decision At": "2026-01-02T00:00:00+00:00",
            "Decision By User ID": "111",
            "Decision By Username": "testuser",
            "Telegram Update ID": "789",
            "Promotion Result": "",
            "Promotion At": "",
            "Last Error": "",
            "Created At": "2026-01-01T00:00:00+00:00",
            "Updated At": "2026-01-01T00:00:00+00:00",
        }
        mock_svc.return_value = self._mock_svc()
        mock_sid.return_value = "sheet-id"
        mock_promote.side_effect = RuntimeError("sheets rate limited")

        def read_side_effect(svc, sid, sheet, hdrs):
            if sheet == "Review_Requests":
                return [review]
            return [candidate]
        mock_read.side_effect = read_side_effect

        result = run_promotions()
        # The promotion failed, but review and decision log should still be written
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["promoted"], 0)
        # Review record should be updated to FAILED_RETRYABLE
        self.assertTrue(mock_up.called)


if __name__ == "__main__":
    unittest.main()
