from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from providers.sec.base import SECProvider
from providers.sec.models import FilingMetadata
from scanners.vpma.guidance_models import (
    GUIDANCE_TEXT_PATTERNS,
    EarningsFundamentalConfirmation,
    EvidenceItem,
    kpi_candidates_for_industry,
)


logger = logging.getLogger(__name__)

REVENUE_RANGE_PATTERN = re.compile(
    r"(?:(?:full[-\s]?year|FY\s?202\d|fiscal\s+year\s+202\d|guidance)[^.]{0,120}?)"
    r"revenu[es]\s*(?:of|in\s*the\s*range\s*of|between|guidance\s*of)?\s*"
    r"\$?([\d,.]+)\s*(?:billion|million|B|M)?\s*(?:to|-|–|—)\s*"
    r"\$?([\d,.]+)\s*(?:billion|million|B|M)?",
    re.IGNORECASE,
)

REVENUE_SINGLE_PATTERN = re.compile(
    r"(?:revenue\s*guidance|expects?\s*(?:full[-\s]?year\s*)?revenu[es])\s*(?:of|to\s*be)\s*"
    r"\$?([\d,.]+)\s*(?:billion|million|B|M)?",
    re.IGNORECASE,
)

GUIDANCE_ACTION_PATTERN = re.compile(
    r"(?:"
    r"raises?\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"raising\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"raised\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"lowers?\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"lowering\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"lowered\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"reaffirms?\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"reaffirmed\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"maintains?\s*(?:its\s*)?(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance|"
    r"withdraws?\s*(?:its\s*)?(?:full[-\s]?year\s*)?guidance|"
    r"withdrawn\s*(?:its\s*)?(?:full[-\s]?year\s*)?guidance|"
    r"suspend(?:s|ed)\s*guidance|"
    r"(?:full[-\s]?year\s*)?(?:revenu[es]?\s*)?guidance\s*(?:was\s*)?(?:raised|lowered|reaffirmed|maintained|withdrawn|reduced)"
    r")",
    re.IGNORECASE,
)

GROWTH_PATTERN = re.compile(
    r"(?:revenu[es]\s*(?:growth|increase|grew|was\s+up|representing\s*growth))\s*(?:of|by)?\s*([\d.]+)\s*%",
    re.IGNORECASE,
)

GROWTH_WAS_PATTERN = re.compile(
    r"(?:was\s*\$[\d,.]+\s*(?:billion|million|B|M)?[^.]*?)(?:representing|or)\s*(?:growth|increase|decline|decrease)\s*(?:of|by)?\s*([\d.-]+)\s*%",
    re.IGNORECASE,
)

GROWTH_DECLINE_PATTERN = re.compile(
    r"(?:declining|declined|decreased|fell|dropped)\s*(?:by)?\s*([\d.]+)\s*%",
    re.IGNORECASE,
)

GROSS_MARGIN_PATTERN = re.compile(
    r"(?:gross\s*margin|gross\s*profit\s*margin)\s*(?:of|was|is|at|improved\s*to|expanded\s*to)?\s*([\d.]+)\s*%",
    re.IGNORECASE,
)

GROSS_MARGIN_CHANGE_PATTERN = re.compile(
    r"(?:gross\s*margin|gross\s*profit\s*margin)"
    r"[^\n]{0,300}?"
    r"(?:an?\s+)?(?:increase|increased|expand|expanded|improve|improved|decrease|decreased|contract|contracted|decline|declined)\s*"
    r"(?:of|by)?\s*([\d.]+)\s*(?:basis\s*points|bps|percentage\s*points)",
    re.IGNORECASE,
)

OPERATING_MARGIN_PATTERN = re.compile(
    r"(?:operating\s*margin|operating\s*income\s*margin)\s*(?:of|was|is|at)\s*([\d.]+)\s*%",
    re.IGNORECASE,
)

FCF_PATTERN = re.compile(
    r"(?:free\s*cash\s*flow|FCF)\s*(?:of|was|is|generated)\s*"
    r"\$?([\d,.]+)\s*(?:billion|million|B|M)?",
    re.IGNORECASE,
)

