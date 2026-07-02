from __future__ import annotations

from typing import Any


_TECHNICAL_SOURCES = {"vpma", "gamma", "earnings"}
_OWNERSHIP_SOURCES = {"congress", "insider"}
_FORWARD_SOURCES = {"fundamental_inflection"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


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


def _risk_flags(candidate: dict[str, Any]) -> list[str]:
    flags: list[str] = []

    conflict = _clean(candidate.get("Conflict Status")).upper()
    if conflict == "MIXED":
        flags.append("mixed scanner evidence")

    btd_gate = _clean(candidate.get("BTD Gate")).upper()
    if btd_gate in {"FAIL", "UNAVAILABLE"}:
        flags.append("BTD economics not confirmed")
    elif btd_gate == "NOT_APPLICABLE":
        flags.append("BTD not applicable")

    feroldi_gate = _clean(candidate.get("Feroldi Gate")).upper()
    if feroldi_gate == "FAIL":
        flags.append("weak first-cut quality")
    elif feroldi_gate == "LOW_COVERAGE":
        flags.append("Feroldi coverage still thin")
    elif feroldi_gate == "PENDING":
        flags.append("Feroldi first cut pending")

    missing_inputs = _clean(candidate.get("Feroldi Missing Inputs"))
    if missing_inputs:
        flags.append(f"missing {missing_inputs}")

    red_flags = _clean(candidate.get("AI Red Flags"))
    if red_flags:
        flags.append(red_flags)

    return flags[:3]


def _decision_lane(candidate: dict[str, Any], families: list[str], risks: list[str]) -> tuple[str, str]:
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

    if feroldi_gate == "PASS":
        if conflict == "MIXED":
            return "WAITING_CONFIRMATION", "Economics and quality passed, but scanner evidence is mixed."
        if family_count >= 2 or strong_corroboration or (has_technical and has_ownership) or has_forward:
            return "RESEARCH_NOW", "Economics passed and the thesis has enough independent confirmation to study now."
        return "WAITING_CONFIRMATION", "Economics passed, but the thesis still needs another confirming layer."

    if feroldi_gate == "REVIEW":
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
    decision_lane: str,
    risks: list[str],
) -> str:
    parts = [
        f"Attention {attention_family.lower()}",
        f"technical {technical_confirmation.lower()}",
        f"ownership {ownership_confirmation.lower()}",
        f"forward {forward_confirmation.lower()}",
        f"lane {decision_lane.lower()}",
    ]
    if risks:
        parts.append(f"risks: {', '.join(risks)}")
    return " | ".join(parts)


def apply_candidate_judgment(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(candidate)
    sources = _source_set(candidate)
    families = _source_families(sources)
    attention_family = " + ".join(families) if families else "NONE"
    technical_confirmation = _technical_confirmation(sources)
    ownership_confirmation = _ownership_confirmation(sources)
    forward_confirmation = _forward_confirmation(sources)
    risks = _risk_flags(candidate)
    decision_lane, decision_reason = _decision_lane(candidate, families, risks)

    candidate["Attention Family"] = attention_family
    candidate["Technical Confirmation"] = technical_confirmation
    candidate["Ownership Confirmation"] = ownership_confirmation
    candidate["Forward Confirmation"] = forward_confirmation
    candidate["Risk Flags"] = ", ".join(risks)
    candidate["Decision Lane"] = decision_lane
    candidate["Decision Lane Reason"] = decision_reason
    candidate["Thesis Summary"] = _thesis_summary(
        candidate,
        attention_family=attention_family,
        technical_confirmation=technical_confirmation,
        ownership_confirmation=ownership_confirmation,
        forward_confirmation=forward_confirmation,
        decision_lane=decision_lane,
        risks=risks,
    )
    return candidate
