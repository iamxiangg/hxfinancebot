from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from scanners.congress.models import (
    MaterialStateChange,
    PoliticalArchiveStats,
    PoliticalBackfillStatus,
    PoliticalWatchlistState,
    TickerPoliticalHistory,
)


@dataclass(frozen=True)
class PoliticalDigestFlag:
    ticker: str
    rank_score: float
    section: str
    flag_category: str
    flag_reasons: tuple[str, ...]
    history: TickerPoliticalHistory
    release_types: tuple[str, ...]
    trigger_trade_keys: tuple[str, ...]
    detailed: bool = True
    exceptional: bool = False
    watchlist_state: PoliticalWatchlistState | None = None
    material_changes: tuple[MaterialStateChange, ...] = ()
    update_heading: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["history"] = self.history.to_dict()
        if self.watchlist_state is not None:
            payload["watchlist_state"] = self.watchlist_state.to_dict()
        return payload


@dataclass(frozen=True)
class PoliticalDigestPlan:
    digest_date: str
    data_status: dict[str, int]
    new_material_flags: tuple[PoliticalDigestFlag, ...] = ()
    material_updates: tuple[PoliticalDigestFlag, ...] = ()
    active_watchlist_items: tuple[PoliticalDigestFlag, ...] = ()
    other_new_activity: tuple[PoliticalDigestFlag, ...] = ()
    expired_watchlist_items: tuple[PoliticalDigestFlag, ...] = ()
    watchlist_state_changes: tuple[PoliticalDigestFlag, ...] = ()
    recorded_only_count: int = 0
    backfill_status: PoliticalBackfillStatus | None = None
    archive_stats: PoliticalArchiveStats | None = None
    send_digest: bool = False
    summary_lines: tuple[str, ...] = ()
    current_watchlist_states: dict[str, PoliticalWatchlistState] = field(default_factory=dict)
    delivered_watchlist_updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    hidden_watchlist_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["new_material_flags"] = [flag.to_dict() for flag in self.new_material_flags]
        payload["material_updates"] = [flag.to_dict() for flag in self.material_updates]
        payload["active_watchlist_items"] = [flag.to_dict() for flag in self.active_watchlist_items]
        payload["other_new_activity"] = [flag.to_dict() for flag in self.other_new_activity]
        payload["expired_watchlist_items"] = [flag.to_dict() for flag in self.expired_watchlist_items]
        payload["watchlist_state_changes"] = [flag.to_dict() for flag in self.watchlist_state_changes]
        payload["current_watchlist_states"] = {
            ticker: state.to_dict()
            for ticker, state in sorted(self.current_watchlist_states.items())
        }
        if self.backfill_status is not None:
            payload["backfill_status"] = self.backfill_status.to_dict()
        if self.archive_stats is not None:
            payload["archive_stats"] = self.archive_stats.to_dict()
        return payload

    @property
    def detailed_flags(self) -> tuple[PoliticalDigestFlag, ...]:
        return self.new_material_flags

    @property
    def compact_flags(self) -> tuple[PoliticalDigestFlag, ...]:
        return self.other_new_activity