REVENUE_REPORTED_PATTERN = re.compile(
    r"(?:revenu[es]?\s*(?:of|was|is|were|total(?:ed)?)\s*)"
    r"\$?([\d,.]+)\s*(?:billion|million|B|M)",
    re.IGNORECASE,
)

YOY_GROWTH_SIMPLE_PATTERN = re.compile(
    r"(?:YoY|year[-\s]?over[-\s]?year|compared\s*to)\s*(?:revenu[es]\s*(?:growth|increase)?\s*(?:of|by)?)?\s*([\d.]+)\s*%",
    re.IGNORECASE,
)

EXPLICIT_MARGIN_GUIDANCE_PATTERN = re.compile(
    r"(?:operating\s*margin|EBITDA\s*margin|gross\s*margin)\s*(?:guidance|outlook|expected|expects?)\s*"
    r"(?:of|at|to\s*be)\s*(?:approximately\s*)?([\d.]+)\s*%",
    re.IGNORECASE,
)

MARGIN_GUIDANCE_ACTION_PATTERN = re.compile(
    r"(raises?\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"raising\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"raised\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"lowers?\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"lowered\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"reaffirms?\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook)|"
    r"reaffirmed\s*(?:its\s*)?(?:operating|EBITDA|gross)?\s*(?:margin\s*)?(?:guidance|outlook))",
    re.IGNORECASE,
)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _scale_billion_million(value: float, unit_hint: str) -> float:
    hint = unit_hint.strip().lower() if unit_hint else ""
    if "billion" in hint or hint == "b":
        return value * 1_000_000_000
    if "million" in hint or hint == "m":
        return value * 1_000_000
    return value


def _extract_revenue_range(text: str) -> tuple[float | None, float | None, str]:
    for match in REVENUE_RANGE_PATTERN.finditer(text):
        low = _to_float(match.group(1))
        high = _to_float(match.group(2))
        if low is not None and high is not None:
            return low, high, match.group(0)[:200]
    for match in REVENUE_SINGLE_PATTERN.finditer(text):
        value = _to_float(match.group(1))
        if value is not None:
            return value, value, match.group(0)[:200]
    return None, None, ""


def _extract_revenue_growth(text: str) -> float | None:
    for match in GROWTH_PATTERN.finditer(text):
        val = _to_float(match.group(1))
        if val is not None:
            return val
    for match in GROWTH_WAS_PATTERN.finditer(text):
        val = _to_float(match.group(1))
        if val is not None:
            return val
    for match in YOY_GROWTH_SIMPLE_PATTERN.finditer(text):
        val = _to_float(match.group(1))
        if val is not None:
            return val
    for match in GROWTH_DECLINE_PATTERN.finditer(text):
        val = _to_float(match.group(1))
        if val is not None:
            return -val
    return None


def _extract_gross_margin(text: str) -> tuple[float | None, float | None]:
    margin = None
    change = None
    for match in GROSS_MARGIN_PATTERN.finditer(text):
        margin = _to_float(match.group(1))
        break
    for match in GROSS_MARGIN_CHANGE_PATTERN.finditer(text):
        raw = match.group(1)
        bps_text = match.group(0).lower()
        val = _to_float(raw)
        if val is not None:
            if "percentage points" in bps_text or "pp" in bps_text:
                change = val * 100.0
            else:
                change = val
        break
    return margin, change


def _extract_operating_margin(text: str) -> float | None:
    for match in OPERATING_MARGIN_PATTERN.finditer(text):
        return _to_float(match.group(1))
    return None


def _extract_fcf(text: str) -> float | None:
    for match in FCF_PATTERN.finditer(text):
        return _to_float(match.group(1))
    return None


def _extract_reported_revenue(text: str) -> float | None:
    for match in REVENUE_REPORTED_PATTERN.finditer(text):
        return _to_float(match.group(1))
    return None


def _detect_guidance_action(text: str) -> str:
    for match in GUIDANCE_ACTION_PATTERN.finditer(text):
        action_text = match.group(0).lower()
        for action, patterns in GUIDANCE_TEXT_PATTERNS.items():
            for pattern in patterns:
                if pattern.lower() in action_text:
                    return action
    return "NOT_PROVIDED"


