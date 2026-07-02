from __future__ import annotations

import hashlib
import math
from typing import Any


_TECHNICAL_SOURCES = {"vpma", "gamma", "earnings"}
_OWNERSHIP_SOURCES = {"congress", "insider"}
_FORWARD_SOURCES = {"fundamental_inflection"}
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _source_set(candidate: dict[str, Any]) -> set[str]:
    return {
        part.strip().lower()
        for part in str(candidate.get("Source") or "").split(",")
        if part.strip()
    }


def _source_families(sources: set[str]) -> list[str]:
    families: list[str] = []
    if sources & _TECHNICAL_SOURCES:
        families.append("TECHNICAL")
    if sources & _OWNERSHIP_SOURCES:
        families.append("OWNERSHIP")
    if sources & _FORWARD_SOURCES:
        families.append("FORWARD")
    if "manual" in sources and not families:
        families.append("MANUAL")
    if not families and sources:
        families.append("OTHER")
    return families


def _ownership_confirmation(sources: set[str]) -> str:
    has_congress = "congress" in sources
    has_insider = "insider" in sources
    if has_congress and has_insider:
        return "POLITICAL + INSIDER"
    if has_congress:
        return "POLITICAL"
    if has_insider:
        return "INSIDER"
    return "NONE"


def _technical_confirmation(sources: set[str]) -> str:
    return "YES" if sources & _TECHNICAL_SOURCES else "NO"


def _forward_confirmation(sources: set[str]) -> str:
    return "YES" if sources & _FORWARD_SOURCES else "NO"


def _forward_confirmation_metrics(candidate: dict[str, Any], sources: set[str]) -> tuple[str, float, str]:
    classification = _clean(candidate.get("Fundamental Inflection Classification")).upper()
    score = _to_float(candidate.get("Fundamental Inflection Score")) or 0.0
    pillars = _clean(candidate.get("Fundamental Inflection Pillars"))
    revenue_growth = _to_float(candidate.get("Fundamental Inflection Revenue Growth"))
    growth_acceleration = _to_float(candidate.get("Fundamental Inflection Growth Acceleration"))
    gross_margin_bps = _to_float(candidate.get("Fundamental Inflection Gross Margin Change Bps"))
    op_margin_bps = _to_float(candidate.get("Fundamental Inflection Operating Margin Change Bps"))
    fcf_margin_bps = _to_float(candidate.get("Fundamental Inflection FCF Margin Change Bps"))

    parts: list[str] = []
    if classification:
        parts.append(classification.replace("_", " ").lower())
    if pillars:
        parts.append(f"pillars {pillars}")
    if revenue_growth is not None:
        parts.append(f"revenue growth {revenue_growth * 100:.1f}%")
    if growth_acceleration is not None:
        parts.append(f"acceleration {growth_acceleration * 100:.1f}%")
    if gross_margin_bps is not None and gross_margin_bps > 0:
        parts.append(f"gross margin +{gross_margin_bps:.0f} bps")
    if op_margin_bps is not None and op_margin_bps > 0:
        parts.append(f"operating margin +{op_margin_bps:.0f} bps")
    if fcf_margin_bps is not None and fcf_margin_bps > 0:
        parts.append(f"FCF margin +{fcf_margin_bps:.0f} bps")

    if "fundamental_inflection" not in sources:
        return "NONE", 0.0, ""
    if classification == "STRONG_INFLECTION":
        return "STRONG", max(score, 90.0), "; ".join(parts)
    if classification == "VALIDATED_INFLECTION":
        return "VALIDATED", max(score, 75.0), "; ".join(parts)
    if classification == "EARLY_INFLECTION":
        return "EARLY", max(score, 60.0), "; ".join(parts)
    return "PRESENT", score, "; ".join(parts)


def _raise_severity(current: str, target: str) -> str:
    if _SEVERITY_RANK.get(target, 0) > _SEVERITY_RANK.get(current, 0):
        return target
    return current


