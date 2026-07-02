from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from googleapiclient.errors import HttpError

from funnel.btd_enrichment import fetch_yfinance_metrics, metrics_to_candidate_updates, to_float
from funnel.candidate_ingestor import classify_signals, get_pending_new_ticker_records
from funnel.congress_adapter import run_congress_adapter
from funnel.feroldi_ai import draft_to_candidate_updates, request_feroldi_draft
from funnel.feroldi_gate import apply_feroldi_gate
from funnel.feroldi_scoring import detail_to_candidate_updates, run_feroldi_first_cut
from funnel.feroldi_sheet_writer import detail_to_sheet_row
from funnel.fundamental_inflection_adapter import run_fundamental_inflection_adapter  # hoisted from lazy import inside the elif branch for thread-pool safety
from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.insider_adapter import run_insider_adapter
from funnel.review_schema import (
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    CANDIDATE_FINAL_STATUSES,
    FEROLDI_AI_DRAFT_HEADERS,
    FEROLDI_AI_DRAFTS_SHEET,
    MANUAL_SEED_HEADERS,
    MANUAL_SEED_SHEET,
    SIGNAL_LOG_HEADERS,
    SIGNAL_LOG_SHEET,
    utc_now_iso,
)
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.sheet_table import append_records, read_table, upsert_records
from funnel.signal_schema import Signal, normalise_ticker
from funnel.telegram_review import candidate_id_for_ticker, send_candidate_review
from funnel.vpma_adapter import run_vpma_adapter


logger = logging.getLogger(__name__)


# Transient Sheets / network errors that we tolerate silently (logged and
# swallowed) at well-defined fault barriers. Anything OUTSIDE this tuple is a
# programmer bug and SHOULD crash loudly so it gets noticed during development
# and staging. We deliberately do NOT include ``OSError`` here because it's a
# broad parent of unrelated conditions (FileNotFoundError, MemoryError, etc.)
# that should never be silently swallowed at a Sheets-write site.
_TRANSIENT_SHEETS_ERRORS: tuple[type[BaseException], ...] = (
    HttpError,                  # 4xx / 5xx Sheets API responses
    requests.RequestException,  # transport-level failures (incl. Timeout, Connection)
)


