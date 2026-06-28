"""Workstream B: Controlled promotion worker.

This is the ONLY authorised path to promote a ticker from a review request
into the master list. It:

1. Reads Review_Requests rows in APPROVED_PENDING_PROMOTION state.
2. Re-reads the candidate fresh from BTD_Candidates.
3. Revalidates eligibility and snapshot hash.
4. Re-reads the master-list ticker set.
5. Performs an idempotent master-list write.
6. Updates the review request, candidate, and decision log.

Idempotency: Review ID is the primary key. Repeated promotion of the same
review returns the previous result. A ticker already in the master list
returns ALREADY_EXISTS.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import (
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    DECISION_LOG_HEADERS,
    DECISION_LOG_SHEET,
    REVIEW_REQUESTS_HEADERS,
    REVIEW_REQUESTS_SHEET,
    REVIEW_STATE_ALREADY_EXISTS,
    REVIEW_STATE_APPROVED_PENDING_PROMOTION,
    REVIEW_STATE_FAILED_RETRYABLE,
    REVIEW_STATE_PROMOTED,
    REVIEW_STATE_STALE_REVIEW,
    utc_now_iso,
)
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import append_records, read_table, upsert_records
from funnel.telegram_review import (
    candidate_is_eligible_for_review,
    verify_snapshot,
)

logger = logging.getLogger(__name__)


def _candidate_index(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(record.get("Candidate ID") or "").strip(): record
        for record in records
        if str(record.get("Candidate ID") or "").strip()
    }


def _review_decision_id(review_id: str, action: str) -> str:
    digest = hashlib.sha1(f"{review_id}|{action}|promotion".encode("utf-8")).hexdigest()[:16]
    return f"promo-{digest}"


def _promoted_candidate_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """Build the updated candidate row after successful promotion."""
    now = utc_now_iso()
    updated = dict(candidate)
    updated["Status"] = "APPROVED_ADDED"
    updated["Decision"] = "APPROVE"
    updated["Decision At"] = now
    updated["Decision By"] = "promotion-worker"
    updated["Active?"] = "NO"
    return updated


def promote_approved_review_request(
    service,
    spreadsheet_id: str,
    review: dict[str, str],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Promote one approved review request through full revalidation.

    Returns (result, updated_review, updated_candidate).

    Result is one of: PROMOTED, ALREADY_EXISTS, STALE_REVIEW, FAILED_RETRYABLE.
    """
    now = utc_now_iso()
    review_id = str(review.get("Review ID") or "").strip()
    updated_review = dict(review)

    # --- 1. Load candidate fresh ---
    candidate_records = read_table(service, spreadsheet_id, BTD_CANDIDATES_SHEET, BTD_CANDIDATE_HEADERS)
    candidates = _candidate_index(candidate_records)
    candidate_id = str(review.get("Candidate ID") or "").strip()
    candidate = candidates.get(candidate_id)

    if not candidate:
        updated_review["State"] = REVIEW_STATE_FAILED_RETRYABLE
        updated_review["Last Error"] = "Candidate not found in BTD_Candidates"
        updated_review["Updated At"] = now
        return REVIEW_STATE_FAILED_RETRYABLE, updated_review, {}

    # --- 2. Verify candidate eligibility ---
    eligible, reason = candidate_is_eligible_for_review(candidate)
    if not eligible:
        updated_review["State"] = REVIEW_STATE_STALE_REVIEW
        updated_review["Last Error"] = f"Candidate ineligible at promotion time: {reason}"
        updated_review["Updated At"] = now
        return REVIEW_STATE_STALE_REVIEW, updated_review, {}

    # --- 3. Verify snapshot (re-read candidate, not the cached one) ---
    expected_hash = str(review.get("Candidate Snapshot Hash") or "").strip()
    if not verify_snapshot(candidate, expected_hash):
        updated_review["State"] = REVIEW_STATE_STALE_REVIEW
        updated_review["Last Error"] = "Candidate snapshot mismatch"
        updated_review["Updated At"] = now
        return REVIEW_STATE_STALE_REVIEW, updated_review, {}

    # --- 4. Re-read master list ticker set ---
    from funnel.review_bot import promote_candidate_to_master

    promotion_result = promote_candidate_to_master(service, spreadsheet_id, candidate)

    # --- 5. Map result to review state ---
    if promotion_result == "ADDED":
        updated_review["State"] = REVIEW_STATE_PROMOTED
        updated_review["Promotion Result"] = "ADDED"
        updated_review["Promotion At"] = now
        updated_review["Last Error"] = ""
        updated_review["Updated At"] = now

        updated_candidate = _promoted_candidate_row(candidate)
        return REVIEW_STATE_PROMOTED, updated_review, updated_candidate

    elif promotion_result == "ALREADY_EXISTS":
        updated_review["State"] = REVIEW_STATE_ALREADY_EXISTS
        updated_review["Promotion Result"] = "ALREADY_EXISTS"
        updated_review["Promotion At"] = now
        updated_review["Last Error"] = ""
        updated_review["Updated At"] = now

        updated_candidate = dict(candidate)
        updated_candidate["Status"] = "APPROVED_ALREADY_EXISTS"
        updated_candidate["Decision At"] = now
        updated_candidate["Active?"] = "NO"
        return REVIEW_STATE_ALREADY_EXISTS, updated_review, updated_candidate

    else:
        updated_review["State"] = REVIEW_STATE_FAILED_RETRYABLE
        updated_review["Last Error"] = f"Unexpected promotion result: {promotion_result}"
        updated_review["Updated At"] = now
        return REVIEW_STATE_FAILED_RETRYABLE, updated_review, {}


