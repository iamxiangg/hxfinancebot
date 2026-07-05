from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from scanners.congress.models import PoliticalArchiveStats, PoliticalBackfillStatus, TickerPoliticalHistory


@dataclass(frozen=True)
class PoliticalDigestFlag:
    ticker: str
    rank_score: float
    flag_category: str
    flag_reasons: tuple[str, ...]
    history: TickerPoliticalHistory
    release_types: tuple[str, ...]
    trigger_trade_keys: tuple[str, ...]
    detailed: bool = True
    exceptional: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["history"] = self.history.to_dict()
        return payload


@dataclass(frozen=True)
class PoliticalDigestPlan:
    digest_date: str
    data_status: dict[str, int]
    detailed_flags: tuple[PoliticalDigestFlag, ...] = ()
    compact_flags: tuple[PoliticalDigestFlag, ...] = ()
    recorded_only_count: int = 0
    backfill_status: PoliticalBackfillStatus | None = None
    archive_stats: PoliticalArchiveStats | None = None
    send_digest: bool = False
    summary_lines: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["detailed_flags"] = [flag.to_dict() for flag in self.detailed_flags]
        payload["compact_flags"] = [flag.to_dict() for flag in self.compact_flags]
        if self.backfill_status is not None:
            payload["backfill_status"] = self.backfill_status.to_dict()
        if self.archive_stats is not None:
            payload["archive_stats"] = self.archive_stats.to_dict()
        return payload
