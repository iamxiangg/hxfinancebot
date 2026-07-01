from __future__ import annotations

"""M01–M03 deterministic management & culture scoring functions."""

import re
from datetime import date, datetime, timezone

from funnel.feroldi_config import (
    CEO_SERVED_SINCE_MARKERS,
    FOUNDER_MARKERS,
    INTERIM_MARKERS,
    OWNERSHIP_TIER_1,
    OWNERSHIP_TIER_2,
    OWNERSHIP_TIER_3,
    TENURE_10_PLUS,
    TENURE_2_TO_5,
    TENURE_5_TO_10,
    TENURE_FOUNDER,
    TENURE_UNDER_2,
)
from funnel.feroldi_models import (
    M01SoulInGameResult,
    M02OwnershipResult,
    M03MissionResult,
)
from funnel.feroldi_mission import analyse_mission


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_year(text: str) -> int | None:
    """Extract the first valid 4-digit year (1900–2100) from text.

    Finds all 4-digit sequences and returns the first one in range.
    This avoids tripping over HTML entities like &#8217; (which contains
    the digits 8217) that appear before the actual year.
    """
    for match in re.finditer(r"(\d{4})", str(text)):
        year = int(match.group(1))
        if 1900 <= year <= 2100:
            return year
    return None


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(marker.lower() in lower for marker in markers)


# ---------------------------------------------------------------------------
# M01 — Soul in the game (max 4)
# ---------------------------------------------------------------------------


def score_m01(
    *,
    evidence_text: str = "",
    source_type: str = "",
    source_filing_date: str = "",
    source_accession: str = "",
    source_url: str = "",
    extraction_confidence: str = "UNKNOWN",
    last_updated: str = "",
) -> M01SoulInGameResult:
    result = M01SoulInGameResult(
        evidence_text=evidence_text,
        primary_source_type=source_type,
        primary_source_filing_date=source_filing_date,
        primary_source_accession=source_accession,
        primary_source_url=source_url,
        extraction_confidence=extraction_confidence.upper(),
        last_updated=last_updated or "",
    )

    # Only HIGH or MEDIUM confidence can be scored
    if result.extraction_confidence not in {"HIGH", "MEDIUM"}:
        result.reason = f"Confidence {result.extraction_confidence} — manual review required"
        return result

    result.available = 4.0
    evidence_lower = evidence_text.lower()

    # Check founder/co-founder status (overrides tenure)
    if _contains_any(evidence_text, FOUNDER_MARKERS):
        result.founder_flag = True
        # Determine specific type
        if "co-founded" in evidence_lower or "co-founder" in evidence_lower or "cofounded" in evidence_lower:
            result.cofounder_flag = True
        elif "founding family" in evidence_lower or "founding-family" in evidence_lower:
            result.founding_family_flag = True
        result.score = TENURE_FOUNDER
        result.reason = "Founder/co-founder/founding-family CEO"
        return result

    # Check interim CEO
    if _contains_any(evidence_text, INTERIM_MARKERS):
        result.interim_ceo_flag = True
        result.score = TENURE_UNDER_2
        result.reason = "Interim CEO"
        return result

    # Extract tenure from "has served as CEO since ..."
    tenure_years: float | None = None
    appointment_year: int | None = None
    for marker in CEO_SERVED_SINCE_MARKERS:
        if marker in evidence_lower:
            idx = evidence_lower.index(marker) + len(marker)
            remainder = evidence_text[idx:].strip()
            year = _extract_year(remainder)
            if year:
                appointment_year = year
                result.ceo_appointment_year = year
                result.ceo_date_precision = "year"
                current_year = _current_year()
                tenure_years = float(current_year - year)
                result.ceo_tenure_years = tenure_years
                break

    if tenure_years is None:
        result.reason = "Cannot determine CEO tenure from evidence"
        return result

    # Score by tenure bucket
    if tenure_years >= 10:
        result.score = TENURE_10_PLUS
        result.reason = f"CEO tenure {tenure_years:.0f} years (>= 10)"
    elif tenure_years >= 5:
        result.score = TENURE_5_TO_10
        result.reason = f"CEO tenure {tenure_years:.0f} years (5–10)"
    elif tenure_years >= 2:
        result.score = TENURE_2_TO_5
        result.reason = f"CEO tenure {tenure_years:.0f} years (2–5)"
    elif tenure_years < 2:
        result.score = TENURE_UNDER_2
        result.reason = f"CEO tenure {tenure_years:.1f} years (< 2)"

    return result


def _current_year() -> int:
    return datetime.now(timezone.utc).year


# ---------------------------------------------------------------------------
# M02 — Insider ownership alignment (max 3)
# ---------------------------------------------------------------------------