def run_promotions(*, service=None, spreadsheet_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Process all APPROVED_PENDING_PROMOTION reviews.

    Returns a summary dict with counts of promoted, already_exists, stale, failed.
    Idempotent: Review ID is the primary key; repeated runs are safe.
    """
    service = service or get_sheets_service(readonly=dry_run)
    spreadsheet_id = spreadsheet_id or get_spreadsheet_id()

    if dry_run:
        logger.info("PROMOTION DRY RUN — no writes will be performed.")

    ensure_review_sheets(service, spreadsheet_id)

    review_records = read_table(service, spreadsheet_id, REVIEW_REQUESTS_SHEET, REVIEW_REQUESTS_HEADERS)
    pending = [
        r for r in review_records
        if str(r.get("State") or "").strip() == REVIEW_STATE_APPROVED_PENDING_PROMOTION
    ]

    if not pending:
        logger.info("No pending promotions found.")
        return {
            "pending": 0,
            "promoted": 0,
            "already_exists": 0,
            "stale_review": 0,
            "failed": 0,
            "dry_run": dry_run,
        }

    logger.info("Found %d review(s) awaiting promotion.", len(pending))

    if dry_run:
        for review in pending:
            logger.info(
                "DRY RUN: would promote review %s for ticker %s",
                review.get("Review ID", "?"),
                review.get("Ticker", "?"),
            )
        return {
            "pending": len(pending),
            "promoted": 0,
            "already_exists": 0,
            "stale_review": 0,
            "failed": 0,
            "dry_run": True,
        }

    counts = {"pending": len(pending), "promoted": 0, "already_exists": 0, "stale_review": 0, "failed": 0, "dry_run": False}
    changed_candidates: list[dict[str, Any]] = []
    changed_reviews: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    now = utc_now_iso()

    for review in pending:
        review_id = str(review.get("Review ID") or "?").strip()
        ticker = str(review.get("Ticker") or "?").strip()
        logger.info("Promoting review %s for %s...", review_id, ticker)

        try:
            result, updated_review, updated_candidate = promote_approved_review_request(
                service, spreadsheet_id, review,
            )
            counts[{"PROMOTED": "promoted", "ALREADY_EXISTS": "already_exists",
                     "STALE_REVIEW": "stale_review"}.get(result, "failed")] += 1

            changed_reviews.append(updated_review)

            if updated_candidate:
                changed_candidates.append(updated_candidate)

            decision_rows.append({
                "Decision ID": _review_decision_id(review_id, "PROMOTE"),
                "Candidate ID": str(review.get("Candidate ID") or ""),
                "Ticker": ticker,
                "Action": "PROMOTE",
                "Actor": "promotion-worker",
                "Telegram Update ID": str(review.get("Telegram Update ID") or ""),
                "Decision At": now,
                "Result": result,
                "Details": f"review_id={review_id}",
            })

            logger.info("  → %s", result)

        except Exception as exc:
            logger.exception("Promotion failed for review %s: %r", review_id, exc)
            counts["failed"] += 1
            updated_review = dict(review)
            updated_review["State"] = REVIEW_STATE_FAILED_RETRYABLE
            updated_review["Last Error"] = f"Promotion exception: {exc!r}"[:500]
            updated_review["Updated At"] = now
            changed_reviews.append(updated_review)

    # --- Persist all changes ---
    if changed_candidates:
        upsert_records(
            service, spreadsheet_id, BTD_CANDIDATES_SHEET, BTD_CANDIDATE_HEADERS,
            "Candidate ID", changed_candidates,
        )
    if changed_reviews:
        upsert_records(
            service, spreadsheet_id, REVIEW_REQUESTS_SHEET, REVIEW_REQUESTS_HEADERS,
            "Review ID", changed_reviews,
        )
    if decision_rows:
        append_records(
            service, spreadsheet_id, DECISION_LOG_SHEET, DECISION_LOG_HEADERS, decision_rows,
        )

    logger.info(
        "Promotion run complete: promoted=%d already_exists=%d stale=%d failed=%d",
        counts["promoted"], counts["already_exists"], counts["stale_review"], counts["failed"],
    )
    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = str(os.getenv("PROMOTION_DRY_RUN", "false")).strip().lower() in {"1", "true", "yes", "on"}
    result = run_promotions(dry_run=dry)
    exit_code = 0 if result.get("failed", 0) == 0 else 1
    if dry:
        logger.info("Dry run complete — no writes performed.")
    raise SystemExit(exit_code)
