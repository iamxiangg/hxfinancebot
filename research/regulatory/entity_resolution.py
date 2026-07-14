from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import requests

from providers.sec.official import SEC_DATA_URL, TICKER_MAP_URL
from research.regulatory.models import CompanyEntity, CompanyOperatingMode, MappingConfidenceLevel

LEGAL_SUFFIX_TOKENS = {
    "AG",
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LIMITED",
    "LLC",
    "LP",
    "LTD",
    "NV",
    "PLC",
    "SA",
    "SPA",
}

GENERIC_ISSUER_TOKENS = {
    "AND",
    "BIO",
    "BIOLOGICS",
    "BIOPHARMA",
    "BIOSCIENCES",
    "BIOTECH",
    "BRANDS",
    "DIAGNOSTIC",
    "DIAGNOSTICS",
    "GROUP",
    "HEALTH",
    "HEALTHCARE",
    "HOLDING",
    "HOLDINGS",
    "INTERNATIONAL",
    "LABORATORIES",
    "MEDICAL",
    "MEDICINES",
    "ONCOLOGY",
    "PHARMACEUTICAL",
    "PHARMACEUTICALS",
    "SERVICES",
    "THERAPEUTIC",
    "THERAPEUTICS",
    "VACCINE",
}

TOKEN_NORMALIZATIONS = {
    "&": "AND",
    "BIOPHARMACEUTICALS": "BIOPHARMA",
    "BIOPHARMACEUTICAL": "BIOPHARMA",
    "CO.": "COMPANY",
    "CORP.": "CORPORATION",
    "HLTH": "HEALTH",
    "HLTHCARE": "HEALTHCARE",
    "INTL": "INTERNATIONAL",
    "LAB": "LABORATORIES",
    "LABS": "LABORATORIES",
    "LTD.": "LIMITED",
    "PHARMA": "PHARMACEUTICALS",
    "PHARMS": "PHARMACEUTICALS",
    "SVCS": "SERVICES",
}

HEALTHCARE_HINT_TOKENS = {
    "BIO",
    "BIOLOGICS",
    "BIOPHARMA",
    "BIOSCIENCES",
    "BIOTECH",
    "DIAGNOSTIC",
    "DIAGNOSTICS",
    "HEALTH",
    "HEALTHCARE",
    "LABORATORIES",
    "MEDICAL",
    "MEDICINES",
    "ONCOLOGY",
    "PHARMACEUTICAL",
    "PHARMACEUTICALS",
    "THERAPEUTIC",
    "THERAPEUTICS",
    "VACCINE",
}


@dataclass
class EntityResolutionResult:
    entity: CompanyEntity | None
    confidence: MappingConfidenceLevel
    reason: str = ""
    manual_required: bool = False


@dataclass(frozen=True)
class _IssuerCandidate:
    row: dict[str, Any]
    canonical_name: str
    informative_tokens: frozenset[str]


def _clean_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _name_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for token in _clean_name(value).split():
        normalized = TOKEN_NORMALIZATIONS.get(token, token)
        if normalized:
            tokens.append(normalized)
    return tokens


def _canonical_name(value: str) -> str:
    tokens = [token for token in _name_tokens(value) if token not in LEGAL_SUFFIX_TOKENS]
    return " ".join(tokens)


def _informative_tokens(value: str) -> frozenset[str]:
    tokens = []
    for token in _name_tokens(value):
        if token in LEGAL_SUFFIX_TOKENS or token in GENERIC_ISSUER_TOKENS:
            continue
        if len(token) < 3 and token != "DR":
            continue
        tokens.append(token)
    return frozenset(tokens)


def _normalized_cik(value: str) -> str:
    digits = "".join(char for char in str(value or "").strip() if char.isdigit())
    return digits.zfill(10) if digits else ""


def _healthcare_hint_from_title(title: str) -> bool:
    tokens = {
        token
        for token in _name_tokens(title)
        if token not in LEGAL_SUFFIX_TOKENS
    }
    return any(
        token in HEALTHCARE_HINT_TOKENS or token.startswith("BIO")
        for token in tokens
    )


