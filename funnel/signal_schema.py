# NEW — Funnel Pilot Step 3: common scanner signal schema

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def normalise_ticker(value: Any) -> str:
    ticker = str(value or "").strip().upper()
    if not ticker:
        raise ValueError("Signal ticker cannot be blank")
    return ticker


def _normalise_iso_datetime(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Signal timestamps must contain a timezone offset")
    return parsed.isoformat()


@dataclass(frozen=True)
class Signal:
    ticker: str
    scanner: str
    classification: str
    score: float | None
    observed_at: str
    valid_until: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    signal_id: str = ""

    def __post_init__(self) -> None:
        ticker = normalise_ticker(self.ticker)
        scanner = str(self.scanner).strip().lower()
        classification = str(self.classification).strip().lower()
        if not scanner:
            raise ValueError("Signal scanner cannot be blank")
        if not classification:
            raise ValueError("Signal classification cannot be blank")

        observed_at = _normalise_iso_datetime(self.observed_at)
        valid_until = (
            _normalise_iso_datetime(self.valid_until)
            if self.valid_until
            else None
        )
        score = None if self.score is None else float(self.score)

        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "scanner", scanner)
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "score", score)

        if not self.signal_id:
            raw_key = "|".join(
                [ticker, scanner, classification, observed_at]
            )
            digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(
                self,
                "signal_id",
                f"{scanner}-{ticker}-{digest}",
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def details_json(self) -> str:
        return json.dumps(
            self.details,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