def _thesis_breaker(candidate: dict[str, Any]) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    severity = "LOW"

    conflict = _clean(candidate.get("Conflict Status")).upper()
    if conflict == "MIXED":
        reasons.append("mixed scanner evidence")
        severity = _raise_severity(severity, "MEDIUM")

    btd_gate = _clean(candidate.get("BTD Gate")).upper()
    if btd_gate in {"FAIL", "UNAVAILABLE"}:
        reasons.append("BTD economics not confirmed")
        severity = _raise_severity(severity, "HIGH")
    elif btd_gate == "NOT_APPLICABLE":
        reasons.append("BTD not applicable")
        severity = _raise_severity(severity, "MEDIUM")

    feroldi_gate = _clean(candidate.get("Feroldi Gate")).upper()
    if feroldi_gate == "FAIL":
        reasons.append("weak first-cut quality")
        severity = _raise_severity(severity, "HIGH")
    elif feroldi_gate == "LOW_COVERAGE":
        reasons.append("Feroldi coverage still thin")
        severity = _raise_severity(severity, "MEDIUM")
    elif feroldi_gate == "PENDING":
        reasons.append("Feroldi first cut pending")
        severity = _raise_severity(severity, "MEDIUM")

    risk_flags = {
        flag.strip().lower()
        for flag in (
            _clean(candidate.get("Fundamental Inflection Risk Flags")).split(",")
            + _clean(candidate.get("AI Red Flags")).split(",")
        )
        if flag.strip()
    }
    severe_flags = {
        "severe_dilution", "severe_dilution_veto", "cash_runway_risk", "severe_margin_deterioration",
    }
    medium_flags = {
        "high_dilution", "operating_margin_deterioration", "elevated_dilution",
    }
    if risk_flags & severe_flags:
        reasons.extend(sorted(risk_flags & severe_flags))
        severity = _raise_severity(severity, "HIGH")
    elif risk_flags & medium_flags:
        reasons.extend(sorted(risk_flags & medium_flags))
        severity = _raise_severity(severity, "MEDIUM")

    missing_inputs = _clean(candidate.get("Feroldi Missing Inputs"))
    if missing_inputs:
        reasons.append(f"missing {missing_inputs}")

    return severity, ", ".join(reasons[:4]), reasons[:4]


def _decision_lane(
    candidate: dict[str, Any],
    families: list[str],
    risks: list[str],
    *,
    forward_status: str,
    breaker_severity: str,
) -> tuple[str, str]:
    btd_gate = _clean(candidate.get("BTD Gate")).upper()
    feroldi_gate = _clean(candidate.get("Feroldi Gate")).upper()
    corroboration = _clean(candidate.get("Corroboration Level")).upper()
    conflict = _clean(candidate.get("Conflict Status")).upper()

    family_count = len(families)
    has_forward = "FORWARD" in families
    has_ownership = "OWNERSHIP" in families
    has_technical = "TECHNICAL" in families
    strong_corroboration = corroboration in {"STRONG", "EXCEPTIONAL"}

    if btd_gate in {"FAIL", "UNAVAILABLE"}:
        return "REJECT", "BTD economics did not clear the minimum hurdle."

    if btd_gate == "NOT_APPLICABLE":
        return "WAITING_CONFIRMATION", "Economics need a non-BTD review path for this business model."

    if breaker_severity == "HIGH" and feroldi_gate != "PASS":
        return "WATCH", "Economics passed, but there is a high-severity breaker that still needs resolving."

    if feroldi_gate == "PASS":
        if conflict == "MIXED":
            return "WAITING_CONFIRMATION", "Economics and quality passed, but scanner evidence is mixed."
        if forward_status in {"STRONG", "VALIDATED"}:
            return "RESEARCH_NOW", "Economics, quality, and forward business confirmation are all aligned."
        if family_count >= 2 or strong_corroboration or (has_technical and has_ownership) or has_forward:
            return "RESEARCH_NOW", "Economics passed and the thesis has enough independent confirmation to study now."
        return "WAITING_CONFIRMATION", "Economics passed, but the thesis still needs another confirming layer."

    if feroldi_gate == "REVIEW":
        if forward_status in {"STRONG", "VALIDATED"}:
            return "RESEARCH_NOW", "Borderline quality is offset by strong forward confirmation, but it still needs human judgment."
        if has_forward or (has_technical and has_ownership) or strong_corroboration:
            return "WAITING_CONFIRMATION", "Economics passed, but business quality is only borderline and needs confirmation."
        return "WATCH", "Economics passed, but quality is not yet strong enough for promotion."

    if feroldi_gate in {"FAIL", "LOW_COVERAGE", "PENDING"}:
        if not risks:
            return "WATCH", "Economics passed, but quality evidence is still incomplete."
        return "WATCH", "Economics passed, but the current evidence is still too incomplete or fragile."

    return "WAITING_CONFIRMATION", "The candidate needs a clearer read before promotion."


def _thesis_summary(
    candidate: dict[str, Any],
    *,
    attention_family: str,
    technical_confirmation: str,
    ownership_confirmation: str,
    forward_confirmation: str,
    forward_detail: str,
    breaker_severity: str,
    decision_lane: str,
    risks: list[str],
) -> str:
    parts = [
        f"Attention {attention_family.lower()}",
        f"technical {technical_confirmation.lower()}",
        f"ownership {ownership_confirmation.lower()}",
        f"forward {forward_confirmation.lower()}",
        f"breaker {breaker_severity.lower()}",
        f"lane {decision_lane.lower()}",
    ]
    if forward_detail:
        parts.append(f"forward detail: {forward_detail}")
    if risks:
        parts.append(f"risks: {', '.join(risks)}")
    return " | ".join(parts)


