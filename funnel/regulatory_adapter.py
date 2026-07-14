from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from funnel.regulatory_archive import (
    archive_raw_payload,
    load_regulatory_archive_state,
    persist_current_state,
    persist_digest_log,
    persist_program_registry,
    persist_raw_events,
    persist_source_state,
    persist_unresolved,
)
from research.regulatory.config import RegulatoryMonitorConfig
from research.regulatory.engine import RegulatoryMonitorEngine, RegulatoryRunResult
from tactical.regulatory_digest import render_digest, write_digest_preview


@dataclass
class RegulatoryAdapterResult:
    run_result: RegulatoryRunResult
    preview_path: str
    digest_text: str | None


def _source_state_rows(run_result: RegulatoryRunResult) -> list[dict[str, Any]]:
    rows = []
    for item in run_result.source_checkpoints:
        rows.append(
            {
                "Source Name": item.source_name,
                "Cursor": item.cursor,
                "Last Success At": item.last_success_at,
                "Last Event At": item.last_event_at,
                "Bootstrap Complete": "YES" if item.bootstrap_complete else "NO",
                "Metadata JSON": json.dumps(item.metadata, sort_keys=True),
            }
        )
    return rows


def _raw_rows(run_result: RegulatoryRunResult) -> list[dict[str, Any]]:
    rows = []
    for item in run_result.raw_records:
        rows.append(
            {
                "Raw Event ID": item.raw_event_id,
                "Source Name": item.source_name,
                "Source Record ID": item.source_record_id,
                "Source URL": item.source_url,
                "Source Document Type": item.source_document_type,
                "Source Tier": item.source_tier.value,
                "Published At": item.published_at,
                "Observed At": item.observed_at,
                "Event Type": item.event_type,
                "Company Name": item.company_name,
                "Ticker": item.ticker,
                "CIK": item.cik,
                "Product Name": item.product_name,
                "Indication Name": item.indication_name,
                "Regimen Name": item.regimen_name,
                "Trial NCT ID": item.trial_nct_id,
                "Jurisdiction": item.jurisdiction,
                "Exact Text": item.exact_text[:4000],
                "Payload Hash": item.payload_hash,
                "Payload Path": item.payload_path,
                "Amendment Of": item.amendment_of,
                "Version": item.version,
                "Active": "YES" if item.active else "NO",
            }
        )
    return rows


def _programme_rows(run_result: RegulatoryRunResult) -> list[dict[str, Any]]:
    return [
        {
            "Programme Key": item.programme_key,
            "Company ID": item.company_id,
            "Economic Owner ID": item.economic_owner_id,
            "Product ID": item.product_id,
            "Regimen ID": item.regimen_id,
            "Indication ID": item.indication_id,
            "Trial ID": item.trial_id,
            "Jurisdiction": item.jurisdiction,
            "Company Name": item.company_name,
            "Ticker": item.ticker,
            "Product Name": item.product_name,
            "Indication Name": item.indication_name,
        }
        for item in run_result.programmes.values()
    ]


def _current_rows(run_result: RegulatoryRunResult) -> list[dict[str, Any]]:
    return [
        {
            "Programme Key": item.programme_key,
            "Company ID": item.company_id,
            "Product ID": item.product_id,
            "Indication ID": item.indication_id,
            "Clinical Evidence": item.clinical_evidence,
            "Trial Operations": item.trial_operations,
            "Regulatory": item.regulatory,
            "CMC": item.cmc,
            "Commercial": item.commercial,
            "Reimbursement": item.reimbursement,
            "Development Status": item.development_status,
            "Legal IP": item.legal_ip,
            "Last Event ID": item.last_event_id,
            "Last Updated At": item.last_updated_at,
            "Current Gate": item.current_gate,
            "Next Catalyst": item.next_catalyst,
            "Catalyst Date": item.catalyst_date,
            "Date Precision": item.date_precision,
        }
        for item in run_result.current_states.values()
    ]


