"""Tests for the controlled promotion worker (Workstream B)."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from funnel.review_promotion import (
    _review_decision_id,
    promote_approved_review_request,
    run_promotions,
)
from funnel.review_schema import (
    REVIEW_STATE_ALREADY_EXISTS,
    REVIEW_STATE_APPROVED_PENDING_PROMOTION,
    REVIEW_STATE_FAILED_RETRYABLE,
    REVIEW_STATE_PROMOTED,
    REVIEW_STATE_STALE_REVIEW,
)
from funnel.telegram_review import compute_snapshot_hash


def _make_review(candidate_id: str = "cand-NVDA-abc", ticker: str = "NVDA", snapshot_hash: str = "hash123") -> dict[str, str]:
    return {
        "Review ID": "rev-test-001",
        "Candidate ID": candidate_id,
        "Ticker": ticker,
        "Candidate Snapshot Hash": snapshot_hash,
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


def _eligible_candidate() -> dict[str, str]:
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


class PromotionWorkerTests(unittest.TestCase):
    def _mock_sheets_service(self):
        svc = Mock()
        svc.spreadsheets().values().get.return_value.execute.return_value = {"values": []}
        svc.spreadsheets().values().append.return_value.execute.return_value = {}
        svc.spreadsheets().values().batchUpdate.return_value.execute.return_value = {}
        return svc

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_promotion_succeeds_with_valid_candidate(self, mock_promote, mock_read) -> None:
        candidate = _eligible_candidate()
        review = _make_review(snapshot_hash=compute_snapshot_hash(candidate))

        mock_read.return_value = [candidate]
        mock_promote.return_value = "ADDED"

        svc = self._mock_sheets_service()
        result, updated_review, updated_candidate = promote_approved_review_request(
            svc, "sheet-id", review,
        )

        self.assertEqual(result, REVIEW_STATE_PROMOTED)
        self.assertEqual(updated_review["State"], REVIEW_STATE_PROMOTED)
        self.assertEqual(updated_review["Promotion Result"], "ADDED")
        self.assertEqual(updated_candidate["Status"], "APPROVED_ADDED")

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_already_exists_in_master(self, mock_promote, mock_read) -> None:
        candidate = _eligible_candidate()
        review = _make_review(snapshot_hash=compute_snapshot_hash(candidate))

        mock_read.return_value = [candidate]
        mock_promote.return_value = "ALREADY_EXISTS"

        svc = self._mock_sheets_service()
        result, updated_review, updated_candidate = promote_approved_review_request(
            svc, "sheet-id", review,
        )

        self.assertEqual(result, REVIEW_STATE_ALREADY_EXISTS)
        self.assertEqual(updated_review["State"], REVIEW_STATE_ALREADY_EXISTS)
        self.assertEqual(updated_candidate["Status"], "APPROVED_ALREADY_EXISTS")

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    def test_snapshot_changed_returns_stale(self, mock_promote, mock_read) -> None:
        candidate = _eligible_candidate()
        review = _make_review(snapshot_hash="different-hash")

        mock_read.return_value = [candidate]
        mock_promote.return_value = "ADDED"

        svc = self._mock_sheets_service()
        result, updated_review, updated_candidate = promote_approved_review_request(
            svc, "sheet-id", review,
        )

        self.assertEqual(result, REVIEW_STATE_STALE_REVIEW)
        self.assertEqual(updated_review["State"], REVIEW_STATE_STALE_REVIEW)
        self.assertIn("snapshot", updated_review["Last Error"].lower())
        # Stale review should not return candidate updates
        self.assertEqual(updated_candidate, {})

    @patch("funnel.review_promotion.read_table")
    def test_ineligible_candidate_returns_stale(self, mock_read) -> None:
        candidate = _eligible_candidate()
        candidate["BTD Gate"] = "FAIL"
        review = _make_review(snapshot_hash=compute_snapshot_hash(candidate))

        mock_read.return_value = [candidate]

        svc = self._mock_sheets_service()
        result, updated_review, updated_candidate = promote_approved_review_request(
            svc, "sheet-id", review,
        )

        self.assertEqual(result, REVIEW_STATE_STALE_REVIEW)
        self.assertIn("ineligible", updated_review["Last Error"].lower())
        self.assertEqual(updated_candidate, {})

    @patch("funnel.review_promotion.read_table")
    def test_candidate_not_found_returns_failed(self, mock_read) -> None:
        mock_read.return_value = []
        review = _make_review()

        svc = self._mock_sheets_service()
        result, updated_review, updated_candidate = promote_approved_review_request(
            svc, "sheet-id", review,
        )

        self.assertEqual(result, REVIEW_STATE_FAILED_RETRYABLE)
        self.assertIn("not found", updated_review["Last Error"].lower())

    @patch("funnel.review_promotion.read_table")
    def test_decision_id_is_deterministic(self, mock_read) -> None:
        id1 = _review_decision_id("rev-001", "PROMOTE")
        id2 = _review_decision_id("rev-001", "PROMOTE")
        id3 = _review_decision_id("rev-002", "PROMOTE")

        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    @patch("funnel.review_promotion.upsert_records")
    @patch("funnel.review_promotion.append_records")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_run_promotions_processes_all_pending(
        self, mock_svc, mock_sheet_id, mock_ensure, mock_append, mock_upsert, mock_promote, mock_read,
    ) -> None:
        base = _eligible_candidate()
        cand_a = {**base, "Candidate ID": "cand-A-abc", "Ticker": "AAA"}
        cand_b = {**base, "Candidate ID": "cand-B-abc", "Ticker": "BBB"}
        hash_a = compute_snapshot_hash(cand_a)
        hash_b = compute_snapshot_hash(cand_b)

        review1 = _make_review(candidate_id="cand-A-abc", ticker="AAA", snapshot_hash=hash_a)
        review1["Review ID"] = "rev-001"
        review2 = _make_review(candidate_id="cand-B-abc", ticker="BBB", snapshot_hash=hash_b)
        review2["Review ID"] = "rev-002"

        review_records = [review1, review2]
        candidate_records = [cand_a, cand_b]

        mock_svc.return_value = self._mock_sheets_service()
        mock_sheet_id.return_value = "sheet-id"
        mock_promote.return_value = "ADDED"

        def read_table_side_effect(svc, sid, sheet, headers):
            if sheet == "Review_Requests":
                return review_records
            return candidate_records

        mock_read.side_effect = read_table_side_effect

        result = run_promotions()

        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["promoted"], 2)
        self.assertEqual(result["failed"], 0)

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_run_promotions_no_pending_is_noop(self, mock_svc, mock_sheet_id, mock_ensure, mock_read) -> None:
        mock_svc.return_value = self._mock_sheets_service()
        mock_sheet_id.return_value = "sheet-id"
        mock_read.return_value = []

        result = run_promotions()

        self.assertEqual(result["pending"], 0)

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_dry_run_does_not_write(self, mock_svc, mock_sheet_id, mock_ensure, mock_read) -> None:
        candidate = _eligible_candidate()
        review = _make_review(snapshot_hash=compute_snapshot_hash(candidate))
        mock_svc.return_value = self._mock_sheets_service()
        mock_sheet_id.return_value = "sheet-id"
        mock_read.return_value = [review]

        result = run_promotions(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["pending"], 1)

    @patch("funnel.review_promotion.read_table")
    @patch("funnel.review_bot.promote_candidate_to_master")
    @patch("funnel.review_promotion.upsert_records")
    @patch("funnel.review_promotion.append_records")
    @patch("funnel.review_promotion.ensure_review_sheets")
    @patch("funnel.review_promotion.get_spreadsheet_id")
    @patch("funnel.review_promotion.get_sheets_service")
    def test_exception_during_promotion_marks_failed_retryable(
        self, mock_svc, mock_sheet_id, mock_ensure, mock_append, mock_upsert, mock_promote, mock_read,
    ) -> None:
        candidate = _eligible_candidate()
        review = _make_review(snapshot_hash=compute_snapshot_hash(candidate))

        mock_svc.return_value = self._mock_sheets_service()
        mock_sheet_id.return_value = "sheet-id"
        mock_promote.side_effect = RuntimeError("sheets rate limited")

        def read_table_side_effect(svc, sid, sheet, headers):
            if sheet == "Review_Requests":
                return [review]
            return [candidate]

        mock_read.side_effect = read_table_side_effect

        result = run_promotions()

        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["promoted"], 0)


if __name__ == "__main__":
    unittest.main()
