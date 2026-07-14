from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from research.regulatory.models import RawRegulatoryRecord


@dataclass
class SourceBatch:
    records: list[RawRegulatoryRecord] = field(default_factory=list)
    next_cursor: str = ""
    fetched_at: str = ""
    payload_hash: str = ""
    source_status: str = "OK"
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = datetime.now(UTC).replace(microsecond=0).isoformat() + "Z"
        if not self.payload_hash:
            payload = json.dumps([item.to_dict() for item in self.records], sort_keys=True)
            self.payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class RegulatorySourceProvider(Protocol):
    source_name: str

    def fetch_changes(
        self,
        *,
        since: datetime,
        until: datetime,
        cursor: str = "",
    ) -> SourceBatch:
        ...
