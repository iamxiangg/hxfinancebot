from __future__ import annotations

from dataclasses import dataclass, field

from research.regulatory.models import NormalizedRegulatoryEvent, ProgrammeCurrentState, ProgrammeIdentity, StateTransition
from research.regulatory.state_machines import apply_event_to_state


@dataclass
class HistoryUpdateResult:
    current_state: ProgrammeCurrentState
    transitions: list[StateTransition] = field(default_factory=list)


def seed_current_state(programme: ProgrammeIdentity) -> ProgrammeCurrentState:
    return ProgrammeCurrentState(
        programme_key=programme.programme_key,
        company_id=programme.company_id,
        product_id=programme.product_id,
        indication_id=programme.indication_id,
    )


def apply_events(
    *,
    programme: ProgrammeIdentity,
    events: list[NormalizedRegulatoryEvent],
    existing_state: ProgrammeCurrentState | None = None,
) -> HistoryUpdateResult:
    current = existing_state or seed_current_state(programme)
    transitions: list[StateTransition] = []
    for event in sorted(events, key=lambda item: (item.event_date, item.normalized_event_id)):
        current, transition = apply_event_to_state(current, event)
        if transition is not None:
            transitions.append(transition)
    return HistoryUpdateResult(current_state=current, transitions=transitions)

