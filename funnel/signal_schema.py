# VERSION: 2026-06-22-PUBLIC-NORMALISE-TICKER-FIX-3
# Funnel Pilot: Common scanner signal schema

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


ALLOWED_SCANNERS = {
    "congress",
    "vpma",
    "gamma",
    "earnings",
    "manual",
}


def clean_text(value: Any) -> str:
    """Return a stripped string representation."""
    if value is None:
        return ""

    return str(value).strip()


def normalise_ticker(value: Any) -> str:
    """
    Return a ticker in the same format used by Stock Summary USD column A.

    This function is intentionally public because other funnel modules,
    including candidate_ingestor.py, import it directly.
    """
    return clean_text(value).upper()


def normalise_scanner(value: Any) -> str:
    """Return the scanner name in lowercase."""
    return clean_text(value).lower()


def normalise_classification(value: Any) -> str:
    """Return the signal classification in lowercase."""
    return clean_text(value).lower()


def normalise_datetime(
    value: str | datetime,
    field_name: str,
) -> str:
    """
    Convert a timezone-aware datetime or ISO-8601 string to ISO format.

    Accepted examples:
        2026-06-22T19:00:00+08:00
        2026-06-22T11:00:00Z

    Date-only and timezone-naive values are rejected because signals require
    an unambiguous observation time.
    """
    if isinstance(value, datetime):
        parsed = value

    else:
        text = clean_text(value)

        if not text:
            raise ValueError(
                f"{field_name} cannot be blank."
            )

        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be a valid ISO-8601 datetime: "
                f"{text}"
            ) from exc

    if parsed.tzinfo is None:
        raise ValueError(
            f"{field_name} must include timezone information."
        )

    return parsed.isoformat()


def normalise_score(
    value: Any,
) -> float | None:
    """Convert a score to float while permitting None."""
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "score must be numeric or None."
        ) from exc


@dataclass
class Signal:
    """
    Common signal structure used by all funnel scanners.

    Scanner-specific information belongs inside details, while the outer
    fields remain consistent across Congress, VP/MA, gamma and earnings.
    """

    ticker: str
    scanner: str
    classification: str
    observed_at: str | datetime

    score: float | None = None
    valid_until: str | datetime | None = None
    details: dict[str, Any] = field(
        default_factory=dict
    )

    signal_id: str = field(
        init=False
    )

    def __post_init__(self) -> None:
        """Normalise and validate the signal."""
        self.ticker = normalise_ticker(
            self.ticker
        )

        self.scanner = normalise_scanner(
            self.scanner
        )

        self.classification = normalise_classification(
            self.classification
        )

        if not self.ticker:
            raise ValueError(
                "ticker cannot be blank."
            )

        if not self.scanner:
            raise ValueError(
                "scanner cannot be blank."
            )

        if self.scanner not in ALLOWED_SCANNERS:
            raise ValueError(
                f"Unsupported scanner '{self.scanner}'. "
                "Allowed scanners: "
                + ", ".join(
                    sorted(ALLOWED_SCANNERS)
                )
            )

        if not self.classification:
            raise ValueError(
                "classification cannot be blank."
            )

        self.observed_at = normalise_datetime(
            self.observed_at,
            "observed_at",
        )

        if self.valid_until is not None:
            self.valid_until = normalise_datetime(
                self.valid_until,
                "valid_until",
            )

        self.score = normalise_score(
            self.score
        )

        if not isinstance(
            self.details,
            dict,
        ):
            raise ValueError(
                "details must be a dictionary."
            )

        self.signal_id = self._generate_signal_id()

    def _generate_signal_id(self) -> str:
        """
        Generate a stable signal identifier.

        Format:
            scanner-TICKER-hash

        Example:
            congress-MSFT-a1b2c3d4e5f6
        """
        identity = "|".join(
            [
                self.scanner,
                self.ticker,
                self.classification,
                str(self.observed_at),
            ]
        )

        short_hash = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:12]

        return (
            f"{self.scanner}-"
            f"{self.ticker}-"
            f"{short_hash}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the signal as a serialisable dictionary."""
        return asdict(self)

    def to_json(
        self,
        indent: int = 2,
    ) -> str:
        """Return the signal as formatted JSON."""
        return json.dumps(
            self.to_dict(),
            indent=indent,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


def signal_from_dict(
    data: dict[str, Any],
) -> Signal:
    """
    Create a Signal from a dictionary.

    Unknown fields are ignored.
    """
    permitted_fields = {
        "ticker",
        "scanner",
        "classification",
        "observed_at",
        "score",
        "valid_until",
        "details",
    }

    clean_data = {
        key: value
        for key, value in data.items()
        if key in permitted_fields
    }

    return Signal(
        **clean_data
    )


# Backwards compatibility for earlier funnel modules.
ScannerSignal = Signal


def main() -> None:
    """Run a small schema demonstration."""
    example_signal = Signal(
        ticker="msft",
        scanner="congress",
        classification="actionable",
        score=74,
        observed_at="2026-06-22T19:00:00+08:00",
        valid_until="2026-07-07T19:00:00+08:00",
        details={
            "buyers": 2,
            "estimated_capital_mid": 180000,
        },
    )

    print()
    print("FUNNEL PILOT — SIGNAL SCHEMA TEST")
    print("=" * 39)
    print(example_signal.to_json())
    print()
    print(f"Signal ID: {example_signal.signal_id}")
    print(
        "normalise_ticker(' msft ') = "
        f"{normalise_ticker(' msft ')}"
    )
    print("SIGNAL SCHEMA TEST COMPLETED SUCCESSFULLY")
    print()


if __name__ == "__main__":
    main()