def _research_rank(
    candidate: dict[str, Any],
    *,
    forward_score: float,
    breaker_severity: str,
) -> tuple[float, str]:
    rank = 0.0
    source_count = len(_source_set(candidate))
    rank += min(20.0, source_count * 6.0)

    corroboration = _clean(candidate.get("Corroboration Level")).upper()
    rank += {"NONE": 0.0, "STANDARD": 6.0, "STRONG": 12.0, "EXCEPTIONAL": 18.0}.get(corroboration, 0.0)

    btd_ratio = _to_float(candidate.get("BTD Ratio"))
    if btd_ratio is not None and btd_ratio > 0:
        rank += max(0.0, 18.0 - min(18.0, btd_ratio * 9.0))

    feroldi_equivalent = _to_float(candidate.get("Feroldi Equivalent Score"))
    if feroldi_equivalent is not None:
        rank += min(25.0, max(0.0, feroldi_equivalent - 12.0))

    rank += min(20.0, forward_score / 5.0)

    if breaker_severity == "HIGH":
        rank -= 25.0
    elif breaker_severity == "MEDIUM":
        rank -= 10.0

    rank = max(0.0, min(100.0, round(rank, 1)))
    if rank >= 75:
        bucket = "TOP"
    elif rank >= 55:
        bucket = "STRONG"
    elif rank >= 35:
        bucket = "MEDIUM"
    else:
        bucket = "LOW"
    return rank, bucket


def apply_candidate_judgment(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(candidate)
    previous_lane = _clean(candidate.get("Decision Lane")).upper()
    previous_summary = _clean(candidate.get("Thesis Summary"))
    previous_signature = _clean(candidate.get("Telegram Last Notified Signature"))
    sources = _source_set(candidate)
    families = _source_families(sources)
    attention_family = " + ".join(families) if families else "NONE"
    technical_confirmation = _technical_confirmation(sources)
    ownership_confirmation = _ownership_confirmation(sources)
    forward_status, forward_score, forward_detail = _forward_confirmation_metrics(candidate, sources)
    forward_confirmation = forward_status
    breaker_severity, breaker_detail, risks = _thesis_breaker(candidate)
    decision_lane, decision_reason = _decision_lane(
        candidate,
        families,
        risks,
        forward_status=forward_status,
        breaker_severity=breaker_severity,
    )
    rank, rank_bucket = _research_rank(candidate, forward_score=forward_score, breaker_severity=breaker_severity)

    candidate["Attention Family"] = attention_family
    candidate["Technical Confirmation"] = technical_confirmation
    candidate["Ownership Confirmation"] = ownership_confirmation
    candidate["Forward Confirmation"] = forward_confirmation
    candidate["Forward Confirmation Score"] = round(forward_score, 1) if forward_score else ""
    candidate["Forward Confirmation Detail"] = forward_detail
    candidate["Thesis Breaker Severity"] = breaker_severity
    candidate["Thesis Breaker Detail"] = breaker_detail
    candidate["Research Rank"] = rank
    candidate["Research Rank Bucket"] = rank_bucket
    candidate["Risk Flags"] = ", ".join(risks)
    candidate["Decision Lane"] = decision_lane
    candidate["Decision Lane Reason"] = decision_reason
    thesis_summary = _thesis_summary(
        candidate,
        attention_family=attention_family,
        technical_confirmation=technical_confirmation,
        ownership_confirmation=ownership_confirmation,
        forward_confirmation=forward_confirmation,
        forward_detail=forward_detail,
        breaker_severity=breaker_severity,
        decision_lane=decision_lane,
        risks=risks,
    )
    candidate["Thesis Summary"] = thesis_summary
    candidate["Previous Decision Lane"] = previous_lane
    if previous_lane != decision_lane:
        candidate["Decision Lane Last Changed At"] = candidate.get("Decision Lane Last Changed At") or ""
    if previous_lane != decision_lane or previous_summary != thesis_summary or not previous_signature:
        prior_count = int(_to_float(candidate.get("Thesis Change Count")) or 0)
        candidate["Thesis Change Count"] = prior_count + 1
    return candidate


def review_signature(candidate: dict[str, Any]) -> str:
    parts = [
        _clean(candidate.get("Ticker")).upper(),
        _clean(candidate.get("Decision Lane")).upper(),
        _clean(candidate.get("Source")).lower(),
        _clean(candidate.get("Positive Sources")).lower(),
        _clean(candidate.get("Risk Sources")).lower(),
        _clean(candidate.get("Corroboration Level")).upper(),
        _clean(candidate.get("Conflict Status")).upper(),
        _clean(candidate.get("BTD Gate")).upper(),
        _clean(candidate.get("Feroldi Gate")).upper(),
        _clean(candidate.get("Forward Confirmation")).upper(),
        _clean(candidate.get("Thesis Breaker Severity")).upper(),
        _clean(candidate.get("Supporting Signal IDs")),
    ]
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
