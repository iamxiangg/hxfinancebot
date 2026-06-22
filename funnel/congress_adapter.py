# VERSION: 2026-06-22-FIX-4
# Funnel Pilot: Congress scanner adapter

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import congress_bot

from funnel.signal_schema import Signal


logger = logging.getLogger(__name__)

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

SUPPORTED_CATEGORIES = {
    "actionable",
    "wait",
    "risk",
}


def _finite_number(
    value: Any,
    default: float = 0.0,
) -> float:
    """Convert a value to a finite float."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)

    if not math.isfinite(number):
        return float(default)

    return number


def _normalise_datetime(
    value: str | date | datetime,
) -> datetime:
    """
    Convert an ISO string, date or datetime into a timezone-aware datetime.

    Date-only and timezone-naive values are treated as Singapore time.
    """
    if isinstance(value, datetime):
        parsed = value

    elif isinstance(value, date):
        parsed = datetime.combine(
            value,
            time.min,
        )

    else:
        text = str(value or "").strip()

        if not text:
            raise ValueError(
                "observed_at cannot be blank."
            )

        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                "observed_at must be a valid ISO-8601 "
                f"date or datetime: {text}"
            ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=SINGAPORE_TZ
        )

    return parsed


def _remaining_validity_days(
    result: dict[str, Any],
) -> int:
    """Calculate the remaining life of the Congress signal."""
    purchase_days = int(
        getattr(
            congress_bot,
            "PURCHASE_DAYS",
            45,
        )
    )

    weighted_age = _finite_number(
        result.get("weighted_age"),
        0.0,
    )

    remaining_days = math.ceil(
        purchase_days - weighted_age
    )

    return max(
        1,
        int(remaining_days),
    )


def result_to_signal(
    result: dict[str, Any],
    observed_at: str | date | datetime,
    min_conviction: float = 15.0,
) -> Signal | None:
    """
    Convert one congress_bot result into the common Signal format.

    - actionable, wait and risk results are retained;
    - other results become near_miss when conviction meets the threshold;
    - blank tickers and weak other results are excluded.
    """
    ticker = str(
        result.get("ticker") or ""
    ).strip().upper()

    if not ticker:
        return None

    original_category = str(
        result.get("category") or "other"
    ).strip().lower()

    conviction = _finite_number(
        result.get("conviction")
    )

    if original_category in SUPPORTED_CATEGORIES:
        classification = original_category

    elif (
        original_category == "other"
        and conviction >= float(min_conviction)
    ):
        classification = "near_miss"

    else:
        return None

    observed_datetime = _normalise_datetime(
        observed_at
    )

    valid_until = observed_datetime + timedelta(
        days=_remaining_validity_days(result)
    )

    details = {
        "model_version": getattr(
            congress_bot,
            "MODEL_VERSION",
            "unknown",
        ),
        "original_category": original_category,
        "conviction": conviction,
        "entry_quality": _finite_number(
            result.get("entry")
        ),
        "base_conviction": _finite_number(
            result.get("base")
        ),
        "sale_penalty": _finite_number(
            result.get("sale_penalty")
        ),
        "call_bonus": _finite_number(
            result.get("call_bonus")
        ),
        "put_penalty": _finite_number(
            result.get("put_penalty")
        ),
        "estimated_capital_low": _finite_number(
            result.get("low")
        ),
        "estimated_capital_mid": _finite_number(
            result.get("mid")
        ),
        "estimated_capital_high": _finite_number(
            result.get("high")
        ),
        "effective_capital": _finite_number(
            result.get("effective")
        ),
        "call_premium_mid": _finite_number(
            result.get("call_mid")
        ),
        "put_premium_mid": _finite_number(
            result.get("put_mid")
        ),
        "buyers": int(
            _finite_number(
                result.get("buyers")
            )
        ),
        "cluster_buyers": int(
            _finite_number(
                result.get("cluster_buyers")
            )
        ),
        "weighted_age_days": _finite_number(
            result.get("weighted_age")
        ),
        "return_since_activity_pct": _finite_number(
            result.get("weighted_return")
        ),
        "flow": str(
            result.get("flow") or ""
        ).strip(),
        "names": list(
            result.get("names") or []
        ),
        "unclear_option_sales": int(
            _finite_number(
                result.get("unclear_sales")
            )
        ),
        "matched_option_sales": int(
            _finite_number(
                result.get("matched_sales")
            )
        ),
    }

    return Signal(
        ticker=ticker,
        scanner="congress",
        classification=classification,
        score=conviction,
        observed_at=observed_datetime.isoformat(),
        valid_until=valid_until.isoformat(),
        details=details,
    )


def _fetch_congress_results() -> list[dict[str, Any]]:
    """
    Run the existing Congress calculations without calling Telegram main().
    """
    congress_bot.init_yf()

    trades = congress_bot.fetch_trades()

    if trades is None:
        raise RuntimeError(
            "Congress trade feed could not be retrieved."
        )

    if not trades:
        raise RuntimeError(
            "Congress trade feed returned no retained transactions."
        )

    results = congress_bot.process(trades)

    if not results:
        raise RuntimeError(
            "Congress analysis produced no usable ticker results."
        )

    return results


def run_congress_adapter(
    min_conviction: float = 15.0,
) -> tuple[list[Signal], int]:
    """
    Return standardised signals and the total analysed ticker count.
    """
    observed_at = datetime.now(
        SINGAPORE_TZ
    )

    results = _fetch_congress_results()

    signals: list[Signal] = []

    for result in results:
        signal = result_to_signal(
            result=result,
            observed_at=observed_at,
            min_conviction=min_conviction,
        )

        if signal is not None:
            signals.append(signal)

    category_priority = {
        "actionable": 4,
        "wait": 3,
        "risk": 2,
        "near_miss": 1,
    }

    signals.sort(
        key=lambda signal: (
            category_priority.get(
                signal.classification,
                0,
            ),
            signal.score or 0.0,
            signal.ticker,
        ),
        reverse=True,
    )

    logger.info(
        "Created %d Congress pilot signals from %d analysed tickers.",
        len(signals),
        len(results),
    )

    return signals, len(results)


def get_congress_signals(
    min_conviction: float = 15.0,
) -> list[Signal]:
    """
    Compatibility function for the original pilot_runner.py.
    """
    signals, _ = run_congress_adapter(
        min_conviction=min_conviction
    )

    return signals