def score_m02(
    *,
    ceo_beneficial_shares: float | None = None,
    basic_shares_outstanding: float | None = None,
    current_share_price: float | None = None,
    directors_officers_group_pct: float | None = None,
    ownership_basis: str = "",
    source_url: str = "",
    source_date: str = "",
    extraction_confidence: str = "UNKNOWN",
) -> M02OwnershipResult:
    result = M02OwnershipResult(
        ceo_beneficial_shares=ceo_beneficial_shares,
        basic_shares_outstanding=basic_shares_outstanding,
        current_share_price=current_share_price,
        directors_officers_group_pct=directors_officers_group_pct,
        ownership_basis=ownership_basis,
        source=source_url,
        source_date=source_date,
        extraction_confidence=extraction_confidence.upper(),
    )

    if result.extraction_confidence not in {"HIGH", "MEDIUM"}:
        result.reason = "No reliable ownership evidence"
        return result

    # Calculate CEO ownership %
    if ceo_beneficial_shares is not None and basic_shares_outstanding is not None and basic_shares_outstanding > 0:
        result.ceo_ownership_pct = ceo_beneficial_shares / basic_shares_outstanding

    # Calculate CEO stake value
    if ceo_beneficial_shares is not None and current_share_price is not None and current_share_price > 0:
        result.ceo_stake_value_usd = ceo_beneficial_shares * current_share_price

    if result.ceo_ownership_pct is None and result.ceo_stake_value_usd is None and directors_officers_group_pct is None:
        result.reason = "No ownership data available"
        return result

    result.available = 3.0

    ceo_pct = result.ceo_ownership_pct or 0
    ceo_stake = result.ceo_stake_value_usd or 0
    group_pct = directors_officers_group_pct or 0

    # Tier 3 (highest): score 3
    if (ceo_pct >= OWNERSHIP_TIER_3["ceo_pct"]
            or ceo_stake >= OWNERSHIP_TIER_3["ceo_stake"]
            or group_pct >= OWNERSHIP_TIER_3["group_pct"]):
        result.score = 3.0
        result.reason = f"CEO owns {ceo_pct * 100:.2f}%, stake ${ceo_stake:,.0f}, group {group_pct * 100:.1f}% — highest tier"
        return result

    # Tier 2: score 2
    if (ceo_pct >= OWNERSHIP_TIER_2["ceo_pct"]
            or ceo_stake >= OWNERSHIP_TIER_2["ceo_stake"]
            or group_pct >= OWNERSHIP_TIER_2["group_pct"]):
        result.score = 2.0
        result.reason = f"CEO owns {ceo_pct * 100:.2f}%, stake ${ceo_stake:,.0f}, group {group_pct * 100:.1f}% — middle tier"
        return result

    # Tier 1: score 1
    if (ceo_pct >= OWNERSHIP_TIER_1["ceo_pct"]
            or ceo_stake >= OWNERSHIP_TIER_1["ceo_stake"]
            or group_pct >= OWNERSHIP_TIER_1["group_pct"]):
        result.score = 1.0
        result.reason = f"CEO owns {ceo_pct * 100:.2f}%, stake ${ceo_stake:,.0f}, group {group_pct * 100:.1f}% — lowest tier"
        return result

    # Below all thresholds
    result.score = 0.0
    result.reason = f"CEO owns {ceo_pct * 100:.2f}%, stake ${ceo_stake:,.0f} — below all thresholds"
    return result


# ---------------------------------------------------------------------------
# M03 — Mission statement quality (max 3)
# ---------------------------------------------------------------------------


def score_m03(
    *,
    mission_text: str = "",
    source_type: str = "",
    source_url: str = "",
    source_date: str = "",
    extraction_phrase: str = "",
    extraction_confidence: str = "UNKNOWN",
    last_updated: str = "",
) -> M03MissionResult:
    result = M03MissionResult(
        mission_text=mission_text,
        source_url=source_url,
        source=source_type,
        source_date=source_date,
        extraction_phrase=extraction_phrase,
        extraction_confidence=extraction_confidence.upper(),
    )

    if not mission_text.strip():
        result.reason = "No mission statement found"
        return result

    if extraction_confidence not in {"HIGH", "MEDIUM"}:
        result.reason = f"Low-confidence mission extraction: {extraction_confidence}"
        return result

    # Delegate to mission analysis module
    analysis = analyse_mission(mission_text)

    result.word_count = analysis["word_count"]
    result.sentence_count = analysis["sentence_count"]
    result.structural_punctuation_count = analysis["punctuation_count"]
    result.parenthetical_count = analysis["parenthetical_count"]
    result.action_verb_found = analysis["action_verb_found"]
    result.object_or_offering_found = analysis["object_found"]
    result.beneficiary_found = analysis["beneficiary_found"]
    result.outcome_found = analysis["outcome_found"]
    result.undefined_acronym_count = analysis["undefined_acronym_count"]
    result.vague_term_count = analysis["vague_term_count"]
    result.financial_only_flag = analysis["financial_only_flag"]

    result.simple_point = analysis["simple_point"]
    result.clear_point = analysis["clear_point"]
    result.inspirational_point = analysis["inspirational_point"]

    result.available = 3.0
    result.score = float(result.simple_point + result.clear_point + result.inspirational_point)
    result.reason = (
        f"Simple={result.simple_point}, Clear={result.clear_point}, "
        f"Inspirational={result.inspirational_point}"
    )

    return result
