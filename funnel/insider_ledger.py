from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import INSIDER_LEDGER_HEADERS, INSIDER_LEDGER_SHEET
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import read_table, upsert_records
from scanners.insider.engine import (
    QualifyingPurchase,
    build_transaction_group_key,
    build_transaction_key,
)


def _state_directory() -> Path:
    path = Path("funnel_output/insider_state")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_path() -> Path:
    return _state_directory() / "processed_accessions.json"


def _rows_path() -> Path:
    return _state_directory() / "ledger_rows.json"


def _read_sheet_rows() -> list[dict[str, str]]:
    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_review_sheets(service, spreadsheet_id)
    return read_table(service, spreadsheet_id, INSIDER_LEDGER_SHEET, INSIDER_LEDGER_HEADERS)


def _read_local_rows() -> list[dict[str, Any]]:
    path = _rows_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def load_processed_accessions() -> set[str]:
    if _sheet_backend_enabled():
        try:
            rows = _read_sheet_rows()
        except Exception:
            rows = []
        if rows:
            return {
                str(row.get("Accession") or "").strip()
                for row in rows
                if str(row.get("Accession") or "").strip()
            }
    path = _ledger_path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item).strip() for item in payload if str(item).strip()}


def save_processed_accessions(accessions: set[str]) -> None:
    _ledger_path().write_text(
        json.dumps(sorted(accessions), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sheet_backend_enabled() -> bool:
    backend = str(os.getenv("INSIDER_LEDGER_BACKEND", "auto")).strip().lower()
    if backend == "local":
        return False
    return bool(os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip() and os.getenv("GOOGLE_SHEET_ID", "").strip())


def _local_row_key(row: dict[str, Any]) -> str:
    transaction_key = str(row.get("Transaction Key") or row.get("transaction_key") or "").strip()
    if transaction_key:
        return transaction_key
    existing = str(row.get("Record Key") or "").strip()
    if existing:
        return existing
    payload = "|".join(
        [
            str(row.get("Accession") or "").strip(),
            str(row.get("Owner CIK") or "").strip(),
            str(row.get("Transaction Date") or "").strip(),
            str(row.get("Security Title") or "").strip(),
            str(row.get("Decision") or "").strip(),
            str(row.get("Reason") or "").strip(),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"insider-{digest}"


def _normalise_rows(rows: list[dict[str, Any]], *, observed_at: str) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for row in rows:
        record = {
            "Record Key": _local_row_key(row),
            "Transaction Key": row.get("Transaction Key", row.get("transaction_key", "")),
            "Transaction Group Key": row.get("Transaction Group Key", row.get("transaction_group_key", "")),
            "Source Fingerprint": row.get("Source Fingerprint", row.get("source_fingerprint", "")),
            "Accession": row.get("Accession", row.get("accession", "")),
            "Issuer CIK": row.get("Issuer CIK", row.get("issuer_cik", "")),
            "Ticker": row.get("Ticker", row.get("ticker", "")),
            "Owner CIK": row.get("Owner CIK", row.get("owner_cik", "")),
            "Owner Name": row.get("Owner Name", row.get("owner_name", "")),
            "Owner Role": row.get("Owner Role", row.get("owner_role", "")),
            "Officer Title": row.get("Officer Title", row.get("officer_title", "")),
            "Owner Is Operating": row.get("Owner Is Operating", row.get("owner_is_operating", "")),
            "Owner Is Director": row.get("Owner Is Director", row.get("owner_is_director", "")),
            "Owner Is Officer": row.get("Owner Is Officer", row.get("owner_is_officer", "")),
            "Owner Is Ten Percent Owner": row.get("Owner Is Ten Percent Owner", row.get("owner_is_ten_percent_owner", "")),
            "Transaction Date": row.get("Transaction Date", row.get("transaction_date", "")),
            "Filing Date": row.get("Filing Date", row.get("filing_date", "")),
            "Security Title": row.get("Security Title", row.get("security_title", "")),
            "Shares": row.get("Shares", row.get("shares", "")),
            "Price Per Share": row.get("Price Per Share", row.get("price_per_share", "")),
            "Transaction Value": row.get("Transaction Value", row.get("transaction_value", "")),
            "Direct Or Indirect": row.get("Direct Or Indirect", row.get("direct_or_indirect", "")),
            "Plan 10b5-1": row.get("Plan 10b5-1", row.get("plan_10b5_1", "")),
            "Shares Owned After": row.get("Shares Owned After", row.get("shares_owned_after", "")),
            "Decision": row.get("Decision", row.get("decision", "")),
            "Reason": row.get("Reason", row.get("reason", "")),
            "Confidence": row.get("Confidence", row.get("confidence", "")),
            "Qualification Decision": row.get("Qualification Decision", row.get("qualification_decision", row.get("Decision", row.get("decision", "")))),
            "Qualification Reason": row.get("Qualification Reason", row.get("qualification_reason", row.get("Reason", row.get("reason", "")))),
            "Superseded By": row.get("Superseded By", row.get("superseded_by", "")),
            "Observed At": row.get("Observed At", row.get("observed_at", observed_at)),
        }
        normalised.append(record)
    return normalised


def _bool_from_row(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _float_from_row(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_purchase_row(row: dict[str, Any]) -> QualifyingPurchase | None:
    decision = str(row.get("Qualification Decision") or row.get("Decision") or "").strip().upper()
    superseded_by = str(row.get("Superseded By") or "").strip()
    if decision != "QUALIFIED" or superseded_by:
        return None
    transaction_date_text = str(row.get("Transaction Date") or "").strip()
    try:
        transaction_date = date.fromisoformat(transaction_date_text[:10])
    except ValueError:
        return None
    filing_date_text = str(row.get("Filing Date") or "").strip()
    filing_date = None
    if filing_date_text:
        try:
            filing_date = date.fromisoformat(filing_date_text[:10])
        except ValueError:
            filing_date = None
    shares = _float_from_row(row.get("Shares")) or 0.0
    price_per_share = _float_from_row(row.get("Price Per Share")) or 0.0
    transaction_value = _float_from_row(row.get("Transaction Value"))
    if transaction_value is None:
        transaction_value = shares * price_per_share
    shares_owned_after = _float_from_row(row.get("Shares Owned After"))
    owner_role = str(row.get("Owner Role") or row.get("Reason") or "").strip() or "Other"
    confidence = str(row.get("Confidence") or "").strip() or "UNKNOWN"
    if confidence == "UNKNOWN":
        confidence = "OPEN_MARKET_MEDIUM_CONFIDENCE"
    direct_or_indirect = str(row.get("Direct Or Indirect") or "").strip().upper() or "?"
    issuer_cik = str(row.get("Issuer CIK") or "").strip()
    owner_cik = str(row.get("Owner CIK") or "").strip()
    accession = str(row.get("Accession") or "").strip()
    security_title = str(row.get("Security Title") or "").strip()
    transaction_group_key = str(row.get("Transaction Group Key") or "").strip() or build_transaction_group_key(
        issuer_cik=issuer_cik,
        owner_cik=owner_cik,
        transaction_date=transaction_date,
        security_title=security_title,
        direct_or_indirect=direct_or_indirect,
    )
    transaction_key = str(row.get("Transaction Key") or "").strip() or build_transaction_key(
        issuer_cik=issuer_cik,
        owner_cik=owner_cik,
        accession=accession,
        transaction_date=transaction_date,
        security_title=security_title,
        direct_or_indirect=direct_or_indirect,
        shares=shares,
        price_per_share=price_per_share,
    )
    return QualifyingPurchase(
        ticker=str(row.get("Ticker") or "").strip().upper(),
        issuer_cik=issuer_cik,
        accession=accession,
        owner_cik=owner_cik,
        owner_name=str(row.get("Owner Name") or "").strip(),
        owner_role=owner_role,
        owner_is_operating=_bool_from_row(row.get("Owner Is Operating")) or (
            _bool_from_row(row.get("Owner Is Officer")) and owner_role.lower() != "director"
        ),
        transaction_date=transaction_date,
        security_title=security_title,
        shares=shares,
        price_per_share=price_per_share,
        transaction_value=transaction_value,
        direct_or_indirect=direct_or_indirect,
        plan_10b5_1=_bool_from_row(row.get("Plan 10b5-1")),
        confidence=confidence,
        shares_owned_after=shares_owned_after,
        transaction_row_count=max(1, int(_float_from_row(row.get("Transaction Row Count")) or 1)),
        footnotes=[],
        owner_is_director=_bool_from_row(row.get("Owner Is Director")),
        owner_is_officer=_bool_from_row(row.get("Owner Is Officer")),
        owner_is_ten_percent_owner=_bool_from_row(row.get("Owner Is Ten Percent Owner")),
        officer_title=str(row.get("Officer Title") or "").strip(),
        filing_date=filing_date,
        qualification_decision=decision,
        qualification_reason=str(row.get("Qualification Reason") or row.get("Reason") or "").strip(),
        observed_at=str(row.get("Observed At") or "").strip(),
        transaction_key=transaction_key,
        transaction_group_key=transaction_group_key,
        source_fingerprint=str(row.get("Source Fingerprint") or "").strip() or transaction_key,
        is_current_trigger=False,
    )


def load_qualified_purchases(*, since: date) -> list[QualifyingPurchase]:
    rows: list[dict[str, Any]]
    if _sheet_backend_enabled():
        try:
            rows = _read_sheet_rows()
        except Exception:
            rows = _read_local_rows()
    else:
        rows = _read_local_rows()

    purchases: list[QualifyingPurchase] = []
    for row in rows:
        purchase = _parse_purchase_row(row)
        if purchase is None:
            continue
        if purchase.transaction_date < since:
            continue
        purchases.append(purchase)
    return purchases


def _apply_supersession(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        rows_by_key[_local_row_key(row)] = dict(row)

    latest_by_group: dict[str, dict[str, Any]] = {}
    for row in rows_by_key.values():
        if str(row.get("Qualification Decision") or row.get("Decision") or "").strip().upper() != "QUALIFIED":
            continue
        group_key = str(row.get("Transaction Group Key") or "").strip()
        if not group_key:
            continue
        current = latest_by_group.get(group_key)
        if current is None:
            latest_by_group[group_key] = row
            continue
        current_rank = (
            str(current.get("Filing Date") or ""),
            str(current.get("Observed At") or ""),
            str(current.get("Accession") or ""),
        )
        row_rank = (
            str(row.get("Filing Date") or ""),
            str(row.get("Observed At") or ""),
            str(row.get("Accession") or ""),
        )
        if row_rank >= current_rank:
            latest_by_group[group_key] = row

    for group_key, latest in latest_by_group.items():
        latest_key = _local_row_key(latest)
        latest_txn = str(latest.get("Transaction Key") or "").strip()
        for row in rows_by_key.values():
            if str(row.get("Transaction Group Key") or "").strip() != group_key:
                continue
            row_key = _local_row_key(row)
            if row_key == latest_key:
                row["Superseded By"] = ""
            else:
                row["Superseded By"] = latest_txn or latest_key
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def _persist_rows_local(rows: list[dict[str, Any]]) -> None:
    path = _rows_path()
    existing_by_key: dict[str, dict[str, Any]] = {}
    for item in _read_local_rows():
        key = _local_row_key(item)
        existing_by_key[key] = item
    for row in rows:
        existing_by_key[_local_row_key(row)] = row
    merged_rows = _apply_supersession(list(existing_by_key.values()))
    path.write_text(
        json.dumps(
            merged_rows,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def persist_ledger_rows(rows: list[dict[str, Any]], *, observed_at: str) -> None:
    if not rows:
        return
    normalised = _normalise_rows(rows, observed_at=observed_at)
    if not _sheet_backend_enabled():
        _persist_rows_local(normalised)
        return

    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    ensure_review_sheets(service, spreadsheet_id)
    existing = read_table(service, spreadsheet_id, INSIDER_LEDGER_SHEET, INSIDER_LEDGER_HEADERS)
    merged = _apply_supersession(existing + normalised)
    upsert_records(
        service,
        spreadsheet_id,
        INSIDER_LEDGER_SHEET,
        INSIDER_LEDGER_HEADERS,
        "Record Key",
        merged,
    )
