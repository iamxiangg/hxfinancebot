from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

from funnel.btd_enrichment import fetch_yfinance_metrics, metrics_to_candidate_updates
from funnel.candidate_ingestor import classify_signals, get_pending_new_ticker_records
from funnel.congress_adapter import run_congress_adapter
from funnel.feroldi_ai import draft_to_candidate_updates, request_feroldi_draft
from funnel.google_client import get_sheets_service, get_spreadsheet_id
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


logger = logging.getLogger(__name__)


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
    return {
        "Candidate ID": candidate_id_for_ticker(ticker),
        "Ticker": ticker,
        "Company Name": record.get("stock_name", ""),
        "Google Ticker": record.get("google_ticker", "") or ticker,
        "Status": "NEW",
        "Review Priority": record.get("review_priority", ""),
        "Source": record.get("scanner", ""),
        "Classification": record.get("classification", ""),
        "Funnel Score": record.get("score", ""),
        "Signal Count": record.get("signal_count", ""),
        "Discovery Reason": record.get("discovery_reason", ""),
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


def _active_for_enrichment(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("Status") or "").strip().upper() not in CANDIDATE_FINAL_STATUSES


def enrich_candidates(candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    count = 0
    now = utc_now_iso()
    for candidate in candidates:
        candidate = dict(candidate)
        if not _active_for_enrichment(candidate):
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
        if not _active_for_enrichment(candidate) or candidate.get("AI Last Updated"):
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

    append_records(
        service,
        spreadsheet_id,
        FEROLDI_AI_DRAFTS_SHEET,
        FEROLDI_AI_DRAFT_HEADERS,
        draft_rows,
    )
    return updated


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

    sources = {part.strip().lower() for part in os.getenv("REVIEW_SOURCES", "congress,manual").split(",")}
    signals: list[Signal] = []
    if "manual" in sources:
        manual_records = read_table(service, spreadsheet_id, MANUAL_SEED_SHEET, MANUAL_SEED_HEADERS)
        signals.extend(manual_seed_signals(manual_records, now))

    if "congress" in sources:
        congress_signals, analysed_count = run_congress_adapter(
            min_conviction=_float_value(os.getenv("MIN_CONVICTION"), 15.0)
        )
        logger.info("Congress adapter returned %d signals from %d tickers.", len(congress_signals), analysed_count)
        signals.extend(congress_signals)

    append_records(
        service,
        spreadsheet_id,
        SIGNAL_LOG_SHEET,
        SIGNAL_LOG_HEADERS,
        signal_log_rows(signals, run_id, now),
    )

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

    candidates = []
    for record in pending:
        incoming = comparison_to_candidate(record, now)
        existing = existing_by_id.get(str(incoming["Candidate ID"]).upper())
        candidates.append(merge_candidate(existing, incoming, now))

    stale_active = [
        record
        for record in existing_candidates
        if str(record.get("Candidate ID") or "").upper()
        not in {str(candidate.get("Candidate ID") or "").upper() for candidate in candidates}
        and _active_for_enrichment(record)
    ]
    candidates.extend(stale_active)

    limit = int(_float_value(os.getenv("BTD_ENRICH_LIMIT"), 10.0))
    candidates = enrich_candidates(candidates, limit=limit)
    candidates = add_optional_ai_drafts(service, spreadsheet_id, candidates)
    candidates = notify_candidates(candidates)

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
    run()