def _detect_margin_guidance_action(text: str) -> str:
    for match in MARGIN_GUIDANCE_ACTION_PATTERN.finditer(text):
        action_text = match.group(0).lower()
        if any(word in action_text for word in ("raises", "raising", "raised")):
            return "RAISED"
        if any(word in action_text for word in ("lowers", "lowering", "lowered")):
            return "LOWERED"
        if any(word in action_text for word in ("reaffirms", "reaffirmed", "maintains")):
            return "MAINTAINED"
    return "NOT_PROVIDED"


def _classify_by_midpoint(
    change_pct: float | None,
    *,
    explicit_action: str = "",
) -> str:
    if change_pct is None:
        return explicit_action or "NOT_PROVIDED"
    if change_pct >= 2.0:
        return "RAISED" if explicit_action not in {"LOWERED", "MODESTLY_LOWERED"} else "MIXED"
    if change_pct >= 0.5:
        return "MODESTLY_RAISED"
    if change_pct >= -0.5:
        return "MAINTAINED"
    if change_pct >= -2.0:
        return "MODESTLY_LOWERED"
    return "LOWERED"


def _extract_kpi_from_text(text: str, kpi_names: list[str]) -> dict[str, float | str]:
    results: dict[str, float | str] = {}
    lines = text.split("\n")
    for kpi_name in kpi_names:
        for line in lines:
            if kpi_name.lower() in line.lower():
                pct_match = re.search(rf"{re.escape(kpi_name)}[^0-9]*?([\d,.]+)\s*%", line, re.IGNORECASE)
                val_match = re.search(rf"{re.escape(kpi_name)}[^0-9]*?\$?([\d,.]+)\s*(?:billion|million|B|M)?", line, re.IGNORECASE)
                if pct_match:
                    results[kpi_name] = _to_float(pct_match.group(1)) or pct_match.group(1)
                    break
                if val_match:
                    results[kpi_name] = _to_float(val_match.group(1)) or val_match.group(1)
                    break
    return results


def find_earnings_8k(
    provider: SECProvider,
    ticker: str,
    earnings_timestamp: datetime,
) -> FilingMetadata | None:
    earnings_date = earnings_timestamp.date()
    search_start = earnings_date - timedelta(days=1)
    all_8k = provider.recent_filings(ticker, forms={"8-K"}, filed_after=search_start - timedelta(days=1))

    candidates: list[FilingMetadata] = []
    for filing in all_8k:
        filing_date = filing.filed_at.date()
        if filing_date < earnings_date - timedelta(days=1):
            continue
        if filing_date > earnings_date + timedelta(days=3):
            continue
        candidates.append(filing)

    if not candidates:
        return None

    for filing in candidates:
        try:
            doc_list = provider.filing_documents(filing)
        except Exception:
            continue
        has_99 = any("99" in doc.document_name or "ex99" in doc.document_name.lower() for doc in doc_list)
        if has_99:
            return filing

    return candidates[0] if candidates else None


def _find_exhibit_99(
    provider: SECProvider,
    filing: FilingMetadata,
) -> tuple[str | None, str]:
    try:
        docs = provider.filing_documents(filing)
    except Exception:
        return None, ""
    for doc in docs:
        name_lower = doc.document_name.lower()
        if "ex99" in name_lower or "99.1" in name_lower or "ex-99" in name_lower:
            return doc.document_name, doc.source_url
    for doc in docs:
        if doc.is_primary:
            return doc.document_name, doc.source_url
    return None, ""


