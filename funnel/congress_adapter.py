# VERSION: 2026-06-24-CONGRESS-ADAPTER-V2
# Funnel adapter for the shared Congress scanner engine.

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import CONGRESS_LEDGER_HEADERS, CONGRESS_LEDGER_SHEET
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import read_table, upsert_records
from funnel.signal_schema import Signal
from scanners.congress.engine import (
    MODEL_VERSION,
    CongressScanResult,
    CongressTickerResult,
    run_live_scan,
)


logger = logging.getLogger(__name__)


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
        "original_category": result.category,
        "conviction": result.conviction,
        "entry_quality": result.entry,
        "base_conviction": result.base,
        "sale_penalty": result.sale_penalty,
        "call_bonus": result.call_bonus,
        "put_penalty": result.put_penalty,
        "estimated_capital_low": result.low,
        "estimated_capital_mid": result.mid,
        "estimated_capital_high": result.high,
        "effective_capital": result.effective,
        "active_bullish_capital": result.active_bullish_capital,
        "historical_context_capital": result.historical_context_capital,
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


def _write_audit_bundle(scan: CongressScanResult, signals: list[Signal]) -> None:
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


def run_congress_adapter_detailed(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
) -> CongressAdapterRun:
    """
    Run the shared Congress engine and convert alertable results into Signals.

    The adapter persists a transaction ledger so previously seen disclosures
    can be suppressed on subsequent runs. Google Sheets is preferred when
    credentials are available; otherwise a local JSON fallback is used.
    """
    ledger, ledger_context = _load_ledger()
    scan = run_live_scan(prior_ledger=ledger)
    if persist_ledger:
        _save_ledger(scan.ledger, ledger_context)

    observed_at = scan.metadata.fetched_at
    signals: list[Signal] = []
    for result in scan.ticker_results:
        signal = result_to_signal(
            result=result,
            observed_at=observed_at,
            min_conviction=min_conviction,
        )
        if signal is not None:
            signals.append(signal)

    _write_audit_bundle(scan, signals)
    breakdown = _signal_breakdown(
        scan.ticker_results,
        signals,
        min_conviction=min_conviction,
    )

    logger.info(
        (
            "Congress engine scan: raw=%d active=%d scored=%d alertable=%d "
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

    return CongressAdapterRun(
        scan=scan,
        signals=signals,
        analysed_tickers=scan.counts.get("scored_tickers", len(scan.ticker_results)),
    )


def run_congress_adapter(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
) -> tuple[list[Signal], int]:
    """
    Return standardised signals and the total analysed ticker count.
    """
    run = run_congress_adapter_detailed(
        min_conviction=min_conviction,
        persist_ledger=persist_ledger,
    )
    return run.signals, run.analysed_tickers


def get_congress_signals(
    min_conviction: float = 15.0,
    *,
    persist_ledger: bool = True,
) -> list[Signal]:
    """
    Compatibility function for existing funnel callers.
    """
    signals, _ = run_congress_adapter(
        min_conviction=min_conviction,
        persist_ledger=persist_ledger,
    )
    return signals
