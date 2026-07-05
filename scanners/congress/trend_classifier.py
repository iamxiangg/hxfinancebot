from __future__ import annotations


def score_label(value: float) -> str:
    if value >= 70.0:
        return "HIGH"
    if value >= 40.0:
        return "MODERATE"
    if value > 0:
        return "LOW"
    return "NONE"


def share_label(value: float) -> str:
    percentage = max(0.0, min(100.0, value * 100.0))
    if percentage >= 70.0:
        return "HIGH"
    if percentage >= 40.0:
        return "MODERATE"
    if percentage > 0:
        return "LOW"
    return "NONE"


def deterministic_interpretation(
    *,
    primary_classification: str,
    structure_classification: str,
    bullish_evidence: float,
    distribution_evidence: float,
    breadth_score: float,
    inference_confidence: str,
) -> str:
    if primary_classification == "BROAD_ACCUMULATION":
        return (
            "Independent households are accumulating the ticker with material "
            "bullish evidence, so the signal reads as broad political accumulation."
        )
    if primary_classification == "REPEAT_FILER_ACCUMULATION":
        return (
            "The same household has built repeated exposure without a matching "
            "recent reversal, so the pattern reads as repeat accumulation."
        )
    if primary_classification == "SINGLE_FILER_BULLISH_BET":
        return (
            "Bullish activity is concentrated in one household. The signal is a "
            f"{structure_classification.lower().replace('_', ' ')} bet rather than broad corroboration."
        )
    if primary_classification == "DISTRIBUTION":
        return (
            "Distribution evidence is stronger than bullish evidence, so the "
            "recent pattern is better read as selling or possible exit activity."
        )
    if primary_classification == "MIXED_HIGH_ACTIVITY":
        return (
            "Material buying and selling coexist in the same recent window, so "
            "directional inference is mixed rather than clean accumulation."
        )
    if bullish_evidence > 0 or distribution_evidence > 0 or breadth_score > 0:
        return (
            "Activity exists, but the evidence quality remains below the threshold "
            f"for a confident directional read. Inference confidence is {inference_confidence.lower()}."
        )
    return "No material directional pattern is present in the current analytical window."
