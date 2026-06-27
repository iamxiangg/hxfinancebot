from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import INSIDER_LEDGER_HEADERS, INSIDER_LEDGER_SHEET
from funnel.review_setup import ensure_review_sheets
from funnel.sheet_table import upsert_records


def _ledger_path() -> Path:
    path = Path("funnel_output/insider_state")
    path.mkdir(parents=True, exist_ok=True)
    return path / "processed_accessions.json"


def _rows_path() -> Path:
    path = Path("funnel_output/insider_state")
    path.mkdir(parents=True, exist_ok=True)
    return path / "ledger_rows.json"


def load_processed_accessions() -> set[str]:
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
            "Accession": row.get("Accession", row.get("accession", "")),
            "Ticker": row.get("Ticker", row.get("ticker", "")),
            "Owner CIK": row.get("Owner CIK", row.get("owner_cik", "")),
            "Owner Name": row.get("Owner Name", row.get("owner_name", "")),
            "Transaction Date": row.get("Transaction Date", row.get("transaction_date", "")),
            "Security Title": row.get("Security Title", row.get("security_title", "")),
            "Shares": row.get("Shares", row.get("shares", "")),
            "Price Per Share": row.get("Price Per Share", row.get("price_per_share", "")),
            "Transaction Value": row.get("Transaction Value", row.get("transaction_value", "")),
            "Direct Or Indirect": row.get("Direct Or Indirect", row.get("direct_or_indirect", "")),
            "Decision": row.get("Decision", row.get("decision", "")),
            "Reason": row.get("Reason", row.get("reason", "")),
            "Confidence": row.get("Confidence", row.get("confidence", "")),
            "Observed At": row.get("Observed At", row.get("observed_at", observed_at)),
        }
        normalised.append(record)
    return normalised


def _persist_rows_local(rows: list[dict[str, Any]]) -> None:
    path = _rows_path()
    existing_by_key: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    key = _local_row_key(item)
                    existing_by_key[key] = item
    for row in rows:
        existing_by_key[_local_row_key(row)] = row
    path.write_text(
        json.dumps(
            [existing_by_key[key] for key in sorted(existing_by_key)],
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
    upsert_records(
        service,
        spreadsheet_id,
        INSIDER_LEDGER_SHEET,
        INSIDER_LEDGER_HEADERS,
        "Record Key",
        normalised,
    )
