from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from providers.sec import get_sec_provider
from providers.sec.base import SECProvider
from providers.sec.errors import SECNotFoundError, SECRequestError
from providers.sec.models import CompanyProfile
from models.common import (
    ConfidenceStatus,
    EntityMapping,
    EntityMappingSuggestion,
    MappingConfidence,
    MappingStatus,
    SourceEvidence,
    now_iso,
    stable_evidence_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EntityMasterConfig:
    enable: bool = True
    max_manual_mapping: bool = True
    fuzzy_min_confidence: float = 0.85

    @classmethod
    def from_env(cls) -> "EntityMasterConfig":
        return cls(
            enable=_env_bool("ENTITY_MASTER_ENABLE", True),
            max_manual_mapping=_env_bool("ENTITY_MASTER_FUZZY_MANUAL_ONLY", True),
            fuzzy_min_confidence=_env_float("ENTITY_MASTER_FUZZY_MIN_CONFIDENCE", 0.85),
        )


# ---------------------------------------------------------------------------
# Entity ID builder
# ---------------------------------------------------------------------------


def build_entity_id(*, cik: str | None = None, ticker: str = "", name: str = "") -> str:
    """Build a stable entity ID from the CIK.

    CIK is the primary identity. Falls back to ticker or name hash.
    """
    cik_str = str(cik or "").strip()
    if cik_str and cik_str.zfill(10) != "0000000000":
        return f"CIK{cik_str.zfill(10)}"
    if ticker:
        return f"TKR-{ticker.strip().upper()}"
    if name:
        digest = hashlib.sha256(name.strip().encode("utf-8")).hexdigest()[:12]
        return f"NAM-{digest}"
    return ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class EntityMasterEngine:
    """Deterministic entity resolution using SEC submissions API."""

    def __init__(
        self,
        *,
        config: EntityMasterConfig | None = None,
        sec_provider: SECProvider | None = None,
    ) -> None:
        self.config = config or EntityMasterConfig.from_env()
        self.sec = sec_provider or get_sec_provider()

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def resolve_entity(self, ticker: str) -> EntityMapping:
        """Build a complete EntityMapping for a ticker from SEC data."""
        normalized = ticker.strip().upper()
        observed_at = now_iso()
        profile: CompanyProfile | None = None
        errors: list[str] = []

        try:
            profile = self.sec.company_profile(normalized)
        except (SECNotFoundError, SECRequestError) as exc:
            errors.append(f"SEC profile lookup failed: {exc.__class__.__name__}")

        if profile is None or not profile.cik:
            return EntityMapping(
                entity_id=build_entity_id(cik="", ticker=normalized),
                ticker=normalized,
                mapping_status=MappingStatus.UNAVAILABLE,
                mapping_confidence=MappingConfidence.UNAVAILABLE,
                last_verified=observed_at,
                manual_override=True,
            )

        entity_id = build_entity_id(cik=profile.cik, ticker=normalized, name=profile.name)
        evidence_url = profile.source_url or self._submissions_url(profile.cik)

        # Get former names, tickers, exchange, SIC from SEC submissions
        former_names: list[str] = []
        former_tickers: list[str] = []
        exchange = ""
        sic = ""
        sic_description = ""

        try:
            submissions = self._load_public_submissions(profile.cik)
            former_names = _extract_former_names(submissions)
            former_tickers = _extract_former_tickers(submissions)
            exchange = _extract_exchange(submissions) or ""
            sic = str(submissions.get("sic", "")).strip()
            sic_description = str(submissions.get("sicDescription", "")).strip()
        except Exception as exc:
            errors.append(f"Submissions data extraction failed: {exc.__class__.__name__}")

        evidence = SourceEvidence(
            source="sec_gov",
            source_record_id=profile.cik,
            source_url=evidence_url,
            source_field="company_profile",
            observed_at=observed_at,
        )

        return EntityMapping(
            entity_id=entity_id,
            ticker=normalized,
            exchange=exchange,
            security_type="common_stock",
            active=True,
            cik=profile.cik,
            sic=sic,
            sic_description=sic_description,
            current_legal_name=profile.name,
            former_legal_names=tuple(former_names),
            former_tickers=tuple(former_tickers),
            yahoo_ticker=normalized,  # simplified: assumes 1:1, override in adapter
            mapping_status=MappingStatus.EXACT,
            mapping_confidence=MappingConfidence.HIGH,
            evidence_url=evidence_url,
            last_verified=observed_at,
            manual_override=False,
        )

    def resolve_batch(
        self,
        tickers: list[str],
    ) -> dict[str, EntityMapping]:
        """Resolve multiple tickers. Returns {ticker: EntityMapping}."""
        results: dict[str, EntityMapping] = {}
        for ticker in tickers:
            try:
                results[ticker] = self.resolve_entity(ticker)
            except Exception as exc:
                logger.warning("Entity resolution failed for %s: %s", ticker, exc.__class__.__name__)
                results[ticker] = EntityMapping(
                    entity_id=build_entity_id(cik="", ticker=ticker),
                    ticker=ticker,
                    mapping_status=MappingStatus.UNAVAILABLE,
                    mapping_confidence=MappingConfidence.UNAVAILABLE,
                    last_verified=now_iso(),
                )
        return results

    def suggest_fuzzy_mapping(
        self,
        ticker: str,
        candidate_name: str,
        *,
        similarity_metric: str = "levenshtein",
        similarity_score: float = 0.0,
    ) -> EntityMappingSuggestion:
        """Generate a fuzzy mapping suggestion that MUST be manually reviewed.

        Fuzzy matches are NEVER activated automatically.
        """
        profile: CompanyProfile | None = None
        try:
            profile = self.sec.company_profile(ticker)
        except Exception:
            pass

        return EntityMappingSuggestion(
            ticker=ticker.strip().upper(),
            suggested_entity_id=build_entity_id(cik=(profile.cik if profile else ""), ticker=ticker),
            suggested_name=candidate_name.strip(),
            similarity_metric=similarity_metric,
            similarity_score=similarity_score,
            evidence=SourceEvidence(
                source="fuzzy_match",
                source_field="company_name",
                observed_at=now_iso(),
            ) if similarity_score > 0 else None,
            status="MANUAL_REQUIRED",
        )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _submissions_url(cik: str) -> str:
        """Build the SEC submissions JSON URL for a CIK."""
        from providers.sec.official import SEC_DATA_URL
        return f"{SEC_DATA_URL}/submissions/CIK{cik.strip().zfill(10)}.json"

    def _load_public_submissions(self, cik: str) -> dict[str, Any]:
        """Load the SEC submissions JSON using the provider's public interface."""
        if hasattr(self.sec, "session") and hasattr(self.sec, "user_agent"):
            import requests
            session = getattr(self.sec, "session")
            user_agent = getattr(self.sec, "user_agent", "hxfinancebot/1.0")
            url = self._submissions_url(cik)
            try:
                response = session.get(url, headers={"User-Agent": user_agent}, timeout=30)
                response.raise_for_status()
                return response.json()
            except Exception:
                return {}
        return {}

    def _load_submissions(self, cik: str) -> dict[str, Any]:
        """Backward-compatible alias for _load_public_submissions."""
        return self._load_public_submissions(cik)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _extract_former_names(submissions: dict[str, Any]) -> list[str]:
    """Extract former company names from SEC submissions data."""
    former = submissions.get("formerCompanyNames", []) or []
    if isinstance(former, list):
        return sorted({str(item.get("name", "")).strip() for item in former if str(item.get("name", "")).strip()})
    if isinstance(former, dict):
        return sorted({str(v).strip() for v in former.values() if str(v).strip()})
    return []


def _extract_former_tickers(submissions: dict[str, Any]) -> list[str]:
    """Extract former tickers from SEC submissions data."""
    tickers = submissions.get("tickers", []) or []
    if isinstance(tickers, list):
        return sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    return []


def _extract_exchange(submissions: dict[str, Any]) -> str:
    """Extract primary exchange from SEC submissions."""
    exchange = str(submissions.get("exchange", "")).strip().upper()
    return exchange


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
