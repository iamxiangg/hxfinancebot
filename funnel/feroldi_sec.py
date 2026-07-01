from __future__ import annotations

"""SEC filing extraction for Feroldi first-cut evidence.

Extracts CEO leadership evidence (M01) and mission statements (M03)
from official SEC filings using deterministic text parsing.
"""

import html
import logging
from typing import Any

from providers.sec import get_sec_provider
from providers.sec.base import SECProvider
from providers.sec.models import FilingDocument, FilingMetadata

logger = logging.getLogger(__name__)

# Module-level cache to avoid repeated downloads of the same filing
_filing_text_cache: dict[str, dict[str, str]] = {}


def clear_filing_cache() -> None:
    """Clear the module-level filing text cache (useful for testing)."""
    _filing_text_cache.clear()


def extract_filing_text(
    *,
    ticker: str,
    sec_provider: SECProvider | None = None,
    form_types: set[str] | None = None,
) -> dict[str, str]:
    """Retrieve the latest relevant filing text for a ticker.

    Returns a dict mapping form type to full filing text.
    """
    sec = sec_provider or get_sec_provider()
    form_types = form_types or {"10-K", "20-F", "DEF 14A", "8-K"}

    # Check cache first
    ticker_key = ticker.strip().upper()
    if ticker_key in _filing_text_cache:
        logger.debug("Filing text cache hit for %s", ticker)
        return dict(_filing_text_cache[ticker_key])

    result: dict[str, str] = {}
    for form in sorted(form_types):
        try:
            filings = sec.recent_filings(ticker, forms={form})
            if not filings:
                continue
            # Use the most recent filing
            latest = filings[0]
            document = sec.filing_text(latest)
            if document and document.text:
                # Decode HTML entities (&#160;, &#8217;, &amp;, etc.) from SEC filing text
                result[form] = html.unescape(document.text)
        except Exception as exc:
            logger.debug("Could not fetch %s for %s: %s", form, ticker, exc.__class__.__name__)

    # Store in cache before returning
    _filing_text_cache[ticker_key] = dict(result)

    return result


def extract_ceo_evidence(
    filing_texts: dict[str, str],
) -> dict[str, Any]:
    """Extract CEO leadership evidence from filing texts.

    Searches proxy statements (DEF 14A), annual reports (10-K/20-F),
    and 8-K filings for explicit CEO appointment/succession language.

    Returns a dict with:
        evidence_text: str
        source_type: str
        source_filing_date: str
        source_accession: str
        confidence: str (HIGH/MEDIUM/LOW/UNKNOWN)
    """
    from funnel.feroldi_config import (
        CEO_SERVED_SINCE_MARKERS,
        CEO_TITLE_PATTERNS,
        FOUNDER_MARKERS,
        INTERIM_MARKERS,
    )

    result: dict[str, Any] = {
        "evidence_text": "",
        "source_type": "",
        "source_filing_date": "",
        "source_accession": "",
        "source_url": "",
        "extraction_confidence": "UNKNOWN",
    }

    # Search order: DEF 14A → 10-K → 20-F → 8-K
    search_order = ["DEF 14A", "10-K", "20-F", "8-K"]

    for form in search_order:
        text = filing_texts.get(form, "")
        if not text:
            continue

        evidence, confidence = _search_ceo_in_text(text)
        if evidence:
            result["evidence_text"] = evidence[:2000]
            result["source_type"] = form
            result["extraction_confidence"] = confidence
            result["source_filing_date"] = ""  # Will be populated by caller
            return result

    return result