def _float_value(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def manual_seed_signals(records: list[dict[str, str]], observed_at: str) -> list[Signal]:
    signals: list[Signal] = []
    for record in records:
        ticker = normalise_ticker(record.get("Ticker"))
        if not ticker:
            continue

        status = str(record.get("Status") or "ACTIVE").strip().upper()
        if status in {"REJECTED", "ARCHIVED", "DONE", "IGNORE"}:
            continue

        reason = str(record.get("Reason") or "Manual seed").strip()
        score = _float_value(record.get("Score"), 50.0)
        signals.append(
            Signal(
                ticker=ticker,
                scanner="manual",
                classification="actionable",
                score=score,
                observed_at=observed_at,
                details={"reason": reason, "source": "Manual_Seed_Tickers"},
            )
        )
    return signals


def signal_log_rows(signals: list[Signal], run_id: str, created_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in signals:
        rows.append(
            {
                "Run ID": run_id,
                "Signal ID": signal.signal_id,
                "Ticker": signal.ticker,
                "Source": signal.scanner,
                "Classification": signal.classification,
                "Signal Score": signal.score if signal.score is not None else "",
                "Observed At": signal.observed_at,
                "Valid Until": signal.valid_until or "",
                "Reason": signal.details.get("reason") or signal.details.get("flow") or "",
                "Details JSON": json.dumps(signal.details, sort_keys=True),
                "Created At": created_at,
            }
        )
    return rows


def comparison_to_candidate(record: dict[str, Any], now: str) -> dict[str, Any]:
    ticker = normalise_ticker(record.get("ticker"))
    source = ", ".join(record.get("all_sources") or []) or record.get("scanner", "")
    return {
        "Candidate ID": candidate_id_for_ticker(ticker),
        "Ticker": ticker,
        "Company Name": record.get("stock_name", ""),
        "Google Ticker": record.get("google_ticker", "") or ticker,
        "Status": "NEW",
        "Review Priority": record.get("review_priority", ""),
        "Source": source,
        "Positive Sources": record.get("positive_sources", ""),
        "Risk Sources": record.get("risk_sources", ""),
        "Corroboration Level": record.get("corroboration_level", ""),
        "Conflict Status": record.get("conflict_status", ""),
        "Supporting Classifications": record.get("supporting_classifications_text", ""),
        "Supporting Scores": record.get("supporting_scores_text", ""),
        "Supporting Reasons": record.get("supporting_reasons_text", ""),
        "Supporting Signal IDs": record.get("supporting_signal_ids_text", ""),
        "Classification": record.get("classification", ""),
        "Funnel Score": record.get("score", ""),
        "Signal Count": record.get("signal_count", ""),
        "Discovery Reason": record.get("discovery_reason", ""),
        "Congress Unique Members": record.get("congress_unique_members", ""),
        "Congress Recent Cluster Members": record.get("congress_recent_cluster_members", ""),
        "Congress Active Purchases": record.get("congress_active_purchases", ""),
        "Congress Member Names": record.get("congress_member_names", ""),
        "Insider Total Score": record.get("insider_total_score", ""),
        "Insider Conviction": record.get("insider_conviction", ""),
        "Insider Economic Commitment": record.get("insider_economic_commitment", ""),
        "Insider Market Context": record.get("insider_market_context", ""),
        "Insider Unique Insiders": record.get("insider_unique_insiders", ""),
        "Insider Roles": record.get("insider_roles", ""),
        "Insider Aggregate Purchase": record.get("insider_aggregate_purchase", ""),
        "Insider Cluster Span Days": record.get("insider_cluster_span_days", ""),
        "Insider Weighted Purchase Price": record.get("insider_weighted_purchase_price", ""),
        "Insider Entry State": record.get("insider_entry_state", ""),
        "First Seen": now,
        "Last Seen": record.get("observed_at", now),
        "Active?": "YES",
    }


def merge_candidate(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    if not existing:
        return dict(incoming)

    merged = dict(existing)
    status = str(existing.get("Status") or "").strip().upper()
    if status in CANDIDATE_FINAL_STATUSES:
        return merged

    for key, value in incoming.items():
        if key in {"Candidate ID", "Ticker", "First Seen"}:
            continue
        if value not in ("", None):
            merged[key] = value

    if not merged.get("First Seen"):
        merged["First Seen"] = incoming.get("First Seen", now)
    merged["Last Seen"] = incoming.get("Last Seen", now)
    merged["Active?"] = "YES"
    if status not in {"NOTIFIED", "REVIEW"}:
        merged["Status"] = "NEW"
    return merged


def _candidate_index(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(record.get("Candidate ID") or "").strip().upper(): record
        for record in records
        if str(record.get("Candidate ID") or "").strip()
    }


def _fingerprint_candidate(record: dict[str, str]) -> str:
    """Stable SHA-256 fingerprint of a BTD candidate row's content.

    Used by the race-condition guard to detect any external mutation of
    ``BTD_CANDIDATES_SHEET`` between the initial read at the top of ``run()``
    and the final upsert. We hash every column the row carries so any
    editorial change (Status / Decision / Telegram Message ID / etc.) is
    detected, while remaining cheap (single sha256 per row).
    """
    payload = "|".join(
        f"{header}={str(record.get(header, '') or '').strip()}"
        for header in BTD_CANDIDATE_HEADERS
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _filter_external_mutation(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_header: str,
    snapshot_fingerprints: dict[str, str],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Refuse to upsert any candidate whose row was externally mutated between
    the top-of-``run()`` read and this final-upsert step.

    Returns the filtered candidate list with conflicting IDs removed. If the
    re-read itself fails for any reason, returns ``[]`` and skips the upsert
    for the entire cycle — we'd rather drop one cycle than silently overwrite
    a parallel writer's edit.

    Known limitation: a write landing between this re-read and the actual
    ``upsert_records`` call below is still invisible to this guard. Eliminating
    that window would require row-level locking which Google Sheets does not
    support natively.
    """
    candidate_ids_being_upserted = {
        str(candidate.get(key_header) or "").strip().upper()
        for candidate in candidates
        if str(candidate.get(key_header) or "").strip()
    }
    if not candidate_ids_being_upserted:
        return candidates

    try:
        current_rows = read_table(service, spreadsheet_id, sheet_name, headers)
    except _TRANSIENT_SHEETS_ERRORS as exc:
        # If the re-read itself fails transiently, err on safety: skip upsert
        # for the cycle because we cannot verify nothing changed externally.
        logger.warning(
            "Race-condition guard: could not re-read %s to verify safety; "
            "skipping BTD_Candidates upsert for this cycle: %r",
            sheet_name,
            exc,
        )
        return []
    except (KeyError, ValueError, TypeError) as exc:
        # Header mismatch / wrong arg count / missing candidate-id field —
        # programmer bug. Re-raise so it's noticed during development,
        # not silently swallowed (which would drop an entire cycle for a typo).
        logger.exception(
            "Race-condition guard: programmer error re-reading %s: %r",
            sheet_name,
            exc,
        )
        raise
    except Exception as exc:
        logger.exception(
            "Race-condition guard: unexpected error re-reading %s; "
            "skipping BTD_Candidates upsert for this cycle: %r",
            sheet_name,
            exc,
        )
        return []

    current_fingerprints = {
        str(row.get(key_header) or "").strip().upper(): _fingerprint_candidate(row)
        for row in current_rows
        if str(row.get(key_header) or "").strip()
    }

    safe: list[dict[str, Any]] = []
    blocked: list[str] = []
    for candidate in candidates:
        cid = str(candidate.get(key_header) or "").strip().upper()
        snap = snapshot_fingerprints.get(cid)
        curr = current_fingerprints.get(cid)
        if snap is None:
            # Row didn't exist at the start of the cycle — the upsert will
            # create it. Safe.
            safe.append(candidate)
            continue
        if curr is None:
            # Row existed at the start but has since been deleted externally;
            # safer to re-create it.
            safe.append(candidate)
            continue
        if snap == curr:
            safe.append(candidate)
            continue
        blocked.append(cid)
        logger.warning(
            "Race-condition guard: external mutation detected on Candidate ID %s; "
            "skipping upsert to avoid clobbering a parallel writer.",
            cid,
        )

    if blocked:
        logger.info(
            "Race-condition guard blocked %d of %d candidates from upsert.",
            len(blocked),
            len(candidate_ids_being_upserted),
        )
    return safe


def _active_for_enrichment(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("Status") or "").strip().upper() not in CANDIDATE_FINAL_STATUSES


def _status_allows_reprocessing(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("Status") or "").strip().upper() not in {"NOTIFIED", "REVIEW"}


def _source_set(candidate: dict[str, Any]) -> set[str]:
    return {
        part.strip().lower()
        for part in str(candidate.get("Source") or "").split(",")
        if part.strip()
    }


def _telegram_eligible(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("Telegram Eligible") or "").strip().upper() == "YES"


def apply_btd_gate(candidate: dict[str, Any], *, manual_bypass: bool, threshold: float) -> dict[str, Any]:
    candidate = dict(candidate)
    source_set = _source_set(candidate)

    if manual_bypass and source_set == {"manual"}:
        candidate["BTD Gate"] = "BYPASSED_MANUAL"
        candidate["BTD Gate Reason"] = "Manual-only candidate bypassed the automatic BTD gate."
        candidate["Telegram Eligible"] = "YES"
        candidate["Status"] = "BTD_PASSED"
        return candidate

    applicability = str(candidate.get("BTD Applicability") or "").strip().upper()
    if applicability == "NOT_APPLICABLE":
        candidate["BTD Gate"] = "NOT_APPLICABLE"
        candidate["BTD Gate Reason"] = "BTD formula is not suitable for this business model."
        candidate["Telegram Eligible"] = "NO"
        candidate["Status"] = "BTD_NOT_APPLICABLE"
        return candidate

    if applicability != "APPLICABLE":
        candidate["BTD Gate"] = "UNAVAILABLE"
        candidate["BTD Gate Reason"] = "BTD applicability or required data is unavailable."
        candidate["Telegram Eligible"] = "NO"
        candidate["Status"] = "BTD_UNAVAILABLE"
        return candidate

    ratio = to_float(candidate.get("BTD Ratio"))
    if ratio is None or ratio <= 0:
        candidate["BTD Gate"] = "UNAVAILABLE"
        candidate["BTD Gate Reason"] = "A valid positive BTD ratio could not be calculated."
        candidate["Telegram Eligible"] = "NO"
        candidate["Status"] = "BTD_UNAVAILABLE"
        return candidate

    if ratio < threshold:
        candidate["BTD Gate"] = "PASS"
        candidate["BTD Gate Reason"] = f"Valid BTD ratio {ratio:.2f} is below {threshold:.1f}."
        candidate["Telegram Eligible"] = "YES"
        candidate["Status"] = "BTD_PASSED"
        return candidate

    candidate["BTD Gate"] = "FAIL"
    candidate["BTD Gate Reason"] = f"BTD ratio {ratio:.2f} is not below {threshold:.1f}."
    candidate["Telegram Eligible"] = "NO"
    candidate["Status"] = "BTD_FAILED"
    return candidate


def enrich_candidates(candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    count = 0
    now = utc_now_iso()
    for candidate in candidates:
        candidate = dict(candidate)
        if not _active_for_enrichment(candidate) or not _status_allows_reprocessing(candidate):
            enriched.append(candidate)
            continue

        if count >= limit:
            enriched.append(candidate)
            continue

        ticker = str(candidate.get("Ticker") or "").strip()
        if not ticker:
            enriched.append(candidate)
            continue

        try:
            metrics = fetch_yfinance_metrics(ticker)
            updates = metrics_to_candidate_updates(metrics)
            candidate.update({key: value for key, value in updates.items() if value not in ("", None)})
            candidate["BTD Last Updated"] = now
            if str(candidate.get("Status") or "").upper() == "NEW":
                candidate["Status"] = "ENRICHED"
            count += 1
        except Exception as exc:
            candidate["Last Error"] = f"BTD enrichment failed: {exc!r}"[:500]
        enriched.append(candidate)
    return enriched


def add_optional_ai_drafts(
    service,
    spreadsheet_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return candidates

    now = utc_now_iso()
    draft_rows: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate = dict(candidate)
        if not _active_for_enrichment(candidate) or candidate.get("AI Last Updated") or not _telegram_eligible(candidate):
            updated.append(candidate)
            continue

        try:
            draft = request_feroldi_draft(candidate)
            if not draft:
                updated.append(candidate)
                continue
            candidate.update(draft_to_candidate_updates(draft))
            candidate["AI Last Updated"] = now
            draft_rows.append(
                {
                    "Candidate ID": candidate.get("Candidate ID", ""),
                    "Ticker": candidate.get("Ticker", ""),
                    "AI Feroldi Score": candidate.get("AI Feroldi Score", ""),
                    "Quality Summary": candidate.get("AI Quality Summary", ""),
                    "Bull Case": candidate.get("AI Bull Case", ""),
                    "Bear Case": candidate.get("AI Bear Case", ""),
                    "Red Flags": candidate.get("AI Red Flags", ""),
                    "Manual Review Needed": candidate.get("AI Manual Review Needed", ""),
                    "Confidence": candidate.get("AI Confidence", ""),
                    "Draft JSON": json.dumps(draft, sort_keys=True),
                    "Created At": now,
                }
            )
        except Exception as exc:
            candidate["Last Error"] = f"AI draft failed: {exc!r}"[:500]
        updated.append(candidate)

    try:
        append_records(
            service,
            spreadsheet_id,
            FEROLDI_AI_DRAFTS_SHEET,
            FEROLDI_AI_DRAFT_HEADERS,
            draft_rows,
        )
    except _TRANSIENT_SHEETS_ERRORS as exc:
        # A transient Sheets failure here must NOT abort the rest of the
        # pipeline. AI drafts are advisory only; losing one cycle of drafts
        # is acceptable, but wiping out the BTD_Candidates upsert that runs
        # later in ``run()`` is not.
        logger.exception("Failed to write Feroldi_AI_Drafts rows: %r", exc)
    return updated


def enrich_feroldi_candidates(
    candidates: list[dict[str, Any]],
    *,
    service,
    spreadsheet_id: str,
    limit: int = 10,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Run Feroldi first-cut enrichment and scoring on eligible candidates.

    Only processes candidates that:
    - Are active (not in a final status)
    - Passed BTD gate (BTD_PASSED, BYPASSED_MANUAL) or already Telegram Eligible
    - Don't already have recent Feroldi data (unless force_refresh)

    On failure, preserves the previous valid Feroldi score.
    """
    from funnel.review_schema import FEROLDI_FIRST_CUT_DETAIL_SHEET, FEROLDI_FIRST_CUT_DETAIL_HEADERS

    now = utc_now_iso()
    refresh_days = int(_float_value(os.getenv("FEROLDI_REFRESH_DAYS"), 7.0))

    detail_rows: list[dict[str, Any]] = []
    enriched: list[dict[str, Any]] = []
    count = 0

    for candidate in candidates:
        candidate = dict(candidate)
        if not _active_for_enrichment(candidate):
            enriched.append(candidate)
            continue

        # Only process candidates that passed BTD or are Telegram Eligible
        status = str(candidate.get("Status") or "").strip().upper()
        telegram_eligible = str(candidate.get("Telegram Eligible") or "").strip().upper() == "YES"
        if status not in {"BTD_PASSED", "BYPASSED_MANUAL"} and not telegram_eligible:
            enriched.append(candidate)
            continue

        # Skip if recent Feroldi data exists (unless force_refresh)
        last_updated = str(candidate.get("Feroldi Last Updated") or "").strip()
        if not force_refresh and last_updated:
            try:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - last).days
                if age_days < refresh_days:
                    enriched.append(candidate)
                    continue
            except (ValueError, TypeError):
                pass

        if count >= limit:
            enriched.append(candidate)
            continue

        ticker = str(candidate.get("Ticker") or "").strip()
        candidate_id = str(candidate.get("Candidate ID") or "").strip()
        if not ticker:
            enriched.append(candidate)
            continue

        # Preserve previous Feroldi data in case of failure
        prev_feroldi = {
            k: candidate.get(k)
            for k in (
                "Feroldi Financial Score", "Feroldi Financial Available",
                "Feroldi Management Score", "Feroldi Management Available",
                "Feroldi Stock Score", "Feroldi Stock Available",
                "Feroldi First Cut Score", "Feroldi Available Points",
                "Feroldi Max Points", "Feroldi Equivalent Score",
                "Feroldi Coverage", "Feroldi Missing Inputs",
                "Feroldi Last Updated",
            )
        }

        try:
            detail = run_feroldi_first_cut(ticker, candidate_id=candidate_id)

            # Write aggregated scores back to candidate row
            updates = detail_to_candidate_updates(detail)
            candidate.update({k: v for k, v in updates.items() if v not in ("", None)})

            # Build detail row for Feroldi_First_Cut_Detail sheet
            detail_rows.append(detail_to_sheet_row(detail, now))

            count += 1
        except Exception as exc:
            # On failure, restore previous valid Feroldi data
            candidate.update({k: v for k, v in prev_feroldi.items() if v not in ("", None)})
            candidate["Last Error"] = f"Feroldi enrichment failed: {exc!r}"[:500]
            logger.exception("Feroldi enrichment failed for %s", ticker)

        enriched.append(candidate)

    # Upsert detail rows to the Feroldi_First_Cut_Detail sheet
    if detail_rows:
        try:
            upsert_records(
                service,
                spreadsheet_id,
                FEROLDI_FIRST_CUT_DETAIL_SHEET,
                FEROLDI_FIRST_CUT_DETAIL_HEADERS,
                "Candidate ID",
                detail_rows,
            )
            logger.info("Wrote %d Feroldi detail rows.", len(detail_rows))
        except Exception as exc:
            logger.exception("Failed to write Feroldi detail rows: %s", exc)

    return enriched


def notify_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if str(os.getenv("SEND_TELEGRAM_REVIEWS", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return candidates

    force_resend = str(
        os.getenv("FORCE_TELEGRAM_REVIEW_RESEND", "false")
    ).strip().lower() in {"1", "true", "yes", "on"}

    now = utc_now_iso()
    updated: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate = dict(candidate)
        if not _active_for_enrichment(candidate):
            updated.append(candidate)
            continue
        if not _telegram_eligible(candidate):
            updated.append(candidate)
            continue
        if candidate.get("Telegram Message ID") and not force_resend:
            updated.append(candidate)
            continue

        try:
            message_id = send_candidate_review(candidate)
            candidate["Telegram Message ID"] = message_id
            candidate["Telegram Last Notified At"] = now
            candidate["Status"] = "NOTIFIED"
        except Exception as exc:
            candidate["Last Error"] = f"Telegram notification failed: {exc!r}"[:500]
        updated.append(candidate)
    return updated


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_review_sheets(service, spreadsheet_id)

    now = utc_now_iso()
    run_id = os.getenv("GITHUB_RUN_ID", now)

    requested_sources = [
        part.strip().lower()
        for part in os.getenv("REVIEW_SOURCES", "congress,vpma,insider,fundamental_inflection,manual").split(",")
        if part.strip()
    ]
    signals: list[Signal] = []
    successful_sources: list[str] = []
    failed_sources: dict[str, str] = {}

    # Network-bound adapters (congress / vpma / insider / fundamental_inflection)
    # are safe -- and significantly faster -- to run in parallel. Their combined
    # SEC request rate is capped process-globally by the ``_THROTTLE_LOCK`` in
    # ``providers/sec/official.py``, so 4 parallel adapters cannot collectively
    # bust the SEC EDGAR 10 req/sec per-IP limit. The pool is sized at
    # ``min(4, N)`` so a single-source run does not spawn idle workers.
    _REMOTE_SOURCES = ("congress", "vpma", "insider", "fundamental_inflection")
    remote_sources = [source for source in requested_sources if source in _REMOTE_SOURCES]

    # Sequential first pass: ``manual`` reads the sheet through ``service``;
    # unknown source strings are logged-and-skipped. Both must NOT execute
    # inside the pool so the Sheets OAuth ``service`` object is never shared
    # across worker threads concurrently (Google API clients are not safe to
    # mutate from multiple threads simultaneously).
    for source in requested_sources:
        if source == "manual":
            try:
                manual_records = read_table(service, spreadsheet_id, MANUAL_SEED_SHEET, MANUAL_SEED_HEADERS)
                source_signals = manual_seed_signals(manual_records, now)
                signals.extend(source_signals)
                successful_sources.append(source)
                logger.info("Manual source returned %d signals.", len(source_signals))
            except Exception as exc:
                failed_sources[source] = exc.__class__.__name__
                logger.exception("Review source '%s' failed.", source)
        elif source not in _REMOTE_SOURCES:
            logger.warning("Unknown review source '%s' ignored.", source)

    # Parallel second pass: dispatch each remote source to a worker. Per-future
    # try/except preserves the per-source failure isolation the sequential loop
    # provided; results are stashed per-source then appended to ``signals`` in
    # ``requested_sources`` order so the downstream ``classify_signals`` doesn't
    # randomly reshuffle based on per-source completion latency.
    if remote_sources:
        min_conviction_remote = _float_value(os.getenv("MIN_CONVICTION"), 15.0)
        max_workers = min(4, len(remote_sources))
        future_to_source: dict[Future, str] = {}
        remote_results: dict[str, list[Signal]] = {}
        remote_analysed: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="review-src") as executor:
            for source in remote_sources:
                if source == "congress":
                    future = executor.submit(
                        run_congress_adapter,
                        min_conviction=min_conviction_remote,
                        observed_at=now,
                    )
                elif source == "vpma":
                    future = executor.submit(run_vpma_adapter, observed_at=now)
                elif source == "insider":
                    future = executor.submit(run_insider_adapter, observed_at=now)
                else:  # fundamental_inflection (guarded by ``_REMOTE_SOURCES`` membership above)
                    future = executor.submit(run_fundamental_inflection_adapter, observed_at=now)
                future_to_source[future] = source

            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    source_signals, analysed_count = future.result()
                    remote_results[source] = source_signals
                    remote_analysed[source] = analysed_count
                    successful_sources.append(source)
                    logger.info(
                        "%s adapter returned %d signals from %d tickers.",
                        source,
                        len(source_signals),
                        analysed_count,
                    )
                except Exception as exc:
                    failed_sources[source] = exc.__class__.__name__
                    logger.exception("Review source '%s' failed.", source)

        for source in remote_sources:
            if source in remote_results:
                signals.extend(remote_results[source])

    if not successful_sources and requested_sources:
        raise RuntimeError(
            "All requested review sources failed: "
            + ", ".join(f"{source}={error}" for source, error in sorted(failed_sources.items()))
        )

    if failed_sources:
        # Sort ``successful_sources`` for deterministic log output: the
        # upstream ThreadPoolExecutor / ``as_completed`` loop populates it in
        # non-deterministic per-source completion order.
        logger.warning(
            "Continuing with partial source success. Successful=%s Failed=%s",
            ",".join(sorted(successful_sources)),
            ",".join(f"{source}:{error}" for source, error in sorted(failed_sources.items())),
        )

    # Signal-log writes are best-effort; losing one cycle after a transient
    # network hiccup (e.g. SSL connection drop on the GitHub runner) is
    # acceptable, but letting that crash the entire ``run()`` after three
    # successful adapter scans is not. ``OSError`` is deliberately excluded
    # from the module-level ``_TRANSIENT_SHEETS_ERRORS`` tuple (its broad
    # parent scope would mask ``FileNotFoundError`` / ``MemoryError`` at
    # other call sites), so we catch it here at this well-defined fault barrier.
    try:
        append_records(
            service,
            spreadsheet_id,
            SIGNAL_LOG_SHEET,
            SIGNAL_LOG_HEADERS,
            signal_log_rows(signals, run_id, now),
        )
    except (_TRANSIENT_SHEETS_ERRORS, OSError) as exc:
        logger.warning("Signal log write failed (network transient); continuing: %r", exc)

    master_records = get_stock_summary_ticker_records(service=service)
    comparison = classify_signals(signals, master_records)
    pending = get_pending_new_ticker_records(comparison)

    existing_candidates = read_table(
        service,
        spreadsheet_id,
        BTD_CANDIDATES_SHEET,
        BTD_CANDIDATE_HEADERS,
    )
    existing_by_id = _candidate_index(existing_candidates)

    # Capture per-row fingerprints now so we can detect any parallel writer
    # that mutates ``BTD_CANDIDATES_SHEET`` between this read and the final
    # ``upsert_records`` at the bottom of ``run()``. The funnel itself does
    # not write to ``BTD_CANDIDATES_SHEET`` mid-cycle, so any difference is
    # by definition external.
    snapshot_fingerprints = {
        str(record.get("Candidate ID") or "").strip().upper(): _fingerprint_candidate(record)
        for record in existing_candidates
        if str(record.get("Candidate ID") or "").strip()
    }

    candidates = []
    for record in pending:
        incoming = comparison_to_candidate(record, now)
        existing = existing_by_id.get(str(incoming["Candidate ID"]).upper())
        candidates.append(merge_candidate(existing, incoming, now))

    # Pre-compute the active candidate-ID set ONCE rather than rebuilding on
    # every iteration. Without this, the comprehension is O(N x M) and re-creates
    # a set for each of N existing records.
    active_candidate_ids = {
        str(candidate.get("Candidate ID") or "").upper()
        for candidate in candidates
        if str(candidate.get("Candidate ID") or "").strip()
    }
    stale_active = [
        record
        for record in existing_candidates
        if str(record.get("Candidate ID") or "").upper() not in active_candidate_ids
        and _active_for_enrichment(record)
    ]
    candidates.extend(stale_active)

    limit = int(_float_value(os.getenv("BTD_ENRICH_LIMIT"), 10.0))
    candidates = enrich_candidates(candidates, limit=limit)
    threshold = _float_value(os.getenv("BTD_GATE_THRESHOLD"), 1.0)
    manual_bypass = str(os.getenv("BTD_GATE_MANUAL_BYPASS", "true")).strip().lower() in {"1", "true", "yes", "on"}
    candidates = [
        apply_btd_gate(candidate, manual_bypass=manual_bypass, threshold=threshold)
        if _active_for_enrichment(candidate) and _status_allows_reprocessing(candidate)
        else dict(candidate)
        for candidate in candidates
    ]
    candidates = add_optional_ai_drafts(service, spreadsheet_id, candidates)

    # Feroldi first-cut enrichment and scoring (deterministic, no LLM)
    feroldi_enrich_limit = int(_float_value(os.getenv("FEROLDI_ENRICH_LIMIT"), 10.0))
    feroldi_force_refresh = str(os.getenv("FEROLDI_FORCE_REFRESH", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    candidates = enrich_feroldi_candidates(
        candidates,
        service=service,
        spreadsheet_id=spreadsheet_id,
        limit=feroldi_enrich_limit,
        force_refresh=feroldi_force_refresh,
    )

    feroldi_mode = os.getenv("FEROLDI_GATE_MODE", "observe")
    feroldi_pass_threshold = _float_value(os.getenv("FEROLDI_GATE_PASS_THRESHOLD"), 27.5)
    feroldi_review_threshold = _float_value(os.getenv("FEROLDI_GATE_REVIEW_THRESHOLD"), 23.0)
    feroldi_min_coverage = _float_value(os.getenv("FEROLDI_GATE_MIN_COVERAGE"), 0.75)
    feroldi_allow_review = str(os.getenv("FEROLDI_GATE_ALLOW_REVIEW", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    candidates = [
        apply_feroldi_gate(
            candidate,
            mode=feroldi_mode,
            pass_threshold=feroldi_pass_threshold,
            review_threshold=feroldi_review_threshold,
            min_coverage=feroldi_min_coverage,
            allow_review=feroldi_allow_review,
        )
        if _active_for_enrichment(candidate) and _status_allows_reprocessing(candidate)
        else dict(candidate)
        for candidate in candidates
    ]

    candidates = notify_candidates(candidates)

    # Race-condition guard. Re-read the sheet and compare fingerprint-by-
    # fingerprint against the snapshot captured at the top of ``run()``;
    # skip upserting any candidate whose row was externally mutated in the
    # meantime. Caller's snapshot is the source of truth for what we plan
    # to overwrite.
    candidates = _filter_external_mutation(
        service,
        spreadsheet_id,
        BTD_CANDIDATES_SHEET,
        BTD_CANDIDATE_HEADERS,
        "Candidate ID",
        snapshot_fingerprints,
        candidates,
    )

    if not candidates:
        logger.warning(
            "Skipping BTD_Candidates upsert for this cycle (race-condition guard "
            "filtered all candidates OR the re-read failed)."
        )
        return

    upsert_records(
        service,
        spreadsheet_id,
        BTD_CANDIDATES_SHEET,
        BTD_CANDIDATE_HEADERS,
        "Candidate ID",
        candidates,
    )
    logger.info("Updated %d candidate rows.", len(candidates))


if __name__ == "__main__":
    # When invoked as a stand-alone script (``python funnel/review_candidates.py``)
    # `funnel.*` imports below would otherwise fail. Guarded so library imports
    # of this module don't mutate ``sys.path``.
    _THIS_FILE = Path(__file__).resolve()
    _PROJECT_ROOT = _THIS_FILE.parent.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    run()