def extract_confirmation(
    provider: SECProvider,
    ticker: str,
    industry: str,
    earnings_timestamp: datetime,
) -> EarningsFundamentalConfirmation:
    earnings_date = earnings_timestamp.date()
    confirmation = EarningsFundamentalConfirmation(
        ticker=ticker,
        earnings_date=earnings_date,
        source_accession=None,
        economic_classification="ECONOMIC_UNAVAILABLE",
        confidence="low",
    )

    filing = find_earnings_8k(provider, ticker, earnings_timestamp)
    if filing is None:
        confirmation.conflict_flags.append("no_filing_match")
        return confirmation

    confirmation.source_accession = filing.accession
    exhibit_name, _ = _find_exhibit_99(provider, filing)

    try:
        document = provider.filing_text(filing, document_name=exhibit_name)
        text = document.text
    except Exception:
        confirmation.conflict_flags.append("filing_text_unavailable")
        return confirmation

    if not text.strip():
        confirmation.conflict_flags.append("empty_filing_text")
        return confirmation

    evidence: list[EvidenceItem] = []

    revenue_growth = _extract_revenue_growth(text)
    if revenue_growth is not None:
        evidence.append(EvidenceItem(
            field="revenue_growth_yoy",
            extracted_value=revenue_growth,
            accession=filing.accession,
            document=exhibit_name or "",
            section="earnings release",
            supporting_text=f"Revenue growth: {revenue_growth}%",
            extraction_method="regex",
            confidence="medium",
        ))
        confirmation.revenue_growth_yoy = revenue_growth

    reported_rev = _extract_reported_revenue(text)
    if reported_rev is not None:
        confirmation.reported_revenue = reported_rev
        evidence.append(EvidenceItem(
            field="reported_revenue",
            extracted_value=reported_rev,
            accession=filing.accession,
            document=exhibit_name or "",
            section="earnings release",
            supporting_text=f"Reported revenue: ${reported_rev:,.0f}",
            extraction_method="regex",
            confidence="medium",
        ))

    gross_margin, gross_change = _extract_gross_margin(text)
    if gross_margin is not None:
        confirmation.gross_margin_pct = gross_margin
        evidence.append(EvidenceItem(
            field="gross_margin_pct",
            extracted_value=gross_margin,
            accession=filing.accession,
            document=exhibit_name or "",
            section="earnings release",
            supporting_text=f"Gross margin: {gross_margin}%",
            extraction_method="regex",
            confidence="medium",
        ))
    if gross_change is not None:
        confirmation.gross_margin_change_bps = gross_change
        evidence.append(EvidenceItem(
            field="gross_margin_change_bps",
            extracted_value=gross_change,
            accession=filing.accession,
            document=exhibit_name or "",
            section="earnings release",
            supporting_text=f"Gross margin change: {gross_change} bps",
            extraction_method="regex",
            confidence="medium",
        ))

    op_margin = _extract_operating_margin(text)
    if op_margin is not None:
        confirmation.operating_margin_pct = op_margin

    fcf = _extract_fcf(text)
    if fcf is not None:
        confirmation.free_cash_flow = fcf

    explicit_action = _detect_guidance_action(text)
    rev_low, rev_high, rev_context = _extract_revenue_range(text)
    if rev_low is not None and rev_high is not None:
        midpoint = (rev_low + rev_high) / 2.0
        confirmation.revenue_guidance_low = rev_low
        confirmation.revenue_guidance_high = rev_high
        confirmation.revenue_guidance_midpoint = midpoint
        evidence.append(EvidenceItem(
            field="revenue_guidance_range",
            extracted_value=f"{rev_low}-{rev_high}",
            accession=filing.accession,
            document=exhibit_name or "",
            section="earnings release",
            supporting_text=rev_context,
            extraction_method="regex",
            confidence="medium",
        ))
        if confirmation.revenue_guidance_change_pct is None:
            final_action = _classify_by_midpoint(None, explicit_action=explicit_action)
            confirmation.revenue_guidance_action = final_action
        else:
            final_action = _classify_by_midpoint(
                confirmation.revenue_guidance_change_pct,
                explicit_action=explicit_action,
            )
            if final_action == "MIXED":
                confirmation.revenue_guidance_action = explicit_action or "NOT_PROVIDED"
                confirmation.conflict_flags.append("numeric_vs_explicit_guidance_conflict")
            else:
                confirmation.revenue_guidance_action = final_action
    else:
        if explicit_action and explicit_action != "NOT_PROVIDED":
            confirmation.revenue_guidance_action = explicit_action
        else:
            confirmation.revenue_guidance_action = "NOT_PROVIDED"

    margin_action = _detect_margin_guidance_action(text)
    if margin_action and margin_action != "NOT_PROVIDED":
        confirmation.margin_guidance_action = margin_action

    kpi_names = kpi_candidates_for_industry(industry)
    if kpi_names:
        kpis = _extract_kpi_from_text(text, kpi_names)
        if kpis:
            confirmation.business_kpis = kpis

    confirmation.evidence = evidence
    confirmation.confidence = "medium" if evidence else "low"
    return confirmation