class RegulatoryEntityResolver:
    def __init__(
        self,
        *,
        config_payload: dict[str, Any] | None = None,
        session: requests.Session | None = None,
        sic_allowlist: list[str] | None = None,
    ) -> None:
        self.config_payload = config_payload or {}
        self.session = session or requests.Session()
        self.sic_allowlist = {str(item or "").strip() for item in (sic_allowlist or []) if str(item or "").strip()}
        self._sic_cache: dict[str, str] = {}
        self._ticker_map = self._load_ticker_map()
        self._by_ticker = {
            str(row.get("ticker") or "").strip().upper(): row
            for row in self._ticker_map.values()
            if str(row.get("ticker") or "").strip()
        }
        self._by_cik = {
            _normalized_cik(str(row.get("cik_str") or "")): row
            for row in self._ticker_map.values()
            if _normalized_cik(str(row.get("cik_str") or ""))
        }
        self._by_name = {
            str(row.get("title") or "").strip().upper(): row
            for row in self._ticker_map.values()
            if str(row.get("title") or "").strip()
        }
        self._canonical_name_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._token_signature_index: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        self._token_candidate_index: dict[str, list[_IssuerCandidate]] = defaultdict(list)
        self._issuer_candidates: list[_IssuerCandidate] = []
        for row in self._ticker_map.values():
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            candidate = _IssuerCandidate(
                row=row,
                canonical_name=_canonical_name(title),
                informative_tokens=_informative_tokens(title),
            )
            self._issuer_candidates.append(candidate)
            if candidate.canonical_name:
                self._canonical_name_index[candidate.canonical_name].append(row)
            if candidate.informative_tokens:
                signature = tuple(sorted(candidate.informative_tokens))
                self._token_signature_index[signature].append(row)
                for token in candidate.informative_tokens:
                    self._token_candidate_index[token].append(candidate)
        self._ownership_name_map: dict[str, dict[str, Any]] = {}
        self._ownership_ticker_map: dict[str, dict[str, Any]] = {}
        self._load_ownership_maps()

    def _load_ticker_map(self) -> dict[str, Any]:
        try:
            user_agent = str(os.getenv("SEC_USER_AGENT", "hxfinancebot/1.0")).strip()
            response = self.session.get(TICKER_MAP_URL, headers={"User-Agent": user_agent}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _load_ownership_maps(self) -> None:
        for edge in self.config_payload.get("ownership_edges") or []:
            if not isinstance(edge, dict):
                continue
            parent_row = self._resolve_parent_row(
                ticker=str(edge.get("parent_ticker") or ""),
                entity_id=str(edge.get("parent_entity_id") or ""),
                legal_name=str(edge.get("parent_legal_name") or edge.get("parent_name") or ""),
            )
            if parent_row is None:
                continue
            for key in self._name_keys(edge):
                self._ownership_name_map[key] = parent_row
            for ticker in self._ticker_keys(edge):
                self._ownership_ticker_map[ticker] = parent_row
        for key, value in (self.config_payload.get("subsidiary_mappings") or {}).items():
            parent_row = self._resolve_parent_row(ticker=str(value or "").strip().upper())
            if parent_row is None:
                continue
            name_key = _canonical_name(str(key or ""))
            if name_key:
                self._ownership_name_map[name_key] = parent_row

    def _resolve_parent_row(self, *, ticker: str = "", entity_id: str = "", legal_name: str = "") -> dict[str, Any] | None:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_entity_id = str(entity_id or "").strip().upper()
        normalized_name = _canonical_name(legal_name)
        if normalized_ticker and normalized_ticker in self._by_ticker:
            return self._by_ticker[normalized_ticker]
        if normalized_entity_id.startswith("CIK"):
            row = self._by_cik.get(_normalized_cik(normalized_entity_id[3:]))
            if row is not None:
                return row
        if normalized_name:
            candidates = self._canonical_name_index.get(normalized_name, [])
            if len(candidates) == 1:
                return candidates[0]
        return None

    def _name_keys(self, edge: dict[str, Any]) -> list[str]:
        keys = [
            _canonical_name(str(edge.get("child_legal_name") or edge.get("child_name") or "")),
            _canonical_name(str(edge.get("subsidiary_legal_name") or "")),
        ]
        return [key for key in keys if key]

    def _ticker_keys(self, edge: dict[str, Any]) -> list[str]:
        tickers = [
            str(edge.get("child_ticker") or "").strip().upper(),
            str(edge.get("subsidiary_ticker") or "").strip().upper(),
        ]
        return [ticker for ticker in tickers if ticker]

    def _row_to_entity(self, row: dict[str, Any]) -> CompanyEntity:
        cik = _normalized_cik(str(row.get("cik_str") or ""))
        ticker = str(row.get("ticker") or "").strip().upper()
        return CompanyEntity(
            company_id=f"CIK{cik}" if cik else f"TKR-{ticker}",
            legal_name=str(row.get("title") or "").strip(),
            ticker=ticker,
            cik=cik,
            operating_mode=CompanyOperatingMode.UNKNOWN,
            source_url=TICKER_MAP_URL,
        )

    def _candidate_is_healthcare(self, row: dict[str, Any]) -> bool:
        title = str(row.get("title") or "")
        if _healthcare_hint_from_title(title):
            return True
        cik = _normalized_cik(str(row.get("cik_str") or ""))
        if not cik or not self.sic_allowlist:
            return False
        if cik not in self._sic_cache:
            self._sic_cache[cik] = self._load_sic(cik)
        return self._sic_cache[cik] in self.sic_allowlist

    def _load_sic(self, cik: str) -> str:
        url = f"{SEC_DATA_URL}/submissions/CIK{cik}.json"
        try:
            user_agent = str(os.getenv("SEC_USER_AGENT", "hxfinancebot/1.0")).strip()
            response = self.session.get(url, headers={"User-Agent": user_agent}, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return ""
        return str(payload.get("sic") or "").strip()

    def _resolve_from_ownership(self, *, ticker: str, legal_name: str, sponsor_name: str) -> dict[str, Any] | None:
        normalized_ticker = str(ticker or "").strip().upper()
        if normalized_ticker and normalized_ticker in self._ownership_ticker_map:
            return self._ownership_ticker_map[normalized_ticker]
        for value in (legal_name, sponsor_name):
            key = _canonical_name(value)
            if key and key in self._ownership_name_map:
                return self._ownership_name_map[key]
        return None

    def _resolve_from_canonical_name(self, *, legal_name: str, sponsor_name: str) -> tuple[dict[str, Any] | None, str]:
        raw_name = str(legal_name or sponsor_name or "").strip()
        if not raw_name:
            return None, ""
        canonical_name = _canonical_name(raw_name)
        if canonical_name:
            exact_candidates = self._canonical_name_index.get(canonical_name, [])
            if len(exact_candidates) == 1:
                if len(_informative_tokens(raw_name)) > 1 or self._candidate_is_healthcare(exact_candidates[0]):
                    return exact_candidates[0], "Deterministic canonical issuer-name match."
        tokens = _informative_tokens(raw_name)
        if not tokens:
            return None, ""
        signature = tuple(sorted(tokens))
        signature_candidates = self._token_signature_index.get(signature, [])
        if len(signature_candidates) == 1 and self._candidate_is_healthcare(signature_candidates[0]):
            return signature_candidates[0], "Deterministic normalized token-signature match."
        candidate_pool: dict[str, _IssuerCandidate] = {}
        for token in tokens:
            for candidate in self._token_candidate_index.get(token, []):
                ticker = str(candidate.row.get("ticker") or "").strip().upper()
                if ticker:
                    candidate_pool[ticker] = candidate
        matches: list[tuple[float, _IssuerCandidate]] = []
        for candidate in candidate_pool.values():
            if not candidate.informative_tokens or not self._candidate_is_healthcare(candidate.row):
                continue
            overlap = tokens.intersection(candidate.informative_tokens)
            if not overlap:
                continue
            if not tokens.issubset(candidate.informative_tokens) and not candidate.informative_tokens.issubset(tokens):
                continue
            score = len(overlap) / max(len(tokens), len(candidate.informative_tokens))
            if score >= 0.6:
                matches.append((score, candidate))
        matches.sort(key=lambda item: (-item[0], len(item[1].informative_tokens), str(item[1].row.get("ticker") or "")))
        if len(matches) == 1:
            return matches[0][1].row, "Deterministic normalized token-overlap match."
        if len(matches) > 1 and matches[0][0] > matches[1][0]:
            return matches[0][1].row, "Deterministic best normalized token-overlap match."
        return None, ""

    def resolve(
        self,
        *,
        ticker: str = "",
        cik: str = "",
        legal_name: str = "",
        sponsor_name: str = "",
    ) -> EntityResolutionResult:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_cik = _normalized_cik(cik)
        normalized_name = str(legal_name or sponsor_name or "").strip().upper()
        if normalized_ticker in self._by_ticker:
            return EntityResolutionResult(self._row_to_entity(self._by_ticker[normalized_ticker]), MappingConfidenceLevel.HIGH)
        if normalized_cik and normalized_cik in self._by_cik:
            return EntityResolutionResult(self._row_to_entity(self._by_cik[normalized_cik]), MappingConfidenceLevel.HIGH)
        if normalized_name in self._by_name:
            return EntityResolutionResult(self._row_to_entity(self._by_name[normalized_name]), MappingConfidenceLevel.HIGH)
        ownership_row = self._resolve_from_ownership(
            ticker=normalized_ticker,
            legal_name=legal_name,
            sponsor_name=sponsor_name,
        )
        if ownership_row is not None:
            return EntityResolutionResult(
                self._row_to_entity(ownership_row),
                MappingConfidenceLevel.MEDIUM,
                reason="Deterministic parent-ownership mapping.",
            )
        sponsor_alias = {
            _canonical_name(str(key or "")): str(value or "").strip().upper()
            for key, value in (self.config_payload.get("sponsor_aliases") or {}).items()
        }
        alias_target = sponsor_alias.get(_canonical_name(legal_name or sponsor_name))
        if alias_target and alias_target in self._by_ticker:
            return EntityResolutionResult(
                self._row_to_entity(self._by_ticker[alias_target]),
                MappingConfidenceLevel.HIGH,
                reason="Manual exact alias mapping.",
            )
        name_row, reason = self._resolve_from_canonical_name(
            legal_name=legal_name,
            sponsor_name=sponsor_name,
        )
        if name_row is not None:
            return EntityResolutionResult(
                self._row_to_entity(name_row),
                MappingConfidenceLevel.MEDIUM,
                reason=reason,
            )
        return EntityResolutionResult(
            None,
            MappingConfidenceLevel.MANUAL_REQUIRED,
            reason="Exact company mapping unavailable.",
            manual_required=True,
        )
