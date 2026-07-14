from __future__ import annotations

from datetime import datetime

import requests

from providers.regulatory.base import SourceBatch
from research.regulatory.identifiers import build_raw_event_id
from research.regulatory.models import RawRegulatoryRecord, SourceTier


class OpenFDAProvider:
    source_name = "openfda"

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def _fetch_endpoint(self, endpoint: str, *, query: str) -> list[dict]:
        response = self.session.get(endpoint, params={"search": query, "limit": 25}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return payload.get("results", []) if isinstance(payload, dict) else []

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        query = f'effective_time:[{since.strftime("%Y%m%d")} TO {until.strftime("%Y%m%d")}]'
        try:
            labels = self._fetch_endpoint("https://api.fda.gov/drug/label.json", query=query)
        except Exception as exc:
            return SourceBatch(source_status="ERROR", errors=[f"openFDA label fetch failed: {exc.__class__.__name__}"])
        records: list[RawRegulatoryRecord] = []
        for row in labels:
            openfda = row.get("openfda", {}) or {}
            product_name = ((openfda.get("brand_name") or [""])[0]) if isinstance(openfda.get("brand_name"), list) else str(openfda.get("brand_name") or "")
            raw_event_id = build_raw_event_id(
                source=self.source_name,
                source_record_id=str(row.get("id") or product_name),
                source_event_type="LABEL_UPDATE",
                source_publication_date=str(row.get("effective_time") or ""),
            )
            records.append(
                RawRegulatoryRecord(
                    raw_event_id=raw_event_id,
                    source_name=self.source_name,
                    source_record_id=str(row.get("id") or product_name),
                    source_url="https://api.fda.gov/drug/label.json",
                    source_document_type="OPENFDA_LABEL",
                    source_tier=SourceTier.TIER_1,
                    published_at=str(row.get("effective_time") or ""),
                    observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    event_type="OPENFDA_LABEL_UPDATE",
                    company_name=((openfda.get("manufacturer_name") or [""])[0]) if isinstance(openfda.get("manufacturer_name"), list) else str(openfda.get("manufacturer_name") or ""),
                    product_name=str(product_name),
                    raw_payload=row,
                    structured_data={"status": "label_update", "effective_time": str(row.get("effective_time") or "")},
                )
            )
        return SourceBatch(records=records, metadata={"record_count": len(records)})
