# VERSION: 2026-06-24-CONGRESS-ADAPTER-V2
# Funnel adapter for the shared Congress scanner engine.

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from googleapiclient.errors import HttpError

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.political_archive import (
    build_archive_stats,
    get_bot_state_value,
    get_bootstrap_marker,
    load_political_archive_state,
    persist_raw_archive_updates,
    persist_summary_rows,
    prepare_raw_archive_upserts,
    LAST_PAYLOAD_HASH_KEY,
    LAST_RECORD_COUNT_KEY,
    set_bot_state_value,
    set_bootstrap_marker,
    summary_row_from_history,
)
from funnel.review_schema import CONGRESS_LEDGER_HEADERS, CONGRESS_LEDGER_SHEET
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import read_table, upsert_records
from funnel.signal_schema import Signal
from scanners.congress.digest_models import PoliticalDigestPlan
from scanners.congress.engine import (
    MODEL_VERSION,
    CongressScanResult,
    CongressTickerResult,
    run_live_scan,
)
from scanners.congress.flag_ranker import build_digest_plan, classify_release_type, detect_backfill_status
from scanners.congress.models import PoliticalArchiveStats, PoliticalBackfillStatus, TickerPoliticalHistory
from scanners.congress.ticker_history import build_ticker_histories


logger = logging.getLogger(__name__)


# Transient Sheets / network errors tolerated silently (logged and swallowed)
# at well-defined fault barriers. Anything OUTSIDE this tuple is a programmer
# bug and SHOULD crack loudly so it gets noticed during development / staging.
# Deliberately excludes ``OSError`` because that parent class also covers
# unrelated conditions (MemoryError, IsADirectoryError, FileNotFoundError…)
# that should never be silently swallowed at a Sheets-write site.
# Mirrors funnel/review_candidates._TRANSIENT_SHEETS_ERRORS but kept local to
# avoid a circular import at module load.
_TRANSIENT_SHEETS_ERRORS: tuple[type[BaseException], ...] = (
    HttpError,                  # 4xx / 5xx Sheets API responses
    requests.RequestException,  # transport-level failures (incl. Timeout, Connection)
)


SUPPORTED_CATEGORIES = {
    "actionable",
    "wait",
    "risk",
}


@dataclass
class CongressAdapterRun:
    scan: CongressScanResult
    signals: list[Signal]
    analysed_tickers: int
    new_records: list[dict[str, Any]]
    material_amendments: list[dict[str, Any]]
    removed_records: list[dict[str, Any]]
    affected_tickers: list[str]
    current_ticker_histories: dict[str, TickerPoliticalHistory]
    previous_ticker_states: dict[str, dict[str, Any]]
    ranked_digest_flags: list[dict[str, Any]]
    compact_digest_items: list[dict[str, Any]]
    new_material_flags: list[dict[str, Any]]
    material_updates: list[dict[str, Any]]
    active_watchlist_items: list[dict[str, Any]]
    other_new_activity: list[dict[str, Any]]
    expired_watchlist_items: list[dict[str, Any]]
    watchlist_state_changes: list[dict[str, Any]]
    backfill_status: PoliticalBackfillStatus
    archive_stats: PoliticalArchiveStats
    digest_plan: PoliticalDigestPlan
    payload_hash: str


@dataclass(frozen=True)
class _SignalBreakdown:
    alertable: int
    already_seen_suppressed: int
    below_threshold: int
    retained: int


@dataclass
class _SheetLedgerContext:
    service: Any
    spreadsheet_id: str