def _search_ceo_in_text(text: str) -> tuple[str, str]:
    """Search filing text for CEO leadership evidence.

    Returns (evidence_snippet, confidence).
    Uses flexible matching: finds markers with nearby CEO-title context.
    """
    from funnel.feroldi_config import (
        CEO_SERVED_SINCE_MARKERS,
        CEO_TITLE_PATTERNS,
        FOUNDER_MARKERS,
        INTERIM_MARKERS,
    )

    lower = text.lower()

    # Priority 1: Founder marker with CEO context nearby
    for marker in FOUNDER_MARKERS:
        if marker in lower:
            idx = lower.index(marker)
            start = max(0, idx - 200)
            end = min(len(text), idx + len(marker) + 400)
            snippet = text[start:end].strip()
            snippet_lower = snippet.lower()
            if "chief executive" in snippet_lower or "ceo" in snippet_lower:
                return snippet, "HIGH"

    # Priority 2: Company-specific CEO tenure — marker near "our" or "the Company"
    company_ceo_markers = (
        "our chief executive officer",
        "the company's chief executive officer",
        "our ceo",
        "the company's ceo",
    )
    for marker in CEO_SERVED_SINCE_MARKERS:
        idx = 0
        while True:
            idx = lower.find(marker, idx)
            if idx < 0:
                break
            start = max(0, idx - 100)
            end = min(len(text), idx + len(marker) + 400)
            snippet = text[start:end].strip()
            snippet_lower = snippet.lower()
            # Prefer company-specific reference ("our CEO", "the Company's CEO")
            is_company_specific = any(c in snippet_lower for c in company_ceo_markers)
            has_ceo_title = any(t in snippet_lower for t in CEO_TITLE_PATTERNS)
            # Skip obvious non-company matches (references to other companies' CEOs)
            skip_markers = ("calico", "of the board", "board of directors")
            is_skip = any(s in snippet_lower for s in skip_markers if s not in snippet_lower[:snippet_lower.find(marker)])
            if has_ceo_title and not is_skip:
                confidence = "HIGH" if is_company_specific else "MEDIUM"
                return snippet, confidence
            idx += len(marker)

    # Priority 3: Fallback — find any "chief executive officer" mention and
    # extract surrounding paragraph as MEDIUM confidence evidence
    for title in CEO_TITLE_PATTERNS:
        if title in lower:
            idx = lower.index(title)
            start = max(0, idx - 300)
            end = min(len(text), idx + len(title) + 500)
            snippet = text[start:end].strip()
            return snippet, "MEDIUM"

    # Priority 4: Interim CEO marker
    for marker in INTERIM_MARKERS:
        if marker in lower:
            idx = lower.index(marker)
            start = max(0, idx - 100)
            end = min(len(text), idx + len(marker) + 200)
            snippet = text[start:end].strip()
            return snippet, "MEDIUM"

    return "", "UNKNOWN"


def extract_mission_evidence(
    filing_texts: dict[str, str],
) -> dict[str, Any]:
    """Extract mission/purpose statement from filing texts.

    Searches 10-K, 20-F, and DEF 14A filings for mission or business statements.
    """
    result: dict[str, Any] = {
        "mission_text": "",
        "source_type": "",
        "source_url": "",
        "extraction_phrase": "",
        "extraction_confidence": "UNKNOWN",
    }

    mission_intros = (
        "our mission is",
        "our purpose is",
        "we exist to",
        "our goal is",
        "we aim to",
        "the company's mission is",
        "mission statement",
        "is dedicated to bringing",
        "is dedicated to creating",
        "is dedicated to making",
        "we believe that technology",
        "we believe technology",
        "the company designs",
        "the company's business is",
    )

    # Search 10-K first, then 20-F, then DEF 14A
    for form in ("10-K", "20-F", "DEF 14A"):
        text = filing_texts.get(form, "")
        if not text:
            continue

        lower = text.lower()
        for intro in mission_intros:
            if intro in lower:
                idx = lower.index(intro)
                # Extract the sentence containing the mission
                start = max(0, idx)
                end = min(len(text), idx + 500)
                snippet = text[start:end].strip()

                # Find sentence boundaries
                first_period = snippet.find(".")
                if first_period > 0 and first_period < 400:
                    snippet = snippet[:first_period + 1]

                result["mission_text"] = snippet[:500]
                result["source_type"] = form
                result["extraction_phrase"] = intro
                result["extraction_confidence"] = "HIGH"
                return result

        # Fallback: extract the first substantive sentence from the Business section
        for section_marker in ("item 1. business", "item 1.  business", "part i", "business overview"):
            business_idx = lower.find(section_marker)
            if business_idx >= 0:
                # Skip past the section header
                search_start = business_idx + len(section_marker)
                search_end = min(len(text), business_idx + 15000)
                section = text[search_start:search_end]
                section_lower = section.lower()
                # Find the first substantive sentence (skip boilerplate, look for descriptive text)
                for word in ("we design", "we develop", "we manufacture", "the company designs",
                             "the company develops", "we are a", "the company is a"):
                    word_idx = section_lower.find(word)
                    if word_idx >= 50 and word_idx < 2000:
                        abs_idx = search_start + word_idx
                        snippet = text[abs_idx:min(len(text), abs_idx + 500)].strip()
                        first_period = snippet.find(".")
                        if first_period > 0 and first_period < 450:
                            snippet = snippet[:first_period + 1]
                        result["mission_text"] = snippet[:500]
                        result["source_type"] = form
                        result["extraction_phrase"] = f"{word} (Business section)"
                        result["extraction_confidence"] = "MEDIUM"
                        return result
                break

    return result
