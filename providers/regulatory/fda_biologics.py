from __future__ import annotations

from datetime import datetime

from providers.regulatory.base import SourceBatch


class FDABiologicsProvider:
    source_name = "fda_biologics"

    def fetch_changes(self, *, since: datetime, until: datetime, cursor: str = "") -> SourceBatch:
        return SourceBatch(
            source_status="UNAVAILABLE",
            errors=["Stable official machine-readable FDA biologics source is not configured in this implementation."],
        )