def _normalise_datetime(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        raise ValueError("observed_at cannot be blank.")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _remaining_validity_days(result: CongressTickerResult) -> int:
    return max(1, int(result.valid_for_days))


def _signal_breakdown(
    results: list[CongressTickerResult],
    signals: list[Signal],
    *,
    min_conviction: float,
) -> _SignalBreakdown:
    alertable = sum(result.alertable for result in results)
    already_seen_suppressed = sum(not result.alertable for result in results)
    below_threshold = sum(
        result.alertable
        and result.category == "other"
        and result.conviction < float(min_conviction)
        for result in results
    )
    return _SignalBreakdown(
        alertable=alertable,
        already_seen_suppressed=already_seen_suppressed,
        below_threshold=below_threshold,
        retained=len(signals),
    )


def _details_from_result(result: CongressTickerResult, classification: str) -> dict[str, Any]:
    return {
        "model_version": result.model_version,
        "display_source": result.display_source,
        "original_category": result.category,
        "conviction": result.conviction,
        "entry_quality": result.entry,
        "base_conviction": result.base,
        "intentionality_score": result.intentionality_score,
        "materiality_score": result.materiality_score,
        "filer_abnormality_score": result.filer_abnormality_score,
        "accumulation_score": result.accumulation_score,
        "timeliness_score": result.timeliness_score,
        "role_relevance_score": result.role_relevance_score,
        "role_relevance_status": result.role_relevance_status,
        "role_confirmation": result.role_confirmation,
        "high_policy_access_flag": result.high_policy_access_flag,
        "role_evidence": result.role_evidence,
        "seniority_classes": result.seniority_classes,
        "seniority_multipliers": result.seniority_multipliers,
        "committee_ids": result.committee_ids,
        "committee_names": result.committee_names,
        "subcommittee_ids": result.subcommittee_ids,
        "agency_keys": result.agency_keys,
        "branches": result.branches,
        "chambers": result.chambers,
        "filers": result.filers,
        "bioguide_ids": result.bioguide_ids,
        "company_sector": result.company_sector,
        "company_industry": result.company_industry,
        "company_thematic_exposures": result.company_thematic_exposures,
        "company_classification_source": result.company_classification_source,
        "company_classification_confidence": result.company_classification_confidence,
        "asset_intent_classes": result.asset_intent_classes,
        "cluster_type": result.cluster_type,
        "sale_penalty": result.sale_penalty,
        "call_bonus": result.call_bonus,
        "put_penalty": result.put_penalty,
        "estimated_capital_low": result.low,
        "estimated_capital_mid": result.mid,
        "estimated_capital_high": result.high,
        "effective_capital": result.effective,
        "active_bullish_capital": result.active_bullish_capital,
        "historical_context_capital": result.historical_context_capital,
        "active_amount_low": result.active_amount_low,
        "active_amount_mid": result.active_amount_mid,
        "active_amount_high": result.active_amount_high,
        "call_premium_mid": result.call_mid,
        "put_premium_mid": result.put_mid,
        "buyers": result.buyers,
        "cluster_buyers": result.cluster_buyers,
        "weighted_age_days": result.weighted_age,
        "weighted_return": result.weighted_return,
        "flow": result.flow,
        "names": result.names,
        "unclear_sales": result.unclear_sales,
        "matched_sales": result.matched_sales,
        "matched_full_sales": result.matched_full_sales,
        "trigger_type": result.signal_trigger,
        "trigger_types": result.trigger_types,
        "transaction_date": result.transaction_dates[0] if result.transaction_dates else "",
        "filing_date": result.filing_dates[0] if result.filing_dates else "",
        "transaction_dates": result.transaction_dates,
        "filing_dates": result.filing_dates,
        "transaction_ages": result.transaction_ages,
        "filing_ages": result.filing_ages,
        "weighted_activity_multiplier": result.weighted_average_activity_weight,
        "active_trade_count": result.active_trade_count,
        "active_fresh_trade_count": result.active_fresh_trade_count,
        "active_late_disclosed_trade_count": result.active_late_disclosed_trade_count,
        "classification_source": classification,
        "source_payload_hash": result.source_payload_hash,
        "source_ids": result.source_ids,
        "filing_ids": result.filing_ids,
        "filing_types": result.filing_types,
        "document_urls": result.document_urls,
        "risk_flags": result.risk_flags,
    }


def result_to_signal(
    result: CongressTickerResult | dict[str, Any],
    observed_at: str | date | datetime,
    min_conviction: float = 15.0,
) -> Signal | None:
    """
    Convert one Congress engine ticker result into the common Signal format.

    - actionable, wait and risk results are retained;
    - other results become near_miss when conviction meets the threshold;
    - non-alertable results are excluded so the same disclosure is not surfaced
      repeatedly once a ledger is in place.
    """
    if isinstance(result, dict):
        result = CongressTickerResult(**result)

    if not result.alertable:
        return None

    if result.category in SUPPORTED_CATEGORIES:
        classification = result.category
    elif result.category == "other" and result.conviction >= float(min_conviction):
        classification = "near_miss"
    else:
        return None

    observed_datetime = _normalise_datetime(observed_at)
    valid_until = observed_datetime + timedelta(days=_remaining_validity_days(result))

    return Signal(
        ticker=result.ticker,
        scanner="congress",
        classification=classification,
        score=result.conviction,
        observed_at=observed_datetime.isoformat(),
        valid_until=valid_until.isoformat(),
        details=_details_from_result(result, classification),
    )


def _state_directory() -> Path:
    path = Path(
        os.getenv(
            "CONGRESS_STATE_DIR",
            "funnel_output/congress_state",
        )
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audit_directory() -> Path | None:
    raw = str(os.getenv("CONGRESS_AUDIT_DIR", "")).strip()
    if not raw:
        return None
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _list_or_empty(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ledger_path() -> Path:
    return _state_directory() / "transaction_ledger.json"


def _ledger_rows_from_state(ledger: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade_key, payload in sorted(ledger.items()):
        rows.append(
            {
                "Trade Key": trade_key,
                "Fingerprint": payload.get("fingerprint", ""),
                "Ticker": payload.get("ticker", ""),
                "Transaction Date": payload.get("transaction_date", ""),
                "Filing Date": payload.get("filing_date", ""),
                "Last Seen At": payload.get("last_seen_at", ""),
                "Last Seen Payload Hash": payload.get("last_seen_payload_hash", ""),
            }
        )
    return rows


def _ledger_state_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ledger: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_key = str(row.get("Trade Key") or "").strip()
        if not trade_key:
            continue
        ledger[trade_key] = {
            "fingerprint": str(row.get("Fingerprint") or "").strip(),
            "ticker": str(row.get("Ticker") or "").strip(),
            "transaction_date": str(row.get("Transaction Date") or "").strip(),
            "filing_date": str(row.get("Filing Date") or "").strip(),
            "last_seen_at": str(row.get("Last Seen At") or "").strip(),
            "last_seen_payload_hash": str(row.get("Last Seen Payload Hash") or "").strip(),
        }
    return ledger


def _sheet_ledger_context() -> _SheetLedgerContext | None:
    backend = str(os.getenv("CONGRESS_LEDGER_BACKEND", "auto")).strip().lower()
    if backend == "local":
        return None
    if not os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip():
        return None
    if not os.getenv("GOOGLE_SHEET_ID", "").strip():
        return None

    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_review_sheets(service, spreadsheet_id)
    return _SheetLedgerContext(service=service, spreadsheet_id=spreadsheet_id)


def _load_local_ledger() -> dict[str, dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Congress ledger JSON is invalid. Starting with an empty ledger.")
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_local_ledger(ledger: dict[str, dict[str, Any]]) -> None:
    _ledger_path().write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_ledger() -> tuple[dict[str, dict[str, Any]], _SheetLedgerContext | None]:
    try:
        context = _sheet_ledger_context()
    except Exception as exc:
        logger.warning("Congress sheet ledger unavailable, falling back to local JSON: %r", exc)
        context = None

    if context is None:
        return _load_local_ledger(), None

    rows = read_table(
        context.service,
        context.spreadsheet_id,
        CONGRESS_LEDGER_SHEET,
        CONGRESS_LEDGER_HEADERS,
    )
    return _ledger_state_from_rows(rows), context


def _save_ledger(
    ledger: dict[str, dict[str, Any]],
    context: _SheetLedgerContext | None,
) -> None:
    if context is None:
        _save_local_ledger(ledger)
        return

    upsert_records(
        context.service,
        context.spreadsheet_id,
        CONGRESS_LEDGER_SHEET,
        CONGRESS_LEDGER_HEADERS,
        "Trade Key",
        _ledger_rows_from_state(ledger),
    )


def _unique_transactions(scan: CongressScanResult) -> list[Any]:
    seen: dict[str, Any] = {}
    records = getattr(scan, "transactions", [])
    if not isinstance(records, list):
        return []
    for record in records:
        if record.trade_key not in seen:
            seen[record.trade_key] = record
    return [seen[key] for key in sorted(seen)]


def _event_row_from_record(record: Any, *, bootstrap_run: bool = False) -> dict[str, Any]:
    payload = {
        "trade_key": record.trade_key,
        "ticker": record.ticker,
        "filer_id": record.filer_id,
        "filer_name": record.filer_name,
        "owner_relationship": record.owner_relationship,
        "transaction_type": record.transaction_type,
        "asset_name": record.asset_name,
        "asset_class": record.asset_class,
        "action": record.action,
        "option_side": record.option_side,
        "strike": record.strike,
        "expiry": record.expiry,
        "amount_low": record.amount_range_low,
        "amount_mid_estimate": record.amount_range_mid,
        "amount_high": record.amount_range_high,
        "transaction_date": record.transaction_date,
        "filing_date": record.filing_date,
        "transaction_age": record.transaction_age,
        "filing_age": record.filing_age,
        "days_to_file": record.days_to_file,
        "document_url": record.doc_url,
        "filing_id": record.filing_id,
        "trigger_type": record.trigger_type,
        "is_new_discovery": record.is_new_discovery,
        "is_materially_amended": record.is_materially_amended,
    }
    payload["release_type"] = classify_release_type(payload, bootstrap_run=bootstrap_run)
    return payload


def _write_audit_bundle(scan: CongressScanResult, signals: list[Signal], run: CongressAdapterRun | None = None) -> None:
    audit_dir = _audit_directory()
    if audit_dir is None:
        return

    (audit_dir / "raw_payload.json").write_text(
        json.dumps(scan.raw_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    (audit_dir / "scan_counts.json").write_text(
        json.dumps(scan.counts, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (audit_dir / "review_audit.json").write_text(
        json.dumps(scan.review_audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (audit_dir / "ticker_results.json").write_text(
        json.dumps([result.to_dict() for result in scan.ticker_results], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (audit_dir / "signals.json").write_text(
        json.dumps([signal.to_dict() for signal in signals], ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    (audit_dir / "scan_metadata.json").write_text(
        json.dumps(asdict(scan.metadata), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (audit_dir / "audit_bundle.json").write_text(
        json.dumps(asdict(scan.audit_bundle), ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    (audit_dir / "scope.json").write_text(
        json.dumps({"scope_used": scan.scope_used}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if run is not None:
        (audit_dir / "political_digest_plan.json").write_text(
            json.dumps(run.digest_plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        (audit_dir / "political_ticker_histories.json").write_text(
            json.dumps(
                {
                    ticker: history.to_dict()
                    for ticker, history in sorted(run.current_ticker_histories.items())
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )


def run_congress_adapter_detailed(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
    observed_at: str | None = None,
) -> CongressAdapterRun:
    """
    Run the shared Congress engine and convert alertable results into Signals.

    The adapter persists a transaction ledger so previously seen disclosures
    can be suppressed on subsequent runs. Google Sheets is preferred when
    credentials are available; otherwise a local JSON fallback is used.
    """
    observed_datetime = _normalise_datetime(observed_at or datetime.now(UTC).isoformat())
    ledger, ledger_context = _load_ledger()
    archive_state = load_political_archive_state()
    previous_summary_rows = dict(archive_state.summary_rows)
    scan = run_live_scan(
        prior_ledger=ledger,
        branch_scope=os.getenv("POLITICAL_DISCLOSURE_SCOPE", "all"),
    )
    if persist_ledger:
        # Saving the ledger is best-effort. If Google Sheets rate-limits the
        # write or the API is temporarily unavailable, we must still return the
        # freshly-collected signals so the rest of the funnel can run. The
        # worst-case cost of skipping a save is one duplicate signal next cycle.
        # Narrow to transient Sheets / network errors — programmer bugs (e.g.
        # KeyError, AttributeError) should crash loudly during development.
        try:
            _save_ledger(scan.ledger, ledger_context)
        except _TRANSIENT_SHEETS_ERRORS as exc:
            logger.warning(
                "Congress ledger save failed; signals will still be returned: %r",
                exc,
            )

    signal_observed_at = observed_at or scan.metadata.fetched_at
    signals: list[Signal] = []
    for result in scan.ticker_results:
        signal = result_to_signal(
            result=result,
            observed_at=signal_observed_at,
            min_conviction=min_conviction,
        )
        if signal is not None:
            signals.append(signal)

    bootstrap_marker = get_bootstrap_marker(archive_state)
    bootstrap_run = not bootstrap_marker
    previous_payload_hash = get_bot_state_value(archive_state, LAST_PAYLOAD_HASH_KEY)
    previous_record_count = _int_or_zero(get_bot_state_value(archive_state, LAST_RECORD_COUNT_KEY))
    unique_records = _unique_transactions(scan)
    source_record_count = _int_or_zero(scan.counts.get("total_raw_records")) or _int_or_zero(getattr(scan.metadata, "record_count", 0))
    raw_update = prepare_raw_archive_upserts(
        unique_records,
        existing_rows=archive_state.raw_rows,
        observed_at=observed_datetime,
        payload_hash=scan.metadata.payload_sha256,
    )
    persist_raw_archive_updates(archive_state, raw_update)

    new_records = [
        _event_row_from_record(record, bootstrap_run=bootstrap_run)
        for record in unique_records
        if record.is_new_discovery
    ]
    material_amendments = [
        _event_row_from_record(record, bootstrap_run=bootstrap_run)
        for record in unique_records
        if record.is_materially_amended
    ]
    removed_records = list(raw_update.removed_events)
    trigger_events_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for event in [*new_records, *material_amendments, *removed_records]:
        ticker = str(event.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        trigger_events_by_ticker.setdefault(ticker, []).append(event)

    ticker_results_by_ticker = {result.ticker: result for result in scan.ticker_results}
    histories = build_ticker_histories(
        unique_records,
        observed_at=observed_datetime,
        ticker_results=ticker_results_by_ticker,
        previous_summary_rows=archive_state.summary_rows,
        trigger_events=trigger_events_by_ticker,
    )

    affected_tickers = sorted(
        {
            ticker
            for ticker, events in trigger_events_by_ticker.items()
            if events
        }
    )
    backfill_status = detect_backfill_status(
        bootstrap_run=bootstrap_run,
        new_records=new_records,
        material_amendments=material_amendments,
        removed_events=removed_records,
        affected_tickers=affected_tickers,
    )
    payload_refreshed = scan.metadata.payload_sha256 != previous_payload_hash
    source_health = "HEALTHY"
    if not payload_refreshed:
        source_health = "SOURCE_PAYLOAD_UNCHANGED"
    elif previous_record_count and source_record_count < previous_record_count:
        source_health = "SOURCE_PAYLOAD_INCOMPLETE"
    unnotified_trade_keys = {
        trade_key
        for trade_key, row in archive_state.raw_rows.items()
        if str(row.get("Notification Status") or "").strip().upper() != "NOTIFIED"
    }
    review_required_items = _list_or_empty(getattr(scan, "review_audit", []))
    excluded_items = _list_or_empty(getattr(getattr(scan, "audit_bundle", None), "excluded_record_reasons", []))
    digest_plan = build_digest_plan(
        histories=histories,
        affected_tickers=affected_tickers,
        backfill_status=backfill_status,
        previous_digest_rows=archive_state.digest_rows,
        previous_summary_rows=previous_summary_rows,
        digest_date=observed_datetime.date().isoformat(),
        archive_stats=build_archive_stats(
            raw_update,
            summary_written=len(histories),
            digest_logged=0,
            bootstrap_completed=bootstrap_run,
        ),
        observed_at=observed_datetime,
        review_required_items=review_required_items,
        excluded_items=excluded_items,
        source_health=source_health,
        payload_refreshed=payload_refreshed,
        source_record_count=source_record_count,
        unnotified_trade_keys=unnotified_trade_keys,
    )

    summary_rows = [
        summary_row_from_history(
            history,
            updated_at=observed_datetime.isoformat(),
            watchlist_state=digest_plan.current_watchlist_states.get(ticker),
        )
        for ticker, history in sorted(histories.items())
    ]
    persist_summary_rows(archive_state, summary_rows)

    archive_stats = build_archive_stats(
        raw_update,
        summary_written=len(summary_rows),
        digest_logged=0,
        bootstrap_completed=bootstrap_run,
    )

    if bootstrap_run:
        set_bootstrap_marker(archive_state, scan.metadata.payload_sha256)
    set_bot_state_value(archive_state, LAST_PAYLOAD_HASH_KEY, scan.metadata.payload_sha256)
    set_bot_state_value(archive_state, LAST_RECORD_COUNT_KEY, str(source_record_count))

    breakdown = _signal_breakdown(
        scan.ticker_results,
        signals,
        min_conviction=min_conviction,
    )

    logger.info(
        (
            "Political disclosure scan: raw=%d active=%d scored=%d alertable=%d "
            "already_seen=%d below_threshold=%d retained=%d hash=%s"
        ),
        scan.counts.get("total_raw_records", 0),
        scan.counts.get("active_tickers_before_market_checks", 0),
        scan.counts.get("scored_tickers", 0),
        breakdown.alertable,
        breakdown.already_seen_suppressed,
        breakdown.below_threshold,
        breakdown.retained,
        scan.metadata.payload_sha256,
    )

    run = CongressAdapterRun(
        scan=scan,
        signals=signals,
        analysed_tickers=scan.counts.get("scored_tickers", len(scan.ticker_results)),
        new_records=new_records,
        material_amendments=material_amendments,
        removed_records=removed_records,
        affected_tickers=affected_tickers,
        current_ticker_histories=histories,
        previous_ticker_states=previous_summary_rows,
        ranked_digest_flags=[flag.to_dict() for flag in digest_plan.new_material_flags],
        compact_digest_items=[flag.to_dict() for flag in digest_plan.other_new_activity],
        new_material_flags=[flag.to_dict() for flag in digest_plan.new_material_flags],
        material_updates=[flag.to_dict() for flag in digest_plan.material_updates],
        active_watchlist_items=[flag.to_dict() for flag in digest_plan.active_watchlist_items],
        other_new_activity=[flag.to_dict() for flag in digest_plan.other_new_activity],
        expired_watchlist_items=[flag.to_dict() for flag in digest_plan.expired_watchlist_items],
        watchlist_state_changes=[flag.to_dict() for flag in digest_plan.watchlist_state_changes],
        backfill_status=backfill_status,
        archive_stats=archive_stats,
        digest_plan=digest_plan,
        payload_hash=scan.metadata.payload_sha256,
    )
    _write_audit_bundle(scan, signals, run)
    return run


def run_congress_adapter(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
    observed_at: str | None = None,
) -> tuple[list[Signal], int]:
    """
    Return standardised signals and the total analysed ticker count.
    """
    run = run_congress_adapter_detailed(
        min_conviction=min_conviction,
        persist_ledger=persist_ledger,
        observed_at=observed_at,
    )
    return run.signals, run.analysed_tickers


def get_congress_signals(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
    observed_at: str | None = None,
) -> list[Signal]:
    """
    Compatibility function for existing funnel callers.
    """
    signals, _ = run_congress_adapter(
        min_conviction=min_conviction,
        persist_ledger=persist_ledger,
        observed_at=observed_at,
    )
    return signals
