from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests

from providers.yahoo_throttle import create_ticker, yahoo_call
from scanners.congress.models import CompanyClassification


logger = logging.getLogger(__name__)

_MAPPINGS_DIR = Path(__file__).with_name("mappings")

KEYWORD_EXPOSURES = {
    "defense": ("industrials", "aerospace_defense", ("defense", "government_contracting")),
    "semiconductor": ("technology", "semiconductors", ("semiconductors",)),
    "bank": ("financials", "banks", ("banks",)),
    "energy": ("energy", "oil_gas", ("energy",)),
    "software": ("technology", "software_infrastructure", ("software",)),
}


def _load_mapping(name: str) -> dict[str, Any]:
    path = _MAPPINGS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _cache_dir() -> Path:
    path = Path(os.getenv("POLITICAL_COMPANY_CACHE_DIR", "funnel_output/political_company_cache"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(ticker: str) -> Path:
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{digest}.json"


def _cache_read(ticker: str, *, ttl_hours: float = 24.0) -> dict[str, Any] | None:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    saved_at = datetime.fromisoformat(str(payload.get("saved_at", "")))
    if saved_at + timedelta(hours=ttl_hours) < datetime.now(UTC):
        return None
    return payload.get("value")


def _cache_write(ticker: str, value: dict[str, Any]) -> None:
    path = _cache_path(ticker)
    payload = {
        "saved_at": datetime.now(UTC).isoformat(),
        "value": value,
    }
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temp_path = Path(handle.name)
    temp_path.replace(path)


class CompanyClassificationProvider:
    def __init__(self, *, metadata_fetcher: Any | None = None, cache_enabled: bool = True) -> None:
        self.metadata_fetcher = metadata_fetcher
        self.cache_enabled = cache_enabled
        self.overrides = _load_mapping("ticker_sector_overrides.yaml")

    def classify(self, ticker: str) -> CompanyClassification:
        normalized = str(ticker or "").strip().upper()
        override = self.overrides.get(normalized)
        if isinstance(override, dict):
            return CompanyClassification(
                ticker=normalized,
                sector=str(override.get("sector", "")),
                industry=str(override.get("industry", "")),
                thematic_exposures=tuple(override.get("thematic_exposures", [])),
                source="override",
                confidence="HIGH",
            )

        if self.cache_enabled:
            cached = _cache_read(normalized)
            if isinstance(cached, dict):
                return CompanyClassification(
                    ticker=normalized,
                    sector=str(cached.get("sector", "")),
                    industry=str(cached.get("industry", "")),
                    thematic_exposures=tuple(cached.get("thematic_exposures", [])),
                    source=str(cached.get("source", "cache")),
                    confidence=str(cached.get("confidence", "MEDIUM")),
                )

        try:
            metadata = self._metadata(normalized)
            sector = str(metadata.get("sectorKey") or metadata.get("sectorDisp") or metadata.get("sector") or "").strip().lower().replace(" ", "_")
            industry = str(metadata.get("industryKey") or metadata.get("industryDisp") or metadata.get("industry") or "").strip().lower().replace(" ", "_")
            exposures = tuple(
                item for item in {
                    sector,
                    industry,
                    *([industry.split("_")[0]] if industry else []),
                }
                if item
            )
            if sector or industry:
                result = CompanyClassification(
                    ticker=normalized,
                    sector=sector,
                    industry=industry,
                    thematic_exposures=exposures,
                    source="yahoo",
                    confidence="MEDIUM",
                )
                if self.cache_enabled:
                    _cache_write(
                        normalized,
                        {
                            "sector": result.sector,
                            "industry": result.industry,
                            "thematic_exposures": list(result.thematic_exposures),
                            "source": result.source,
                            "confidence": result.confidence,
                        },
                    )
                return result
        except Exception as exc:
            logger.warning("Political company classification failed for %s: %s", normalized, exc.__class__.__name__)

        for term, payload in KEYWORD_EXPOSURES.items():
            if term in normalized.lower():
                sector, industry, exposures = payload
                return CompanyClassification(
                    ticker=normalized,
                    sector=sector,
                    industry=industry,
                    thematic_exposures=tuple(exposures),
                    source="keyword",
                    confidence="LOW",
                )
        return CompanyClassification(
            ticker=normalized,
            sector="",
            industry="",
            thematic_exposures=(),
            source="unavailable",
            confidence="UNAVAILABLE",
        )

    def _metadata(self, ticker: str) -> dict[str, Any]:
        if callable(self.metadata_fetcher):
            return dict(self.metadata_fetcher(ticker))
        return dict(
            yahoo_call(
                lambda: getattr(create_ticker(ticker), "info", {}) or {},
                label=f"congress-company-info:{ticker}",
            )
            or {}
        )
