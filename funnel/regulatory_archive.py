from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.regulatory_schema import (
    REGULATORY_CURRENT_HEADERS,
    REGULATORY_CURRENT_SHEET,
    REGULATORY_DIGEST_LOG_HEADERS,
    REGULATORY_DIGEST_LOG_SHEET,
    REGULATORY_EVENTS_RAW_HEADERS,
    REGULATORY_EVENTS_RAW_SHEET,
    REGULATORY_PROGRAM_REGISTRY_HEADERS,
    REGULATORY_PROGRAM_REGISTRY_SHEET,
    REGULATORY_SOURCE_STATE_HEADERS,
    REGULATORY_SOURCE_STATE_SHEET,
)
from funnel.regulatory_setup import ensure_regulatory_sheets
from funnel.sheet_table import append_records, read_table, upsert_records
from research.regulatory.identifiers import build_unresolved_issue_id


@dataclass
class RegulatoryArchiveState:
    context: Any | None
    source_state: dict[str, dict[str, Any]]
    events_raw: dict[str, dict[str, Any]]
    program_registry: dict[str, dict[str, Any]]
    current_state: dict[str, dict[str, Any]]
    unresolved: dict[str, dict[str, Any]]
    digest_log: list[dict[str, Any]]


def _state_directory() -> Path:
    path = Path(os.getenv("REGULATORY_STATE_DIR", "funnel_output/regulatory_state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audit_directory() -> Path:
    path = Path(os.getenv("REGULATORY_AUDIT_DIR", "funnel_output/regulatory_audit"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sheet_backend_enabled() -> bool:
    backend = str(os.getenv("REGULATORY_STATE_BACKEND", "auto")).strip().lower()
    if backend == "local":
        return False
    if backend == "sheets":
        return True
    return bool(os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip() and os.getenv("GOOGLE_SHEET_ID", "").strip())


def _json_path(name: str) -> Path:
    return _state_directory() / name


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    os.close(handle)
    tmp = Path(temp_path)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def archive_raw_payload(*, source_name: str, raw_event_id: str, payload_hash: str, payload: dict[str, Any]) -> str:
    source_dir = _audit_directory() / "raw_payloads" / source_name
    source_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{raw_event_id or payload_hash}.json"
    path = source_dir / file_name
    _atomic_save_json(path, payload)
    return str(path)


def load_regulatory_archive_state() -> RegulatoryArchiveState:
    if _sheet_backend_enabled():
        try:
            service = get_sheets_service(readonly=False)
            spreadsheet_id = get_spreadsheet_id()
            ensure_regulatory_sheets(service, spreadsheet_id)
            return RegulatoryArchiveState(
                context={"service": service, "spreadsheet_id": spreadsheet_id},
                source_state={row["Source Name"]: row for row in read_table(service, spreadsheet_id, REGULATORY_SOURCE_STATE_SHEET, REGULATORY_SOURCE_STATE_HEADERS) if row.get("Source Name")},
                events_raw={row["Raw Event ID"]: row for row in read_table(service, spreadsheet_id, REGULATORY_EVENTS_RAW_SHEET, REGULATORY_EVENTS_RAW_HEADERS) if row.get("Raw Event ID")},
                program_registry={row["Programme Key"]: row for row in read_table(service, spreadsheet_id, REGULATORY_PROGRAM_REGISTRY_SHEET, REGULATORY_PROGRAM_REGISTRY_HEADERS) if row.get("Programme Key")},
                current_state={row["Programme Key"]: row for row in read_table(service, spreadsheet_id, REGULATORY_CURRENT_SHEET, REGULATORY_CURRENT_HEADERS) if row.get("Programme Key")},
                unresolved={},
                digest_log=read_table(service, spreadsheet_id, REGULATORY_DIGEST_LOG_SHEET, REGULATORY_DIGEST_LOG_HEADERS),
            )
        except Exception:
            pass
    return RegulatoryArchiveState(
        context=None,
        source_state={row["Source Name"]: row for row in _load_json(_json_path("source_state.json"), default=[]) if isinstance(row, dict) and row.get("Source Name")},
        events_raw={row["Raw Event ID"]: row for row in _load_json(_json_path("events_raw.json"), default=[]) if isinstance(row, dict) and row.get("Raw Event ID")},
        program_registry={row["Programme Key"]: row for row in _load_json(_json_path("program_registry.json"), default=[]) if isinstance(row, dict) and row.get("Programme Key")},
        current_state={row["Programme Key"]: row for row in _load_json(_json_path("current_state.json"), default=[]) if isinstance(row, dict) and row.get("Programme Key")},
        unresolved={row["Unresolved ID"]: row for row in _load_json(_json_path("unresolved.json"), default=[]) if isinstance(row, dict) and row.get("Unresolved ID")},
        digest_log=[row for row in _load_json(_json_path("digest_log.json"), default=[]) if isinstance(row, dict)],
    )


def _persist_upsert_rows(state: RegulatoryArchiveState, *, sheet_name: str, headers: list[str], key_header: str, rows: list[dict[str, Any]], local_file: str, local_index_attr: str) -> None:
    if not rows:
        return
    if state.context is None:
        merged = dict(getattr(state, local_index_attr))
        for row in rows:
            merged[str(row.get(key_header) or "").strip()] = row
        _atomic_save_json(_json_path(local_file), list(merged.values()))
        setattr(state, local_index_attr, merged)
        return
    upsert_records(state.context["service"], state.context["spreadsheet_id"], sheet_name, headers, key_header, rows)


def _persist_append_rows(state: RegulatoryArchiveState, *, sheet_name: str, headers: list[str], rows: list[dict[str, Any]], local_file: str, local_attr: str) -> None:
    if not rows:
        return
    if state.context is None:
        merged = list(getattr(state, local_attr)) + rows
        _atomic_save_json(_json_path(local_file), merged)
        setattr(state, local_attr, merged)
        return
    append_records(state.context["service"], state.context["spreadsheet_id"], sheet_name, headers, rows)


def _coalesce_unresolved_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        source_name = str(row.get("Source Name") or "")
        source_record_id = str(row.get("Source Record ID") or "")
        if source_name.startswith("http") or source_record_id == "Exact company mapping unavailable.":
            return {
                "Unresolved ID": row.get("Unresolved ID", ""),
                "Raw Event ID": row.get("Raw Event ID", ""),
                "Source Record ID": "",
                "Reason": row.get("Source Record ID", ""),
                "Source Name": row.get("Reason", ""),
                "Source URL": row.get("Source Name", ""),
                "Company Name": row.get("Source URL", ""),
                "Ticker": row.get("Company Name", ""),
                "Trial NCT ID": row.get("Ticker", ""),
                "Product Name": row.get("Trial NCT ID", ""),
                "Required Action": row.get("Product Name", "") or "MANUAL_REVIEW_REQUIRED",
                "Conflicting Source": row.get("Required Action", ""),
                "Created At": row.get("Conflicting Source", ""),
            }
        return dict(row)

    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized = _normalize(row)
        issue_id = build_unresolved_issue_id(
            source_name=str(normalized.get("Source Name") or ""),
            source_record_id=str(normalized.get("Source Record ID") or ""),
            company_name=str(normalized.get("Company Name") or ""),
            ticker=str(normalized.get("Ticker") or ""),
            trial_nct_id=str(normalized.get("Trial NCT ID") or ""),
            product_name=str(normalized.get("Product Name") or ""),
            reason=str(normalized.get("Reason") or ""),
        )
        normalized["Unresolved ID"] = issue_id
        existing = merged.get(issue_id)
        if existing is None or str(normalized.get("Created At") or "") >= str(existing.get("Created At") or ""):
            merged[issue_id] = normalized
    return sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("Created At") or ""),
            str(row.get("Source Name") or ""),
            str(row.get("Company Name") or ""),
            str(row.get("Product Name") or ""),
        ),
        reverse=True,
    )


def persist_source_state(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    _persist_upsert_rows(state, sheet_name=REGULATORY_SOURCE_STATE_SHEET, headers=REGULATORY_SOURCE_STATE_HEADERS, key_header="Source Name", rows=rows, local_file="source_state.json", local_index_attr="source_state")


def persist_raw_events(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    _persist_upsert_rows(state, sheet_name=REGULATORY_EVENTS_RAW_SHEET, headers=REGULATORY_EVENTS_RAW_HEADERS, key_header="Raw Event ID", rows=rows, local_file="events_raw.json", local_index_attr="events_raw")


def persist_program_registry(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    _persist_upsert_rows(state, sheet_name=REGULATORY_PROGRAM_REGISTRY_SHEET, headers=REGULATORY_PROGRAM_REGISTRY_HEADERS, key_header="Programme Key", rows=rows, local_file="program_registry.json", local_index_attr="program_registry")


def persist_current_state(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    _persist_upsert_rows(state, sheet_name=REGULATORY_CURRENT_SHEET, headers=REGULATORY_CURRENT_HEADERS, key_header="Programme Key", rows=rows, local_file="current_state.json", local_index_attr="current_state")


def persist_unresolved(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    if not rows and state.context is None:
        return
    existing_rows = list(state.unresolved.values())
    merged_rows = _coalesce_unresolved_rows(existing_rows + rows)
    if state.context is None:
        merged = {str(row.get("Unresolved ID") or "").strip(): row for row in merged_rows if str(row.get("Unresolved ID") or "").strip()}
        _atomic_save_json(_json_path("unresolved.json"), merged_rows)
        state.unresolved = merged
        return
    state.unresolved = {str(row.get("Unresolved ID") or "").strip(): row for row in merged_rows if str(row.get("Unresolved ID") or "").strip()}


def persist_digest_log(state: RegulatoryArchiveState, rows: list[dict[str, Any]]) -> None:
    _persist_append_rows(state, sheet_name=REGULATORY_DIGEST_LOG_SHEET, headers=REGULATORY_DIGEST_LOG_HEADERS, rows=rows, local_file="digest_log.json", local_attr="digest_log")
