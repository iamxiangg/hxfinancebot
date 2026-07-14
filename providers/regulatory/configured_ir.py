from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import requests

from providers.regulatory.base import SourceBatch
from research.regulatory.config import RegulatoryMonitorConfig
from research.regulatory.identifiers import build_raw_event_id
from research.regulatory.models import RawRegulatoryRecord, SourceTier


class ConfiguredIssuerFeedProvider:
    source_name = "configured_ir"

    def __init__(self, *, config: RegulatoryMonitorConfig | None = None, session: requests.Session | None = None) -> None:
        self.config = config or RegulatoryMonitorConfig.from_env()
        self.session = session or requests.Session()

    def _feeds(self) -> list[dict[str, Any]]:
        payload = self.config.configured_sources_payload()
        feeds = payload.get("issuer_feeds", [])
        return feeds if isinstance(feeds, list) else []

    def _records_from_rss(self, feed: dict[str, Any], text: str) -> list[RawRegulatoryRecord]:
        root = ET.fromstring(text)
        records: list[RawRegulatoryRecord] = []
        for item in root.findall(".//item")[: self.config.max_issuer_feed_records]:
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            pub_date = str(item.findtext("pubDate") or "").strip()
            raw_event_id = build_raw_event_id(
                source=self.source_name,
                source_record_id=link or title,
                source_event_type="RSS_ITEM",
                source_publication_date=pub_date,
            )
            records.append(
                RawRegulatoryRecord(
                    raw_event_id=raw_event_id,
                    source_name=self.source_name,
                    source_record_id=link or title,
                    source_url=link,
                    source_document_type="ISSUER_FEED_RSS",
                    source_tier=SourceTier.TIER_2,
                    published_at=pub_date,
                    observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    event_type="ISSUER_FEED_ITEM",
                    company_name=str(feed.get("company_name") or ""),
                    ticker=str(feed.get("ticker") or ""),
                    exact_text=title,
                    raw_payload={"title": title, "link": link, "pub_date": pub_date},
                )
            )
        return records

    def _records_from_json(self, feed: dict[str, Any], payload: Any) -> list[RawRegulatoryRecord]:
        rows = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
        records: list[RawRegulatoryRecord] = []
        for item in rows[: self.config.max_issuer_feed_records]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("headline") or "")
            link = str(item.get("url") or item.get("link") or "")
            published = str(item.get("published_at") or item.get("date") or "")
            raw_event_id = build_raw_event_id(
                source=self.source_name,
                source_record_id=link or title,
                source_event_type="JSON_ITEM",
                source_publication_date=published,
            )
            records.append(
                RawRegulatoryRecord(
                    raw_event_id=raw_event_id,
                    source_name=self.source_name,
                    source_record_id=link or title,
                    source_url=link,
                    source_document_type="ISSUER_FEED_JSON",
                    source_tier=SourceTier.TIER_2,
                    published_at=published,
                    observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    event_type="ISSUER_FEED_ITEM",
                    company_name=str(feed.get("company_name") or ""),
                    ticker=str(feed.get("ticker") or ""),
                    exact_text=title,
                    raw_payload=item,
                )
            )
        return records

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        records: list[RawRegulatoryRecord] = []
        errors: list[str] = []
        for feed in self._feeds():
            url = str(feed.get("url") or "").strip()
            if not url:
                continue
            try:
                response = self.session.get(url, timeout=20)
                response.raise_for_status()
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "json" in content_type or str(feed.get("format") or "").lower() == "json":
                    records.extend(self._records_from_json(feed, response.json()))
                else:
                    records.extend(self._records_from_rss(feed, response.text))
            except Exception as exc:
                errors.append(f"{url}:{exc.__class__.__name__}")
        return SourceBatch(records=records, errors=errors, metadata={"record_count": len(records)})

