from __future__ import annotations

from scanners.congress.models import MaterialStateChange, PoliticalWatchlistState, TickerPoliticalHistory
from scanners.congress.watchlist import PoliticalWatchlistConfig


MEANINGFUL_CLASSIFICATIONS = {
    "SINGLE_FILER_BULLISH_BET",
    "REPEAT_FILER_ACCUMULATION",
    "BROAD_ACCUMULATION",
    "MIXED_HIGH_ACTIVITY",
    "DISTRIBUTION",
}

SUPPORTING_RELEASE_TYPES = {"MATERIAL_AMENDMENT", "DATA_CORRECTION"}


def _classification_rank(value: str) -> int:
    return {
        "INSUFFICIENT_EVIDENCE": 0,
        "SINGLE_FILER_BULLISH_BET": 1,
        "REPEAT_FILER_ACCUMULATION": 2,
        "BROAD_ACCUMULATION": 3,
        "MIXED_HIGH_ACTIVITY": 4,
        "DISTRIBUTION": 5,
    }.get(str(value or "").strip().upper(), 0)


def detect_material_state_changes(
    previous: PoliticalWatchlistState | None,
    current: PoliticalWatchlistState,
    history: TickerPoliticalHistory,
    *,
    config: PoliticalWatchlistConfig,
) -> list[MaterialStateChange]:
    if previous is None:
        return []
    changes: list[MaterialStateChange] = []
    previous_entry = previous.current_entry_category
    current_entry = current.current_entry_category
    release_types = set(history.release_types)
    if previous.watchlist_status in {"EXPIRED", "RESOLVED"} and current.watchlist_status == "ACTIVE" and current.has_new_material_event:
        changes.append(MaterialStateChange("WATCHLIST_REACTIVATED", "A new material event reactivated an older signal."))
    if "DATA_CORRECTION" in release_types:
        changes.append(MaterialStateChange("DATA_CORRECTION", "A previously recorded disclosure was corrected or withdrawn."))
    if "MATERIAL_AMENDMENT" in release_types:
        changes.append(MaterialStateChange("MATERIAL_AMENDMENT", "A previously recorded disclosure was materially amended."))
    if previous_entry != current_entry:
        if current_entry == "ACTIONABLE":
            changes.append(
                MaterialStateChange(
                    "ENTRY_BECAME_ACTIONABLE",
                    f"Entry status moved from {previous_entry} to ACTIONABLE.",
                    previous_entry,
                    current_entry,
                )
            )
        elif previous_entry == "ACTIONABLE":
            change_type = "RISK_ESCALATION" if current_entry == "RISK" else "ENTRY_LEFT_ACTIONABLE"
            changes.append(
                MaterialStateChange(
                    change_type,
                    f"Entry status moved from ACTIONABLE to {current_entry}.",
                    previous_entry,
                    current_entry,
                )
            )
        elif previous_entry == "WAIT" and current_entry == "RISK":
            changes.append(MaterialStateChange("RISK_ESCALATION", "Entry status deteriorated from WAIT to RISK.", previous_entry, current_entry))
        elif previous_entry == "RISK" and current_entry in {"WAIT", "ACTIONABLE"}:
            changes.append(MaterialStateChange("RISK_RESOLUTION", f"Entry status improved from RISK to {current_entry}.", previous_entry, current_entry))
    classification_changed = previous.current_political_classification != current.current_political_classification
    if (
        classification_changed
        and config.repeat_full_on_classification_change
        and previous.current_political_classification in MEANINGFUL_CLASSIFICATIONS | {"INSUFFICIENT_EVIDENCE"}
        and current.current_political_classification in MEANINGFUL_CLASSIFICATIONS | {"INSUFFICIENT_EVIDENCE"}
        and release_types & SUPPORTING_RELEASE_TYPES
    ):
        change_type = "CLASSIFICATION_UPGRADE"
        if _classification_rank(current.current_political_classification) < _classification_rank(previous.current_political_classification):
            change_type = "CLASSIFICATION_DOWNGRADE"
        changes.append(
            MaterialStateChange(
                change_type,
                f"Political classification moved from {previous.current_political_classification} to {current.current_political_classification}.",
                previous.current_political_classification,
                current.current_political_classification,
            )
        )
    previous_structure = getattr(previous, "structure_classification", "") or ""
    if previous_structure and previous_structure != history.structure_classification and release_types & SUPPORTING_RELEASE_TYPES:
        changes.append(
            MaterialStateChange(
                "STRUCTURE_CHANGE",
                f"Trade structure shifted from {previous_structure} to {history.structure_classification}.",
                previous_structure,
                history.structure_classification,
            )
        )
    previous_bullish = float(getattr(previous, "bullish_evidence_score", 0.0) or 0.0)
    if abs(history.bullish_evidence_score - previous_bullish) >= config.bullish_evidence_threshold and release_types & SUPPORTING_RELEASE_TYPES:
        changes.append(
            MaterialStateChange(
                "BULLISH_EVIDENCE_CHANGE",
                "Bullish evidence changed materially after a supported disclosure update.",
                f"{previous_bullish:.0f}",
                f"{history.bullish_evidence_score:.0f}",
            )
        )
    previous_distribution = float(getattr(previous, "distribution_evidence_score", 0.0) or 0.0)
    if abs(history.distribution_evidence_score - previous_distribution) >= config.distribution_evidence_threshold and release_types & SUPPORTING_RELEASE_TYPES:
        changes.append(
            MaterialStateChange(
                "DISTRIBUTION_EVIDENCE_CHANGE",
                "Distribution evidence changed materially after a supported disclosure update.",
                f"{previous_distribution:.0f}",
                f"{history.distribution_evidence_score:.0f}",
            )
        )
    previous_breadth = float(getattr(previous, "breadth_score", 0.0) or 0.0)
    if abs(history.breadth_score - previous_breadth) >= config.breadth_threshold and release_types & SUPPORTING_RELEASE_TYPES:
        changes.append(
            MaterialStateChange(
                "BREADTH_CHANGE",
                "Breadth changed materially after a supported disclosure update.",
                f"{previous_breadth:.0f}",
                f"{history.breadth_score:.0f}",
            )
        )
    previous_concentration = float(getattr(previous, "concentration_score", 0.0) or 0.0)
    concentration_before = previous_concentration / 100.0 if previous_concentration > 1.0 else previous_concentration
    concentration_after = history.concentration_score / 100.0 if history.concentration_score > 1.0 else history.concentration_score
    if abs(concentration_after - concentration_before) >= config.concentration_threshold and release_types & SUPPORTING_RELEASE_TYPES:
        changes.append(
            MaterialStateChange(
                "CONCENTRATION_CHANGE",
                "Buyer concentration changed materially after a supported disclosure update.",
                f"{concentration_before:.2f}",
                f"{concentration_after:.2f}",
            )
        )
    return changes
