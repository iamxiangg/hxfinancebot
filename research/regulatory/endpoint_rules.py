from __future__ import annotations

from scanners.no_llm_guard import require_no_llm
from research.regulatory.models import EndpointResult, EndpointRole, EndpointStatisticalState, EventOutcome

require_no_llm()


def classify_endpoint_statistical_state(
    *,
    explicit_pass: bool = False,
    explicit_fail: bool = False,
    p_value: float | None = None,
    evidence_text: str = "",
) -> EndpointStatisticalState:
    if explicit_pass:
        return EndpointStatisticalState.PASSED
    if explicit_fail:
        return EndpointStatisticalState.FAILED
    if p_value is not None and p_value <= 0.05:
        return EndpointStatisticalState.PASSED
    lowered = str(evidence_text or "").lower()
    if "favourable trend" in lowered or "favorable trend" in lowered:
        return EndpointStatisticalState.FAVOURABLE_TREND
    if "immature" in lowered:
        return EndpointStatisticalState.IMMATURE
    return EndpointStatisticalState.NOT_REPORTED


def aggregate_trial_outcome(endpoint_results: list[EndpointResult]) -> EventOutcome:
    for endpoint in endpoint_results:
        if endpoint.endpoint_role in {EndpointRole.PRIMARY, EndpointRole.KEY_SECONDARY}:
            if endpoint.statistical_state == EndpointStatisticalState.FAILED:
                return EventOutcome.FAILED
            if endpoint.statistical_state == EndpointStatisticalState.PASSED:
                return EventOutcome.PASSED
    for endpoint in endpoint_results:
        if endpoint.statistical_state == EndpointStatisticalState.FAVOURABLE_TREND:
            return EventOutcome.CONDITIONAL_PASS
    return EventOutcome.UNRESOLVED

