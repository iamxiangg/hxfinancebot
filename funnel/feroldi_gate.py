from __future__ import annotations

import math
from typing import Any


FEROLDI_FIRST_CUT_MAX_POINTS = 42.0
VALID_GATE_MODES = {"off", "observe", "enforce"}


def _install_feroldi_telegram_renderer() -> None:
    """Install the Feroldi-aware sender before review_candidates imports it.

    This keeps the existing Telegram module unchanged while adding the first-cut
    section breakdown to candidate review cards.
    """

    from funnel import telegram_review
    from funnel.feroldi_telegram import send_candidate_review

    telegram_review.send_candidate_review = send_candidate_review


_install_feroldi_telegram_renderer()


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _number_text(value: float) -> str:
    rounded = round(float(value), 1)
    if float(rounded).is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def _sum_fields(candidate: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    values = [_to_float(candidate.get(field)) for field in fields]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def first_cut_components(candidate: dict[str, Any]) -> tuple[float | None, float | None, float]:
    score = _to_float(candidate.get("Feroldi First Cut Score"))
    if score is None:
        score = _sum_fields(
            candidate,
            (
                "Feroldi Financial Score",
                "Feroldi Management Score",
                "Feroldi Stock Score",
            ),
        )

    available = _to_float(candidate.get("Feroldi Available Points"))
    if available is None:
        available = _sum_fields(
            candidate,
            (
                "Feroldi Financial Available",
                "Feroldi Management Available",
                "Feroldi Stock Available",
            ),
        )

    maximum = _to_float(candidate.get("Feroldi Max Points"))
    if maximum is None or maximum <= 0:
        maximum = FEROLDI_FIRST_CUT_MAX_POINTS

    return score, available, maximum


def format_feroldi_score(
    score: float,
    available: float,
    maximum: float = FEROLDI_FIRST_CUT_MAX_POINTS,
) -> str:
    if available <= 0 or maximum <= 0:
        return ""

    percentage = score / available * 100
    equivalent = score / available * maximum
    score_text = _number_text(score)
    available_text = _number_text(available)
    maximum_text = _number_text(maximum)

    if math.isclose(available, maximum, rel_tol=0.0, abs_tol=0.05):
        return f"{score_text}/{maximum_text} ({percentage:.1f}%)"

    return (
        f"{score_text}/{available_text} available "
        f"({percentage:.1f}%; {equivalent:.1f}/{maximum_text} equivalent)"
    )


def _set_enforcement_result(
    candidate: dict[str, Any],
    *,
    gate: str,
    allow_review: bool,
) -> None:
    if gate == "PASS":
        candidate["Telegram Eligible"] = "YES"
        candidate["Status"] = "FEROLDI_PASSED"
    elif gate == "REVIEW":
        candidate["Telegram Eligible"] = "YES" if allow_review else "NO"
        candidate["Status"] = "FEROLDI_REVIEW"
    elif gate in {"PENDING", "LOW_COVERAGE"}:
        candidate["Telegram Eligible"] = "NO"
        candidate["Status"] = "FEROLDI_UNAVAILABLE"
    elif gate == "FAIL":
        candidate["Telegram Eligible"] = "NO"
        candidate["Status"] = "FEROLDI_FAILED"


def apply_feroldi_gate(
    candidate: dict[str, Any],
    *,
    mode: str = "observe",
    pass_threshold: float = 30.0,
    review_threshold: float = 25.0,
    min_coverage: float = 0.75,
    allow_review: bool = True,
) -> dict[str, Any]:
    """Apply a coverage-aware first-cut Feroldi gate.

    Thresholds are expressed as equivalent points out of the full 42-point
    first-cut maximum. Partial data is normalised only when minimum coverage is
    met. In observe mode the gate is recorded but the preceding BTD eligibility
    decision is preserved.
    """

    candidate = dict(candidate)
    normalised_mode = str(mode or "observe").strip().lower()
    if normalised_mode not in VALID_GATE_MODES:
        raise ValueError(f"Unknown Feroldi gate mode: {mode}")
    if pass_threshold < review_threshold:
        raise ValueError("Feroldi pass threshold must be at least the review threshold")
    if not 0 <= min_coverage <= 1:
        raise ValueError("Feroldi minimum coverage must be between 0 and 1")

    candidate["Feroldi Gate Mode"] = normalised_mode.upper()

    if normalised_mode == "off":
        candidate["Feroldi Gate"] = "DISABLED"
        candidate["Feroldi Gate Reason"] = "Feroldi first-cut gate is disabled."
        return candidate

    if str(candidate.get("Telegram Eligible") or "").strip().upper() != "YES":
        candidate["Feroldi Gate"] = "SKIPPED_BTD"
        candidate["Feroldi Gate Reason"] = "Feroldi gate was not applied because the BTD gate did not pass."
        return candidate

    score, available, maximum = first_cut_components(candidate)
    candidate["Feroldi Max Points"] = maximum

    if score is None or available is None or available <= 0:
        candidate["Feroldi Gate"] = "PENDING"
        candidate["Feroldi Gate Reason"] = "First-cut score or available-point coverage has not been populated."
        if normalised_mode == "enforce":
            _set_enforcement_result(candidate, gate="PENDING", allow_review=allow_review)
        return candidate

    if score < 0 or score > available or available > maximum:
        candidate["Feroldi Gate"] = "PENDING"
        candidate["Feroldi Gate Reason"] = (
            f"Invalid first-cut values: score={score}, available={available}, maximum={maximum}."
        )
        if normalised_mode == "enforce":
            _set_enforcement_result(candidate, gate="PENDING", allow_review=allow_review)
        return candidate

    coverage = available / maximum
    equivalent = score / available * maximum
    display = format_feroldi_score(score, available, maximum)

    candidate["Feroldi First Cut Score"] = round(score, 2)
    candidate["Feroldi Available Points"] = round(available, 2)
    candidate["Feroldi Equivalent Score"] = round(equivalent, 2)
    candidate["Feroldi Coverage"] = round(coverage, 4)
    candidate["Feroldi Score Display"] = display

    if coverage < min_coverage:
        gate = "LOW_COVERAGE"
        reason = (
            f"{display}; coverage {coverage * 100:.1f}% is below the "
            f"{min_coverage * 100:.1f}% minimum."
        )
    elif equivalent >= pass_threshold:
        gate = "PASS"
        reason = f"{display}; equivalent score meets the {pass_threshold:.1f}/42 pass threshold."
    elif equivalent >= review_threshold:
        gate = "REVIEW"
        reason = (
            f"{display}; equivalent score is below {pass_threshold:.1f}/42 but meets the "
            f"{review_threshold:.1f}/42 review threshold."
        )
    else:
        gate = "FAIL"
        reason = f"{display}; equivalent score is below the {review_threshold:.1f}/42 review threshold."

    if normalised_mode == "observe":
        reason += " Observe-only mode preserved the BTD eligibility decision."
    else:
        _set_enforcement_result(candidate, gate=gate, allow_review=allow_review)

    candidate["Feroldi Gate"] = gate
    candidate["Feroldi Gate Reason"] = reason
    return candidate