def _unresolved_rows(run_result: RegulatoryRunResult) -> list[dict[str, Any]]:
    return [
        {
            "Unresolved ID": item.unresolved_id,
            "Raw Event ID": item.raw_event_id,
            "Source Record ID": item.source_record_id,
            "Reason": item.reason,
            "Source Name": item.source_name,
            "Source URL": item.source_url,
            "Company Name": item.company_name,
            "Ticker": item.ticker,
            "Trial NCT ID": item.trial_nct_id,
            "Product Name": item.product_name,
            "Required Action": item.required_action,
            "Conflicting Source": item.conflicting_source,
            "Created At": item.created_at,
        }
        for item in run_result.unresolved
    ]


def _digest_rows(run_result: RegulatoryRunResult, *, preview_path: str, telegram_included: bool, telegram_status: str) -> list[dict[str, Any]]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat() + "Z"
    plan = run_result.digest_plan
    if plan is None:
        return []
    rows = []
    for collection in (plan.material_events, plan.state_updates):
        for item in collection:
            rows.append(
                {
                    "Digest Date": plan.digest_date,
                    "Event ID": item.event_id,
                    "Ticker": item.ticker,
                    "Company Name": item.company_name,
                    "Product Name": item.product_name,
                    "Indication Name": item.indication_name,
                    "Event Summary": item.event_summary,
                    "Gate Change": item.gate_change,
                    "Outcome": item.outcome.value,
                    "Priority": item.priority.value,
                    "Detailed": "YES" if item.detailed else "NO",
                    "Summary Hash": item.summary_hash,
                    "State Hash": item.state_hash,
                    "Telegram Included": "YES" if telegram_included else "NO",
                    "Telegram Delivery Status": telegram_status,
                    "Telegram Sent At": now if telegram_included else "",
                    "Preview Path": preview_path,
                    "Created At": now,
                }
            )
    return rows


def persist_digest_delivery(
    *,
    run_result: RegulatoryRunResult,
    preview_path: str,
    telegram_included: bool,
    telegram_status: str,
) -> None:
    state = load_regulatory_archive_state()
    persist_digest_log(
        state,
        _digest_rows(
            run_result,
            preview_path=preview_path,
            telegram_included=telegram_included,
            telegram_status=telegram_status,
        ),
    )


def run_regulatory_adapter(
    *,
    config: RegulatoryMonitorConfig | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    preview_path: str | None = None,
    telegram_included: bool = False,
    telegram_status: str = "PENDING",
) -> RegulatoryAdapterResult:
    cfg = config or RegulatoryMonitorConfig.from_env()
    state = load_regulatory_archive_state()
    previous_hashes = {str(row.get("Summary Hash") or "").strip() for row in state.digest_log if str(row.get("Summary Hash") or "").strip()}
    engine = RegulatoryMonitorEngine(config=cfg)
    run_result = engine.run(since=since, until=until, previous_digest_hashes=previous_hashes)
    for record in run_result.raw_records:
        record.payload_path = archive_raw_payload(
            source_name=record.source_name,
            raw_event_id=record.raw_event_id,
            payload_hash=record.payload_hash,
            payload=record.raw_payload,
        )
    digest_text = render_digest(run_result.digest_plan) if run_result.digest_plan is not None else None
    preview = preview_path or str(Path(cfg.audit_dir) / "regulatory_digest_preview.txt")
    write_digest_preview(Path(preview), digest_text)
    persist_source_state(state, _source_state_rows(run_result))
    persist_raw_events(state, _raw_rows(run_result))
    persist_program_registry(state, _programme_rows(run_result))
    persist_current_state(state, _current_rows(run_result))
    persist_unresolved(state, _unresolved_rows(run_result))
    return RegulatoryAdapterResult(run_result=run_result, preview_path=preview, digest_text=digest_text)
