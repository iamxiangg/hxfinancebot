from __future__ import annotations

from datetime import datetime

import requests

from providers.regulatory.base import SourceBatch
from research.regulatory.identifiers import build_raw_event_id
from research.regulatory.models import RawRegulatoryRecord, SourceTier


class DrugsAtFDAProvider:
    source_name = "drugs_at_fda"
    endpoint = "https://api.fda.gov/drug/drugsfda.json"

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        query = f'submissions.submission_status_date:[{since.strftime("%Y%m%d")} TO {until.strftime("%Y%m%d")}]'
        try:
            response = self.session.get(self.endpoint, params={"search": query, "limit": 50}, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return SourceBatch(source_status="ERROR", errors=[f"Drugs@FDA fetch failed: {exc.__class__.__name__}"])
        records: list[RawRegulatoryRecord] = []
        for row in payload.get("results", []) if isinstance(payload, dict) else []:
            submissions = row.get("submissions", []) or []
            for submission in submissions:
                status_date = str(submission.get("submission_status_date") or "")
                raw_event_id = build_raw_event_id(
                    source=self.source_name,
                    source_record_id=str(row.get("application_number") or ""),
                    source_event_type=str(submission.get("submission_type") or "FDA_SUBMISSION"),
                    source_publication_date=status_date,
                )
                sponsor = str(row.get("sponsor_name") or "")
                records.append(
                    RawRegulatoryRecord(
                        raw_event_id=raw_event_id,
                        source_name=self.source_name,
                        source_record_id=str(row.get("application_number") or ""),
                        source_url=self.endpoint,
                        source_document_type="DRUGS_FDA",
                        source_tier=SourceTier.TIER_1,
                        published_at=status_date,
                        observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                        event_type="FDA_APPLICATION",
                        company_name=sponsor,
                        product_name=str(((row.get("products") or [{}])[0]).get("brand_name") or ""),
                        raw_payload=row,
                        structured_data={
                            "application_number": str(row.get("application_number") or ""),
                            "submission_status": str(submission.get("submission_status") or ""),
                            "submission_type": str(submission.get("submission_type") or ""),
                            "status_date": status_date,
                            "status_text": str(submission.get("submission_status") or ""),
                        },
                    )
                )
        return SourceBatch(records=records, metadata={"record_count": len(records)})
