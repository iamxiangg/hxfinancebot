from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from providers.regulatory.base import SourceBatch
from research.regulatory.config import RegulatoryMonitorConfig
from research.regulatory.identifiers import build_raw_event_id
from research.regulatory.models import RawRegulatoryRecord, SourceTier


class ClinicalTrialsGovProvider:
    source_name = "clinicaltrials"

    def __init__(self, *, config: RegulatoryMonitorConfig | None = None, session: requests.Session | None = None) -> None:
        self.config = config or RegulatoryMonitorConfig.from_env()
        self.session = session or requests.Session()

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        params = {
            "format": "json",
            "pageSize": self.config.ct_gov_page_size,
            "query.term": "",
            "filter.overallStatus": "NOT_YET_RECRUITING|RECRUITING|ACTIVE_NOT_RECRUITING|COMPLETED",
            "filter.lastUpdatePostDate": f"RANGE[{since.date().isoformat()},{until.date().isoformat()}]",
        }
        try:
            response = self.session.get(self.config.ct_gov_api_url, params=params, timeout=30)
            response.raise_for_status()
            if "html" in str(response.headers.get("Content-Type", "")).lower():
                return SourceBatch(source_status="UNAVAILABLE", errors=["ClinicalTrials.gov endpoint returned HTML instead of JSON. Set REGULATORY_CT_GOV_API_URL if the official endpoint changed."])
            payload = response.json()
        except Exception as exc:
            return SourceBatch(source_status="ERROR", errors=[f"ClinicalTrials.gov fetch failed: {exc.__class__.__name__}"])
        studies = payload.get("studies", []) if isinstance(payload, dict) else []
        records: list[RawRegulatoryRecord] = []
        for study in studies:
            protocol = study.get("protocolSection", {}) if isinstance(study, dict) else {}
            identification = protocol.get("identificationModule", {})
            status = protocol.get("statusModule", {})
            sponsor = protocol.get("sponsorCollaboratorsModule", {})
            conditions = protocol.get("conditionsModule", {})
            design = protocol.get("designModule", {})
            nct_id = str(identification.get("nctId") or "").strip()
            source_record_id = nct_id or str(identification.get("briefTitle") or "")[:80]
            raw_event_id = build_raw_event_id(
                source=self.source_name,
                source_record_id=source_record_id,
                source_event_type="STUDY_UPDATE",
                source_publication_date=str(status.get("lastUpdatePostDateStruct", {}).get("date") or status.get("studyFirstPostDateStruct", {}).get("date") or ""),
            )
            records.append(
                RawRegulatoryRecord(
                    raw_event_id=raw_event_id,
                    source_name=self.source_name,
                    source_record_id=source_record_id,
                    source_url=f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
                    source_document_type="CTGOV_STUDY",
                    source_tier=SourceTier.TIER_1,
                    published_at=str(status.get("lastUpdatePostDateStruct", {}).get("date") or ""),
                    observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    event_type="CT_STUDY_UPDATE",
                    company_name=str(sponsor.get("leadSponsor", {}).get("name") or ""),
                    product_name="; ".join(str(item).strip() for item in (protocol.get("armsInterventionsModule", {}) or {}).get("interventions", []) if isinstance(item, str)),
                    indication_name=", ".join(str(item).strip() for item in conditions.get("conditions", []) if str(item).strip()),
                    trial_nct_id=nct_id,
                    raw_payload=study,
                    structured_data={
                        "phase": ",".join(design.get("phases", [])) if isinstance(design.get("phases"), list) else str(design.get("phases") or ""),
                        "overall_status": str(status.get("overallStatus") or ""),
                        "results_first_posted": str(status.get("resultsFirstPostDateStruct", {}).get("date") or ""),
                        "last_update_posted": str(status.get("lastUpdatePostDateStruct", {}).get("date") or ""),
                        "enrollment": ((protocol.get("contactsLocationsModule", {}) or {}).get("overallOfficials") or []),
                    },
                )
            )
        return SourceBatch(records=records, metadata={"record_count": len(records)})

